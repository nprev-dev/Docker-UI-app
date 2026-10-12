"""What macOS has to be asked in its own way: routes, open sockets and the names of its hardware.

The reading of each tool's answer is kept apart from the asking, so it can be tested on any system.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from rack_dashboard.collectors.util import run

# --- routes ---------------------------------------------------------------------


def parse_route(text: str) -> tuple[str | None, str | None]:
    """(interface, gateway) from `route -n get default`, or (None, None)."""
    fields = {}
    for line in text.splitlines():
        key, found, value = line.partition(":")
        if found:
            fields[key.strip()] = value.strip()
    gateway = fields.get("gateway")
    # A route straight onto a link names the link, not an address.
    return fields.get("interface"), gateway if gateway and re.fullmatch(r"[0-9a-fA-F:.]+", gateway) and not gateway.startswith("link") else None


def read_route() -> tuple[str | None, str | None]:
    try:
        return parse_route(run(["route", "-n", "get", "default"]))
    except Exception:  # noqa: BLE001 - no default route makes the tool fail; that is an answer too
        return None, None


# --- sockets --------------------------------------------------------------------

_STATES = {"ESTABLISHED": "ESTAB", "TIME_WAIT": "TIME-WAIT"}
_OWNER = re.compile(r"^(.*):(\d+)$")


def _endpoint(text: str) -> tuple[str, int | None]:
    """"192.168.64.9.49172" -> ("192.168.64.9", 49172); "*.22" -> ("*", 22); "*.*" -> ("*", None)."""
    address, _, port = text.rpartition(".")
    # "fe80::1%lo0" carries the name of an interface we do not need.
    address = address.split("%")[0]
    return address or "*", int(port) if port.isdigit() else None


def parse_netstat(text: str, make: Callable[..., object]) -> list:
    """The sockets in `netstat -anv`, as `make(proto, state, local, local_port, peer, peer_port,
    acked, received, process)` builds them. macOS counts the bytes of every connection, which is
    what tells who the machine is talking to most."""
    sockets = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].startswith(("tcp", "udp")):
            continue
        proto = parts[0][:3]
        local, local_port = _endpoint(parts[3])
        peer, peer_port = _endpoint(parts[4])
        rest = parts[5:]
        # TCP lines name a state next; UDP has none and goes straight on to the byte counts.
        state = None
        if rest and not rest[0].isdigit():
            state = _STATES.get(rest[0], rest[0])
            rest = rest[1:]
        if proto == "udp":
            state = "UNCONN" if peer_port is None else "ESTAB"
        received = int(rest[0]) if len(rest) > 1 and rest[0].isdigit() and rest[1].isdigit() else None
        sent = int(rest[1]) if received is not None else None
        # After two buffer sizes comes "name:pid". A name can have spaces in it.
        process = None
        for index in range(4, len(rest)):
            owner = _OWNER.match(rest[index])
            if owner:
                process = " ".join(rest[4:index] + [owner.group(1)]).strip() or None
                break
        sockets.append(make(proto, state or "UNKNOWN", local, local_port, peer, peer_port, sent, received, process))
    return sockets


def read_sockets(make: Callable[..., object]) -> list:
    # One call per protocol: asked for together, netstat adds sockets of other kinds in between.
    return parse_netstat(run(["netstat", "-anv", "-p", "tcp"]), make) + parse_netstat(run(["netstat", "-anv", "-p", "udp"]), make)


# --- hardware names -------------------------------------------------------------

_SIZE = re.compile(r"^\s*([\d.]+)\s*(GB|TB|MB)\s*$", re.IGNORECASE)


def _bytes(text: object) -> int | None:
    """"16 GB" -> bytes. Apple states memory in binary units."""
    found = _SIZE.match(str(text or ""))
    if not found:
        return None
    return int(float(found.group(1)) * {"MB": 2**20, "GB": 2**30, "TB": 2**40}[found.group(2).upper()])


def parse_hardware(text: str) -> dict:
    """Model and firmware from `system_profiler SPHardwareDataType -json`. Serial numbers and
    identifiers in the same answer are never read."""
    try:
        entry = (json.loads(text).get("SPHardwareDataType") or [{}])[0]
    except (ValueError, AttributeError, IndexError):
        entry = {}
    return {
        "board": entry.get("machine_model"),
        "vendor": "Apple" if entry else None,
        "bios": entry.get("boot_rom_version"),
        "bios_date": None,
        "cpu_model": entry.get("chip_type") or entry.get("cpu_type"),
        "ram_bytes": _bytes(entry.get("physical_memory")),
    }


def parse_disks(text: str) -> list[dict]:
    """The drives in `system_profiler SPNVMeDataType SPSerialATADataType SPStorageDataType -json`."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    disks = []
    for section, kind in (("SPNVMeDataType", "nvme"), ("SPSerialATADataType", None)):
        for controller in data.get(section) or []:
            for item in controller.get("_items") or []:
                size = item.get("size_in_bytes")
                if not isinstance(size, int) or size <= 0:
                    continue
                solid = str(item.get("spsata_medium_type") or "").lower().startswith("solid")
                disks.append({"name": item.get("bsd_name") or f"disk{len(disks)}", "bytes": size, "model": item.get("device_model") or item.get("_name"),
                              "kind": kind or ("ssd" if solid else "hdd")})
    if disks:
        return disks
    # Some Macs (and every virtual one) list no drives of their own; the drive under the system
    # volume is then all there is to go on.
    seen = set()
    for volume in data.get("SPStorageDataType") or []:
        drive = volume.get("physical_drive") or {}
        name = drive.get("device_name") or drive.get("media_name")
        size = volume.get("size_in_bytes")
        if not name or name in seen or drive.get("is_internal_disk") != "yes" or not isinstance(size, int):
            continue
        seen.add(name)
        whole = re.match(r"(disk\d+)", str(volume.get("bsd_name") or ""))
        disks.append({"name": whole.group(1) if whole else f"disk{len(disks)}", "bytes": size, "model": name,
                      "kind": "ssd" if drive.get("medium_type") == "ssd" else "hdd" if drive.get("medium_type") in ("hdd", "rotational") else "disk"})
    return disks


def read_inventory(clean_cpu: Callable[[str], str] = lambda name: name, basics: Callable[[], dict] = lambda: {}) -> dict:
    """The machine's parts. `basics` supplies what can be had without asking a slow tool."""
    known = basics()
    try:
        hardware = parse_hardware(run(["system_profiler", "SPHardwareDataType", "-json"], timeout=30))
    except Exception:  # noqa: BLE001
        hardware = parse_hardware("")
    try:
        disks = parse_disks(run(["system_profiler", "SPNVMeDataType", "SPSerialATADataType", "SPStorageDataType", "-json"], timeout=30))
    except Exception:  # noqa: BLE001
        disks = []
    cpu = known.get("cpu") or {}
    model = hardware["cpu_model"] or cpu.get("model")
    return {
        "board": hardware["board"],
        "vendor": hardware["vendor"],
        "bios": hardware["bios"],
        "bios_date": hardware["bios_date"],
        "cpu": {"model": clean_cpu(model) if model else None, "cores": cpu.get("cores"), "threads": cpu.get("threads")},
        "ram_bytes": hardware["ram_bytes"] or known.get("ram_bytes"),
        "disks": disks,
    }
