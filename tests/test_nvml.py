"""Reading NVIDIA cards through the driver's library, with a stand-in for the library."""

from __future__ import annotations

import ctypes
import shutil

import pytest

from rack_dashboard.collectors import nvml
from rack_dashboard.collectors.nvml import Nvml, NvmlError

GIB = 1024**3


class Card:
    def __init__(self, name="NVIDIA GeForce RTX 3060", total=12 * GIB, used=GIB, load=14, temp=46, fan=30, milliwatts=42080, limit=170000,
                 compute=(), graphics=()):
        self.__dict__.update(locals())


class FakeLibrary:
    """Answers as libnvidia-ml does: a status code, with the values written through the pointers."""

    def __init__(self, cards, init=nvml.SUCCESS, memory_v2=True, process_suffixes=("_v3", "_v2", ""), unsupported=()):
        self.cards = cards
        self.init = init
        self.unsupported = set(unsupported)
        self.calls = []
        self.shutdowns = 0
        if not memory_v2:
            self.nvmlDeviceGetMemoryInfo_v2 = None
        for kind in ("Compute", "Graphics"):
            for suffix in ("_v3", "_v2", ""):
                if suffix not in process_suffixes:
                    setattr(self, f"nvmlDeviceGet{kind}RunningProcesses{suffix}", None)

    def __getattribute__(self, name):
        value = object.__getattribute__(self, name)
        if value is None and name.startswith("nvml"):
            # A call this driver does not have, as ctypes reports it.
            raise AttributeError(name)
        return value

    def nvmlInit_v2(self):
        return self.init

    def nvmlShutdown(self):
        self.shutdowns += 1
        return nvml.SUCCESS

    def nvmlDeviceGetCount_v2(self, count):
        count._obj.value = len(self.cards)
        return nvml.SUCCESS

    def nvmlDeviceGetHandleByIndex_v2(self, index, handle):
        handle._obj.value = index + 1
        return nvml.SUCCESS

    def card(self, handle):
        return self.cards[handle.value - 1]

    def nvmlDeviceGetName(self, handle, buffer, size):
        buffer.value = self.card(handle).name.encode()
        return nvml.SUCCESS

    def _put(self, name, handle, out, value):
        if name in self.unsupported or value is None:
            return nvml.NOT_SUPPORTED
        out._obj.value = value
        return nvml.SUCCESS

    def nvmlDeviceGetMemoryInfo_v2(self, handle, memory):
        assert memory._obj.version == ctypes.sizeof(nvml.MemoryV2) | (2 << 24)
        memory._obj.total, memory._obj.used = self.card(handle).total, self.card(handle).used
        return nvml.SUCCESS

    def nvmlDeviceGetMemoryInfo(self, handle, memory):
        # The first version counts what the driver reserves as used, which is why it comes second.
        memory._obj.total, memory._obj.used = self.card(handle).total, self.card(handle).used + 100
        return nvml.SUCCESS

    def nvmlDeviceGetUtilizationRates(self, handle, rates):
        if "load" in self.unsupported:
            return nvml.NOT_SUPPORTED
        rates._obj.gpu = self.card(handle).load
        return nvml.SUCCESS

    def nvmlDeviceGetTemperature(self, handle, sensor, value):
        assert sensor == nvml.TEMPERATURE_GPU
        return self._put("temp", handle, value, self.card(handle).temp)

    def nvmlDeviceGetFanSpeed(self, handle, value):
        return self._put("fan", handle, value, self.card(handle).fan)

    def nvmlDeviceGetPowerUsage(self, handle, value):
        return self._put("power", handle, value, self.card(handle).milliwatts)

    def nvmlDeviceGetEnforcedPowerLimit(self, handle, value):
        return self._put("limit", handle, value, self.card(handle).limit)

    def _list(self, programs, count, entries):
        self.calls.append((len(programs), count._obj.value, entries is not None))
        if not programs:
            count._obj.value = 0
            return nvml.SUCCESS
        if entries is None or count._obj.value < len(programs):
            count._obj.value = len(programs)
            return nvml.INSUFFICIENT_SIZE
        for index, (pid, size) in enumerate(programs):
            entries[index].pid, entries[index].usedGpuMemory = pid, size
        count._obj.value = len(programs)
        return nvml.SUCCESS

    def nvmlDeviceGetComputeRunningProcesses_v3(self, handle, count, entries):
        assert entries is None or ctypes.sizeof(entries._type_) == ctypes.sizeof(nvml.ProcessV2)
        return self._list(self.card(handle).compute, count, entries)

    nvmlDeviceGetComputeRunningProcesses_v2 = nvmlDeviceGetComputeRunningProcesses_v3

    def nvmlDeviceGetComputeRunningProcesses(self, handle, count, entries):
        assert entries is None or ctypes.sizeof(entries._type_) == ctypes.sizeof(nvml.ProcessV1)
        return self._list(self.card(handle).compute, count, entries)

    def nvmlDeviceGetGraphicsRunningProcesses_v3(self, handle, count, entries):
        return self._list(self.card(handle).graphics, count, entries)

    nvmlDeviceGetGraphicsRunningProcesses_v2 = nvmlDeviceGetGraphicsRunningProcesses_v3
    nvmlDeviceGetGraphicsRunningProcesses = nvmlDeviceGetGraphicsRunningProcesses_v3


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_readings_have_the_shape_nvidia_smi_gives():
    library = FakeLibrary([Card()])

    assert Nvml(lambda: library).readings() == [{
        "name": "NVIDIA GeForce RTX 3060", "mem_total": 12 * GIB, "mem_used": GIB, "load": 14.0, "temp": 46.0, "fan": 30.0, "watts": 42.08, "limit_watts": 170.0,
    }]


def test_what_a_card_cannot_tell_is_none():
    library = FakeLibrary([Card(name="Tesla T4")], unsupported={"fan", "power", "load"})

    reading = Nvml(lambda: library).readings()[0]

    assert reading["fan"] is None and reading["watts"] is None and reading["load"] is None and reading["limit_watts"] == 170.0


def test_older_drivers_give_memory_the_first_way():
    library = FakeLibrary([Card()], memory_v2=False)

    assert Nvml(lambda: library).readings()[0]["mem_used"] == GIB + 100


def test_several_cards_and_none():
    assert [card["name"] for card in Nvml(lambda: FakeLibrary([Card(name="A"), Card(name="B")])).readings()] == ["A", "B"]
    assert Nvml(lambda: FakeLibrary([])).readings() == []


def test_no_library_is_not_tried_again_at_once():
    clock, attempts = Clock(), []

    def missing():
        attempts.append(1)
        raise OSError("libnvidia-ml.so.1: cannot open shared object file")

    reader = Nvml(missing, monotonic=clock)
    for _ in range(3):
        with pytest.raises(NvmlError):
            reader.readings()
    assert len(attempts) == 1

    clock.now += nvml.RETRY_AFTER
    with pytest.raises(NvmlError):
        reader.readings()
    assert len(attempts) == 2


def test_a_driver_that_will_not_start_is_an_error():
    with pytest.raises(NvmlError):
        Nvml(lambda: FakeLibrary([Card()], init=9)).readings()


def test_a_card_that_stops_answering_reopens_the_library_next_time():
    library = FakeLibrary([Card()])
    reader = Nvml(lambda: library)
    reader.readings()

    library.nvmlDeviceGetName = lambda handle, buffer, size: 15  # "the GPU has fallen off the bus"
    with pytest.raises(NvmlError):
        reader.readings()
    assert library.shutdowns == 1

    del library.nvmlDeviceGetName
    assert reader.readings()[0]["name"] == "NVIDIA GeForce RTX 3060"


def test_programs_are_counted_once_and_their_memory_added_up():
    card = Card(compute=[(10, 100), (11, 200)], graphics=[(11, 200), (12, 50)])

    assert Nvml(lambda: FakeLibrary([card])).programs() == {"count": 3, "mem": 350}


def test_programs_on_every_card():
    cards = [Card(compute=[(10, 100)]), Card(graphics=[(20, 30)])]

    assert Nvml(lambda: FakeLibrary(cards)).programs() == {"count": 2, "mem": 130}


def test_memory_the_driver_will_not_tell():
    unknown = Card(graphics=[(1, nvml.UNKNOWN_SIZE)])
    mixed = Card(graphics=[(1, nvml.UNKNOWN_SIZE), (2, 64)])
    misread = Card(total=GIB, compute=[(1, 5 * GIB)])

    assert Nvml(lambda: FakeLibrary([unknown])).programs() == {"count": 1, "mem": None}
    assert Nvml(lambda: FakeLibrary([mixed])).programs() == {"count": 2, "mem": 64}
    assert Nvml(lambda: FakeLibrary([misread])).programs() == {"count": 1, "mem": None}


def test_no_programs():
    assert Nvml(lambda: FakeLibrary([Card()])).programs() == {"count": 0, "mem": 0}


@pytest.mark.parametrize("suffixes", [("_v3", "_v2", ""), ("_v2", ""), ("",)])
def test_whichever_version_of_the_list_the_driver_has(suffixes):
    card = Card(compute=[(10, 100), (11, 200)])

    assert Nvml(lambda: FakeLibrary([card], process_suffixes=suffixes)).programs() == {"count": 2, "mem": 300}


def test_a_driver_with_no_program_list_at_all():
    with pytest.raises(NvmlError):
        Nvml(lambda: FakeLibrary([Card(compute=[(1, 1)])], process_suffixes=())).programs()


def test_where_the_library_is_looked_for(tmp_path):
    with pytest.raises(OSError):
        nvml.load_library("darwin")
    with pytest.raises(OSError):
        nvml.load_library("win32", {"SystemRoot": str(tmp_path), "ProgramFiles": str(tmp_path)})


@pytest.mark.skipif(shutil.which("nvidia-smi") is None, reason="needs an NVIDIA card")
def test_on_a_real_card_it_agrees_with_nvidia_smi():
    from rack_dashboard.collectors import hardware

    library = Nvml().readings()
    tool = hardware.parse_gpus(hardware.query_gpus())

    assert [card["name"] for card in library] == [card["name"] for card in tool]
    for mine, theirs in zip(library, tool):
        assert mine["mem_total"] == theirs["mem_total"] and mine["limit_watts"] == theirs["limit_watts"]
        # nvidia-smi rounds memory to whole MiB; the rest moves from one moment to the next.
        assert abs(mine["mem_used"] - theirs["mem_used"]) < 64 * 1024**2
        assert abs(mine["temp"] - theirs["temp"]) <= 3
    assert Nvml().programs()["count"] == hardware.parse_gpu_apps(hardware.query_gpu_apps())["count"]
