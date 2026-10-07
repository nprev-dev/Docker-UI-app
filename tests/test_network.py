"""Edge cases for the network collector, run against a scripted machine."""

from __future__ import annotations

import json
import time

import pytest

from backend.collectors import network as mod
from backend.collectors.network import DayTotals, NetworkCollector, Sock, Talkers
from backend.state import StateFile

ROUTES = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
enp3s0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0
enp3s0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
"""

SS = """\
tcp LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:* cubic cwnd:10
tcp LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* cubic cwnd:10
tcp LISTEN 0 4096 [::]:22 [::]:* cubic cwnd:10
tcp LISTEN 0 4096 *:8096 *:* cubic cwnd:10
tcp LISTEN 0 4096 100.64.0.7:443 0.0.0.0:* cubic cwnd:10
tcp LISTEN 0 4096 [fd7a:115c:a1e0::7]:443 [::]:* cubic cwnd:10
tcp LISTEN 0 4096 127.0.0.1:631 0.0.0.0:* cubic cwnd:10
tcp LISTEN 0 4096 [::1]:631 [::]:* cubic cwnd:10
tcp LISTEN 0 4096 192.168.1.253:9000 0.0.0.0:* cubic cwnd:10
tcp ESTAB 0 0 [::ffff:192.168.1.253]:8096 [::ffff:192.168.1.20]:36314 cubic rto:201 bytes_sent:900 bytes_acked:800 bytes_received:100 segs_out:5
tcp ESTAB 0 0 192.168.1.253:40332 198.51.100.9:443 users:(("curl",pid=1,fd=2)) cubic bytes_sent:5815 bytes_acked:5816 bytes_received:4292
tcp ESTAB 0 0 127.0.0.1:50000 127.0.0.1:8787 cubic bytes_acked:999999 bytes_received:999999
tcp TIME-WAIT 0 0 192.168.1.253:40000 1.2.3.4:443
tcp SYN-SENT 0 1 192.168.1.253:40001 5.6.7.8:443
udp UNCONN 0 0 0.0.0.0:41641 0.0.0.0:*
udp UNCONN 0 0 0.0.0.0:5353 0.0.0.0:*
udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:*
udp ESTAB 0 0 192.168.1.253%enp3s0:68 192.168.1.1:67
garbage line that is not a socket
"""

SS_NAMES = """\
tcp LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=900,fd=3))
tcp LISTEN 0 4096 127.0.0.1:631 0.0.0.0:* users:(("cupsd",pid=901,fd=7))
tcp LISTEN 0 4096 *:8096 *:*
"""


def link(rx=0, tx=0, **over):
    base = {
        "name": "enp3s0", "state": "up", "speed": 1000, "duplex": "full", "mtu": 1500, "mac": "aa:bb:cc:dd:ee:ff",
        "flaps": 2, "rx_bytes": rx, "tx_bytes": tx, "rx_errs": 0, "tx_errs": 0, "rx_drop": 3, "tx_drop": 0,
    }
    base.update(over)
    return base


class Machine:
    """A scripted stand-in for the kernel files and the `ss` command."""

    def __init__(self):
        self.now = 1000.0
        self.wall = time.mktime((2026, 10, 5, 12, 0, 0, 0, 0, -1))
        self.route = ("enp3s0", "192.168.1.1")
        self.link = link()
        self.ss = SS
        self.ss_names = SS_NAMES
        self.ss_error = None
        self.boot = ("boot-1", self.wall - 86400 * 3)
        self.name_lookups = 0

    def sockets(self, with_processes):
        if self.ss_error:
            raise self.ss_error
        if with_processes:
            self.name_lookups += 1
            return self.ss_names
        return self.ss

    def tick(self, seconds=1.0, rx=0, tx=0):
        self.now += seconds
        self.wall += seconds
        if self.link:
            self.link = {**self.link, "rx_bytes": self.link["rx_bytes"] + rx, "tx_bytes": self.link["tx_bytes"] + tx}


@pytest.fixture
def machine():
    return Machine()


@pytest.fixture
def state(tmp_path):
    return StateFile(tmp_path / "state.json")


def make(machine, state, **over):
    settings = dict(
        probes_enabled=False,
        route=lambda: machine.route,
        link=lambda name: machine.link if machine.link and machine.link["name"] == name else None,
        sockets=machine.sockets,
        clock=lambda: machine.wall,
        monotonic=lambda: machine.now,
        boot=lambda: machine.boot,
    )
    return NetworkCollector(state, **{**settings, **over})


# --- routes -------------------------------------------------------------------


def test_default_route_is_found_with_its_gateway():
    assert mod.default_route(ROUTES) == ("enp3s0", "192.168.1.1")


def test_lowest_metric_default_route_wins():
    text = ROUTES + "wlp2s0\t00000000\t01000A0A\t0003\t0\t0\t20\t00000000\t0\t0\t0\n"

    assert mod.default_route(text) == ("wlp2s0", "10.10.0.1")


def test_default_route_without_a_gateway():
    text = "header\ntun0\t00000000\t00000000\t0001\t0\t0\t50\t00000000\t0\t0\t0\n"

    assert mod.default_route(text) == ("tun0", None)


@pytest.mark.parametrize("text", ["", "header only\n", "header\nbroken line\n", "header\neth0\tzz\tzz\tzz\t0\t0\tx\tzz\n"])
def test_no_usable_default_route(text):
    assert mod.default_route(text) == (None, None)


# --- link files ---------------------------------------------------------------


def write_link(root, name="eth0", **files):
    base = root / name
    (base / "statistics").mkdir(parents=True)
    defaults = {
        "operstate": "up", "speed": "1000", "duplex": "full", "mtu": "1500", "address": "aa:bb:cc:dd:ee:ff",
        "carrier_changes": "4", "statistics/rx_bytes": "10", "statistics/tx_bytes": "20",
        "statistics/rx_errors": "1", "statistics/tx_errors": "2", "statistics/rx_dropped": "3", "statistics/tx_dropped": "4",
    }
    for field, value in {**defaults, **files}.items():
        if value is not None:
            (base / field).write_text(value + "\n")


def test_link_is_read_from_kernel_files(tmp_path):
    write_link(tmp_path)

    info = mod.read_link("eth0", tmp_path)

    assert info == {
        "name": "eth0", "state": "up", "speed": 1000, "duplex": "full", "mtu": 1500, "mac": "aa:bb:cc:dd:ee:ff",
        "flaps": 4, "rx_bytes": 10, "tx_bytes": 20, "rx_errs": 1, "tx_errs": 2, "rx_drop": 3, "tx_drop": 4,
    }


def test_link_that_is_down_has_no_speed_or_duplex(tmp_path):
    # The kernel reports -1 or refuses the read altogether while the cable is out.
    write_link(tmp_path, operstate="down", speed="-1", duplex="unknown")
    assert mod.read_link("eth0", tmp_path)["speed"] is None
    assert mod.read_link("eth0", tmp_path)["duplex"] is None

    write_link(tmp_path, name="eth1", operstate="down", speed=None, duplex=None)
    info = mod.read_link("eth1", tmp_path)
    assert info["state"] == "down" and info["speed"] is None and info["duplex"] is None


def test_missing_interface(tmp_path):
    assert mod.read_link("nope0", tmp_path) is None


# --- sockets ------------------------------------------------------------------


def test_socket_lines_are_parsed():
    sockets = mod.parse_sockets(SS)

    assert len(sockets) == 18
    assert Sock("tcp", "LISTEN", "127.0.0.53", 53, "0.0.0.0", None, None, None, None) in sockets
    assert Sock("tcp", "LISTEN", "*", 8096, "*", None, None, None, None) in sockets
    assert Sock("tcp", "LISTEN", "fd7a:115c:a1e0::7", 443, "::", None, None, None, None) in sockets
    # IPv4 seen through a dual-stack socket loses its ::ffff: wrapper.
    assert Sock("tcp", "ESTAB", "192.168.1.253", 8096, "192.168.1.20", 36314, 800, 100, None) in sockets
    assert Sock("tcp", "ESTAB", "192.168.1.253", 40332, "198.51.100.9", 443, 5816, 4292, "curl") in sockets
    assert Sock("udp", "ESTAB", "192.168.1.253", 68, "192.168.1.1", 67, None, None, None) in sockets


@pytest.mark.parametrize("text", ["", "\n\n", "State Recv-Q Send-Q\n", "tcp LISTEN 0\n"])
def test_unusable_socket_output_gives_nothing(text):
    assert mod.parse_sockets(text) == []


def test_listening_ports_merge_addresses_and_sort_by_exposure():
    sockets = mod.parse_sockets(SS)
    names = mod.listener_names(mod.parse_sockets(SS_NAMES))

    entries = mod.listening(sockets, names)

    assert [(e["port"], e["proto"], e["scope"]) for e in entries] == [
        (22, "tcp", "*"),
        (5353, "udp", "*"),
        (8096, "tcp", "*"),
        (9000, "tcp", "lan"),
        (443, "tcp", "ts"),
        (53, "tcp", "lo"),
        (53, "udp", "lo"),
        (631, "tcp", "lo"),
    ]
    who = {(e["port"], e["proto"]): e["who"] for e in entries}
    # Process name when we may see it, registered service name otherwise.
    assert who[(22, "tcp")] == "sshd"
    assert who[(631, "tcp")] == "cupsd"
    assert who[(53, "udp")] == "dns"
    assert who[(5353, "udp")] == "mdns"
    assert who[(9000, "tcp")] in (None, "cslistener")


def test_udp_sockets_on_random_client_ports_are_not_services():
    entries = mod.listening(mod.parse_sockets(SS), {})

    assert (41641, "udp") not in {(e["port"], e["proto"]) for e in entries}


@pytest.mark.parametrize(
    "addresses, expected",
    [
        (["0.0.0.0"], "*"),
        (["::"], "*"),
        (["*"], "*"),
        (["127.0.0.1", "0.0.0.0"], "*"),
        (["127.0.0.1", "::1"], "lo"),
        (["127.0.0.53"], "lo"),
        (["100.64.0.7", "fd7a:115c:a1e0::7"], "ts"),
        (["192.168.1.253"], "lan"),
        (["100.64.0.7", "192.168.1.253"], "lan"),
        (["127.0.0.1", "192.168.1.253"], "lan"),
        (["not-an-address"], "lan"),
    ],
)
def test_scope_says_who_can_reach_a_port(addresses, expected):
    assert mod._scope(addresses) == expected


def test_socket_counts():
    assert mod.socket_counts(mod.parse_sockets(SS)) == {
        "tcp_estab": 3, "tcp_listen": 9, "tcp_time_wait": 1, "tcp_other": 1, "udp": 4,
    }


def test_ports_are_named_after_the_container_that_publishes_them():
    network = {"listening": [{"port": 8096, "proto": "tcp", "scope": "*", "who": "com.docker.backend"},
                             {"port": 22, "proto": "tcp", "scope": "*", "who": "sshd"},
                             {"port": 9999, "proto": "tcp", "scope": "*", "who": None}]}
    containers = {"items": [
        {"name": "jellyfin", "state": "running", "ports": [{"host": 8096, "container": 8096, "proto": "tcp"}]},
        {"name": "stopped", "state": "exited", "ports": [{"host": 9999, "container": 80, "proto": "tcp"}]},
        {"name": "internal", "state": "running", "ports": [{"host": None, "container": 22, "proto": "tcp"}]},
    ]}

    mod.label_ports(network, containers)

    assert network["listening"][0] == {"port": 8096, "proto": "tcp", "scope": "*", "who": "jellyfin", "container": True}
    assert network["listening"][1]["who"] == "sshd"
    assert network["listening"][2]["who"] is None


@pytest.mark.parametrize("network, containers", [({}, {}), ({"listening": None}, {"items": None}), ({"listening": []}, {"ok": False})])
def test_labelling_survives_missing_data(network, containers):
    mod.label_ports(network, containers)


# --- top talkers --------------------------------------------------------------


def conn(peer, acked, received, port=40000, local_port=8096):
    return Sock("tcp", "ESTAB", "192.168.1.253", local_port, peer, port, acked, received, None)


def test_connections_already_open_at_start_are_only_a_baseline():
    talkers = Talkers(smoothing=0.001)

    assert talkers.update([conn("192.168.1.20", 10_000_000, 10_000_000)], now=0) == []
    # Their old bytes must not show up as a burst on the next sample either.
    assert talkers.update([conn("192.168.1.20", 10_000_000, 10_000_000)], now=1) == []


def test_rates_come_from_the_bytes_moved_between_samples():
    talkers = Talkers(smoothing=0.001)
    talkers.update([conn("192.168.1.20", 0, 0)], now=0)

    result = talkers.update([conn("192.168.1.20", 1_000_000, 250_000)], now=1)

    assert result == [{"host": "192.168.1.20", "rx_bps": 2_000_000, "tx_bps": 8_000_000, "conns": 1}]


def test_connections_to_one_host_are_added_together_and_ranked():
    talkers = Talkers(smoothing=0.001)
    talkers.update([conn("10.0.0.1", 0, 0, port=1), conn("10.0.0.1", 0, 0, port=2), conn("10.0.0.2", 0, 0)], now=0)

    result = talkers.update(
        [conn("10.0.0.1", 1000, 0, port=1), conn("10.0.0.1", 3000, 0, port=2), conn("10.0.0.2", 500_000, 0)], now=1
    )

    assert [(r["host"], r["tx_bps"], r["conns"]) for r in result] == [("10.0.0.2", 4_000_000, 1), ("10.0.0.1", 32_000, 2)]


def test_connection_opened_after_start_counts_from_zero():
    talkers = Talkers(smoothing=0.001)
    talkers.update([], now=0)

    result = talkers.update([conn("10.0.0.9", 125_000, 0)], now=1)

    assert result[0]["tx_bps"] == 1_000_000


def test_reused_address_pair_does_not_go_negative():
    talkers = Talkers(smoothing=0.001)
    talkers.update([conn("10.0.0.9", 9_000_000, 9_000_000)], now=0)

    result = talkers.update([conn("10.0.0.9", 125_000, 125_000)], now=1)

    assert result == [{"host": "10.0.0.9", "rx_bps": 1_000_000, "tx_bps": 1_000_000, "conns": 1}]


def test_loopback_and_non_tcp_sockets_are_ignored():
    talkers = Talkers(smoothing=0.001)
    talkers.update([], now=0)
    sockets = [
        conn("127.0.0.1", 9_000_000, 9_000_000),
        conn("::1", 9_000_000, 9_000_000),
        Sock("udp", "ESTAB", "192.168.1.253", 68, "192.168.1.1", 67, None, None, None),
        Sock("tcp", "TIME-WAIT", "192.168.1.253", 1, "1.2.3.4", 443, None, None, None),
    ]

    assert talkers.update(sockets, now=1) == []


def test_quiet_hosts_fade_out_and_are_forgotten():
    talkers = Talkers(smoothing=2.0, forget=10.0)
    talkers.update([conn("10.0.0.9", 0, 0)], now=0)
    first = talkers.update([conn("10.0.0.9", 1_250_000, 0)], now=1)[0]["tx_bps"]

    later = talkers.update([conn("10.0.0.9", 1_250_000, 0)], now=3)

    assert 0 < later[0]["tx_bps"] < first
    assert talkers.update([], now=20) == []
    assert talkers._hosts == {}


def test_background_chatter_is_not_a_talker():
    talkers = Talkers(smoothing=0.001)
    talkers.update([conn("10.0.0.9", 0, 0)], now=0)

    assert talkers.update([conn("10.0.0.9", 50, 50)], now=1) == []


def test_only_the_top_few_talkers_are_reported():
    talkers = Talkers(smoothing=0.001)
    hosts = [f"10.0.0.{n}" for n in range(1, 21)]
    talkers.update([conn(h, 0, 0) for h in hosts], now=0)

    result = talkers.update([conn(h, 100_000 * n, 0) for n, h in enumerate(hosts, 1)], now=1)

    assert len(result) == mod.TOP_TALKERS
    assert result[0]["host"] == "10.0.0.20"


# --- today's totals -----------------------------------------------------------


def at(hour, minute=0, day=5):
    return time.mktime((2026, 10, day, hour, minute, 0, 0, 0, -1))


class Day:
    def __init__(self, state, now, boot=("boot-1", None)):
        self.now = now
        self.boot = (boot[0], boot[1] if boot[1] is not None else at(0, day=1))
        self.totals = DayTotals(state, clock=lambda: self.now, boot=lambda: self.boot)


def test_first_run_on_a_machine_that_booted_earlier_starts_from_now(state):
    day = Day(state, at(14))

    first = day.totals.update("eth0", 5_000, 9_000)
    day.now += 60
    second = day.totals.update("eth0", 5_700, 9_100)

    assert (first["rx"], first["tx"], first["whole_day"]) == (0, 0, False)
    assert first["since"] == at(14)
    assert (second["rx"], second["tx"]) == (700, 100)


def test_machine_that_booted_today_counts_everything_since_boot(state):
    day = Day(state, at(14), boot=("boot-1", at(9)))

    result = day.totals.update("eth0", 5_000, 9_000)

    assert (result["rx"], result["tx"], result["since"], result["whole_day"]) == (5_000, 9_000, at(9), False)


def test_dashboard_restart_keeps_the_total_and_adds_what_it_missed(state):
    day = Day(state, at(10))
    day.totals.update("eth0", 1_000, 1_000)
    day.now += 30
    day.totals.update("eth0", 2_000, 1_500)
    day.totals.save()

    # The dashboard was down for an hour while traffic kept flowing.
    restarted = Day(state, at(11, 30))
    result = restarted.totals.update("eth0", 10_000, 4_000)

    assert (result["rx"], result["tx"]) == (9_000, 3_000)


def test_reboot_during_the_day_adds_the_new_counters_on_top(state):
    day = Day(state, at(10))
    day.totals.update("eth0", 1_000, 1_000)
    day.now += 60
    day.totals.update("eth0", 6_000, 3_000)
    day.totals.save()

    after_reboot = Day(state, at(12), boot=("boot-2", at(11, 55)))
    result = after_reboot.totals.update("eth0", 400, 100)

    assert (result["rx"], result["tx"]) == (5_400, 2_100)


def test_midnight_resets_the_total_when_we_were_watching(state):
    day = Day(state, at(23, 59, day=4) + 59)
    day.totals.update("eth0", 1_000_000, 1_000_000)
    day.now = at(0) + 1

    result = day.totals.update("eth0", 1_000_300, 1_000_050)

    assert (result["rx"], result["tx"], result["since"], result["whole_day"]) == (300, 50, at(0), True)


def test_new_day_after_the_dashboard_was_off_overnight_starts_from_now(state):
    day = Day(state, at(18, day=4))
    day.totals.update("eth0", 1_000, 1_000)
    day.totals.save()

    morning = Day(state, at(8))
    result = morning.totals.update("eth0", 90_000, 90_000)

    assert (result["rx"], result["tx"], result["since"], result["whole_day"]) == (0, 0, at(8), False)


def test_switching_interface_does_not_add_the_other_interfaces_history(state):
    day = Day(state, at(10))
    day.totals.update("eth0", 1_000, 1_000)
    day.now += 10
    day.totals.update("eth0", 1_500, 1_200)
    day.now += 10

    switched = day.totals.update("wlan0", 80_000_000, 80_000_000)
    day.now += 10
    after = day.totals.update("wlan0", 80_000_100, 80_000_010)

    assert (switched["rx"], switched["tx"]) == (500, 200)
    assert (after["rx"], after["tx"]) == (600, 210)


def test_total_is_written_to_disk_at_most_once_a_minute(state, tmp_path):
    day = Day(state, at(10))
    day.totals.update("eth0", 1_000, 1_000)
    saved = json.loads((tmp_path / "state.json").read_text())["today"]["last_rx"]
    day.now += 30
    day.totals.update("eth0", 2_000, 1_000)
    assert json.loads((tmp_path / "state.json").read_text())["today"]["last_rx"] == saved

    day.now += 31
    day.totals.update("eth0", 3_000, 1_000)
    assert json.loads((tmp_path / "state.json").read_text())["today"]["last_rx"] == 3_000


# --- the collector ------------------------------------------------------------


def test_first_sample_has_no_rate_and_no_history(machine, state):
    result = make(machine, state).collect()

    assert result["ok"] is True and result["error"] is None
    assert result["rx_bps"] is None and result["tx_bps"] is None
    assert result["history"] == {"rx": [], "tx": []}
    assert result["gateway"] == "192.168.1.1"
    assert result["link"]["name"] == "enp3s0" and result["link"]["speed"] == 1000
    assert "rx_bytes" not in result["link"]


def test_rates_use_the_real_time_between_samples(machine, state):
    collector = make(machine, state)
    collector.collect()
    machine.tick(2.0, rx=2_500_000, tx=250_000)

    result = collector.collect()

    assert (result["rx_bps"], result["tx_bps"]) == (10_000_000, 1_000_000)
    assert result["history"] == {"rx": [10_000_000], "tx": [1_000_000]}
    assert result["totals"]["boot"] == {"rx": 2_500_000, "tx": 250_000}


def test_history_is_capped(machine, state):
    collector = make(machine, state)
    collector.collect()
    for n in range(mod.HISTORY + 25):
        machine.tick(1.0, rx=n)
        result = collector.collect()

    assert len(result["history"]["rx"]) == mod.HISTORY
    assert result["history"]["rx"][-1] == (mod.HISTORY + 24) * 8


def test_counter_reset_skips_one_sample_instead_of_going_negative(machine, state):
    collector = make(machine, state)
    machine.link = link(rx=9_000_000, tx=9_000_000)
    collector.collect()
    machine.now += 1
    machine.link = link(rx=100, tx=100)

    result = collector.collect()
    assert result["rx_bps"] is None and result["history"]["rx"] == []

    machine.tick(1.0, rx=125, tx=0)
    assert collector.collect()["rx_bps"] == 1000


def test_no_default_route(machine, state):
    machine.route = (None, None)

    result = make(machine, state).collect()

    assert result["ok"] is False and result["error"] == "no default route"
    assert result["link"] is None and result["totals"] is None and result["gateway"] is None
    # Open ports are still worth showing without a route.
    assert len(result["listening"]) == 8


def test_interface_disappearing_drops_the_graph_to_zero(machine, state):
    collector = make(machine, state)
    collector.collect()
    machine.tick(1.0, rx=1250)
    collector.collect()
    machine.link = None
    machine.now += 1

    result = collector.collect()

    assert result["ok"] is False and result["error"] == "interface enp3s0 not found"
    assert result["rx_bps"] is None
    assert result["history"]["rx"] == [10_000, 0]

    # And it must not keep adding zeros forever with nothing to measure, nor crash on return.
    machine.now += 1
    assert collector.collect()["history"]["rx"] == [10_000, 0]
    machine.link = link(rx=5, tx=5)
    machine.now += 1
    assert collector.collect()["rx_bps"] is None


def test_forced_interface_overrides_the_route(machine, state):
    machine.link = link(name="wlan9")

    result = make(machine, state, iface="wlan9").collect()

    assert result["ok"] is True and result["link"]["name"] == "wlan9"


def test_forced_interface_that_does_not_exist(machine, state):
    result = make(machine, state, iface="nope0").collect()

    assert result["ok"] is False and result["error"] == "interface nope0 not found"


def test_link_down_is_reported_not_hidden(machine, state):
    machine.link = link(state="down", speed=None, duplex=None)

    result = make(machine, state).collect()

    assert result["ok"] is True
    assert result["link"]["state"] == "down" and result["link"]["speed"] is None


def test_missing_byte_counters(machine, state):
    machine.link = link(rx_bytes=None, tx_bytes=None)
    collector = make(machine, state)

    collector.collect()
    machine.now += 1
    result = collector.collect()

    assert result["rx_bps"] is None and result["totals"] is None


def test_socket_tool_missing_or_failing(machine, state):
    collector = make(machine, state)
    for error in (FileNotFoundError("ss"), mod.subprocess.TimeoutExpired("ss", 3), mod.subprocess.CalledProcessError(1, "ss")):
        machine.ss_error = error
        result = collector.collect()
        assert result["sockets"] is None and result["listening"] is None and result["talkers"] is None
        assert result["ok"] is True

    machine.ss_error = None
    assert collector.collect()["sockets"]["tcp_listen"] == 9


def test_process_names_are_looked_up_only_now_and_then(machine, state):
    collector = make(machine, state)
    for _ in range(10):
        collector.collect()
        machine.tick(1.0)
    assert machine.name_lookups == 1

    machine.tick(mod.NAMES_EVERY)
    collector.collect()
    assert machine.name_lookups == 2


def test_probes_switched_off_report_nothing(machine, state):
    result = make(machine, state).collect()

    assert result["ping"] == [] and result["dns"] is None and result["wan"] is None and result["speedtest"] is None


class FakePinger:
    made = []

    def __init__(self, host, interval=1.0):
        self.host, self.interval, self.stopped = host, interval, False
        FakePinger.made.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def summary(self):
        return {"host": self.host, "interval": self.interval, "history": []}


@pytest.fixture
def probing(machine, state, monkeypatch):
    """A collector with probes on, but with stand-ins so nothing is sent onto the network."""
    FakePinger.made = []
    monkeypatch.setattr(mod.probes, "Pinger", FakePinger)
    monkeypatch.setattr(mod.probes.Every, "start", lambda self: None)
    monkeypatch.setattr(mod.probes.SpeedTester, "start", lambda self: None)

    def build(**over):
        return make(machine, state, **{"probes_enabled": True, "speedtest_hours": 0, **over})

    return build


def test_outside_target_is_pinged_far_less_often_than_the_gateway(probing):
    result = probing().collect()

    cadence = {p["name"]: (p["host"], p["interval"]) for p in result["ping"]}
    assert cadence == {"gateway": ("192.168.1.1", 1.0), "internet": ("1.1.1.1", 30.0)}


def test_ping_interval_can_be_set_but_not_below_one_second(probing):
    assert {p["name"]: p["interval"] for p in probing(ping_interval=120).collect()["ping"]}["internet"] == 120
    FakePinger.made = []
    assert {p["name"]: p["interval"] for p in probing(ping_interval=0).collect()["ping"]}["internet"] == 1.0


def test_pingers_are_started_once_and_kept(machine, probing):
    collector = probing()
    for _ in range(5):
        collector.collect()
        machine.tick(1.0)

    assert [(p.host, p.interval) for p in FakePinger.made] == [("192.168.1.1", 1.0), ("1.1.1.1", 30.0)]
    assert not any(p.stopped for p in FakePinger.made)


def test_new_gateway_gets_a_new_pinger_and_keeps_the_internet_history(machine, probing):
    collector = probing()
    collector.collect()
    machine.route = ("enp3s0", "10.0.0.1")

    result = collector.collect()

    old_gateway, internet, new_gateway = FakePinger.made
    assert old_gateway.stopped and not internet.stopped
    assert (new_gateway.host, new_gateway.interval) == ("10.0.0.1", 1.0)
    assert [p["host"] for p in result["ping"]] == ["10.0.0.1", "1.1.1.1"]


def test_no_gateway_means_no_gateway_pinger(machine, probing):
    machine.route = ("enp3s0", None)

    result = probing().collect()

    assert [p["name"] for p in result["ping"]] == ["internet"]


def test_result_is_plain_json(machine, state):
    collector = make(machine, state)
    collector.collect()
    machine.tick(1.0, rx=1000, tx=1000)

    json.dumps(collector.collect(), allow_nan=False)


def test_close_saves_todays_total(machine, state, tmp_path):
    collector = make(machine, state)
    collector.collect()
    machine.tick(5.0, rx=4_000, tx=2_000)
    collector.collect()

    collector.close()

    assert json.loads((tmp_path / "state.json").read_text())["today"]["last_rx"] == 4_000
