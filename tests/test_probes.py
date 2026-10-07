"""The probes: ping maths, DNS packets, public-address tracking, speed-test scheduling."""

from __future__ import annotations

import socket
import struct

import pytest

from backend.collectors import probes
from backend.collectors.probes import Every, SpeedTester, WanTracker
from backend.state import StateFile


@pytest.fixture
def state(tmp_path):
    return StateFile(tmp_path / "state.json")


# --- ping ---------------------------------------------------------------------


class FakeIcmp:
    """Replies with whatever packets the test queued; an empty queue means silence."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def sendto(self, data, address):
        self.sent.append((data, address))

    def settimeout(self, seconds):
        pass

    def recvfrom(self, size):
        if not self.replies:
            raise socket.timeout()
        return self.replies.pop(0), ("192.0.2.1", 0)


def reply(sequence, kind=0):
    return struct.pack("!BBHHH", kind, 0, 0, 0, sequence) + b"payload"


def test_echo_sends_a_request_and_times_the_matching_reply():
    sock = FakeIcmp([reply(7)])

    rtt = probes.echo(sock, "192.0.2.1", 7, timeout=1)

    assert rtt is not None and 0 <= rtt < 1000
    packet, address = sock.sent[0]
    assert address == ("192.0.2.1", 0)
    assert struct.unpack("!BBHHH", packet[:8]) == (8, 0, 0, 0, 7)


def test_echo_skips_late_replies_to_earlier_pings():
    sock = FakeIcmp([reply(5), reply(6), reply(7)])

    assert probes.echo(sock, "192.0.2.1", 7, timeout=1) is not None
    assert sock.replies == []


def test_echo_ignores_packets_that_are_not_echo_replies():
    # Type 3 is "destination unreachable"; a truncated packet must not crash the parser either.
    sock = FakeIcmp([reply(7, kind=3), b"\x00\x00"])

    assert probes.echo(sock, "192.0.2.1", 7, timeout=0.05) is None


def test_echo_without_any_reply_is_a_lost_ping():
    assert probes.echo(FakeIcmp([]), "192.0.2.1", 1, timeout=0.05) is None


class Flow(FakeIcmp):
    """A fake ping socket that answers every ping, or none at all, and remembers being closed."""

    def __init__(self, answers):
        super().__init__([])
        self.answers = answers
        self.closed = False

    def sendto(self, data, address):
        super().sendto(data, address)
        if self.answers:
            self.replies.append(reply(struct.unpack("!H", data[6:8])[0]))

    def close(self):
        self.closed = True


def pinger_with(flows, **kwargs):
    opened = []

    def open_socket():
        flow = flows[min(len(opened), len(flows) - 1)]
        if isinstance(flow, Exception):
            opened.append(flow)
            raise flow
        opened.append(flow)
        return flow

    return probes.Pinger("192.0.2.1", interval=0.02, open_socket=open_socket, **kwargs), opened


def test_healthy_flow_keeps_its_socket():
    flow = Flow(answers=True)
    pinger, opened = pinger_with([flow])

    for _ in range(5):
        pinger.ping_once()

    assert opened == [flow] and not flow.closed
    assert all(r is not None for r in pinger.results) and len(pinger.results) == 5
    # Sequence numbers count up, so replies can be matched to their ping.
    assert [struct.unpack("!H", data[6:8])[0] for data, _ in flow.sent] == [1, 2, 3, 4, 5]


def test_a_flow_that_stops_answering_is_replaced_after_one_lost_ping():
    dead, alive = Flow(answers=False), Flow(answers=True)
    pinger, opened = pinger_with([dead, alive])

    for _ in range(4):
        pinger.ping_once()

    assert dead.closed and opened == [dead, alive]
    assert [r is None for r in pinger.results] == [True, False, False, False]
    assert pinger.summary()["loss"] == 25.0


def test_real_outage_keeps_counting_losses_without_crashing():
    pinger, opened = pinger_with([Flow(answers=False)])

    for _ in range(3):
        pinger.ping_once()

    assert pinger.summary()["loss"] == 100.0 and len(opened) == 3


def test_network_errors_count_as_lost_pings():
    class Unreachable(Flow):
        def sendto(self, data, address):
            raise OSError(101, "Network is unreachable")

    broken = Unreachable(answers=False)
    pinger, opened = pinger_with([broken, Flow(answers=True)])

    pinger.ping_once()
    assert list(pinger.results) == [None] and pinger.error == "Network is unreachable" and broken.closed

    pinger.ping_once()
    assert pinger.results[-1] is not None and pinger.error is None


def test_ping_not_permitted_is_reported_without_inventing_losses():
    pinger, _ = pinger_with([PermissionError(1, "Operation not permitted")])

    pinger.ping_once()

    assert list(pinger.results) == [] and pinger.error == "ping is not permitted for this user"


def test_slow_cadence_does_not_slow_down_noticing_a_lost_ping():
    waits = []

    class Recording(Flow):
        def settimeout(self, seconds):
            waits.append(seconds)

    opened = []

    def open_socket():
        opened.append(Recording(answers=False))
        return opened[-1]

    pinger = probes.Pinger("192.0.2.1", interval=30, timeout=0.05, open_socket=open_socket)
    pinger.ping_once()

    # Lost after the short timeout, not after the 30-second interval.
    assert list(pinger.results) == [None]
    assert waits and max(waits) <= 0.05
    assert pinger.summary()["interval"] == 30


def test_timeout_never_exceeds_the_interval():
    assert probes.Pinger("192.0.2.1", interval=0.5, timeout=1.0).timeout == 0.5
    assert probes.Pinger("192.0.2.1", interval=30).timeout == 1.0


def test_old_results_are_kept_between_pings():
    flow = Flow(answers=True)
    pinger, _ = pinger_with([flow], keep=120)
    for _ in range(3):
        pinger.ping_once()
    before = list(pinger.results)

    # Reading the summary any number of times between pings changes nothing.
    assert [pinger.summary()["history"] for _ in range(3)] == [[round(r, 2) for r in before]] * 3
    pinger.ping_once()
    assert list(pinger.results)[:3] == before and len(pinger.results) == 4


def test_history_is_capped_to_the_window():
    pinger, _ = pinger_with([Flow(answers=True)], keep=10)

    for _ in range(25):
        pinger.ping_once()

    assert len(pinger.results) == 10


def test_pinger_thread_stops_and_closes_its_socket():
    flow = Flow(answers=True)
    pinger, _ = pinger_with([flow])
    pinger.start()
    while not pinger.results:
        pass
    pinger.stop()
    pinger.join(timeout=2)

    assert not pinger.is_alive() and flow.closed


def test_ping_summary():
    summary = probes.ping_summary("1.1.1.1", [10.0, None, 30.0, 20.004])

    assert summary == {
        "host": "1.1.1.1", "interval": 1.0, "last": 20.0, "avg": 20.0, "max": 30.0, "loss": 25.0,
        "history": [10.0, None, 30.0, 20.0], "error": None,
    }


def test_ping_summary_when_every_ping_was_lost():
    summary = probes.ping_summary("192.0.2.1", [None, None, None], error="Network is unreachable")

    assert summary["loss"] == 100.0
    assert summary["last"] is None and summary["avg"] is None and summary["max"] is None
    assert summary["error"] == "Network is unreachable"


def test_ping_summary_before_the_first_ping():
    summary = probes.ping_summary("1.1.1.1", [])

    assert summary["loss"] is None and summary["last"] is None and summary["history"] == []


# --- DNS ----------------------------------------------------------------------


def test_dns_question_bytes():
    packet = probes.dns_question("example.com", 0xBEEF)

    assert packet[:12] == struct.pack("!HHHHHH", 0xBEEF, 0x0100, 1, 0, 0, 0)
    assert packet[12:] == b"\x07example\x03com\x00\x00\x01\x00\x01"


def test_dns_question_tolerates_a_trailing_dot():
    assert probes.dns_question("example.com.", 1) == probes.dns_question("example.com", 1)


def answer(ident, flags=0x8180):
    return struct.pack("!HHHHHH", ident, flags, 1, 1, 0, 0) + b"rest"


@pytest.mark.parametrize(
    "data, expected",
    [
        (answer(42), True),
        (answer(43), False),           # somebody else's answer
        (answer(42, 0x8182), False),   # SERVFAIL
        (answer(42, 0x8183), False),   # NXDOMAIN
        (answer(42, 0x0100), False),   # our own question echoed back, not a reply
        (b"\x00\x2a", False),          # truncated
        (b"", False),
    ],
)
def test_dns_answer_check(data, expected):
    assert probes.dns_answer_ok(data, 42) is expected


def test_resolvers_put_real_servers_before_the_local_cache(tmp_path):
    stub = tmp_path / "stub.conf"
    stub.write_text("# comment\nnameserver 127.0.0.53\noptions edns0\n")
    real = tmp_path / "real.conf"
    real.write_text("nameserver 192.168.1.1\nnameserver 127.0.0.53\nnameserver 9.9.9.9\nsearch lan\n")

    assert probes.resolvers((str(stub), str(real))) == ["192.168.1.1", "9.9.9.9", "127.0.0.53"]


def test_resolvers_with_no_readable_file(tmp_path):
    assert probes.resolvers((str(tmp_path / "missing"),)) == []


# --- public address -----------------------------------------------------------


def test_public_address_is_read_from_the_reply():
    body = "fl=1f1\nh=1.1.1.1\nip=203.0.113.4\nts=1.5\nloc=CA\n"

    assert probes.parse_wan_ip(body) == "203.0.113.4"


def test_public_ipv6_address():
    assert probes.parse_wan_ip("ip=2001:db8::1\n") == "2001:db8::1"


@pytest.mark.parametrize("body", ["", "loc=CA\n", "ip=\n", "ip=<html>blocked</html>\n", "ip=999.1.1.1\n"])
def test_reply_without_a_valid_address_is_an_error(body):
    with pytest.raises((RuntimeError, ValueError)):
        probes.parse_wan_ip(body)


def test_address_changes_are_logged_and_survive_restarts(state):
    answers = iter(["203.0.113.4", "203.0.113.4", "198.51.100.7"])
    clock = iter([100.0, 900.0])
    tracker = WanTracker(state, lookup=lambda: next(answers), clock=lambda: next(clock))

    first = tracker.check()
    same = tracker.check()
    changed = WanTracker(state, lookup=lambda: next(answers), clock=lambda: next(clock)).check()

    assert (first["ip"], first["since"], first["previous"]) == ("203.0.113.4", 100.0, None)
    assert same == first
    assert (changed["ip"], changed["since"], changed["previous"]) == ("198.51.100.7", 900.0, "203.0.113.4")
    assert changed["changes"] == [{"ts": 100.0, "ip": "203.0.113.4"}, {"ts": 900.0, "ip": "198.51.100.7"}]


def test_change_log_is_capped(state):
    count = iter(range(1000))
    tracker = WanTracker(state, lookup=lambda: f"203.0.113.{next(count) % 250}", clock=lambda: 1.0)
    for _ in range(60):
        record = tracker.check()

    assert len(record["changes"]) == 20


def test_failed_lookup_keeps_the_last_known_address(state):
    WanTracker(state, lookup=lambda: "203.0.113.4", clock=lambda: 1.0).check()

    def broken():
        raise OSError("network is unreachable")

    with pytest.raises(OSError):
        WanTracker(state, lookup=broken).check()
    assert state.get("wan")["ip"] == "203.0.113.4"


# --- speed test ---------------------------------------------------------------


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_speed(state, hours=6, results=None, clock=None):
    clock = clock or Clock()
    runs = []

    def measure():
        runs.append(clock.now)
        result = (results or [{"down_bps": 100, "up_bps": 10}])[min(len(runs), len(results or [1])) - 1]
        if isinstance(result, Exception):
            raise result
        return {"ts": clock.now, **result}

    return SpeedTester(state, hours, measure=measure, clock=clock), runs, clock


def test_first_test_waits_a_little_after_start(state):
    speed, runs, clock = make_speed(state)

    assert speed.run_if_due() is False
    clock.now += SpeedTester.FIRST_DELAY
    assert speed.run_if_due() is True
    assert runs == [clock.now]
    assert speed.summary()["last"]["down_bps"] == 100


def test_next_test_comes_one_interval_after_the_last(state):
    speed, runs, clock = make_speed(state, hours=6)
    clock.now += SpeedTester.FIRST_DELAY
    speed.run_if_due()
    first = clock.now

    clock.now += 6 * 3600 - 1
    assert speed.run_if_due() is False
    clock.now += 1
    assert speed.run_if_due() is True
    assert runs == [first, first + 6 * 3600]
    assert speed.summary()["next"] == first + 12 * 3600


def test_restart_does_not_trigger_an_extra_test(state):
    speed, runs, clock = make_speed(state)
    clock.now += SpeedTester.FIRST_DELAY
    speed.run_if_due()

    restarted, more_runs, _ = make_speed(state, clock=Clock(clock.now + 3600))
    restarted._clock.now += SpeedTester.FIRST_DELAY + 1

    assert restarted.run_if_due() is False and more_runs == []
    assert restarted.summary()["last"]["down_bps"] == 100


def test_zero_hours_switches_testing_off(state):
    speed, runs, clock = make_speed(state, hours=0)
    clock.now += 10 * 86400

    assert speed.run_if_due() is False and runs == []
    assert speed.summary()["enabled"] is False and speed.summary()["next"] is None


def test_failed_test_is_reported_and_not_retried_at_once(state):
    speed, runs, clock = make_speed(state, results=[RuntimeError("speed test download refused (HTTP 429)"), {"down_bps": 5, "up_bps": 1}])
    clock.now += SpeedTester.FIRST_DELAY

    assert speed.run_if_due() is True
    assert speed.summary()["error"] == "speed test download refused (HTTP 429)"
    assert speed.summary()["last"] is None and speed.running is False

    clock.now += 60
    assert speed.run_if_due() is False
    clock.now += SpeedTester.RETRY_AFTER
    assert speed.run_if_due() is True
    assert speed.summary()["error"] is None and speed.summary()["last"]["down_bps"] == 5


def test_history_is_capped_and_summary_is_shorter_still(state):
    speed, runs, clock = make_speed(state, hours=1)
    clock.now += SpeedTester.FIRST_DELAY
    for _ in range(SpeedTester.KEEP + 15):
        speed.run_if_due()
        clock.now += 3600

    assert len(speed.history) == SpeedTester.KEEP
    assert len(speed.summary()["history"]) == 20
    assert len(state.get("speedtests")) == SpeedTester.KEEP


# --- scheduling ---------------------------------------------------------------


def test_every_keeps_the_last_good_value_when_a_probe_fails():
    answers = iter([{"ms": 3.0}, RuntimeError("resolver answered with an error"), {"ms": 4.0}])

    def probe():
        value = next(answers)
        if isinstance(value, Exception):
            raise value
        return value

    every = Every("test", 5, probe)
    every.run_once()
    assert (every.value, every.error) == ({"ms": 3.0}, None) and every.checked is not None

    every.run_once()
    assert (every.value, every.error) == ({"ms": 3.0}, "resolver answered with an error")

    every.run_once()
    assert (every.value, every.error) == ({"ms": 4.0}, None)


def test_every_stops_promptly():
    every = Every("test", 3600, lambda: 1)
    every.start()
    every.stop()
    every.join(timeout=2)

    assert not every.is_alive()
