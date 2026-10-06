"""Reads every Docker container and its live resource use into one plain dict."""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from backend.collectors.util import brief

# Only these states have live counters; asking for stats on the rest is wasted work.
STATS_STATES = {"running", "paused"}
# A hung daemon must not stall the whole dashboard.
API_TIMEOUT = 5

NO_USAGE = {
    "cpu_percent": None,
    "cpu_count": None,
    "mem_used": None,
    "mem_limit": None,
    "mem_percent": None,
    "net_rx_bps": None,
    "net_tx_bps": None,
    "net_rx_bytes": None,
    "net_tx_bytes": None,
    "blk_read_bytes": None,
    "blk_write_bytes": None,
    "pids": None,
}


def socket_candidates() -> list[str]:
    """Places a Docker daemon may be listening."""
    explicit = os.environ.get("DOCKER_HOST")
    if explicit:
        # An explicit choice is final; quietly using another daemon would show the wrong machine.
        return [explicit]
    return [
        "unix:///var/run/docker.sock",
        # Docker Desktop for Linux keeps its socket in the user's home instead.
        f"unix://{Path.home()}/.docker/desktop/docker.sock",
    ]


def connect() -> Any:
    """Return a client for the first daemon that answers a ping."""
    import docker

    last_error: Exception | None = None
    for url in socket_candidates():
        client = None
        try:
            client = docker.DockerClient(base_url=url, timeout=API_TIMEOUT)
            client.ping()
            return client
        except Exception as exc:
            last_error = exc
            if client is not None:
                client.close()
    raise ConnectionError("no Docker daemon answered") from last_error


class ContainerCollector:
    """Turns Docker's raw counters into rates by remembering the previous sample."""

    def __init__(self, client_factory: Callable[[], Any] = connect):
        self._client_factory = client_factory
        self._client: Any = None
        self._previous: dict[str, dict] = {}
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="docker-stats")

    def collect(self) -> dict:
        try:
            if self._client is None:
                self._client = self._client_factory()
            summaries = self._client.api.containers(all=True)
        except Exception as exc:
            self._drop_client()
            return {
                "ok": False,
                "error": f"Docker is not reachable: {brief(exc)}",
                "total": 0,
                "states": {},
                "items": [],
            }

        live_ids = [c["Id"] for c in summaries if c.get("State") in STATS_STATES]
        stats = dict(zip(live_ids, self._pool.map(self._stats, live_ids)))

        items = [self._describe(c, stats.get(c["Id"])) for c in summaries]
        items.sort(key=lambda item: (item["state"] != "running", item["name"].lower()))

        # Forget counters of containers that are gone, so the map cannot grow forever.
        known = {c["Id"] for c in summaries}
        for container_id in set(self._previous) - known:
            del self._previous[container_id]

        states: dict[str, int] = {}
        for item in items:
            states[item["state"]] = states.get(item["state"], 0) + 1
        return {"ok": True, "error": None, "total": len(items), "states": states, "items": items}

    def _stats(self, container_id: str) -> dict | None:
        try:
            return self._client.api.stats(container_id, stream=False, one_shot=True)
        except Exception:
            # Usually the container vanished between listing and asking; show it without numbers.
            return None

    def _drop_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        # Counters from before an outage would produce bogus rates afterwards.
        self._previous.clear()

    def _describe(self, summary: dict, stats: dict | None) -> dict:
        container_id = summary["Id"]
        status = summary.get("Status") or ""
        item = {
            "id": container_id[:12],
            "name": (summary.get("Names") or [container_id[:12]])[0].lstrip("/"),
            "image": _image_name(summary.get("Image") or ""),
            "state": summary.get("State") or "unknown",
            "status": status,
            "health": _health(status),
            "project": (summary.get("Labels") or {}).get("com.docker.compose.project"),
            "ports": _ports(summary.get("Ports") or []),
            **NO_USAGE,
        }
        if stats:
            item.update(self._usage(container_id, stats))
        else:
            self._previous.pop(container_id, None)
        return item

    def _usage(self, container_id: str, stats: dict) -> dict:
        cpu = stats.get("cpu_stats") or {}
        cpu_usage = cpu.get("cpu_usage") or {}
        networks = stats.get("networks")
        memory = stats.get("memory_stats") or {}

        now = {
            "cpu": cpu_usage.get("total_usage"),
            "system": cpu.get("system_cpu_usage"),
            # Host-network containers report no per-container traffic at all.
            "rx": _sum(networks, "rx_bytes") if networks else None,
            "tx": _sum(networks, "tx_bytes") if networks else None,
            "time": _parse_time(stats.get("read")) or time.time(),
        }
        before = self._previous.get(container_id)
        self._previous[container_id] = now

        cores = cpu.get("online_cpus") or len(cpu_usage.get("percpu_usage") or []) or None
        elapsed = now["time"] - before["time"] if before else 0
        mem_used = _memory_used(memory)
        mem_limit = memory.get("limit") or None
        blk_read, blk_write = _block_io(stats.get("blkio_stats") or {})

        return {
            "cpu_percent": _round(_cpu_percent(now, before, cores, elapsed), 2),
            "cpu_count": cores,
            "mem_used": mem_used,
            "mem_limit": mem_limit,
            "mem_percent": _round(mem_used / mem_limit * 100, 2) if mem_used is not None and mem_limit else None,
            "net_rx_bps": _round(_rate(now["rx"], before and before["rx"], elapsed), 0),
            "net_tx_bps": _round(_rate(now["tx"], before and before["tx"], elapsed), 0),
            "net_rx_bytes": now["rx"],
            "net_tx_bytes": now["tx"],
            "blk_read_bytes": blk_read,
            "blk_write_bytes": blk_write,
            "pids": (stats.get("pids_stats") or {}).get("current"),
        }


def _cpu_percent(now: dict, before: dict | None, cores: int | None, elapsed: float) -> float | None:
    """Docker's convention, same as `docker stats`: 100 means one full core."""
    if not before or now["cpu"] is None or before["cpu"] is None:
        return None
    used = now["cpu"] - before["cpu"]
    if used < 0:
        # Counters start over when the container restarts.
        return None
    if now["system"] is not None and before["system"] is not None and cores:
        total = now["system"] - before["system"]
        return used / total * cores * 100 if total > 0 else None
    # No system counter on this platform: fall back to wall-clock time.
    return used / (elapsed * 1e9) * 100 if elapsed > 0 else None


def _rate(now: int | None, before: int | None, elapsed: float) -> float | None:
    """Bits per second between two byte counters."""
    if now is None or before is None or elapsed <= 0 or now < before:
        return None
    return (now - before) * 8 / elapsed


def _memory_used(memory: dict) -> int | None:
    """Usage minus reclaimable file cache, the same number `docker stats` shows."""
    usage = memory.get("usage")
    if usage is None:
        return None
    detail = memory.get("stats") or {}
    # cgroup v1 names the field total_inactive_file, cgroup v2 inactive_file.
    cache = detail.get("total_inactive_file", detail.get("inactive_file", 0)) or 0
    return usage - cache if cache < usage else usage


def _block_io(blkio: dict) -> tuple[int | None, int | None]:
    entries = blkio.get("io_service_bytes_recursive")
    if not entries:
        return None, None
    read = sum(e.get("value", 0) for e in entries if str(e.get("op", "")).lower() == "read")
    write = sum(e.get("value", 0) for e in entries if str(e.get("op", "")).lower() == "write")
    return read, write


def _ports(raw: list[dict]) -> list[dict]:
    """One entry per port; Docker lists the IPv4 and IPv6 binding separately."""
    seen = set()
    for port in raw:
        seen.add((port.get("PublicPort"), port.get("PrivatePort"), port.get("Type") or "tcp"))
    # Published ports first, then by number.
    ordered = sorted(seen, key=lambda p: (p[0] is None, p[0] or 0, p[1] or 0, p[2]))
    return [{"host": host, "container": container, "proto": proto} for host, container, proto in ordered]


def _health(status: str) -> str | None:
    lowered = status.lower()
    if "(unhealthy)" in lowered:
        return "unhealthy"
    if "(healthy)" in lowered:
        return "healthy"
    if "(health: starting)" in lowered:
        return "starting"
    return None


def _image_name(image: str) -> str:
    # An image whose tag was removed shows up as its full 64-character digest.
    return image[:19] if image.startswith("sha256:") else image


_FRACTION = re.compile(r"\.(\d+)")


def _parse_time(stamp: str | None) -> float | None:
    """Docker stamps carry nanoseconds; trim them to the microseconds datetime accepts."""
    if not stamp or stamp.startswith("0001-"):
        return None
    trimmed = _FRACTION.sub(lambda m: "." + m.group(1)[:6], stamp, count=1).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(trimmed).timestamp()
    except ValueError:
        return None


def _sum(networks: dict, field: str) -> int:
    return sum((nic or {}).get(field, 0) for nic in networks.values())


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)
