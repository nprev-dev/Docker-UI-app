"""Probes that send something onto the network: ping, a DNS query, a public-address lookup, a speed test."""

from __future__ import annotations

import http.client
import ipaddress
import random
import socket
import struct
import threading
import time
import urllib.request
from collections import deque
from typing import Callable

from backend.collectors.util import brief

MB = 1000 * 1000

WAN_URL = "https://1.1.1.1/cdn-cgi/trace"
SPEED_HOST = "speed.cloudflare.com"
# The largest download the speed-test host hands out in one request; more is refused.
SPEED_DOWN_BYTES = 50 * MB
SPEED_UP_CAP = 25 * MB
# Without a name of its own the request is taken for a bot and refused.
HEADERS = {"User-Agent": "rack-dashboard/1.0"}

PING_PAYLOAD = b"rack-dashboard--"


# --- ping ---------------------------------------------------------------------


def echo(sock: socket.socket, host: str, sequence: int, timeout: float) -> float | None:
    """One ICMP echo; the round trip in milliseconds, or None if no reply came in time."""
    sent = time.perf_counter()
    # Type 8 is "echo request". The kernel fills in the checksum and the identifier.
    sock.sendto(struct.pack("!BBHHH", 8, 0, 0, 0, sequence) + PING_PAYLOAD, (host, 0))
    deadline = sent + timeout
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return None
        sock.settimeout(remaining)
        try:
            data, _ = sock.recvfrom(256)
        except socket.timeout:
            return None
        # A late reply to an earlier ping can still arrive here; only our own sequence counts.
        if len(data) >= 8 and data[0] == 0 and struct.unpack("!H", data[6:8])[0] == sequence:
            return (time.perf_counter() - sent) * 1000


def ping_summary(host: str, results: list[float | None], error: str | None = None) -> dict:
    answered = [r for r in results if r is not None]
    return {
        "host": host,
        "last": _round(results[-1]) if results else None,
        "avg": _round(sum(answered) / len(answered)) if answered else None,
        "max": _round(max(answered)) if answered else None,
        "loss": round((len(results) - len(answered)) / len(results) * 100, 1) if results else None,
        "history": [_round(r) for r in results],
        "error": error,
    }


def open_icmp() -> socket.socket:
    # A datagram ICMP socket needs no root, unlike the raw socket ping traditionally used.
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)


class Pinger(threading.Thread):
    """Pings one host once a second and remembers the last two minutes."""

    def __init__(self, host: str, interval: float = 1.0, keep: int = 120,
                 open_socket: Callable[[], socket.socket] = open_icmp):
        super().__init__(daemon=True, name=f"ping-{host}")
        self.host = host
        self.interval = interval
        self.results: deque[float | None] = deque(maxlen=keep)
        self.error: str | None = None
        self._open = open_socket
        self._sock: socket.socket | None = None
        self._sequence = 0
        self._halt = threading.Event()

    def ping_once(self) -> None:
        self._sequence = (self._sequence + 1) % 65536
        try:
            if self._sock is None:
                self._sock = self._open()
            rtt = echo(self._sock, self.host, self._sequence, self.interval)
            self.results.append(rtt)
            self.error = None
            if rtt is None:
                # Every socket is its own ping "flow", told apart on the way by an id the kernel picks.
                # A router or firewall can lose track of one flow and drop its replies from then on
                # while everything else still works (seen on this very network). So after a lost ping,
                # start a new flow: a broken one then costs a single ping instead of looking like an outage.
                self._drop_socket()
        except PermissionError:
            self.error = "ping is not permitted for this user"
        except OSError as exc:
            # No route, cable out, host unreachable: that is a lost ping, not a crash.
            self.results.append(None)
            self.error = brief(exc)
            self._drop_socket()

    def _drop_socket(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def run(self) -> None:
        while not self._halt.is_set():
            started = time.monotonic()
            self.ping_once()
            self._halt.wait(max(0.0, self.interval - (time.monotonic() - started)))
        self._drop_socket()

    def stop(self) -> None:
        self._halt.set()

    def summary(self) -> dict:
        return ping_summary(self.host, list(self.results), self.error)


# --- DNS ----------------------------------------------------------------------


def dns_question(name: str, ident: int) -> bytes:
    """A minimal query for the A record of `name`."""
    labels = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.strip(".").split("."))
    # Flags 0x0100: a standard query, recursion wanted. One question, type A, class IN.
    return struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0) + labels + b"\x00" + struct.pack("!HH", 1, 1)


def dns_answer_ok(data: bytes, ident: int) -> bool:
    """True if `data` is the resolver's error-free reply to our question."""
    if len(data) < 12 or struct.unpack("!H", data[:2])[0] != ident:
        return False
    is_reply = bool(data[2] & 0x80)
    rcode = data[3] & 0x0F
    return is_reply and rcode == 0


def dns_time(server: str, name: str = "example.com", timeout: float = 2.0) -> float:
    """Milliseconds the resolver takes to answer one query; raises if it does not answer properly."""
    ident = random.randrange(65536)
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        started = time.perf_counter()
        sock.sendto(dns_question(name, ident), (server, 53))
        data, _ = sock.recvfrom(512)
        elapsed = (time.perf_counter() - started) * 1000
    if not dns_answer_ok(data, ident):
        raise RuntimeError("resolver answered with an error")
    return round(elapsed, 2)


def resolvers(paths=("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf")) -> list[str]:
    """Name servers this machine is set up to use, real ones before the local stub."""
    found: list[str] = []
    for path in paths:
        try:
            lines = open(path).read().splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver" and parts[1] not in found:
                found.append(parts[1])
    # 127.0.0.53 is systemd's local cache; timing it says nothing about the real resolver.
    return sorted(found, key=lambda server: server.startswith("127."))


# --- public address -----------------------------------------------------------


def parse_wan_ip(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("ip="):
            # Raises ValueError on anything that is not an address.
            return str(ipaddress.ip_address(line[3:].strip()))
    raise RuntimeError("no address in the reply")


def lookup_wan_ip(url: str = WAN_URL, timeout: float = 5.0) -> str:
    """The address the internet sees this network as."""
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_wan_ip(response.read(4096).decode("ascii", "replace"))


class WanTracker:
    """Remembers the public address and when it last changed."""

    def __init__(self, state, lookup: Callable[[], str] = lookup_wan_ip, clock: Callable[[], float] = time.time):
        self._state = state
        self._lookup = lookup
        self._clock = clock

    def check(self) -> dict:
        address = self._lookup()
        record = self._state.get("wan") or {}
        if address != record.get("ip"):
            now = self._clock()
            changes = (record.get("changes") or [])[-19:] + [{"ts": now, "ip": address}]
            record = {"ip": address, "since": now, "previous": record.get("ip"), "changes": changes}
            self._state.set("wan", record)
        return record


# --- speed test ---------------------------------------------------------------


def measure_download(seconds: float = 5.0, size: int = SPEED_DOWN_BYTES, host: str = SPEED_HOST) -> tuple[float, int]:
    """Download for up to `seconds`; returns (bits per second, bytes used)."""
    connection = http.client.HTTPSConnection(host, timeout=10)
    try:
        connection.request("GET", f"/__down?bytes={size}", headers=HEADERS)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"speed test download refused (HTTP {response.status})")
        # The clock starts once data is flowing, so connection setup is not counted.
        started = time.perf_counter()
        total = 0
        while chunk := response.read(256 * 1024):
            total += len(chunk)
            if time.perf_counter() - started >= seconds:
                break
        elapsed = time.perf_counter() - started
    finally:
        connection.close()
    if total == 0 or elapsed <= 0:
        raise RuntimeError("speed test download returned nothing")
    return total * 8 / elapsed, total


def measure_upload(seconds: float = 3.0, cap: int = SPEED_UP_CAP, host: str = SPEED_HOST) -> tuple[float, int]:
    """Upload growing chunks until one takes long enough to time; returns (bits per second, bytes used)."""
    connection = http.client.HTTPSConnection(host, timeout=20)
    used = 0
    size = 1 * MB
    try:
        connection.connect()
        while True:
            started = time.perf_counter()
            connection.request(
                "POST", "/__up", body=bytes(size), headers={**HEADERS, "Content-Type": "application/octet-stream"}
            )
            response = connection.getresponse()
            response.read()
            elapsed = time.perf_counter() - started
            used += size
            if response.status != 200:
                raise RuntimeError(f"speed test upload refused (HTTP {response.status})")
            # A transfer shorter than this is mostly start-up and says little about the line.
            if elapsed >= seconds / 2 or size >= cap:
                break
            size = min(cap, size * 4)
    finally:
        connection.close()
    return size * 8 / elapsed, used


def run_speedtest() -> dict:
    down, down_bytes = measure_download()
    up, up_bytes = measure_upload()
    return {"ts": time.time(), "down_bps": round(down), "up_bps": round(up), "bytes": down_bytes + up_bytes}


class SpeedTester(threading.Thread):
    """Runs a speed test every few hours and keeps the history on disk."""

    KEEP = 40
    FIRST_DELAY = 30
    RETRY_AFTER = 30 * 60

    def __init__(self, state, hours: float, measure: Callable[[], dict] = run_speedtest,
                 clock: Callable[[], float] = time.time):
        super().__init__(daemon=True, name="speedtest")
        self._state = state
        self._measure = measure
        self._clock = clock
        self.interval = hours * 3600
        self.history: list[dict] = list(state.get("speedtests") or [])
        self.running = False
        self.error: str | None = None
        self._not_before = clock() + self.FIRST_DELAY
        self._halt = threading.Event()

    def due_at(self) -> float | None:
        """When the next test should start; None if testing is switched off."""
        if self.interval <= 0:
            return None
        after_last = self.history[-1]["ts"] + self.interval if self.history else 0
        return max(after_last, self._not_before)

    def run_if_due(self) -> bool:
        due = self.due_at()
        if due is None or self._clock() < due:
            return False
        self.running = True
        try:
            self.history = (self.history + [self._measure()])[-self.KEEP :]
            self._state.set("speedtests", self.history)
            self.error = None
        except Exception as exc:
            self.error = brief(exc)
            # Leave the line alone for a while instead of hammering a host that just refused us.
            self._not_before = self._clock() + self.RETRY_AFTER
        finally:
            self.running = False
        return True

    def run(self) -> None:
        while not self._halt.wait(5):
            self.run_if_due()

    def stop(self) -> None:
        self._halt.set()

    def summary(self) -> dict:
        return {
            "enabled": self.interval > 0,
            "running": self.running,
            "last": self.history[-1] if self.history else None,
            "history": self.history[-20:],
            "next": self.due_at(),
            "error": self.error,
        }


# --- scheduling ---------------------------------------------------------------


class Every(threading.Thread):
    """Runs one probe on a timer and keeps its latest answer and its latest error."""

    def __init__(self, name: str, interval: float, probe: Callable[[], object], first_delay: float = 0.0):
        super().__init__(daemon=True, name=name)
        self.interval = interval
        self.first_delay = first_delay
        self._probe = probe
        self.value = None
        self.error: str | None = None
        self.checked: float | None = None
        self._halt = threading.Event()

    def run_once(self) -> None:
        try:
            self.value = self._probe()
            self.error = None
        except Exception as exc:
            self.error = brief(exc)
        self.checked = time.time()

    def run(self) -> None:
        if self._halt.wait(self.first_delay):
            return
        while True:
            self.run_once()
            if self._halt.wait(self.interval):
                return

    def stop(self) -> None:
        self._halt.set()


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)
