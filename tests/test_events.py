"""The event log: what counts as news between two snapshots."""

from __future__ import annotations

import copy

import pytest

from backend.events import KEEP, REPEAT_AFTER, EventLog
from backend.state import StateFile


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def container(key="a1", name="jellyfin", state="running", health=None):
    return {"id": key, "name": name, "state": state, "health": health}


def snapshot(**over):
    base = {
        "containers": {"ok": True, "error": None, "items": [container()]},
        "network": {
            "ok": True, "error": None, "gateway": "192.168.1.1", "link": {"name": "enp3s0", "state": "up"},
            "listening": [{"port": 22, "proto": "tcp", "scope": "*", "who": "sshd"}],
            "ping": [{"name": "internet", "host": "1.1.1.1", "last": 20.0, "history": [20.0]}],
            "dns": {"ok": True, "error": None}, "wan": {"ip": "203.0.113.4"},
            "speedtest": {"last": {"ts": 1.0, "down_bps": 202_000_000, "up_bps": 19_000_000}, "error": None},
        },
        "hardware": {
            "ok": True, "error": None, "ups": {"present": False}, "board_sensors": False,
            "temps": [{"name": "cpu", "c": 40.0, "warn": 80.0, "crit": 90.0}], "power": {"cpu_measured": False},
        },
    }
    for path, value in over.items():
        node = base
        *parents, last = path.split("__")
        for part in parents:
            node = node[part]
        node[last] = value
    return base


@pytest.fixture
def log(tmp_path):
    clock = Clock()
    events = EventLog(StateFile(tmp_path / "state.json"), clock)
    events.observe(snapshot())
    events.clock = clock
    return events


def said(log, start=1):
    """Lines logged after the start-up line, oldest first, as (tag, level, text)."""
    return [(e["tag"], e["level"], e["text"]) for e in log.recent(KEEP)[::-1]][start:]


def test_starting_up_is_the_first_line(log):
    assert said(log, start=0) == [("SYS", "info", "monitor started")]


def test_first_snapshot_is_not_news(log):
    # Whatever is there when we start is simply how things are.
    assert said(log) == []


def test_nothing_changing_logs_nothing(log):
    for _ in range(5):
        log.observe(snapshot())

    assert said(log) == []


def test_container_stopping_and_starting(log):
    log.observe(snapshot(containers__items=[container(state="exited")]))
    log.observe(snapshot())

    assert said(log) == [("CTR", "warn", "jellyfin running -> exited"), ("CTR", "info", "jellyfin exited -> running")]


def test_container_appearing_and_being_removed(log):
    log.observe(snapshot(containers__items=[container(), container("b2", "sonarr")]))
    log.observe(snapshot())

    assert said(log) == [("CTR", "info", "sonarr appeared (running)"), ("CTR", "warn", "sonarr removed")]


def test_container_health(log):
    log.observe(snapshot(containers__items=[container(health="unhealthy")]))
    log.observe(snapshot(containers__items=[container(health="healthy")]))
    log.observe(snapshot(containers__items=[container(health=None)]))

    assert said(log) == [("CTR", "crit", "jellyfin is unhealthy"), ("CTR", "info", "jellyfin is healthy")]


def test_docker_going_away_does_not_report_every_container_as_removed(log):
    down = snapshot(containers__ok=False, containers__error="Docker is not reachable: Connection refused", containers__items=[])
    log.observe(down)
    log.observe(down)
    log.observe(snapshot())

    assert said(log) == [("DOCK", "crit", "Docker is not reachable: Connection refused"), ("DOCK", "info", "docker is back")]


def test_ports_opening_and_closing(log):
    ports = [{"port": 22, "proto": "tcp", "scope": "*", "who": "sshd"}, {"port": 4444, "proto": "tcp", "scope": "*", "who": "nc"},
             {"port": 9000, "proto": "udp", "scope": "lo", "who": None}]
    log.observe(snapshot(network__listening=ports))
    log.observe(snapshot())

    assert said(log) == [
        ("PORT", "warn", "4444/tcp opened by nc (*)"),
        ("PORT", "info", "9000/udp opened (lo)"),
        ("PORT", "info", "4444/tcp closed"),
        ("PORT", "info", "9000/udp closed"),
    ]


def test_the_dashboards_own_port_is_not_news(tmp_path):
    events = EventLog(StateFile(tmp_path / "s.json"), Clock(), own_port=8787)
    ours = [{"port": 22, "proto": "tcp", "scope": "*", "who": "sshd"}, {"port": 8787, "proto": "tcp", "scope": "lo", "who": "python3"},
            {"port": 8787, "proto": "udp", "scope": "lo", "who": "other"}]
    events.observe(snapshot())
    events.observe(snapshot(network__listening=ours))
    events.observe(snapshot())

    # Only the unrelated UDP port with the same number is reported.
    assert [e["text"] for e in events.recent()][:2] == ["8787/udp closed", "8787/udp opened by other (lo)"]
    assert len(events.recent()) == 3


def test_port_list_becoming_unavailable_is_not_every_port_closing(log):
    log.observe(snapshot(network__listening=None))
    log.observe(snapshot())

    assert said(log) == []


def test_link_gateway_dns_and_public_address(log):
    log.observe(snapshot(network__link={"name": "enp3s0", "state": "down"}))
    log.observe(snapshot(network__gateway="10.0.0.1"))
    log.observe(snapshot(network__dns={"ok": False, "error": "timed out"}))
    log.observe(snapshot(network__wan={"ip": "198.51.100.7"}))

    assert said(log) == [
        ("NET", "crit", "enp3s0 link down"),
        ("NET", "info", "enp3s0 link up"),
        ("NET", "warn", "gateway now 10.0.0.1"),
        ("NET", "warn", "gateway now 192.168.1.1"),
        ("DNS", "crit", "lookups failing: timed out"),
        ("DNS", "info", "lookups working again"),
        ("WAN", "warn", "public address now 198.51.100.7"),
    ]


def test_first_public_address_or_first_dns_answer_is_not_news(tmp_path):
    events = EventLog(StateFile(tmp_path / "s.json"), Clock())
    events.observe(snapshot(network__wan={"ip": None}, network__dns={"ok": False, "error": None}, network__gateway=None))
    events.observe(snapshot())

    assert [e["text"] for e in events.recent()] == ["monitor started"]


def test_ping_lost_and_back(log):
    lost = [{"name": "internet", "host": "1.1.1.1", "last": None, "history": [20.0, None]}]
    log.observe(snapshot(network__ping=lost))
    log.observe(snapshot(network__ping=lost))
    log.observe(snapshot())

    assert said(log) == [("PING", "warn", "internet 1.1.1.1 not answering"), ("PING", "info", "internet answering again (20 ms)")]


def test_pinger_that_has_not_pinged_yet_is_not_a_lost_ping(log):
    log.observe(snapshot(network__ping=[{"name": "internet", "host": "1.1.1.1", "last": None, "history": []}]))

    assert said(log) == []


def test_speed_test_results_and_failures(log):
    log.observe(snapshot(network__speedtest={"last": {"ts": 2.0, "down_bps": 941_000_000, "up_bps": 35_200_000}, "error": None}))
    failed = {"last": {"ts": 2.0, "down_bps": 941_000_000, "up_bps": 35_200_000}, "error": "speed test download refused (HTTP 429)"}
    log.observe(snapshot(network__speedtest=failed))
    log.observe(snapshot(network__speedtest=failed))

    assert said(log) == [
        ("SPEED", "info", "941 Mbps down, 35 Mbps up"),
        ("SPEED", "warn", "test failed: speed test download refused (HTTP 429)"),
    ]


def test_temperature_crossing_its_limits(log):
    hot = lambda c: [{"name": "cpu", "c": c, "warn": 80.0, "crit": 90.0}]
    for c in (79.9, 82.0, 85.0, 91.0, 84.0, 60.0):
        log.observe(snapshot(hardware__temps=hot(c)))

    assert said(log) == [
        ("TEMP", "warn", "cpu at 82 C"),
        ("TEMP", "crit", "cpu at 91 C"),
        ("TEMP", "warn", "cpu at 84 C"),
        ("TEMP", "info", "cpu back to 60 C"),
    ]


def test_ups_power_events(log):
    log.observe(snapshot(hardware__ups={"present": True, "on_battery": False}))
    log.observe(snapshot(hardware__ups={"present": True, "on_battery": True}))
    log.observe(snapshot(hardware__ups={"present": True, "on_battery": False}))
    log.observe(snapshot())

    assert said(log) == [
        ("UPS", "info", "UPS connected"),
        ("UPS", "crit", "mains power lost, running on battery"),
        ("UPS", "info", "mains power is back"),
        ("UPS", "info", "UPS disconnected"),
    ]


CARD = {"name": "NVIDIA GeForce RTX 3060", "load": 14.0, "mem_used": 1, "mem_total": 2}


def test_graphics_card_dropping_out_and_coming_back(log):
    log.observe(snapshot(hardware__gpu={"cards": [CARD], "error": None}))
    log.observe(snapshot(hardware__gpu={"cards": [], "error": "Failed to initialize NVML: Driver/library version mismatch"}))
    log.observe(snapshot(hardware__gpu={"cards": [], "error": "Failed to initialize NVML: Driver/library version mismatch"}))
    log.observe(snapshot(hardware__gpu={"cards": [CARD], "error": None}))

    assert said(log) == [
        ("GPU", "crit", "card not answering: Failed to initialize NVML: Driver/library version mismatch"),
        ("GPU", "info", "card is answering again"),
    ]


def test_machine_without_a_graphics_card_says_nothing(log):
    log.observe(snapshot(hardware__gpu={"cards": [], "error": None}))
    log.observe(snapshot(hardware__gpu={"cards": [], "error": None}))
    # A snapshot from before the section existed has no entry for it at all.
    log.observe(snapshot())

    assert said(log) == []


def test_error_that_clears_without_a_card_is_not_called_a_return(log):
    log.observe(snapshot(hardware__gpu={"cards": [], "error": "No devices were found"}))
    log.observe(snapshot(hardware__gpu={"cards": [], "error": None}))

    assert said(log) == [("GPU", "crit", "card not answering: No devices were found")]


def test_sensors_and_power_becoming_available(log):
    log.observe(snapshot(hardware__board_sensors=True, hardware__power={"cpu_measured": True}))

    assert said(log) == [("PWR", "info", "processor power is now measured"), ("HW", "info", "board fan and voltage sensors found")]


def test_collector_failures_are_logged_once_and_recovery_too(log):
    broken = snapshot()
    broken["network"] = {"ok": False, "error": "network collector failed: boom"}
    broken["hardware"] = {"ok": False, "error": None}
    log.observe(broken)
    log.observe(broken)
    log.observe(snapshot())

    assert said(log) == [
        ("NET", "crit", "network collector failed: boom"),
        ("HW", "crit", "hardware data unavailable"),
        ("NET", "info", "network is back"),
        ("HW", "info", "hardware is back"),
    ]


def test_a_flapping_thing_does_not_flood_the_log(log):
    up, down = snapshot(), snapshot(containers__items=[container(state="restarting")])
    for _ in range(10):
        log.observe(down)
        log.observe(up)
    assert len(said(log)) == 2

    log.clock.now += REPEAT_AFTER
    log.observe(down)
    assert len(said(log)) == 3


def test_snapshots_missing_whole_sections_do_not_crash(log):
    for broken in ({}, {"containers": None, "network": None, "hardware": None}, {"containers": {"ok": True}}, snapshot()):
        log.observe(copy.deepcopy(broken))


def test_log_is_capped_and_newest_comes_first(tmp_path):
    clock = Clock()
    events = EventLog(StateFile(tmp_path / "s.json"), clock)
    events.observe(snapshot())
    for n in range(KEEP + 50):
        clock.now += 1
        events.observe(snapshot(network__gateway=f"10.0.{n // 250}.{n % 250 + 1}"))

    assert len(events.recent(KEEP * 2)) == KEEP
    newest = events.recent(3)
    assert len(newest) == 3 and newest[0]["ts"] > newest[1]["ts"] > newest[2]["ts"]
    assert len(events.recent()) == 30


def test_recent_lines_survive_a_restart(tmp_path):
    state = StateFile(tmp_path / "s.json")
    first = EventLog(state, Clock(1000.0))
    first.observe(snapshot())
    first.observe(snapshot(containers__items=[container(state="exited")]))

    second = EventLog(StateFile(tmp_path / "s.json"), Clock(5000.0))

    assert [e["text"] for e in second.recent()] == ["monitor started", "jellyfin running -> exited", "monitor started"]
