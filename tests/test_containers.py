"""Edge cases for the container collector, run against a fake Docker daemon."""

from __future__ import annotations

import json

import pytest

from backend.collectors import containers as mod
from backend.collectors.containers import ContainerCollector

GIB = 1024**3
T0 = "2026-10-04T18:00:00.000000000Z"
T1 = "2026-10-04T18:00:01.000000000Z"
T2 = "2026-10-04T18:00:02.000000000Z"


def summary(cid="a" * 64, name="web", state="running", status="Up 2 hours", ports=None, **extra):
    return {
        "Id": cid,
        "Names": [f"/{name}"],
        "Image": "nginx:1.27",
        "State": state,
        "Status": status,
        "Ports": ports or [],
        "Labels": {},
        **extra,
    }


def stats(read=T0, cpu=0, system=0, rx=0, tx=0, usage=200 * 1024**2, inactive=0, cores=4, **extra):
    return {
        "read": read,
        "cpu_stats": {"cpu_usage": {"total_usage": cpu}, "system_cpu_usage": system, "online_cpus": cores},
        "memory_stats": {"usage": usage, "limit": 2 * GIB, "stats": {"inactive_file": inactive}},
        "networks": {"eth0": {"rx_bytes": rx, "tx_bytes": tx}},
        "blkio_stats": {
            "io_service_bytes_recursive": [{"op": "read", "value": 1000}, {"op": "write", "value": 50}]
        },
        "pids_stats": {"current": 7},
        **extra,
    }


class FakeAPI:
    def __init__(self):
        self.summaries = []
        self.stats_by_id = {}
        self.list_error = None
        self.stats_calls = []

    def containers(self, all=False):
        if self.list_error:
            raise self.list_error
        return self.summaries

    def stats(self, container_id, stream=False, one_shot=True):
        self.stats_calls.append(container_id)
        result = self.stats_by_id[container_id]
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self):
        self.api = FakeAPI()
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def collector(client):
    return ContainerCollector(client_factory=lambda: client)


def only(result):
    assert result["ok"] is True
    assert len(result["items"]) == 1
    return result["items"][0]


def test_first_sample_has_no_rates_yet(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(cpu=5_000_000_000, system=100_000_000_000, rx=1000, tx=2000)

    item = only(collector.collect())

    assert item["cpu_percent"] is None
    assert item["net_rx_bps"] is None and item["net_tx_bps"] is None
    # Totals and memory need no previous sample.
    assert item["net_rx_bytes"] == 1000 and item["net_tx_bytes"] == 2000
    assert item["mem_used"] == 200 * 1024**2


def test_second_sample_matches_docker_stats_maths(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(T0, cpu=1_000_000_000, system=100_000_000_000, rx=0, tx=0)
    collector.collect()
    # One second later: half a core used out of four, 1 MB in, 250 kB out.
    client.api.stats_by_id[cid] = stats(
        T1, cpu=1_500_000_000, system=104_000_000_000, rx=1_000_000, tx=250_000
    )

    item = only(collector.collect())

    assert item["cpu_percent"] == pytest.approx(50.0)
    assert item["cpu_count"] == 4
    assert item["net_rx_bps"] == pytest.approx(8_000_000)
    assert item["net_tx_bps"] == pytest.approx(2_000_000)
    assert item["blk_read_bytes"] == 1000 and item["blk_write_bytes"] == 50
    assert item["pids"] == 7


def test_rate_uses_real_time_between_samples(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(T0, rx=0)
    collector.collect()
    client.api.stats_by_id[cid] = stats(T2, rx=1_000_000)

    # Two seconds apart, so half the rate a one-second gap would give.
    assert only(collector.collect())["net_rx_bps"] == pytest.approx(4_000_000)


def test_memory_subtracts_file_cache_like_docker_stats(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(usage=1 * GIB, inactive=GIB // 4)

    item = only(collector.collect())

    assert item["mem_used"] == GIB - GIB // 4
    assert item["mem_limit"] == 2 * GIB
    assert item["mem_percent"] == pytest.approx(37.5)


def test_memory_on_cgroup_v1_uses_total_inactive_file(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(
        memory_stats={"usage": 1000, "limit": 4000, "stats": {"total_inactive_file": 400, "inactive_file": 1}}
    )

    assert only(collector.collect())["mem_used"] == 600


def test_memory_without_detail_or_limit(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(memory_stats={"usage": 1000})

    item = only(collector.collect())

    assert item["mem_used"] == 1000
    assert item["mem_limit"] is None and item["mem_percent"] is None


def test_empty_memory_block_does_not_crash(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(memory_stats={})

    item = only(collector.collect())

    assert item["mem_used"] is None and item["mem_percent"] is None


def test_stopped_container_is_listed_without_asking_for_stats(client, collector):
    cid = "b" * 64
    client.api.summaries = [summary(cid, name="old", state="exited", status="Exited (0) 3 days ago")]

    item = only(collector.collect())

    assert client.api.stats_calls == []
    assert item["state"] == "exited"
    assert item["cpu_percent"] is None and item["mem_used"] is None and item["net_rx_bps"] is None


def test_paused_container_still_gets_stats(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid, state="paused", status="Up 5 minutes (Paused)")]
    client.api.stats_by_id[cid] = stats()

    assert only(collector.collect())["mem_used"] is not None


def test_container_removed_between_list_and_stats(client, collector):
    gone, alive = "a" * 64, "b" * 64
    client.api.summaries = [summary(gone, name="gone"), summary(alive, name="alive")]
    client.api.stats_by_id[gone] = RuntimeError("404 no such container")
    client.api.stats_by_id[alive] = stats()

    result = collector.collect()

    assert result["ok"] is True
    by_name = {item["name"]: item for item in result["items"]}
    assert by_name["gone"]["mem_used"] is None
    assert by_name["alive"]["mem_used"] is not None


def test_restart_resets_counters_without_negative_numbers(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(T0, cpu=9_000_000_000, system=100_000_000_000, rx=5_000_000, tx=5_000_000)
    collector.collect()
    client.api.stats_by_id[cid] = stats(T1, cpu=10_000_000, system=104_000_000_000, rx=100, tx=100)

    item = only(collector.collect())

    assert item["cpu_percent"] is None
    assert item["net_rx_bps"] is None and item["net_tx_bps"] is None

    # The sample after that is back to normal.
    client.api.stats_by_id[cid] = stats(T2, cpu=1_010_000_000, system=108_000_000_000, rx=1100, tx=100)
    item = only(collector.collect())
    assert item["cpu_percent"] == pytest.approx(100.0)
    assert item["net_rx_bps"] == pytest.approx(8000)
    assert item["net_tx_bps"] == 0


def test_host_network_container_has_no_traffic_numbers(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    no_networks = stats()
    del no_networks["networks"]
    client.api.stats_by_id[cid] = no_networks
    collector.collect()

    item = only(collector.collect())

    assert item["net_rx_bps"] is None and item["net_rx_bytes"] is None


def test_several_networks_are_added_together(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(
        networks={"eth0": {"rx_bytes": 100, "tx_bytes": 10}, "eth1": {"rx_bytes": 50, "tx_bytes": 5}}
    )

    item = only(collector.collect())

    assert item["net_rx_bytes"] == 150 and item["net_tx_bytes"] == 15


def test_cpu_falls_back_to_wall_clock_without_system_counter(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    first = stats(T0, cpu=0)
    second = stats(T1, cpu=250_000_000)
    for sample in (first, second):
        del sample["cpu_stats"]["system_cpu_usage"]
    client.api.stats_by_id[cid] = first
    collector.collect()
    client.api.stats_by_id[cid] = second

    assert only(collector.collect())["cpu_percent"] == pytest.approx(25.0)


def test_identical_system_counter_gives_no_cpu_value(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(T0, cpu=100, system=500)
    collector.collect()
    client.api.stats_by_id[cid] = stats(T1, cpu=200, system=500)

    assert only(collector.collect())["cpu_percent"] is None


def test_ports_merge_ipv4_and_ipv6_and_keep_unpublished(client, collector):
    cid = "a" * 64
    client.api.summaries = [
        summary(
            cid,
            ports=[
                {"IP": "0.0.0.0", "PrivatePort": 8096, "PublicPort": 8096, "Type": "tcp"},
                {"IP": "::", "PrivatePort": 8096, "PublicPort": 8096, "Type": "tcp"},
                {"PrivatePort": 7359, "Type": "udp"},
                {"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"},
            ],
        )
    ]
    client.api.stats_by_id[cid] = stats()

    assert only(collector.collect())["ports"] == [
        {"host": 8080, "container": 80, "proto": "tcp"},
        {"host": 8096, "container": 8096, "proto": "tcp"},
        {"host": None, "container": 7359, "proto": "udp"},
    ]


def test_container_without_ports(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid, Ports=None)]
    client.api.stats_by_id[cid] = stats()

    assert only(collector.collect())["ports"] == []


@pytest.mark.parametrize(
    "status, expected",
    [
        ("Up 2 hours (healthy)", "healthy"),
        ("Up 2 hours (unhealthy)", "unhealthy"),
        ("Up 4 seconds (health: starting)", "starting"),
        ("Up 2 hours", None),
        ("Exited (137) 2 minutes ago", None),
    ],
)
def test_health_is_read_from_status(client, collector, status, expected):
    cid = "a" * 64
    client.api.summaries = [summary(cid, status=status)]
    client.api.stats_by_id[cid] = stats()

    assert only(collector.collect())["health"] == expected


def test_no_containers(client, collector):
    result = collector.collect()

    assert result == {"ok": True, "error": None, "total": 0, "states": {}, "items": []}


def test_running_containers_sort_first_then_by_name(client, collector):
    ids = {name: char * 64 for name, char in (("zeta", "1"), ("Alpha", "2"), ("beta", "3"), ("stopped", "4"))}
    client.api.summaries = [
        summary(ids["stopped"], name="stopped", state="exited"),
        summary(ids["zeta"], name="zeta"),
        summary(ids["beta"], name="beta"),
        summary(ids["Alpha"], name="Alpha"),
    ]
    for name in ("zeta", "beta", "Alpha"):
        client.api.stats_by_id[ids[name]] = stats()

    result = collector.collect()

    assert [item["name"] for item in result["items"]] == ["Alpha", "beta", "zeta", "stopped"]
    assert result["total"] == 4
    assert result["states"] == {"running": 3, "exited": 1}


def test_docker_unreachable_then_recovers(client):
    attempts = []

    def factory():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("socket missing")
        return client

    collector = ContainerCollector(client_factory=factory)

    down = collector.collect()
    assert down["ok"] is False
    assert "socket missing" in down["error"]
    assert down["items"] == []

    up = collector.collect()
    assert up["ok"] is True and len(attempts) == 2


def test_error_message_shows_the_root_cause_only():
    def factory():
        try:
            try:
                raise ConnectionRefusedError(111, "Connection refused")
            except OSError as low:
                raise RuntimeError("('Connection aborted.', ConnectionRefusedError(111, ...))") from low
        except RuntimeError as wrapped:
            raise ConnectionError("no Docker daemon answered") from wrapped

    result = ContainerCollector(client_factory=factory).collect()

    assert result["error"] == "Docker is not reachable: Connection refused"


def test_very_long_error_is_cut_short():
    def factory():
        raise RuntimeError("x" * 500)

    error = ContainerCollector(client_factory=factory).collect()["error"]

    assert len(error) < 180 and error.endswith("...")


def test_daemon_dying_mid_run_drops_client_and_stale_counters(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(T0, cpu=1_000, system=1_000_000, rx=1_000_000)
    collector.collect()

    client.api.list_error = OSError("connection reset")
    assert collector.collect()["ok"] is False
    assert client.closed is True

    # Back up: the first sample after an outage must not invent a rate from old counters.
    client.api.list_error = None
    client.api.stats_by_id[cid] = stats(T2, cpu=2_000, system=2_000_000, rx=9_000_000)
    item = only(collector.collect())
    assert item["cpu_percent"] is None and item["net_rx_bps"] is None


def test_counters_of_removed_containers_are_forgotten(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats()
    collector.collect()
    assert cid in collector._previous

    client.api.summaries = []
    collector.collect()

    assert collector._previous == {}


def test_stopping_a_container_forgets_its_counters(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(T0, rx=1_000_000)
    collector.collect()

    client.api.summaries = [summary(cid, state="exited", status="Exited (0) 1 second ago")]
    collector.collect()
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(T2, rx=10)

    assert only(collector.collect())["net_rx_bps"] is None


def test_missing_name_falls_back_to_short_id(client, collector):
    cid = "abcdef123456" + "0" * 52
    client.api.summaries = [summary(cid, Names=None)]
    client.api.stats_by_id[cid] = stats()

    item = only(collector.collect())

    assert item["name"] == "abcdef123456" and item["id"] == "abcdef123456"


def test_untagged_image_digest_is_shortened(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid, Image="sha256:" + "f" * 64)]
    client.api.stats_by_id[cid] = stats()

    assert only(collector.collect())["image"] == "sha256:" + "f" * 12


def test_compose_project_is_exposed(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid, Labels={"com.docker.compose.project": "media"})]
    client.api.stats_by_id[cid] = stats()

    assert only(collector.collect())["project"] == "media"


def test_result_is_plain_json(client, collector):
    cid = "a" * 64
    client.api.summaries = [summary(cid)]
    client.api.stats_by_id[cid] = stats(T0)
    collector.collect()
    client.api.stats_by_id[cid] = stats(T1, cpu=10, system=1000)

    json.dumps(collector.collect(), allow_nan=False)


@pytest.mark.parametrize(
    "stamp, expected",
    [
        ("2026-10-04T18:00:00.123456789Z", 0.123456),
        ("2026-10-04T18:00:00Z", 0.0),
        ("2026-10-04T18:00:00.5Z", 0.5),
    ],
)
def test_docker_timestamps_parse(stamp, expected):
    base = mod._parse_time("2026-10-04T18:00:00Z")
    assert mod._parse_time(stamp) - base == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("stamp", [None, "", "0001-01-01T00:00:00Z", "not a time"])
def test_unusable_timestamps_are_ignored(stamp):
    assert mod._parse_time(stamp) is None


def test_explicit_docker_host_is_the_only_candidate(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/custom.sock")

    assert mod.socket_candidates() == ["unix:///tmp/custom.sock"]


def test_default_candidates_cover_engine_and_desktop(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    candidates = mod.socket_candidates()

    assert candidates[0] == "unix:///var/run/docker.sock"
    assert candidates[1].endswith("/.docker/desktop/docker.sock")
