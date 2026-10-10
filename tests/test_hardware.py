"""Edge cases for the power and hardware collector, run against scripted kernel files and tools."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from backend.collectors import hardware as mod
from backend.collectors.hardware import CpuPower, EnergyMeter, HardwareCollector
from backend.state import StateFile

GIB = 1024**3
MIB = 1024**2


@pytest.fixture
def state(tmp_path):
    return StateFile(tmp_path / "state.json")


def write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


# --- inventory ----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("AMD Ryzen 7 5800X 8-Core Processor", "AMD Ryzen 7 5800X"),
        ("Intel(R) Core(TM) i7-9700K CPU @ 3.60GHz", "Intel Core i7-9700K"),
        ("AMD EPYC 7302P 16-Core Processor", "AMD EPYC 7302P"),
        ("Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz", "Intel Xeon E5-2680 v4"),
        ("  Some   Odd    Chip  ", "Some Odd Chip"),
    ],
)
def test_cpu_names_lose_their_marketing_padding(raw, clean):
    assert mod.clean_cpu(raw) == clean


def test_cpu_is_read_from_cpuinfo(tmp_path):
    block = "processor\t: {n}\nmodel name\t: AMD Ryzen 7 5800X 8-Core Processor\ncpu cores\t: 8\n\n"
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("".join(block.format(n=n) for n in range(16)))

    assert mod.read_cpu(cpuinfo) == {"model": "AMD Ryzen 7 5800X", "cores": 8, "threads": 16}


def test_unreadable_cpuinfo(tmp_path):
    assert mod.read_cpu(tmp_path / "missing") == {"model": None, "cores": None, "threads": None}


def test_installed_ram_counts_memory_blocks(tmp_path):
    files = {"block_size_bytes": "8000000\n"}
    files.update({f"memory{n}/online": "1\n" for n in range(256)})
    files["memory300/online"] = "0\n"

    assert mod.installed_ram(write(tmp_path, files)) == 32 * GIB


def test_installed_ram_falls_back_to_the_kernels_total(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       31717148 kB\nMemFree:  1 kB\n")

    assert mod.installed_ram(tmp_path / "nothing", meminfo) == 31717148 * 1024
    assert mod.installed_ram(tmp_path / "nothing", tmp_path / "missing") is None


def test_disks_are_listed_with_their_kind(tmp_path):
    write(tmp_path, {
        "nvme0n1/device/model": "Samsung SSD 970 EVO Plus 1TB  \n", "nvme0n1/size": "1953525168\n",
        "nvme0n1/queue/rotational": "0\n", "nvme0n1/removable": "0\n",
        "sdb/device/model": "ST2000DM008-2FR1\n", "sdb/size": "3907029168\n", "sdb/queue/rotational": "1\n", "sdb/removable": "0\n",
        "sda/device/model": "CT500MX500SSD1\n", "sda/size": "976773168\n", "sda/queue/rotational": "0\n", "sda/removable": "0\n",
        # None of these are fitted drives: a loop device, a USB stick, an empty card reader.
        "loop0/size": "1000\n",
        "sdc/device/model": "USB Flash\n", "sdc/size": "30000000\n", "sdc/removable": "1\n",
        "sdd/device/model": "Card Reader\n", "sdd/size": "0\n", "sdd/removable": "0\n",
    })

    assert mod.list_disks(tmp_path) == [
        {"name": "nvme0n1", "bytes": 1953525168 * 512, "model": "Samsung SSD 970 EVO Plus 1TB", "kind": "nvme"},
        {"name": "sda", "bytes": 976773168 * 512, "model": "CT500MX500SSD1", "kind": "ssd"},
        {"name": "sdb", "bytes": 3907029168 * 512, "model": "ST2000DM008-2FR1", "kind": "hdd"},
    ]


@pytest.mark.parametrize("raw, expected", [("02/25/2022", "2022-02-25"), ("unknown", "unknown"), (None, None), ("", "")])
def test_bios_date(raw, expected):
    assert mod.bios_date(raw) == expected


def test_inventory_with_nothing_readable(tmp_path):
    inventory = mod.read_inventory(tmp_path, cpu=lambda: mod.read_cpu(tmp_path / "x"), ram=lambda: None, disks=lambda: [])

    assert inventory == {
        "board": None, "vendor": None, "bios": None, "bios_date": None,
        "cpu": {"model": None, "cores": None, "threads": None}, "ram_bytes": None, "disks": [],
    }


# --- processor power ----------------------------------------------------------


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def stat_line(busy, idle):
    # user nice system idle iowait irq softirq steal
    return f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 1 2 3 4\n"


def cpu_power(tmp_path, **over):
    clock = Clock()
    zone = tmp_path / "rapl"
    zone.mkdir(exist_ok=True)
    stat = tmp_path / "stat"
    stat.write_text(stat_line(0, 0))
    return CpuPower(zone=zone, stat=stat, monotonic=clock, **{"idle_watts": 20.0, "max_watts": 120.0, **over}), zone, stat, clock


def test_measured_power_comes_from_the_energy_counter(tmp_path):
    power, zone, stat, clock = cpu_power(tmp_path)
    (zone / "energy_uj").write_text("1000000000\n")

    assert power.read() == {"watts": None, "measured": True, "load": None}
    clock.now += 2
    (zone / "energy_uj").write_text("1090000000\n")

    reading = power.read()
    assert reading["measured"] is True and reading["watts"] == pytest.approx(45.0)


def test_energy_counter_wrapping_around(tmp_path):
    power, zone, stat, clock = cpu_power(tmp_path)
    (zone / "max_energy_range_uj").write_text("65532610987\n")
    (zone / "energy_uj").write_text("65532000000\n")
    power.read()
    clock.now += 1
    (zone / "energy_uj").write_text("29389013\n")

    assert power.read()["watts"] == pytest.approx(30.0)


def test_wrap_without_a_known_maximum_skips_one_reading(tmp_path):
    power, zone, stat, clock = cpu_power(tmp_path)
    (zone / "energy_uj").write_text("9000000\n")
    power.read()
    clock.now += 1
    (zone / "energy_uj").write_text("100\n")

    assert power.read() == {"watts": None, "measured": True, "load": None}


def test_without_the_counter_power_is_estimated_from_load(tmp_path):
    power, zone, stat, clock = cpu_power(tmp_path)

    assert power.read() == {"watts": None, "measured": False, "load": None}
    stat.write_text(stat_line(busy=250, idle=750))

    reading = power.read()
    assert reading["measured"] is False
    assert reading["load"] == pytest.approx(0.25)
    # A quarter of the way from idle (20 W) to flat out (120 W).
    assert reading["watts"] == pytest.approx(45.0)


def test_idle_and_flat_out_estimates(tmp_path):
    power, zone, stat, clock = cpu_power(tmp_path)
    power.read()
    stat.write_text(stat_line(busy=0, idle=1000))
    assert power.read()["watts"] == pytest.approx(20.0)
    stat.write_text(stat_line(busy=1000, idle=1000))
    assert power.read()["watts"] == pytest.approx(120.0)


def test_waiting_on_disk_counts_as_idle(tmp_path):
    power, zone, stat, clock = cpu_power(tmp_path)
    power.read()
    stat.write_text("cpu  0 0 0 500 500 0 0 0 0 0\n")

    assert power.read()["load"] == pytest.approx(0.0)


def test_switches_to_measuring_as_soon_as_the_counter_is_opened_up(tmp_path):
    power, zone, stat, clock = cpu_power(tmp_path)
    power.read()
    stat.write_text(stat_line(100, 900))
    assert power.read()["measured"] is False

    (zone / "energy_uj").write_text("5000000\n")
    assert power.read()["measured"] is True
    clock.now += 1
    (zone / "energy_uj").write_text("38000000\n")
    assert power.read()["watts"] == pytest.approx(33.0)

    # And back to the estimate if it is locked again.
    (zone / "energy_uj").unlink()
    stat.write_text(stat_line(200, 1800))
    assert power.read()["measured"] is False


@pytest.mark.parametrize("text", ["", "garbage\n", "cpu  a b c d e\n", "cpu0 1 2 3 4 5 6 7 8\n"])
def test_unusable_load_figures_give_no_estimate(tmp_path, text):
    power, zone, stat, clock = cpu_power(tmp_path)
    stat.write_text(text)

    assert power.read() == {"watts": None, "measured": False, "load": None}


# --- graphics card ------------------------------------------------------------


def test_gpu_readings_are_parsed():
    gpus = mod.parse_gpus("NVIDIA GeForce RTX 3060, 12288, 911, 14, 46, 0, 42.08, 170.00\n")

    assert gpus == [{
        "name": "NVIDIA GeForce RTX 3060", "mem_total": 12288 * 1024**2, "mem_used": 911 * 1024**2,
        "load": 14.0, "temp": 46.0, "fan": 0.0, "watts": 42.08, "limit_watts": 170.0,
    }]


def test_gpu_fields_the_card_cannot_report_become_none():
    gpus = mod.parse_gpus("Tesla T4, 15360, 0, 0, 40, [N/A], [N/A], 70.00\nA, 1, 1, 1, 1, 1, 1, 1\n")

    assert gpus[0]["fan"] is None and gpus[0]["watts"] is None and gpus[0]["limit_watts"] == 70.0
    assert len(gpus) == 2


@pytest.mark.parametrize("text", ["", "\n", "NVIDIA-SMI has failed because it couldn't communicate\n", "a, b\n"])
def test_unusable_gpu_output(text):
    assert mod.parse_gpus(text) == []


# --- sensors ------------------------------------------------------------------


def sensor_tree(tmp_path, board_chip=False):
    files = {
        "hwmon0/name": "nvme\n",
        "hwmon0/temp1_input": "40850\n", "hwmon0/temp1_label": "Composite\n", "hwmon0/temp1_crit": "84850\n",
        "hwmon0/temp2_input": "45850\n", "hwmon0/temp2_label": "Sensor 1\n", "hwmon0/temp2_crit": "65261850\n",
        "hwmon1/name": "k10temp\n",
        "hwmon1/temp3_input": "41250\n", "hwmon1/temp3_label": "Tccd1\n",
        "hwmon1/temp1_input": "39500\n", "hwmon1/temp1_label": "Tctl\n",
        "hwmon2/name": "asus\n",
        "hwmon3/name": "iwlwifi_1_0\n", "hwmon3/temp1_input": "39000\n",
        "hwmon4/temp1_input": "1\n",
    }
    if board_chip:
        files.update({
            "hwmon10/name": "nct6798\n",
            "hwmon10/temp1_input": "33000\n", "hwmon10/temp1_label": "SYSTIN\n",
            "hwmon10/temp2_input": "127000\n", "hwmon10/temp2_label": "AUXTIN0\n",
            "hwmon10/temp3_input": "-62000\n", "hwmon10/temp3_label": "AUXTIN1\n",
            "hwmon10/fan1_input": "0\n",
            "hwmon10/fan2_input": "1180\n",
            "hwmon10/fan10_input": "742\n", "hwmon10/fan10_label": "pump\n",
            "hwmon10/in0_input": "1104\n", "hwmon10/in0_label": "Vcore\n",
            "hwmon10/in1_input": "1000\n", "hwmon10/in1_label": "in1\n",
            "hwmon10/in3_input": "3344\n", "hwmon10/in3_label": "+3.3V\n",
            "hwmon10/in8_input": "3168\n", "hwmon10/in8_label": "Vbat\n",
            "hwmon10/in9_input": "garbage\n", "hwmon10/in9_label": "AVCC\n",
            "hwmon10/in4_input": "1016\n",
        })
    return write(tmp_path, files)


def test_sensor_files_are_scanned_in_natural_order(tmp_path):
    chips = mod.scan_hwmon(sensor_tree(tmp_path, board_chip=True))

    assert [chip["name"] for chip in chips] == ["nvme", "k10temp", "asus", "iwlwifi_1_0", "nct6798"]
    assert [t["id"] for t in chips[1]["temps"]] == ["temp1", "temp3"]
    assert chips[0]["temps"][0] == {"id": "temp1", "label": "Composite", "value": 40.85, "crit": 84.85}
    assert [f["id"] for f in chips[4]["fans"]] == ["fan1", "fan2", "fan10"]
    # An unreadable value is left out rather than reported as zero.
    assert "in9" not in [v["id"] for v in chips[4]["volts"]]


def test_no_sensors_at_all(tmp_path):
    assert mod.scan_hwmon(tmp_path) == []
    assert mod.pick_temps([], []) == [] and mod.pick_fans([], []) == [] and mod.pick_volts([]) == []
    assert mod.has_board_sensors([]) is False


def test_one_temperature_per_part_in_a_fixed_order(tmp_path):
    chips = mod.scan_hwmon(sensor_tree(tmp_path, board_chip=True))
    gpus = mod.parse_gpus("NVIDIA GeForce RTX 3060, 12288, 911, 14, 46, 0, 42.08, 170.00\n")

    assert mod.pick_temps(chips, gpus) == [
        {"name": "cpu", "c": 39.5, "warn": 80.0, "crit": 90.0},
        {"name": "gpu", "c": 46.0, "warn": 80.0, "crit": 90.0},
        # The drive's own limit is used, not the nonsense one on its second sensor.
        {"name": "nvme", "c": 40.9, "warn": pytest.approx(69.85), "crit": 84.85},
        {"name": "board", "c": 33.0, "warn": 60.0, "crit": 75.0},
    ]


def test_drive_with_a_nonsense_limit_gets_a_default(tmp_path):
    write(tmp_path, {"hwmon0/name": "nvme\n", "hwmon0/temp1_input": "50000\n", "hwmon0/temp1_crit": "65261850\n"})

    assert mod.pick_temps(mod.scan_hwmon(tmp_path), []) == [{"name": "nvme", "c": 50.0, "warn": 70.0, "crit": 85.0}]


def test_unconnected_sensor_inputs_are_ignored(tmp_path):
    write(tmp_path, {"hwmon0/name": "k10temp\n", "hwmon0/temp1_input": "-62000\n", "hwmon0/temp2_input": "127000\n"})

    assert mod.pick_temps(mod.scan_hwmon(tmp_path), []) == []


def test_several_graphics_cards_are_numbered():
    gpus = mod.parse_gpus("A, 1, 1, 1, 60, 30, 100, 200\nB, 1, 1, 1, 70, [N/A], 150, 200\n")

    assert [t["name"] for t in mod.pick_temps([], gpus)] == ["gpu0", "gpu1"]
    assert mod.pick_fans([], gpus) == [{"name": "gpu0", "percent": 30.0}]


def test_fans_skip_empty_headers_and_keep_labels(tmp_path):
    chips = mod.scan_hwmon(sensor_tree(tmp_path, board_chip=True))
    gpus = mod.parse_gpus("NVIDIA GeForce RTX 3060, 12288, 911, 14, 46, 37, 42.08, 170.00\n")

    assert mod.pick_fans(chips, gpus) == [
        {"name": "gpu", "percent": 37.0},
        {"name": "fan2", "rpm": 1180},
        {"name": "pump", "rpm": 742},
    ]


def test_only_voltages_with_a_trustworthy_label_are_shown(tmp_path):
    chips = mod.scan_hwmon(sensor_tree(tmp_path, board_chip=True))

    assert mod.pick_volts(chips) == [{"name": "Vcore", "v": 1.104}, {"name": "+3.3V", "v": 3.344}, {"name": "Vbat", "v": 3.168}]


def test_board_sensor_driver_detection(tmp_path):
    assert mod.has_board_sensors(mod.scan_hwmon(sensor_tree(tmp_path / "without"))) is False
    assert mod.has_board_sensors(mod.scan_hwmon(sensor_tree(tmp_path / "with", board_chip=True))) is True


# --- UPS and room sensor ------------------------------------------------------

UPS_ON_MAINS = """  native-path:          /sys/devices/pci0000:00/usb1/1-2/1-2:1.0/usbmisc/hiddev0
  vendor:               American Power Conversion
  model:                Back-UPS ES 700G
  serial:               5B1234T56789
  power supply:         yes
  updated:              Mon 05 Oct 2026 03:05:15 PM EDT (12 seconds ago)
  has history:          yes
  ups
    present:             yes
    state:               fully-charged
    warning-level:       none
    time to empty:       23.4 minutes
    percentage:          100%
    icon-name:          'battery-full-charged-symbolic'
"""


def test_ups_on_mains_power():
    assert mod.parse_ups(UPS_ON_MAINS) == {
        "present": True, "model": "American Power Conversion Back-UPS ES 700G", "state": "fully-charged",
        "on_battery": False, "percent": 100.0, "runtime_s": 1404,
    }


def test_ups_running_on_battery():
    text = UPS_ON_MAINS.replace("fully-charged", "discharging").replace("100%", "64%").replace("23.4 minutes", "1.2 hours")

    ups = mod.parse_ups(text)

    assert ups["on_battery"] is True and ups["percent"] == 64.0 and ups["runtime_s"] == 4320


def test_ups_reporting_almost_nothing():
    assert mod.parse_ups("  ups\n    present: yes\n") == {
        "present": True, "model": "UPS", "state": None, "on_battery": False, "percent": None, "runtime_s": None,
    }


@pytest.mark.parametrize("text, seconds", [("45 seconds", 45), ("2.5 minutes", 150), ("3 hours", 10800), ("unknown", None), (None, None)])
def test_runtime_units(text, seconds):
    assert mod._seconds(text) == seconds


def test_ups_is_found_among_other_power_devices():
    calls = []

    def run(*arguments):
        calls.append(arguments)
        if arguments == ("-e",):
            return "/org/freedesktop/UPower/devices/line_power_AC\n/org/freedesktop/UPower/devices/ups_hiddev0\n/org/freedesktop/UPower/devices/DisplayDevice\n"
        return UPS_ON_MAINS

    assert mod.read_ups(run)["model"].endswith("Back-UPS ES 700G")
    assert calls == [("-e",), ("-i", "/org/freedesktop/UPower/devices/ups_hiddev0")]


def test_no_ups_attached():
    assert mod.read_ups(lambda *a: "/org/freedesktop/UPower/devices/DisplayDevice\n") == {"present": False}


@pytest.mark.parametrize("error", [FileNotFoundError("upower"), subprocess.TimeoutExpired("upower", 5), subprocess.CalledProcessError(1, "upower")])
def test_power_service_missing_or_failing(error):
    def run(*arguments):
        raise error

    assert mod.read_ups(run) == {"present": False}


def test_room_sensor_slot(tmp_path):
    assert mod.read_room(None) == {"configured": False, "c": None}
    assert mod.read_room("") == {"configured": False, "c": None}

    sensor = tmp_path / "temp"
    sensor.write_text("22.4\n")
    assert mod.read_room(str(sensor)) == {"configured": True, "c": 22.4}
    # Kernel files count thousandths of a degree.
    sensor.write_text("21875\n")
    assert mod.read_room(str(sensor)) == {"configured": True, "c": 21.9}
    sensor.write_text("-3.5 C\n")
    assert mod.read_room(str(sensor)) == {"configured": True, "c": -3.5}


@pytest.mark.parametrize("text", ["", "no number here\n", "\n"])
def test_room_sensor_that_cannot_be_read(tmp_path, text):
    sensor = tmp_path / "temp"
    sensor.write_text(text)

    assert mod.read_room(str(sensor)) == {"configured": True, "c": None}
    assert mod.read_room(str(tmp_path / "missing")) == {"configured": True, "c": None}


# --- energy -------------------------------------------------------------------


def test_allowance_for_parts_without_a_sensor():
    inventory = {"ram_bytes": 32 * GIB, "disks": [{"kind": "nvme"}, {"kind": "hdd"}, {"kind": "tape"}]}

    assert mod.rest_watts(inventory) == pytest.approx(15 + 32 * 0.19 + 2 + 5 + 4, abs=0.05)
    assert mod.rest_watts({}) == pytest.approx(19.0)


def at(hour, minute=0, second=0, day=5):
    return time.mktime((2026, 10, day, hour, minute, second, 0, 0, -1))


def test_energy_adds_up_and_projects_an_average(state):
    clock = Clock(at(10))
    meter = EnergyMeter(state, clock)

    first = meter.update(100.0)
    assert first == {"wh": 0.0, "counted_s": 0, "avg_w": 100.0}
    for _ in range(3600):
        clock.now += 1
        reading = meter.update(100.0)

    assert reading["wh"] == pytest.approx(100.0, abs=0.05)
    assert reading["counted_s"] == 3600 and reading["avg_w"] == pytest.approx(100.0)


def test_average_follows_changing_draw(state):
    clock = Clock(at(10))
    meter = EnergyMeter(state, clock)
    meter.update(50.0)
    for watts in [50.0] * 1800 + [150.0] * 1800:
        clock.now += 1
        reading = meter.update(watts)

    assert reading["avg_w"] == pytest.approx(100.0, abs=0.1)


def test_time_the_dashboard_was_off_is_not_counted(state):
    clock = Clock(at(10))
    meter = EnergyMeter(state, clock)
    meter.update(100.0)
    clock.now += 60
    meter.update(100.0)
    meter.save()

    clock.now += 2 * 3600
    restarted = EnergyMeter(state, clock)
    reading = restarted.update(100.0)

    assert reading["counted_s"] == 60 and reading["wh"] == pytest.approx(100 * 60 / 3600, abs=0.01)


def test_short_restart_is_bridged(state):
    clock = Clock(at(10))
    meter = EnergyMeter(state, clock)
    meter.update(120.0)
    meter.save()
    clock.now += 30

    assert EnergyMeter(state, clock).update(120.0)["counted_s"] == 30


def test_midnight_starts_a_new_day_and_keeps_only_its_own_seconds(state):
    clock = Clock(at(23, 59, 50, day=4))
    meter = EnergyMeter(state, clock)
    meter.update(360.0)
    clock.now = at(0, 0, 5)

    reading = meter.update(360.0)

    # Fifteen seconds passed, but only five of them belong to the new day.
    assert reading["counted_s"] == 5 and reading["wh"] == pytest.approx(0.5)


def test_new_day_after_a_night_off_starts_empty(state):
    clock = Clock(at(18, day=4))
    meter = EnergyMeter(state, clock)
    meter.update(100.0)
    meter.save()
    clock.now = at(8)

    assert EnergyMeter(state, clock).update(100.0) == {"wh": 0.0, "counted_s": 0, "avg_w": 100.0}


def test_energy_is_saved_at_most_once_a_minute(state, tmp_path):
    clock = Clock(at(10))
    meter = EnergyMeter(state, clock)
    meter.update(100.0)
    for _ in range(59):
        clock.now += 1
        meter.update(100.0)
    assert json.loads((tmp_path / "state.json").read_text())["energy"]["counted"] == 0

    clock.now += 1
    meter.update(100.0)
    assert json.loads((tmp_path / "state.json").read_text())["energy"]["counted"] == 60


# --- the collector ------------------------------------------------------------

INVENTORY = {
    "board": "TUF GAMING X570-PLUS (WI-FI)", "vendor": "ASUSTeK COMPUTER INC.", "bios": "4204", "bios_date": "2022-02-25",
    "cpu": {"model": "AMD Ryzen 7 5800X", "cores": 8, "threads": 16}, "ram_bytes": 32 * GIB,
    "disks": [{"name": "nvme0n1", "bytes": 10**12, "model": "Samsung", "kind": "nvme"}, {"name": "sdb", "bytes": 2 * 10**12, "model": "Seagate", "kind": "hdd"}],
}
GPU_LINE = "NVIDIA GeForce RTX 3060, 12288, 911, 14, 46, 0, 40.0, 170.00\n"


def app(pid, kind, name, memory):
    return (f"<process_info><gpu_instance_id>N/A</gpu_instance_id><compute_instance_id>N/A</compute_instance_id><pid>{pid}</pid>"
            f"<type>{kind}</type><process_name>{name}</process_name><used_memory>{memory}</used_memory></process_info>")


def report(*cards):
    """nvidia-smi -q -x, cut down to the parts that are read. Each card is a list of programs."""
    body = "".join(f'<gpu id="00000000:0{n}:00.0"><product_name>RTX</product_name><processes>{"".join(apps)}</processes></gpu>' for n, apps in enumerate(cards))
    return f'<?xml version="1.0" ?>\n<!DOCTYPE nvidia_smi_log SYSTEM "nvsmi_device_v12.dtd">\n<nvidia_smi_log><driver_version>595.99.02</driver_version>{body}</nvidia_smi_log>'


# As seen on the development machine: a desktop session and nothing else on the card.
APPS = report([
    app(8389, "G", "/usr/bin/gnome-shell", "175 MiB"), app(11857, "G", "/usr/bin/gnome-shell", "117 MiB"),
    app(12214, "C+G", "/usr/libexec/gnome-remote-desktop-daem", "185 MiB"), app(13377, "C+G", "/usr/bin/nautilus", "20 MiB"),
    app(14178, "G", "/usr/bin/Xwayland", "2 MiB"),
])


class FakeCpu:
    def __init__(self, watts=27.0, measured=False):
        self.watts, self.measured = watts, measured

    def read(self):
        return {"watts": self.watts, "measured": self.measured, "load": 0.1}


class Rig:
    def __init__(self, state):
        self.state = state
        self.mono = Clock(500.0)
        self.wall = Clock(at(12))
        self.cpu = FakeCpu()
        self.gpu_text = GPU_LINE
        self.gpu_error = None
        self.apps_text = APPS
        self.apps_error = None
        self.calls = {"inventory": 0, "gpus": 0, "apps": 0, "ups": 0}

    def build(self, **over):
        def inventory():
            self.calls["inventory"] += 1
            return INVENTORY

        def gpus():
            self.calls["gpus"] += 1
            if self.gpu_error:
                raise self.gpu_error
            return self.gpu_text

        def apps():
            self.calls["apps"] += 1
            if self.apps_error:
                raise self.apps_error
            return self.apps_text

        def ups():
            self.calls["ups"] += 1
            return {"present": False}

        settings = dict(inventory=inventory, cpu_power=self.cpu, gpus=gpus, gpu_apps=apps, hwmon=lambda: [], ups=ups, clock=self.wall, monotonic=self.mono)
        return HardwareCollector(self.state, **{**settings, **over})

    def tick(self, seconds=1.0):
        self.mono.now += seconds
        self.wall.now += seconds


@pytest.fixture
def rig(state):
    return Rig(state)


def test_wall_estimate_adds_the_parts_and_the_losses(rig):
    power = rig.build().collect()["power"]

    rest = mod.rest_watts(INVENTORY)
    assert power["cpu_w"] == 27.0 and power["gpu_w"] == 40.0 and power["gpu_limit_w"] == 170.0 and power["rest_w"] == rest
    assert power["wall_w"] == pytest.approx((27.0 / 0.9 + 40.0 + rest) / 0.87, abs=0.05)
    assert power["cpu_measured"] is False


def test_allowance_and_efficiency_can_be_calibrated(rig):
    power = rig.build(base_watts=50.0, psu_efficiency=0.9).collect()["power"]

    assert power["rest_w"] == 50.0
    assert power["wall_w"] == pytest.approx((27.0 / 0.9 + 40.0 + 50.0) / 0.9, abs=0.05)


def test_absurd_efficiency_settings_are_reined_in(rig):
    low = rig.build(psu_efficiency=0.0).collect()["power"]["wall_w"]
    high = rig.build(psu_efficiency=5.0).collect()["power"]["wall_w"]

    assert low == pytest.approx(high * 2, rel=0.01)


def test_no_processor_reading_yet_means_no_total(rig):
    rig.cpu.watts = None

    power = rig.build().collect()["power"]

    assert power["wall_w"] is None and power["today"] is None and power["month_kwh"] is None
    assert power["history"]["wall"] == []


def test_machine_without_a_graphics_card_tool(rig):
    for error in (FileNotFoundError("nvidia-smi"), subprocess.CalledProcessError(9, "nvidia-smi"), subprocess.TimeoutExpired("nvidia-smi", 5)):
        rig.gpu_error = error
        result = rig.build().collect()

        assert result["ok"] is True
        assert result["power"]["gpu_w"] is None and result["power"]["gpu_limit_w"] is None
        assert result["power"]["wall_w"] == pytest.approx((27.0 / 0.9 + mod.rest_watts(INVENTORY)) / 0.87, abs=0.05)
        assert result["inventory"]["gpus"] == []


def test_monthly_cost_needs_a_price(rig):
    without = rig.build().collect()["power"]
    priced = rig.build(price=0.10, currency="CA$").collect()["power"]

    assert without["month_cost"] is None and without["price"] is None
    assert priced["month_kwh"] == pytest.approx(priced["wall_w"] * 730 / 1000, abs=0.1)
    assert priced["month_cost"] == pytest.approx(priced["month_kwh"] * 0.10, abs=0.01)
    assert priced["currency"] == "CA$"


def test_graph_gets_one_averaged_point_every_few_seconds(rig):
    collector = rig.build()
    collector.collect()
    for watts in (27.0, 27.0, 27.0, 27.0, 117.0):
        rig.tick()
        rig.cpu.watts = watts
        result = collector.collect()

    history = result["power"]["history"]
    assert history["step"] == mod.HISTORY_STEP and len(history["wall"]) == 1
    low = (27.0 / 0.9 + 40.0 + mod.rest_watts(INVENTORY)) / 0.87
    high = (117.0 / 0.9 + 40.0 + mod.rest_watts(INVENTORY)) / 0.87
    assert history["wall"][0] == round((5 * low + high) / 6)


def test_graph_history_is_capped(rig):
    collector = rig.build()
    for _ in range(int(mod.HISTORY * mod.HISTORY_STEP) + 60):
        collector.collect()
        rig.tick()

    assert len(collector.collect()["power"]["history"]["wall"]) == mod.HISTORY


def test_slow_things_are_not_asked_every_second(rig):
    collector = rig.build()
    for _ in range(31):
        collector.collect()
        rig.tick()

    assert rig.calls["inventory"] == 1
    assert rig.calls["gpus"] == 16
    assert rig.calls["apps"] == 4
    assert rig.calls["ups"] == 2


def test_everything_missing_still_reports(state):
    collector = HardwareCollector(
        state,
        inventory=lambda: {"board": None, "vendor": None, "bios": None, "bios_date": None,
                           "cpu": {"model": None, "cores": None, "threads": None}, "ram_bytes": None, "disks": []},
        cpu_power=FakeCpu(watts=None), gpus=lambda: "", hwmon=lambda: [], ups=lambda: {"present": False},
    )

    result = collector.collect()

    assert result["ok"] is True and result["temps"] == [] and result["fans"] == [] and result["volts"] == []
    assert result["board_sensors"] is False and result["ups"] == {"present": False}
    assert result["room"] == {"configured": False, "c": None}
    json.dumps(result, allow_nan=False)


def test_result_is_plain_json_and_energy_survives_close(rig, tmp_path):
    collector = rig.build(price=0.1)
    collector.collect()
    rig.tick(10)
    result = collector.collect()

    json.dumps(result, allow_nan=False)
    collector.close()
    assert json.loads((tmp_path / "state.json").read_text())["energy"]["counted"] == 10


# --- graphics card section ---------------------------------------------------------


def test_programs_on_the_card_are_counted():
    assert mod.parse_gpu_apps(APPS) == {"count": 5, "mem": (175 + 117 + 185 + 20 + 2) * MIB}


def test_programs_on_several_cards_are_added_up():
    text = report([app(1, "C", "python3", "9000 MiB")], [], [app(2, "G", "Xorg", "30 MiB"), app(3, "C", "python3", "70 MiB")])

    assert mod.parse_gpu_apps(text) == {"count": 3, "mem": 9100 * MIB}


def test_an_empty_card_has_no_programs():
    assert mod.parse_gpu_apps(report([])) == {"count": 0, "mem": 0}
    # Older drivers write the word instead of leaving the list empty.
    assert mod.parse_gpu_apps(report(["None"])) == {"count": 0, "mem": 0}


def test_memory_the_driver_will_not_tell():
    # Inside a container the driver lists programs without their sizes.
    assert mod.parse_gpu_apps(report([app(1, "C", "python3", "N/A")])) == {"count": 1, "mem": None}
    assert mod.parse_gpu_apps(report([app(1, "C", "python3", "N/A"), app(2, "G", "Xorg", "64 MiB")])) == {"count": 2, "mem": 64 * MIB}
    assert mod.parse_gpu_apps(report([app(1, "C", "python3", "")])) == {"count": 1, "mem": None}


@pytest.mark.parametrize("text", ["", "not xml at all", "<nvidia_smi_log><gpu>", "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver."])
def test_unreadable_program_report(text):
    assert mod.parse_gpu_apps(text) is None


def test_a_report_without_cards():
    assert mod.parse_gpu_apps("<nvidia_smi_log><attached_gpus>0</attached_gpus></nvidia_smi_log>") == {"count": 0, "mem": 0}


def test_trace_averages_each_step_and_forgets_the_oldest():
    trace = mod.Trace(keep=3, step=5.0)
    for second, value in enumerate([10, 10, 10, 10, 10, 40, 0, 0, 0, 0, 20, 100]):
        trace.add(float(second), value)
    # Six readings make the first point (the step has to be over), five each after that.
    # The last reading belongs to a point that is not finished yet.
    assert trace.points() == [15, 4]

    for second in range(12, 40):
        trace.add(float(second), 7)
    assert trace.points() == [7, 7, 7]


def test_trace_with_gaps_between_readings():
    trace = mod.Trace(step=5.0)
    trace.add(0.0, 50)
    trace.add(3600.0, 70)
    # A long silence still closes the point it was part of; it does not invent the ones in between.
    assert trace.points() == [60]


def test_card_section(rig):
    result = rig.build().collect()

    assert result["gpu"] == {
        "cards": [{"name": "NVIDIA GeForce RTX 3060", "load": 14.0, "mem_used": 911 * MIB, "mem_total": 12288 * MIB}],
        "history": {"step": mod.HISTORY_STEP, "load": []},
        "apps": {"count": 5, "mem": 499 * MIB},
        "error": None,
    }


def test_card_load_history(rig):
    collector = rig.build()
    rig.gpu_text = GPU_LINE.replace(", 14,", ", 10,")
    for second in range(6):
        if second == 4:
            rig.gpu_text = GPU_LINE.replace(", 14,", ", 70,")
        result = collector.collect()
        rig.tick()

    # The card is read every other second, so each reading counts for two: 10, 10, 10, 10, 70, 70.
    assert result["gpu"]["history"]["load"] == [30]


def test_card_load_history_is_capped(rig):
    collector = rig.build()
    for _ in range(int(mod.HISTORY * mod.HISTORY_STEP) + 60):
        collector.collect()
        rig.tick()

    history = collector.collect()["gpu"]["history"]["load"]
    assert len(history) == mod.HISTORY and set(history) == {14}


def test_graph_follows_the_busiest_of_several_cards(rig):
    rig.gpu_text = "NVIDIA RTX A4000, 16376, 100, 5, 40, 30, 20.0, 140.00\nNVIDIA RTX A4000, 16376, 15000, 93, 71, 80, 131.0, 140.00\n"
    collector = rig.build()
    for _ in range(7):
        result = collector.collect()
        rig.tick()

    assert [card["load"] for card in result["gpu"]["cards"]] == [5.0, 93.0]
    assert result["gpu"]["history"]["load"] == [93]


def test_card_that_cannot_report_its_load(rig):
    rig.gpu_text = "Tesla K80, 11441, [N/A], [N/A], 35, [N/A], 26.0, 149.00\n"
    collector = rig.build()
    for _ in range(12):
        result = collector.collect()
        rig.tick()

    assert result["gpu"]["cards"] == [{"name": "Tesla K80", "load": None, "mem_used": None, "mem_total": 11441 * MIB}]
    assert result["gpu"]["history"]["load"] == []
    assert result["gpu"]["error"] is None


def test_machine_without_a_card(rig):
    rig.gpu_error = FileNotFoundError("nvidia-smi")
    result = rig.build().collect()

    assert result["gpu"] == {"cards": [], "history": {"step": mod.HISTORY_STEP, "load": []}, "apps": None, "error": None}
    # Nothing to list programs of, so the slow report is never asked for.
    assert rig.calls["apps"] == 0


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (subprocess.CalledProcessError(9, "nvidia-smi", output="NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver. Make sure that the latest NVIDIA driver is installed and running.\n\n"),
         "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver. Make sure that the latest NVIDIA driver is installed and running."),
        (subprocess.CalledProcessError(18, "nvidia-smi", output="", stderr="Failed to initialize NVML: Driver/library version mismatch\nNVML library version: 595.99\n"),
         "Failed to initialize NVML: Driver/library version mismatch"),
        (subprocess.CalledProcessError(255, "nvidia-smi"), "Command 'nvidia-smi' returned non-zero exit status 255."),
        (subprocess.TimeoutExpired("nvidia-smi", 5), "nvidia-smi did not answer in time"),
        (PermissionError(13, "Permission denied"), "[Errno 13] Permission denied"),
    ],
)
def test_card_that_stops_answering(rig, error, said):
    collector = rig.build()
    assert collector.collect()["gpu"]["error"] is None

    rig.gpu_error = error
    rig.tick(mod.GPU_EVERY)
    result = collector.collect()

    assert result["gpu"]["cards"] == [] and result["gpu"]["apps"] is None
    assert result["gpu"]["error"] == said
    # The rest of the machine is still reported.
    assert result["ok"] is True and result["power"]["gpu_w"] is None and result["power"]["wall_w"] is not None

    rig.gpu_error = None
    rig.tick(mod.GPU_EVERY)
    result = collector.collect()
    assert result["gpu"]["error"] is None and len(result["gpu"]["cards"]) == 1


def test_history_survives_a_card_that_drops_out(rig):
    collector = rig.build()
    for _ in range(7):
        collector.collect()
        rig.tick()
    rig.gpu_error = subprocess.TimeoutExpired("nvidia-smi", 5)
    for _ in range(20):
        result = collector.collect()
        rig.tick()

    # What was seen stays on the graph; the silence adds nothing to it.
    assert result["gpu"]["history"]["load"] == [14]


@pytest.mark.parametrize("error", [FileNotFoundError("nvidia-smi"), subprocess.CalledProcessError(9, "nvidia-smi"), subprocess.TimeoutExpired("nvidia-smi", 5)])
def test_program_list_failing_does_not_cost_the_readings(rig, error):
    rig.apps_error = error
    result = rig.build().collect()

    assert result["gpu"]["apps"] is None and result["gpu"]["error"] is None
    assert result["gpu"]["cards"][0]["load"] == 14.0


def test_program_list_that_cannot_be_read(rig):
    rig.apps_text = "garbage"

    assert rig.build().collect()["gpu"]["apps"] is None


def test_program_list_is_refreshed(rig):
    collector = rig.build()
    assert collector.collect()["gpu"]["apps"]["count"] == 5

    rig.apps_text = report([app(1, "C", "python3", "9000 MiB")])
    rig.tick(mod.GPU_APPS_EVERY - 1)
    assert collector.collect()["gpu"]["apps"]["count"] == 5
    rig.tick(1)
    assert collector.collect()["gpu"]["apps"] == {"count": 1, "mem": 9000 * MIB}
