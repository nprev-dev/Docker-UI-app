"""This machine's network: link, throughput, sockets, and what the probes say about the path out."""

from __future__ import annotations

import ipaddress
import math
import re
import socket
import struct
import subprocess
import time
from collections import Counter, deque
from pathlib import Path
from typing import Callable, NamedTuple

from backend.collectors import probes

# Samples kept for the throughput graph, one per tick.
HISTORY = 240
TOP_TALKERS = 8
# Below this a host is background chatter, not a talker.
TALKER_FLOOR_BPS = 1000
# Ports from here up are handed out to clients at random; a UDP socket bound there is not a service.
EPHEMERAL_FROM = 32768
# How often to ask which process owns each listening port; that lookup is the slow one.
NAMES_EVERY = 30
SAVE_EVERY = 60

SCOPE_ORDER = {"*": 0, "lan": 1, "ts": 2, "lo": 3}
TAILSCALE = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


# --- reading the system ---------------------------------------------------------


def default_route(text: str) -> tuple[str | None, str | None]:
    """(interface, gateway) of the preferred default route in /proc/net/route, or (None, None)."""
    best = None
    for line in text.splitlines()[1:]:
        fields = line.split()
        try:
            if len(fields) < 8 or int(fields[1], 16) != 0 or int(fields[7], 16) != 0:
                continue
            metric = int(fields[6])
            # Flag 0x2 marks a route that goes through a gateway; the address is stored little-endian.
            via_gateway = int(fields[3], 16) & 0x2
            gateway = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16))) if via_gateway else None
        except ValueError:
            continue
        if best is None or metric < best[0]:
            best = (metric, fields[0], gateway)
    return (best[1], best[2]) if best else (None, None)


def read_route() -> tuple[str | None, str | None]:
    try:
        return default_route(Path("/proc/net/route").read_text())
    except OSError:
        return None, None


def read_link(name: str, root: Path = Path("/sys/class/net")) -> dict | None:
    """Everything the kernel says about one interface, or None if it does not exist."""
    base = root / name
    if not base.is_dir():
        return None

    def text(field: str) -> str | None:
        try:
            return (base / field).read_text().strip()
        except OSError:
            # Speed and duplex refuse to be read while the link is down.
            return None

    def number(field: str) -> int | None:
        try:
            return int(text(field))
        except (TypeError, ValueError):
            return None

    speed = number("speed")
    duplex = text("duplex")
    return {
        "name": name,
        "state": text("operstate") or "unknown",
        "speed": speed if speed and speed > 0 else None,
        "duplex": duplex if duplex in ("full", "half") else None,
        "mtu": number("mtu"),
        "mac": text("address"),
        "flaps": number("carrier_changes"),
        "rx_bytes": number("statistics/rx_bytes"),
        "tx_bytes": number("statistics/tx_bytes"),
        "rx_errs": number("statistics/rx_errors"),
        "tx_errs": number("statistics/tx_errors"),
        "rx_drop": number("statistics/rx_dropped"),
        "tx_drop": number("statistics/tx_dropped"),
    }


def local_address(toward: str) -> str | None:
    """The address this machine would use to reach `toward`. Connecting a UDP socket sends nothing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((toward, 9))
            return sock.getsockname()[0]
    except OSError:
        return None


def read_boot() -> tuple[str, float]:
    """(an id that changes on every boot, the time of this boot)."""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot_id = ""
    boot_time = 0.0
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                boot_time = float(line.split()[1])
    except (OSError, ValueError):
        pass
    return boot_id, boot_time


def list_sockets(with_processes: bool = False) -> str:
    """Every TCP and UDP socket, one per line, as `ss` prints them."""
    flags = "-tulnp" if with_processes else "-tuani"
    return subprocess.run(["ss", "-H", "-O", flags], capture_output=True, text=True, timeout=3, check=True).stdout


# --- sockets --------------------------------------------------------------------


class Sock(NamedTuple):
    proto: str
    state: str
    local: str
    local_port: int | None
    peer: str
    peer_port: int | None
    acked: int | None
    received: int | None
    process: str | None


_PROCESS = re.compile(r'users:\(\("([^"]+)"')
_ACKED = re.compile(r"\bbytes_acked:(\d+)")
_RECEIVED = re.compile(r"\bbytes_received:(\d+)")


def _endpoint(text: str) -> tuple[str, int | None]:
    address, _, port = text.rpartition(":")
    # "[fe80::1%eth0]" and "127.0.0.53%lo" carry brackets and an interface name we do not need.
    address = address.strip("[]").split("%")[0]
    # A dual-stack socket reports IPv4 peers as "::ffff:1.2.3.4".
    if address.startswith("::ffff:") and "." in address:
        address = address[7:]
    return address or "*", int(port) if port.isdigit() else None


def parse_sockets(text: str) -> list[Sock]:
    sockets = []
    for line in text.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 6 or parts[0] not in ("tcp", "udp"):
            continue
        local, local_port = _endpoint(parts[4])
        peer, peer_port = _endpoint(parts[5])
        rest = parts[6] if len(parts) > 6 else ""
        process = _PROCESS.search(rest)
        acked = _ACKED.search(rest)
        received = _RECEIVED.search(rest)
        sockets.append(
            Sock(
                proto=parts[0],
                state=parts[1],
                local=local,
                local_port=local_port,
                peer=peer,
                peer_port=peer_port,
                acked=int(acked.group(1)) if acked else None,
                received=int(received.group(1)) if received else None,
                process=process.group(1) if process else None,
            )
        )
    return sockets


def _is_listener(sock: Sock) -> bool:
    if sock.local_port is None:
        return False
    if sock.proto == "tcp":
        return sock.state == "LISTEN"
    return sock.state == "UNCONN" and sock.local_port < EPHEMERAL_FROM


def _scope(addresses: list[str]) -> str:
    """Who can reach a port: everyone ("*"), the LAN, Tailscale only ("ts"), or this machine only ("lo")."""
    if any(address in ("*", "0.0.0.0", "::") for address in addresses):
        return "*"
    try:
        parsed = [ipaddress.ip_address(address) for address in addresses]
    except ValueError:
        return "lan"
    if all(address.is_loopback for address in parsed):
        return "lo"
    if all(any(address in net for net in TAILSCALE) for address in parsed):
        return "ts"
    return "lan"


# The registered names of a few common services are more cryptic than what everyone calls them.
SERVICE_ALIASES = {"domain": "dns", "ms-wbt-server": "rdp", "ipp": "cups"}


def _service(port: int, proto: str) -> str | None:
    try:
        name = socket.getservbyport(port, proto)
    except (OSError, OverflowError):
        return None
    return SERVICE_ALIASES.get(name, name)


def listener_names(sockets: list[Sock]) -> dict[tuple[int, str], str]:
    return {(s.local_port, s.proto): s.process for s in sockets if s.process and s.local_port is not None}


def listening(sockets: list[Sock], names: dict[tuple[int, str], str]) -> list[dict]:
    """One entry per open port, most exposed first."""
    binds: dict[tuple[int, str], list[str]] = {}
    for sock in sockets:
        if _is_listener(sock):
            binds.setdefault((sock.local_port, sock.proto), []).append(sock.local)
    entries = [
        {
            "port": port,
            "proto": proto,
            "scope": _scope(addresses),
            # The owning process if we may see it, otherwise the service the port number is registered for.
            "who": names.get((port, proto)) or _service(port, proto),
        }
        for (port, proto), addresses in binds.items()
    ]
    entries.sort(key=lambda entry: (SCOPE_ORDER[entry["scope"]], entry["port"], entry["proto"]))
    return entries


def socket_counts(sockets: list[Sock]) -> dict:
    tcp = Counter(sock.state for sock in sockets if sock.proto == "tcp")
    known = tcp["ESTAB"] + tcp["LISTEN"] + tcp["TIME-WAIT"]
    return {
        "tcp_estab": tcp["ESTAB"],
        "tcp_listen": tcp["LISTEN"],
        "tcp_time_wait": tcp["TIME-WAIT"],
        "tcp_other": sum(tcp.values()) - known,
        "udp": sum(1 for sock in sockets if sock.proto == "udp"),
    }


def label_ports(network: dict, containers: dict) -> None:
    """Name a port after the container that publishes it; that says more than Docker's proxy process."""
    published = {}
    for item in containers.get("items") or []:
        if item.get("state") != "running":
            continue
        for port in item.get("ports") or []:
            if port.get("host") is not None:
                published[(port["host"], port.get("proto"))] = item["name"]
    for entry in network.get("listening") or []:
        name = published.get((entry["port"], entry["proto"]))
        if name:
            entry["who"] = name
            entry["container"] = True


class Talkers:
    """Traffic per remote host, worked out from the byte counters the kernel keeps for each TCP connection."""

    def __init__(self, smoothing: float = 4.0, forget: float = 30.0):
        self._smoothing = smoothing
        self._forget = forget
        self._sockets: dict[tuple, tuple[int, int]] = {}
        self._hosts: dict[str, dict] = {}
        self._last: float | None = None
        self._warm = False

    def update(self, sockets: list[Sock], now: float) -> list[dict]:
        elapsed = now - self._last if self._last is not None else 0.0
        fresh: dict[tuple, tuple[int, int]] = {}
        moved: dict[str, list[int]] = {}
        connections: Counter = Counter()

        for sock in sockets:
            if sock.proto != "tcp" or sock.state != "ESTAB" or sock.acked is None or sock.received is None:
                continue
            if _is_loopback(sock.peer):
                continue
            key = (sock.local, sock.local_port, sock.peer, sock.peer_port)
            fresh[key] = (sock.acked, sock.received)
            connections[sock.peer] += 1
            before = self._sockets.get(key)
            if before is None:
                if not self._warm:
                    # Already open when we started watching: no telling when its bytes moved.
                    continue
                before = (0, 0)
            elif sock.acked < before[0] or sock.received < before[1]:
                # Same address pair, smaller counters: a new connection took the old one's place.
                before = (0, 0)
            host = moved.setdefault(sock.peer, [0, 0])
            host[0] += sock.received - before[1]
            host[1] += sock.acked - before[0]

        self._sockets = fresh
        self._warm = True
        self._last = now

        if elapsed > 0:
            # Smoothed, so the list does not reshuffle on every burst.
            weight = 1 - math.exp(-elapsed / self._smoothing)
            for host in set(moved) | set(self._hosts):
                received, sent = moved.get(host, (0, 0))
                entry = self._hosts.setdefault(host, {"rx": 0.0, "tx": 0.0, "seen": now})
                entry["rx"] += (received * 8 / elapsed - entry["rx"]) * weight
                entry["tx"] += (sent * 8 / elapsed - entry["tx"]) * weight
                if received or sent:
                    entry["seen"] = now
            for host in [h for h, entry in self._hosts.items() if now - entry["seen"] > self._forget]:
                del self._hosts[host]

        ranked = sorted(self._hosts.items(), key=lambda item: item[1]["rx"] + item[1]["tx"], reverse=True)
        return [
            {"host": host, "rx_bps": round(entry["rx"]), "tx_bps": round(entry["tx"]), "conns": connections[host]}
            for host, entry in ranked[:TOP_TALKERS]
            if entry["rx"] + entry["tx"] >= TALKER_FLOOR_BPS
        ]


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


# --- totals ---------------------------------------------------------------------


class DayTotals:
    """Bytes moved since local midnight, kept across restarts of the dashboard and of the machine."""

    def __init__(self, state, clock: Callable[[], float] = time.time, boot: Callable[[], tuple[str, float]] = read_boot):
        self._state = state
        self._clock = clock
        self._boot = boot
        self._record: dict = dict(state.get("today") or {})
        self._saved = 0.0

    def update(self, iface: str, rx: int, tx: int) -> dict:
        now = self._clock()
        local = time.localtime(now)
        today = time.strftime("%Y-%m-%d", local)
        midnight = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
        boot_id, boot_time = self._boot()
        record = self._record
        same_counters = (
            record.get("iface") == iface
            and record.get("boot") == boot_id
            and rx >= record.get("last_rx", 0)
            and tx >= record.get("last_tx", 0)
        )

        if record.get("date") != today:
            watched_until_midnight = same_counters and record.get("seen", 0) >= midnight - 120
            if boot_time >= midnight:
                # The machine came up today, so everything it has counted belongs to today.
                record = {"rx": rx, "tx": tx, "since": boot_time}
            elif watched_until_midnight:
                record = {"rx": rx - record["last_rx"], "tx": tx - record["last_tx"], "since": midnight}
            else:
                # Nobody was counting at midnight; start from now and say so.
                record = {"rx": 0, "tx": 0, "since": now}
            record["date"] = today
        elif record.get("iface") != iface:
            # A different interface has different counters: carry the total, restart the baseline.
            pass
        elif same_counters:
            # Also covers any stretch the dashboard was not running during this boot.
            record["rx"] += rx - record["last_rx"]
            record["tx"] += tx - record["last_tx"]
        else:
            # Counters went back to zero: the machine rebooted earlier today.
            record["rx"] += rx
            record["tx"] += tx

        record.update(iface=iface, boot=boot_id, last_rx=rx, last_tx=tx, seen=now)
        self._record = record
        if now - self._saved >= SAVE_EVERY:
            self.save()
        return {"rx": record["rx"], "tx": record["tx"], "since": record["since"], "whole_day": record["since"] <= midnight}

    def save(self) -> None:
        if self._record:
            self._state.set("today", self._record)
            self._saved = self._clock()


# --- the collector --------------------------------------------------------------


class NetworkCollector:
    def __init__(
        self,
        state,
        *,
        iface: str | None = None,
        ping_target: str = "1.1.1.1",
        dns_name: str = "example.com",
        speedtest_hours: float = 6.0,
        probes_enabled: bool = True,
        route: Callable[[], tuple[str | None, str | None]] = read_route,
        link: Callable[[str], dict | None] = read_link,
        sockets: Callable[[bool], str] = list_sockets,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        boot: Callable[[], tuple[str, float]] = read_boot,
    ):
        self._state = state
        self._forced_iface = iface
        self._ping_target = ping_target
        self._dns_name = dns_name
        self._speedtest_hours = speedtest_hours
        self._probes_enabled = probes_enabled
        self._route = route
        self._link = link
        self._sockets = sockets
        self._monotonic = monotonic

        self._rx: deque[int] = deque(maxlen=HISTORY)
        self._tx: deque[int] = deque(maxlen=HISTORY)
        self._before: tuple[str, float, int, int] | None = None
        self._today = DayTotals(state, clock, boot)
        self._talkers = Talkers()
        self._names: dict[tuple[int, str], str] = {}
        self._names_at: float | None = None

        self._pingers: dict[str, probes.Pinger] = {}
        self._dns: probes.Every | None = None
        self._dns_server: str | None = None
        self._wan: probes.Every | None = None
        self._speed: probes.SpeedTester | None = None

    def collect(self) -> dict:
        now = self._monotonic()
        route_iface, gateway = self._route()
        name = self._forced_iface or route_iface
        link = self._link(name) if name else None

        rx_bps, tx_bps = self._rates(link, now)
        result = {
            "ok": link is not None,
            "error": None if link else (f"interface {name} not found" if name else "no default route"),
            "gateway": gateway,
            "link": None,
            "rx_bps": rx_bps,
            "tx_bps": tx_bps,
            "history": {"rx": list(self._rx), "tx": list(self._tx)},
            "totals": None,
        }
        if link:
            result["link"] = {
                key: link[key]
                for key in ("name", "state", "speed", "duplex", "mtu", "mac", "flaps", "rx_errs", "tx_errs", "rx_drop", "tx_drop")
            }
            result["link"]["address"] = local_address(gateway or self._ping_target)
            if link["rx_bytes"] is not None and link["tx_bytes"] is not None:
                result["totals"] = {
                    "boot": {"rx": link["rx_bytes"], "tx": link["tx_bytes"]},
                    "today": self._today.update(link["name"], link["rx_bytes"], link["tx_bytes"]),
                }

        result.update(self._socket_info(now))
        result.update(self._probe_info(gateway))
        return result

    def _rates(self, link: dict | None, now: float) -> tuple[int | None, int | None]:
        before = self._before
        if not link or link["rx_bytes"] is None or link["tx_bytes"] is None:
            self._before = None
            if before:
                # The link vanished: let the graph fall to zero instead of freezing on the last value.
                self._rx.append(0)
                self._tx.append(0)
            return None, None
        self._before = (link["name"], now, link["rx_bytes"], link["tx_bytes"])
        if not before or before[0] != link["name"]:
            return None, None
        elapsed = now - before[1]
        received = link["rx_bytes"] - before[2]
        sent = link["tx_bytes"] - before[3]
        if elapsed <= 0 or received < 0 or sent < 0:
            # Counters restart when the driver reloads; skip that one sample.
            return None, None
        rx_bps, tx_bps = round(received * 8 / elapsed), round(sent * 8 / elapsed)
        self._rx.append(rx_bps)
        self._tx.append(tx_bps)
        return rx_bps, tx_bps

    def _socket_info(self, now: float) -> dict:
        try:
            if self._names_at is None or now - self._names_at >= NAMES_EVERY:
                self._names = listener_names(parse_sockets(self._sockets(True)))
                self._names_at = now
            sockets = parse_sockets(self._sockets(False))
        except (OSError, subprocess.SubprocessError):
            return {"sockets": None, "listening": None, "talkers": None}
        return {
            "sockets": socket_counts(sockets),
            "listening": listening(sockets, self._names),
            "talkers": self._talkers.update(sockets, now),
        }

    # --- probes -----------------------------------------------------------------

    def _probe_info(self, gateway: str | None) -> dict:
        if not self._probes_enabled:
            return {"ping": [], "dns": None, "wan": None, "speedtest": None}
        if self._dns is None:
            self._dns = probes.Every("dns", 5, self._time_dns)
            self._wan = probes.Every("wan", 600, probes.WanTracker(self._state).check, first_delay=1)
            self._speed = probes.SpeedTester(self._state, self._speedtest_hours)
            for thread in (self._dns, self._wan, self._speed):
                thread.start()
        self._point_pinger("gateway", gateway)
        self._point_pinger("internet", self._ping_target)

        dns = self._dns.value or {}
        wan = self._wan.value or self._state.get("wan") or {}
        return {
            "ping": [
                {"name": role, **self._pingers[role].summary()} for role in ("gateway", "internet") if role in self._pingers
            ],
            "dns": {
                "server": self._dns_server,
                "ms": dns.get("ms") if self._dns.error is None else None,
                "ok": self._dns.error is None and self._dns.checked is not None,
                "error": self._dns.error,
            },
            "wan": {
                "ip": wan.get("ip"),
                "since": wan.get("since"),
                "previous": wan.get("previous"),
                "checked": self._wan.checked,
                "error": self._wan.error,
            },
            "speedtest": self._speed.summary(),
        }

    def _point_pinger(self, role: str, host: str | None) -> None:
        current = self._pingers.get(role)
        if current and current.host == host:
            return
        if current:
            # The gateway changed (new network, new route): the old history no longer applies.
            current.stop()
            del self._pingers[role]
        if host:
            self._pingers[role] = probes.Pinger(host)
            self._pingers[role].start()

    def _time_dns(self) -> dict:
        servers = probes.resolvers()
        if not servers:
            self._dns_server = None
            raise RuntimeError("no resolver configured")
        self._dns_server = servers[0]
        return {"ms": probes.dns_time(servers[0], self._dns_name)}

    def close(self) -> None:
        self._today.save()
        for thread in (*self._pingers.values(), self._dns, self._wan, self._speed):
            if thread is not None:
                thread.stop()
