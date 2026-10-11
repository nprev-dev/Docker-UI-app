"""Picks, for the system the dashboard is running on, the readers that know how to ask it."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

NAMES = {"linux": "Linux", "win32": "Windows", "darwin": "macOS"}
# What each system gives no way to read (without extra drivers or administrator rights):
#   talkers  how many bytes each open connection has moved
#   sensors  temperatures, fan speeds and voltages
MISSING = {"linux": [], "win32": ["talkers", "sensors"], "darwin": ["sensors"]}


def name(platform: str = sys.platform) -> str:
    return NAMES.get(platform, platform)


def missing(platform: str = sys.platform) -> list[str]:
    return list(MISSING.get(platform, ["talkers", "sensors"]))


def portable(platform: str = sys.platform, environ: Any = os.environ) -> bool:
    """True where the psutil-based readers are used. DASH_PORTABLE=1 forces them on Linux, which
    is how they get tried out on a machine where the answers can be compared."""
    return platform != "linux" or environ.get("DASH_PORTABLE") == "1"


def remembered(seconds: float, function: Callable[[], Any], clock: Callable[[], float] = time.monotonic) -> Callable[[], Any]:
    """`function`, but asked at most once in `seconds`: for answers that cost a program start."""
    kept: list = []

    def ask() -> Any:
        now = clock()
        if not kept or now - kept[0] >= seconds:
            kept[:] = [now, function()]
        return kept[1]

    return ask


def docker_sockets(platform: str = sys.platform, home: Path | None = None) -> list[str]:
    """Places a Docker daemon may be listening on this kind of system."""
    home = home or Path.home()
    if platform == "win32":
        return ["npipe:////./pipe/docker_engine", "npipe:////./pipe/dockerDesktopLinuxEngine"]
    if platform == "darwin":
        return ["unix:///var/run/docker.sock", f"unix://{home}/.docker/run/docker.sock"]
    return [
        "unix:///var/run/docker.sock",
        # Docker Desktop for Linux keeps its socket in the user's home instead.
        f"unix://{home}/.docker/desktop/docker.sock",
    ]


def network_readers(platform: str = sys.platform, environ: Any = os.environ) -> dict:
    """Arguments for NetworkCollector that replace its Linux readers where those cannot work."""
    if not portable(platform, environ):
        return {}
    from rack_dashboard.collectors import network, portable as readers, probes

    chosen: dict = {"link": readers.read_link, "boot": readers.read_boot, "talkers": "talkers" not in missing(platform)}
    if platform == "win32":
        from rack_dashboard.collectors import windows

        def route() -> tuple[str | None, str | None]:
            address, gateway = windows.read_route()
            return readers.interface_of(address), gateway

        chosen["route"] = remembered(10, route)
        chosen["sockets"] = lambda with_processes=False: readers.read_sockets(network.Sock, with_processes)
        chosen["resolvers"] = remembered(60, lambda: windows.resolvers(lambda: network.local_address("1.1.1.1")))
        chosen["pinger"] = lambda host, interval: probes.Pinger(host, interval=interval, open_socket=windows.IcmpHandle, echo=windows.echo)
    elif platform == "darwin":
        from rack_dashboard.collectors import macos

        chosen["route"] = remembered(10, macos.read_route)
        chosen["sockets"] = lambda with_processes=False: macos.read_sockets(network.Sock)
    else:
        # Linux in try-out mode: psutil for what psutil can do, the usual way for the rest.
        chosen["sockets"] = lambda with_processes=False: readers.read_sockets(network.Sock, with_processes)
    return chosen


def hardware_readers(platform: str = sys.platform, environ: Any = os.environ) -> dict:
    """Arguments for HardwareCollector that replace its Linux readers where those cannot work."""
    if not portable(platform, environ):
        return {}
    from rack_dashboard.collectors import hardware, portable as readers

    chosen: dict = {"cpu_ticks": readers.cpu_ticks}
    if platform == "win32":
        from rack_dashboard.collectors import windows

        chosen["inventory"] = windows.Inventory(clean_cpu=hardware.clean_cpu, fallback=readers.basics).read
    elif platform == "darwin":
        from rack_dashboard.collectors import macos

        chosen["inventory"] = lambda: macos.read_inventory(hardware.clean_cpu, readers.basics)
    if platform != "linux":
        # No sensor chips to scan, and the battery is reached another way.
        chosen["hwmon"] = lambda: []
        chosen["ups"] = readers.read_battery
    return chosen
