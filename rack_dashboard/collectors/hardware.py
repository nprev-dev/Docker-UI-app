"""Power and hardware: what the machine is made of, what it draws, how warm it runs."""

from __future__ import annotations

import re
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree

# Wall-power samples kept for the graph, and how many seconds each one covers.
HISTORY = 120
HISTORY_STEP = 5.0
GPU_EVERY = 2.0
# The list of programs on the card takes nvidia-smi five times as long as the readings, and changes rarely.
GPU_APPS_EVERY = 10.0
UPS_EVERY = 30.0
INVENTORY_EVERY = 600.0
SAVE_EVERY = 60.0
HOURS_PER_MONTH = 730
# A longer silence than this means the dashboard was not running; what was drawn meanwhile is unknown.
MAX_GAP = 300.0

# --- The power estimate ---------------------------------------------------------
# Only the processor and the graphics card have power sensors. Everything else is a fixed
# allowance built from typical figures, and the wall figure adds conversion losses on top.
# It is an estimate (expect it to be off by 15-25%); a metering plug is the only way to know.
BOARD_WATTS = 15.0
RAM_WATTS_PER_GB = 0.19
DISK_WATTS = {"nvme": 2.0, "ssd": 1.5, "hdd": 5.0}
FANS_WATTS = 4.0
# The board's regulators lose about a tenth of what the processor draws before it gets there.
VRM_EFFICIENCY = 0.90

CPU_CHIPS = ("k10temp", "zenpower", "coretemp")
CPU_LABELS = ("Tctl", "Tdie", "Package id 0")
# Voltage inputs whose label says what they are. The rest sit behind board-specific
# resistor dividers and would show misleading numbers without that board's scaling table.
KNOWN_RAILS = ("Vcore", "+12V", "+5V", "+3.3V", "AVCC", "3VSB", "Vbat")


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _number(text: str | None) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _natural(path: Path) -> list:
    """Sort key that puts temp2 before temp10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.name)]


# --- inventory ------------------------------------------------------------------


def clean_cpu(model: str) -> str:
    """'AMD Ryzen 7 5800X 8-Core Processor' -> 'AMD Ryzen 7 5800X'."""
    model = re.sub(r"\((R|TM|tm)\)", "", model)
    model = re.sub(r"\s+\d+-Core Processor$|\s+@.*$|\s+Processor$", "", model)
    model = re.sub(r"\bCPU\b", "", model)
    return " ".join(model.split())


def read_cpu(cpuinfo: Path = Path("/proc/cpuinfo")) -> dict:
    text = _read(cpuinfo) or ""
    model = re.search(r"^model name\s*:\s*(.+)$", text, re.M)
    cores = re.search(r"^cpu cores\s*:\s*(\d+)$", text, re.M)
    threads = len(re.findall(r"^processor\s*:", text, re.M))
    return {
        "model": clean_cpu(model.group(1)) if model else None,
        "cores": int(cores.group(1)) if cores else None,
        "threads": threads or None,
    }


def installed_ram(root: Path = Path("/sys/devices/system/memory"), meminfo: Path = Path("/proc/meminfo")) -> int | None:
    """Memory fitted to the board, in bytes. The kernel's own total is lower: it leaves out what firmware keeps."""
    try:
        block = int(_read(root / "block_size_bytes"), 16)
        online = sum(1 for flag in root.glob("memory*/online") if _read(flag) == "1")
        if online:
            return block * online
    except (TypeError, ValueError):
        pass
    total = re.search(r"^MemTotal:\s*(\d+) kB", _read(meminfo) or "", re.M)
    return int(total.group(1)) * 1024 if total else None


def list_disks(root: Path = Path("/sys/block")) -> list[dict]:
    disks = []
    for base in sorted(root.glob("*")):
        # Real drives have a device behind them; loop, ram and device-mapper entries do not.
        if not (base / "device").exists() or _read(base / "removable") == "1":
            continue
        sectors = _number(_read(base / "size"))
        if not sectors:
            continue
        if base.name.startswith("nvme"):
            kind = "nvme"
        else:
            kind = "hdd" if _read(base / "queue" / "rotational") == "1" else "ssd"
        disks.append(
            {"name": base.name, "bytes": int(sectors) * 512, "model": _read(base / "device" / "model"), "kind": kind}
        )
    return disks


def bios_date(american: str | None) -> str | None:
    """'02/25/2022' -> '2022-02-25'."""
    match = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", american or "")
    return f"{match.group(3)}-{match.group(1)}-{match.group(2)}" if match else american


def read_inventory(
    dmi: Path = Path("/sys/class/dmi/id"),
    cpu: Callable[[], dict] = read_cpu,
    ram: Callable[[], int | None] = installed_ram,
    disks: Callable[[], list[dict]] = list_disks,
) -> dict:
    return {
        "board": _read(dmi / "board_name"),
        "vendor": _read(dmi / "board_vendor"),
        "bios": _read(dmi / "bios_version"),
        "bios_date": bios_date(_read(dmi / "bios_date")),
        "cpu": cpu(),
        "ram_bytes": ram(),
        "disks": disks(),
    }


# --- processor power ------------------------------------------------------------


class CpuPower:
    """Watts drawn by the processor: measured when the kernel lets us read its energy counter,
    estimated from how busy it is when not."""

    def __init__(
        self,
        zone: Path = Path("/sys/class/powercap/intel-rapl:0"),
        stat: Path = Path("/proc/stat"),
        idle_watts: float = 25.0,
        max_watts: float = 142.0,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._zone = zone
        self._stat = stat
        self._idle_watts = idle_watts
        self._max_watts = max_watts
        self._monotonic = monotonic
        self._energy: tuple[float, int] | None = None
        self._busy: tuple[int, int] | None = None

    def read(self) -> dict:
        load = self._load()
        measured = self._measured()
        if measured is not None:
            return {"watts": measured, "measured": True, "load": load}
        if self._energy_readable():
            # The counter is readable but this is its first sample; a rate needs two.
            return {"watts": None, "measured": True, "load": load}
        estimate = None if load is None else self._idle_watts + (self._max_watts - self._idle_watts) * load
        return {"watts": estimate, "measured": False, "load": load}

    def _energy_readable(self) -> bool:
        return self._energy is not None

    def _measured(self) -> float | None:
        # Since Linux 5.10 this file is readable by root only, because watching it closely can leak
        # what other programs are computing. If the owner of the machine opens it up, we use it.
        microjoules = _number(_read(self._zone / "energy_uj"))
        now = self._monotonic()
        if microjoules is None:
            self._energy = None
            return None
        before, self._energy = self._energy, (now, int(microjoules))
        if before is None or now <= before[0]:
            return None
        used = microjoules - before[1]
        if used < 0:
            # The counter wrapped around its maximum.
            used += _number(_read(self._zone / "max_energy_range_uj")) or 0
            if used < 0:
                return None
        return used / 1e6 / (now - before[0])

    def _load(self) -> float | None:
        """Share of processor time spent working since the last call, 0..1."""
        line = (_read(self._stat) or "").split("\n", 1)[0].split()
        if len(line) < 6 or line[0] != "cpu":
            return None
        try:
            ticks = [int(value) for value in line[1:9]]
        except ValueError:
            return None
        total = sum(ticks)
        idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
        before, self._busy = self._busy, (total, idle)
        if before is None or total <= before[0]:
            return None
        return max(0.0, min(1.0, 1 - (idle - before[1]) / (total - before[0])))


# --- graphics card --------------------------------------------------------------

GPU_FIELDS = "name,memory.total,memory.used,utilization.gpu,temperature.gpu,fan.speed,power.draw,power.limit"


def query_gpus() -> str:
    command = ["nvidia-smi", f"--query-gpu={GPU_FIELDS}", "--format=csv,noheader,nounits"]
    return subprocess.run(command, capture_output=True, text=True, timeout=5, check=True).stdout


def parse_gpus(text: str) -> list[dict]:
    gpus = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 8:
            continue
        # A card without a given sensor answers "[N/A]"; that becomes None.
        total, used, load, temp, fan, watts, limit = (_number(part) for part in parts[1:])
        gpus.append(
            {
                "name": parts[0],
                "mem_total": None if total is None else int(total * 1024 * 1024),
                "mem_used": None if used is None else int(used * 1024 * 1024),
                "load": load,
                "temp": temp,
                "fan": fan,
                "watts": watts,
                "limit_watts": limit,
            }
        )
    return gpus


def query_gpu_apps() -> str:
    """nvidia-smi's full report. It is the one form that lists every program on a card, desktop ones included."""
    return subprocess.run(["nvidia-smi", "-q", "-x"], capture_output=True, text=True, timeout=5, check=True).stdout


def parse_gpu_apps(text: str) -> dict | None:
    """How many programs hold memory on the cards, and how much between them."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    count, sizes = 0, []
    for process in root.iter("process_info"):
        count += 1
        # "175 MiB", or "N/A" where the driver will not say (inside some containers).
        size = _number(((process.findtext("used_memory") or "").split() or [None])[0])
        if size is not None:
            sizes.append(size)
    return {"count": count, "mem": int(sum(sizes) * 1024 * 1024) if sizes or not count else None}


def _failure(exc: Exception) -> str:
    """Why nvidia-smi gave no answer, in its own words where it has any."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return "nvidia-smi did not answer in time"
    said = f"{getattr(exc, 'stdout', None) or ''}\n{getattr(exc, 'stderr', None) or ''}".strip().splitlines()
    return said[0].strip() if said else str(exc)


# --- sensors --------------------------------------------------------------------


def scan_hwmon(root: Path = Path("/sys/class/hwmon")) -> list[dict]:
    """Every temperature, fan and voltage the kernel's sensor drivers expose."""
    chips = []
    for base in sorted(root.glob("hwmon*"), key=_natural):
        name = _read(base / "name")
        if not name:
            continue
        # For a drive the device behind the sensor is its name ("nvme0"); other chips just keep their own.
        device = (base / "device").resolve().name if (base / "device").exists() else name
        chip = {"name": name, "device": device, "temps": [], "fans": [], "volts": []}
        for kind, pattern, scale in (("temps", "temp*_input", 1000), ("fans", "fan*_input", 1), ("volts", "in*_input", 1000)):
            for file in sorted(base.glob(pattern), key=_natural):
                value = _number(_read(file))
                if value is None:
                    continue
                stem = file.name[: -len("_input")]
                reading = {"id": stem, "label": _read(base / f"{stem}_label") or None, "value": value / scale}
                if kind == "temps":
                    crit = _number(_read(base / f"{stem}_crit"))
                    reading["crit"] = None if crit is None else crit / scale
                chip[kind].append(reading)
        chips.append(chip)
    return chips


def _plausible(celsius: float | None) -> bool:
    # Unconnected sensor inputs report nonsense such as -62 or 127 degrees.
    return celsius is not None and -20 < celsius < 125


def pick_temps(chips: list[dict], gpus: list[dict]) -> list[dict]:
    """The few temperatures worth a line each: processor, graphics card, drives, board."""
    cpu, drives, board = [], [], []
    for chip in chips:
        temps = [t for t in chip["temps"] if _plausible(t["value"])]
        if not temps:
            continue
        if chip["name"] in CPU_CHIPS:
            best = next((t for t in temps if t["label"] in CPU_LABELS), temps[0])
            cpu.append({"name": "cpu", "c": best["value"], "warn": 80.0, "crit": 90.0})
        elif chip["name"] == "nvme":
            best = next((t for t in temps if t["label"] == "Composite"), temps[0])
            crit = best["crit"] if _plausible(best.get("crit")) else 85.0
            drives.append({"name": chip["device"], "c": best["value"], "warn": crit - 15, "crit": crit})
        elif chip["name"].startswith("nct"):
            best = next((t for t in temps if t["label"] == "SYSTIN"), None)
            if best:
                board.append({"name": "board", "c": best["value"], "warn": 60.0, "crit": 75.0})
    cards = [
        {"name": "gpu" if len(gpus) == 1 else f"gpu{index}", "c": gpu["temp"], "warn": 80.0, "crit": 90.0}
        for index, gpu in enumerate(gpus)
        if _plausible(gpu["temp"])
    ]
    return [{**t, "c": round(t["c"], 1)} for t in cpu[:1] + cards + drives + board[:1]]


def pick_fans(chips: list[dict], gpus: list[dict]) -> list[dict]:
    fans = [
        {"name": "gpu" if len(gpus) == 1 else f"gpu{index}", "percent": gpu["fan"]}
        for index, gpu in enumerate(gpus)
        if gpu["fan"] is not None
    ]
    for chip in chips:
        for fan in chip["fans"]:
            # A header with nothing plugged in reads zero.
            if fan["value"] > 0:
                fans.append({"name": fan["label"] or fan["id"], "rpm": int(fan["value"])})
    return fans


def pick_volts(chips: list[dict]) -> list[dict]:
    return [
        {"name": volt["label"], "v": round(volt["value"], 3)}
        for chip in chips
        for volt in chip["volts"]
        if volt["label"] in KNOWN_RAILS and 0 < volt["value"] < 20
    ]


def has_board_sensors(chips: list[dict]) -> bool:
    """True once a driver for the board's own fan and voltage chip is loaded."""
    return any(chip["fans"] or chip["volts"] for chip in chips)


# --- UPS and room sensor ----------------------------------------------------------


def run_upower(*arguments: str) -> str:
    return subprocess.run(["upower", *arguments], capture_output=True, text=True, timeout=5, check=True).stdout


def _seconds(text: str | None) -> int | None:
    """'23.4 minutes' -> 1404."""
    match = re.match(r"([\d.]+)\s*(second|minute|hour|day)", text or "")
    if not match:
        return None
    return round(float(match.group(1)) * {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[match.group(2)])


def parse_ups(text: str) -> dict:
    fields = {}
    for line in text.splitlines():
        key, found, value = line.partition(":")
        if found:
            fields.setdefault(key.strip().lower(), value.strip())
    state = fields.get("state")
    return {
        "present": True,
        "model": " ".join(filter(None, [fields.get("vendor"), fields.get("model")])) or "UPS",
        "state": state,
        "on_battery": state == "discharging",
        "percent": _number((fields.get("percentage") or "").rstrip("%")),
        "runtime_s": _seconds(fields.get("time to empty")),
    }


def read_ups(run: Callable[..., str] = run_upower) -> dict:
    """The first UPS the desktop's power service knows about; it finds USB ones by itself."""
    try:
        devices = [line.strip() for line in run("-e").splitlines()]
        path = next((d for d in devices if d.rsplit("/", 1)[-1].startswith("ups_")), None)
        return parse_ups(run("-i", path)) if path else {"present": False}
    except (OSError, subprocess.SubprocessError):
        return {"present": False}


def read_room(path: str | None) -> dict:
    """A temperature from any file holding a number, as 1-wire and USB sensors provide."""
    if not path:
        return {"configured": False, "c": None}
    try:
        value = float((_read(Path(path)) or "").split()[0])
    except (IndexError, ValueError):
        return {"configured": True, "c": None}
    # Kernel sensor files count thousandths of a degree.
    return {"configured": True, "c": round(value / 1000 if abs(value) > 200 else value, 1)}


# --- energy ---------------------------------------------------------------------


def rest_watts(inventory: dict) -> float:
    """The allowance for everything that has no power sensor of its own."""
    ram_gb = (inventory.get("ram_bytes") or 0) / 2**30
    disks = sum(DISK_WATTS.get(disk["kind"], 0) for disk in inventory.get("disks") or [])
    return round(BOARD_WATTS + ram_gb * RAM_WATTS_PER_GB + disks + FANS_WATTS, 1)


class EnergyMeter:
    """Adds up the watt-hours counted today and keeps the total across restarts."""

    def __init__(self, state, clock: Callable[[], float] = time.time):
        self._state = state
        self._clock = clock
        self._record: dict = dict(state.get("energy") or {})
        self._saved = 0.0

    def update(self, watts: float) -> dict:
        now = self._clock()
        local = time.localtime(now)
        today = time.strftime("%Y-%m-%d", local)
        midnight = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
        record = self._record
        gap = now - record.get("seen", 0)
        watched = 0 < gap <= MAX_GAP

        if record.get("date") != today:
            # Only the part of the gap that falls after midnight belongs to the new day.
            seconds = min(gap, now - midnight) if watched else 0
            record = {"date": today, "wh": 0.0, "counted": 0.0}
        else:
            seconds = gap if watched else 0
        record["wh"] += watts * seconds / 3600
        record["counted"] += seconds
        record["seen"] = now
        self._record = record
        if now - self._saved >= SAVE_EVERY:
            self.save()

        # A minute of data is the least that makes an average; until then, the present draw stands in.
        average = record["wh"] / (record["counted"] / 3600) if record["counted"] >= 60 else watts
        return {"wh": round(record["wh"], 2), "counted_s": round(record["counted"]), "avg_w": round(average, 1)}

    def save(self) -> None:
        if self._record:
            self._state.set("energy", self._record)
            self._saved = self._clock()


# --- the collector --------------------------------------------------------------


class Trace:
    """A graph's worth of history: one point per few seconds, the average of what was seen in them."""

    def __init__(self, keep: int = HISTORY, step: float = HISTORY_STEP):
        self._points: deque[int] = deque(maxlen=keep)
        self._step = step
        self._bucket: list[float] = []
        self._bucket_from: float | None = None

    def add(self, now: float, value: float) -> None:
        if self._bucket_from is None:
            self._bucket_from = now
        self._bucket.append(value)
        if now - self._bucket_from >= self._step:
            self._points.append(round(sum(self._bucket) / len(self._bucket)))
            self._bucket, self._bucket_from = [], now

    def points(self) -> list[int]:
        return list(self._points)


class HardwareCollector:
    def __init__(
        self,
        state,
        *,
        price: float | None = None,
        currency: str = "$",
        base_watts: float | None = None,
        psu_efficiency: float = 0.87,
        cpu_idle_watts: float = 25.0,
        cpu_max_watts: float = 142.0,
        room_sensor: str | None = None,
        inventory: Callable[[], dict] = read_inventory,
        cpu_power: CpuPower | None = None,
        gpus: Callable[[], str] = query_gpus,
        gpu_apps: Callable[[], str] = query_gpu_apps,
        hwmon: Callable[[], list[dict]] = scan_hwmon,
        ups: Callable[[], dict] = read_ups,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._price = price
        self._currency = currency
        self._base_watts = base_watts
        self._psu_efficiency = min(1.0, max(0.5, psu_efficiency))
        self._room_sensor = room_sensor
        self._read_inventory = inventory
        self._cpu = cpu_power or CpuPower(idle_watts=cpu_idle_watts, max_watts=cpu_max_watts, monotonic=monotonic)
        self._query_gpus = gpus
        self._query_gpu_apps = gpu_apps
        self._scan_hwmon = hwmon
        self._read_ups = ups
        self._monotonic = monotonic
        self._energy = EnergyMeter(state, clock)

        self._inventory: dict | None = None
        self._inventory_at = 0.0
        self._gpus: list[dict] = []
        self._gpus_at: float | None = None
        self._gpu_error: str | None = None
        self._gpu_apps: dict | None = None
        self._gpu_apps_at: float | None = None
        self._gpu_load = Trace()
        self._ups: dict = {"present": False}
        self._ups_at: float | None = None
        self._wall = Trace()

    def collect(self) -> dict:
        now = self._monotonic()
        if self._inventory is None or now - self._inventory_at >= INVENTORY_EVERY:
            self._inventory, self._inventory_at = self._read_inventory(), now
        if self._gpus_at is None or now - self._gpus_at >= GPU_EVERY:
            self._gpus, self._gpus_at = self._gpu_readings(), now
        if self._gpu_apps_at is None or now - self._gpu_apps_at >= GPU_APPS_EVERY:
            self._gpu_apps, self._gpu_apps_at = self._gpu_programs(), now
        if self._ups_at is None or now - self._ups_at >= UPS_EVERY:
            self._ups, self._ups_at = self._read_ups(), now

        chips = self._scan_hwmon()
        gpus = self._gpus
        inventory = {**self._inventory, "gpus": [{"name": g["name"], "mem_total": g["mem_total"]} for g in gpus]}
        return {
            "ok": True,
            "error": None,
            "inventory": inventory,
            "power": self._power(now, gpus),
            "gpu": self._gpu(now, gpus),
            "temps": pick_temps(chips, gpus),
            "fans": pick_fans(chips, gpus),
            "volts": pick_volts(chips),
            "board_sensors": has_board_sensors(chips),
            "ups": self._ups,
            "room": read_room(self._room_sensor),
        }

    def _gpu_readings(self) -> list[dict]:
        self._gpu_error = None
        try:
            return parse_gpus(self._query_gpus())
        except FileNotFoundError:
            # No NVIDIA tools at all: the machine simply has no card of that make to report on.
            return []
        except (OSError, subprocess.SubprocessError) as exc:
            # The tools are there but the card is not answering: a driver that crashed or was half updated.
            self._gpu_error = _failure(exc)
            return []

    def _gpu_programs(self) -> dict | None:
        if not self._gpus:
            return None
        try:
            return parse_gpu_apps(self._query_gpu_apps())
        except (OSError, subprocess.SubprocessError):
            return None

    def _gpu(self, now: float, gpus: list[dict]) -> dict:
        loads = [g["load"] for g in gpus if g["load"] is not None]
        if loads:
            # With several cards the graph follows whichever is working hardest.
            self._gpu_load.add(now, max(loads))
        return {
            "cards": [{"name": g["name"], "load": g["load"], "mem_used": g["mem_used"], "mem_total": g["mem_total"]} for g in gpus],
            "history": {"step": HISTORY_STEP, "load": self._gpu_load.points()},
            "apps": self._gpu_apps if gpus else None,
            "error": self._gpu_error,
        }

    def _power(self, now: float, gpus: list[dict]) -> dict:
        cpu = self._cpu.read()
        gpu_watts = [g["watts"] for g in gpus if g["watts"] is not None]
        limits = [g["limit_watts"] for g in gpus if g["limit_watts"] is not None]
        rest = self._base_watts if self._base_watts is not None else rest_watts(self._inventory)

        wall = None
        if cpu["watts"] is not None:
            direct = cpu["watts"] / VRM_EFFICIENCY + sum(gpu_watts) + rest
            wall = direct / self._psu_efficiency

        power = {
            "cpu_w": None if cpu["watts"] is None else round(cpu["watts"], 1),
            "cpu_measured": cpu["measured"],
            "cpu_load": None if cpu["load"] is None else round(cpu["load"] * 100, 1),
            "gpu_w": round(sum(gpu_watts), 1) if gpu_watts else None,
            "gpu_limit_w": round(sum(limits), 1) if limits else None,
            "rest_w": rest,
            "wall_w": None if wall is None else round(wall, 1),
            "history": {"step": HISTORY_STEP, "wall": self._wall.points()},
            "today": None,
            "month_kwh": None,
            "month_cost": None,
            "price": self._price,
            "currency": self._currency,
        }
        if wall is None:
            return power

        self._wall.add(now, wall)
        power["history"]["wall"] = self._wall.points()
        today = self._energy.update(wall)
        month_kwh = today["avg_w"] * HOURS_PER_MONTH / 1000
        power["today"] = today
        power["month_kwh"] = round(month_kwh, 1)
        power["month_cost"] = None if self._price is None else round(month_kwh * self._price, 2)
        return power

    def close(self) -> None:
        self._energy.save()
