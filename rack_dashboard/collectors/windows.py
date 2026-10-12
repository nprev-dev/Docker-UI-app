"""What Windows has to be asked in its own way: routes, name servers, ping and the names of its hardware.

The reading of each tool's answer is kept apart from the asking, so it can be tested on any system.
Nothing here depends on the language Windows is set to: answers are matched by their numbers, taken
from the registry, or asked for as JSON.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import struct
import threading
import time
from typing import Any, Callable

from rack_dashboard.collectors.util import run

# --- routes ---------------------------------------------------------------------

# A default route in `route print`: destination and mask both 0.0.0.0, then the gateway, the
# address of the interface it leaves by, and the metric.
_DEFAULT_ROUTE = re.compile(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(.+?)\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+)\s*$", re.MULTILINE)


def _is_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def parse_route(text: str) -> tuple[str | None, str | None]:
    """(address of the interface, gateway) for the preferred default route, or (None, None)."""
    best = None
    for gateway, address, metric in _DEFAULT_ROUTE.findall(text):
        if best is None or int(metric) < best[0]:
            # A route that needs no gateway says so in words, in the system's language.
            best = (int(metric), address, gateway if _is_address(gateway) else None)
    return (best[1], best[2]) if best else (None, None)


def read_route() -> tuple[str | None, str | None]:
    try:
        return parse_route(run(["route", "print", "-4", "0.0.0.0"]))
    except Exception:  # noqa: BLE001 - a missing or failing tool only means "no route known"
        return None, None


# --- name servers ---------------------------------------------------------------


def _split(value: Any) -> list[str]:
    """The registry holds lists of addresses as text separated by commas or spaces, or as a list."""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    return [part for part in re.split(r"[,\s]+", str(value or "")) if _is_address(part)]


def pick_name_servers(adapters: list[dict], address: str | None) -> list[str]:
    """The name servers in use, given each adapter's registry values. The adapter holding
    `address` (the one traffic actually leaves by) comes first; set-by-hand wins over DHCP."""

    def servers(adapter: dict) -> list[str]:
        return _split(adapter.get("NameServer")) or _split(adapter.get("DhcpNameServer"))

    def holds(adapter: dict) -> bool:
        return address is not None and address in _split(adapter.get("IPAddress")) + _split(adapter.get("DhcpIPAddress"))

    found: list[str] = []
    for adapter in sorted(adapters, key=lambda adapter: not holds(adapter)):
        # Windows keeps the settings of adapters that are long gone; one that never had an
        # address of its own says nothing about now.
        if not holds(adapter) and found:
            break
        for server in servers(adapter):
            if server not in found:
                found.append(server)
    return found


def read_adapters() -> list[dict]:
    import winreg  # only exists on Windows

    wanted = ("NameServer", "DhcpNameServer", "IPAddress", "DhcpIPAddress")
    adapters = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces") as root:
        for index in range(winreg.QueryInfoKey(root)[0]):
            values = {}
            with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                for name in wanted:
                    try:
                        values[name] = winreg.QueryValueEx(key, name)[0]
                    except OSError:
                        pass
            adapters.append(values)
    return adapters


def resolvers(local_address: Callable[[], str | None] | None = None) -> list[str]:
    try:
        return pick_name_servers(read_adapters(), local_address() if local_address else None)
    except Exception:  # noqa: BLE001
        return []


# --- ping -----------------------------------------------------------------------

PING_PAYLOAD = b"rack-dashboard--"


class IcmpHandle:
    """Windows gives ordinary programs no ping socket; it offers a service that pings for them."""

    def __init__(self) -> None:
        import ctypes

        self._api = ctypes.WinDLL("iphlpapi", use_last_error=True)
        self._api.IcmpCreateFile.restype = ctypes.c_void_p
        self._api.IcmpSendEcho.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint16,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
        ]
        self._api.IcmpSendEcho.restype = ctypes.c_uint32
        self._api.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
        self._handle = self._api.IcmpCreateFile()
        if self._handle in (None, ctypes.c_void_p(-1).value):
            raise OSError("the system's ping service could not be opened")
        # Room for the reply's own fields, our payload back, and an error message's eight bytes.
        self._buffer = ctypes.create_string_buffer(64 + len(PING_PAYLOAD) + 8)

    def send(self, address: str, timeout_ms: int) -> int | None:
        """The status of one echo: 0 for a reply, a non-zero code otherwise, None for no reply at all."""
        target = struct.unpack("<I", socket.inet_aton(address))[0]
        replies = self._api.IcmpSendEcho(self._handle, target, PING_PAYLOAD, len(PING_PAYLOAD), None, self._buffer, len(self._buffer), timeout_ms)
        if not replies:
            return None
        return struct.unpack_from("<I", self._buffer.raw, 4)[0]

    def close(self) -> None:
        if self._handle:
            self._api.IcmpCloseHandle(self._handle)
            self._handle = None


def echo(handle: Any, host: str, sequence: int, timeout: float) -> float | None:
    """One ping through the system's service; the round trip in milliseconds, or None if lost."""
    address = host if _is_address(host) else socket.gethostbyname(host)
    started = time.perf_counter()
    status = handle.send(address, max(1, int(timeout * 1000)))
    # The service only reports whole milliseconds, so the time is taken here instead.
    return (time.perf_counter() - started) * 1000 if status == 0 else None


# --- hardware names -------------------------------------------------------------

# One PowerShell call for everything: starting PowerShell is what takes the time.
INVENTORY_SCRIPT = (
    "$b=Get-CimInstance Win32_BaseBoard;$i=Get-CimInstance Win32_BIOS;$p=@(Get-CimInstance Win32_Processor);"
    "$m=@(Get-CimInstance Win32_PhysicalMemory);$d=@(Get-PhysicalDisk);"
    "[pscustomobject]@{board=$b.Product;vendor=$b.Manufacturer;bios=$i.SMBIOSBIOSVersion;"
    "bios_date=$(if($i.ReleaseDate){$i.ReleaseDate.ToString('yyyy-MM-dd')});cpu=$p[0].Name;"
    "cores=($p|Measure-Object NumberOfCores -Sum).Sum;threads=($p|Measure-Object NumberOfLogicalProcessors -Sum).Sum;"
    "ram=($m|Measure-Object Capacity -Sum).Sum;"
    "disks=@($d|ForEach-Object{[pscustomobject]@{id=[string]$_.DeviceId;name=$_.FriendlyName;bytes=$_.Size;media=[string]$_.MediaType;bus=[string]$_.BusType}})}"
    "|ConvertTo-Json -Compress -Depth 4"
)

EMPTY_INVENTORY = {"board": None, "vendor": None, "bios": None, "bios_date": None, "cpu": {"model": None, "cores": None, "threads": None}, "ram_bytes": None, "disks": []}


def _text(value: Any) -> str | None:
    return " ".join(str(value).split()) or None if value is not None else None


def _whole(value: Any) -> int | None:
    try:
        return int(value) if value is not None and int(value) > 0 else None
    except (TypeError, ValueError):
        return None


def disk_kind(media: str | None, bus: str | None) -> str:
    if (bus or "").lower() == "nvme":
        return "nvme"
    return {"ssd": "ssd", "hdd": "hdd"}.get((media or "").lower(), "disk")


def parse_inventory(text: str, clean_cpu: Callable[[str], str] = lambda name: name) -> dict:
    """The machine's parts, from the JSON that INVENTORY_SCRIPT prints."""
    data = json.loads(text)
    disks = data.get("disks") or []
    if isinstance(disks, dict):
        # PowerShell writes a list of one as the thing itself.
        disks = [disks]
    model = _text(data.get("cpu"))
    return {
        "board": _text(data.get("board")),
        "vendor": _text(data.get("vendor")),
        "bios": _text(data.get("bios")),
        "bios_date": _text(data.get("bios_date")),
        "cpu": {"model": clean_cpu(model) if model else None, "cores": _whole(data.get("cores")), "threads": _whole(data.get("threads"))},
        "ram_bytes": _whole(data.get("ram")),
        "disks": [
            {"name": f"disk{_text(disk.get('id')) or index}", "bytes": _whole(disk.get("bytes")), "model": _text(disk.get("name")), "kind": disk_kind(disk.get("media"), disk.get("bus"))}
            for index, disk in enumerate(sorted(disks, key=lambda disk: str(disk.get("id"))))
            if _whole(disk.get("bytes"))
        ],
    }


def query_inventory() -> str:
    return run(["powershell", "-NoProfile", "-NonInteractive", "-Command", INVENTORY_SCRIPT], timeout=60)


class Inventory:
    """Asking Windows what it is made of takes several seconds the first time. That happens off to
    the side; until the answer is in, callers get an empty list marked "pending" and ask again."""

    def __init__(self, query: Callable[[], str] = query_inventory, clean_cpu: Callable[[str], str] = lambda name: name,
                 fallback: Callable[[], dict] = lambda: {}):
        self._query = query
        self._clean_cpu = clean_cpu
        self._fallback = fallback
        self._answer: dict | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _work(self) -> None:
        try:
            answer = parse_inventory(self._query(), self._clean_cpu)
        except Exception:  # noqa: BLE001 - PowerShell missing, blocked or slow: do without the names
            answer = dict(EMPTY_INVENTORY)
        with self._lock:
            self._answer = answer

    def read(self) -> dict:
        with self._lock:
            answer = self._answer
            if answer is None and self._thread is None:
                self._thread = threading.Thread(target=self._work, daemon=True, name="inventory")
                self._thread.start()
        basics = self._fallback()
        if answer is None:
            return {**EMPTY_INVENTORY, **basics, "pending": True}
        # What the quick way knows fills any gap the slow way left.
        merged = {**answer, "ram_bytes": answer["ram_bytes"] or basics.get("ram_bytes")}
        cpu = {**(basics.get("cpu") or {}), **{key: value for key, value in answer["cpu"].items() if value is not None}}
        return {**merged, "cpu": {"model": cpu.get("model"), "cores": cpu.get("cores"), "threads": cpu.get("threads")}}
