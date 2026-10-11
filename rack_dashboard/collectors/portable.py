"""Readers for systems other than Linux, built on psutil, which asks each system in its own way.

They hand back the same shapes as the Linux readers in network.py and hardware.py, with None
wherever a system has no answer.
"""

from __future__ import annotations

import socket
from typing import Any, Callable

_STATES = {"ESTABLISHED": "ESTAB", "TIME_WAIT": "TIME-WAIT", "LISTEN": "LISTEN"}
# Virtual adapters on Windows report the largest number their counter holds instead of a speed:
# 4294 and 1073 megabits are those numbers, not links anyone has.
BOGUS_SPEEDS = (4294, 1073)


def _psutil() -> Any:
    import psutil  # installed only where it is needed; see pyproject.toml

    return psutil


def read_link(name: str, ps: Any = None) -> dict | None:
    """Everything known about one interface, or None if it does not exist."""
    ps = ps or _psutil()
    stats = ps.net_if_stats().get(name)
    if stats is None:
        return None
    counters = ps.net_io_counters(pernic=True).get(name)
    mac = None
    for address in ps.net_if_addrs().get(name, []):
        if address.family == ps.AF_LINK and address.address:
            # Windows writes it with dashes and capitals.
            mac = address.address.replace("-", ":").lower()
    duplex = {ps.NIC_DUPLEX_FULL: "full", ps.NIC_DUPLEX_HALF: "half"}.get(stats.duplex)
    return {
        "name": name,
        "state": "up" if stats.isup else "down",
        "speed": stats.speed if 0 < stats.speed <= 400_000 and stats.speed not in BOGUS_SPEEDS else None,
        "duplex": duplex,
        "mtu": stats.mtu or None,
        "mac": mac,
        # How often the link has dropped is not something these systems count.
        "flaps": None,
        "rx_bytes": counters.bytes_recv if counters else None,
        "tx_bytes": counters.bytes_sent if counters else None,
        "rx_errs": counters.errin if counters else None,
        "tx_errs": counters.errout if counters else None,
        "rx_drop": counters.dropin if counters else None,
        "tx_drop": counters.dropout if counters else None,
    }


def interface_of(address: str | None, ps: Any = None) -> str | None:
    """The name of the interface that holds `address`."""
    if not address:
        return None
    ps = ps or _psutil()
    for name, addresses in ps.net_if_addrs().items():
        if any(entry.address == address for entry in addresses):
            return name
    return None


def read_sockets(make: Callable[..., object], with_processes: bool = False, ps: Any = None) -> list:
    """Every TCP and UDP socket the system lets us see, built by `make` (see network.Sock)."""
    ps = ps or _psutil()
    names: dict[int, str | None] = {}

    def owner(pid: int | None) -> str | None:
        if not with_processes or not pid:
            return None
        if pid not in names:
            try:
                names[pid] = ps.Process(pid).name()
            except Exception:  # noqa: BLE001 - gone already, or not ours to look at
                names[pid] = None
        name = names[pid]
        # Nobody calls a Windows program by its ".exe".
        return name[:-4] if name and name.lower().endswith(".exe") else name

    sockets = []
    for conn in ps.net_connections(kind="inet"):
        proto = "tcp" if conn.type == socket.SOCK_STREAM else "udp"
        local, local_port = (conn.laddr.ip, conn.laddr.port) if conn.laddr else ("*", None)
        peer, peer_port = (conn.raddr.ip, conn.raddr.port) if conn.raddr else ("*", None)
        if proto == "udp":
            state = "UNCONN" if peer_port is None else "ESTAB"
        else:
            state = _STATES.get(conn.status, conn.status.replace("_", "-"))
        # A dual-stack socket reports IPv4 peers as "::ffff:1.2.3.4".
        if peer.startswith("::ffff:") and "." in peer:
            peer = peer[7:]
        sockets.append(make(proto, state, local, local_port, peer, peer_port, None, None, owner(conn.pid)))
    return sockets


def read_boot(ps: Any = None) -> tuple[str, float]:
    """(an id that changes on every boot, the time of this boot)."""
    started = float((ps or _psutil()).boot_time())
    # The moment of the boot is as good an id as any; it is rounded because some systems work it
    # out afresh each time and land a fraction of a second apart.
    return str(round(started / 10)), started


def cpu_ticks(ps: Any = None) -> tuple[float, float]:
    """(all processor time so far, the idle part of it), in seconds."""
    times = (ps or _psutil()).cpu_times()
    return float(sum(times)), float(times.idle) + float(getattr(times, "iowait", 0.0))


def basics(ps: Any = None) -> dict:
    """What can be said about the machine at once, without asking any slow tool."""
    ps = ps or _psutil()
    return {"cpu": {"model": None, "cores": ps.cpu_count(logical=False), "threads": ps.cpu_count()}, "ram_bytes": ps.virtual_memory().total}


def read_battery(ps: Any = None) -> dict:
    """A laptop's battery, in the place a UPS takes on a server: both keep it running when the mains fail."""
    try:
        battery = (ps or _psutil()).sensors_battery()
    except Exception:  # noqa: BLE001
        battery = None
    if battery is None:
        return {"present": False}
    on_battery = not battery.power_plugged
    left = battery.secsleft
    return {
        "present": True,
        "model": "Built-in battery",
        "state": "discharging" if on_battery else "fully-charged" if battery.percent >= 99.5 else "charging",
        "on_battery": on_battery,
        "percent": float(battery.percent),
        # psutil marks "unknown" and "plugged in" with negative numbers.
        "runtime_s": int(left) if on_battery and isinstance(left, (int, float)) and left > 0 else None,
    }
