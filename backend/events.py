"""Turns the stream of snapshots into a log of what changed."""

from __future__ import annotations

import time
from collections import deque
from typing import Callable

KEEP = 200
# The same line is not repeated within this many seconds; a flapping link would otherwise fill the log.
REPEAT_AFTER = 60.0


def _rate(bits_per_second: float) -> str:
    for unit, size in (("Gbps", 1e9), ("Mbps", 1e6), ("Kbps", 1e3)):
        if bits_per_second >= size:
            return f"{bits_per_second / size:.0f} {unit}"
    return f"{bits_per_second:.0f} bps"


def _level(temp: dict) -> str:
    return "crit" if temp["c"] >= temp["crit"] else "warn" if temp["c"] >= temp["warn"] else "ok"


class EventLog:
    def __init__(self, state, clock: Callable[[], float] = time.time, own_port: int | None = None):
        self._state = state
        self._clock = clock
        # Our own port comes and goes whenever the dashboard restarts; that is not worth a line.
        self._own_port = own_port
        self._events: deque[dict] = deque(state.get("events") or [], maxlen=KEEP)
        self._previous: dict | None = None
        self._last_said: dict[tuple[str, str], float] = {}
        self._add("SYS", "info", "monitor started")
        self._save()

    def recent(self, count: int = 30) -> list[dict]:
        """The newest events, newest first."""
        return list(self._events)[-count:][::-1]

    def observe(self, snapshot: dict) -> None:
        before, self._previous = self._previous, snapshot
        if before is None:
            # Nothing to compare the first snapshot with; everything in it is simply how things are.
            return
        size = len(self._events)
        last = self._events[-1] if self._events else None
        self._containers(before.get("containers") or {}, snapshot.get("containers") or {})
        self._network(before.get("network") or {}, snapshot.get("network") or {})
        self._hardware(before.get("hardware") or {}, snapshot.get("hardware") or {})
        if len(self._events) != size or (self._events and self._events[-1] is not last):
            self._save()

    def _add(self, tag: str, level: str, text: str) -> None:
        now = self._clock()
        key = (tag, text)
        if now - self._last_said.get(key, float("-inf")) < REPEAT_AFTER:
            return
        self._last_said[key] = now
        self._events.append({"ts": now, "tag": tag, "level": level, "text": text})

    def _save(self) -> None:
        self._state.set("events", list(self._events)[-50:])

    # --- what counts as news ------------------------------------------------------

    def _collector(self, tag: str, name: str, before: dict, after: dict) -> bool:
        """Reports a collector failing or recovering; True if both snapshots are usable."""
        was_ok, is_ok = before.get("ok") is not False, after.get("ok") is not False
        if was_ok and not is_ok:
            self._add(tag, "crit", after.get("error") or f"{name} data unavailable")
        elif is_ok and not was_ok:
            self._add(tag, "info", f"{name} is back")
        return was_ok and is_ok

    def _containers(self, before: dict, after: dict) -> None:
        if not self._collector("DOCK", "docker", before, after):
            return
        old = {item["id"]: item for item in before.get("items") or []}
        new = {item["id"]: item for item in after.get("items") or []}
        for key, item in new.items():
            was = old.get(key)
            if was is None:
                self._add("CTR", "info", f"{item['name']} appeared ({item['state']})")
                continue
            if was["state"] != item["state"]:
                # Leaving the running state is the kind of change someone should notice.
                level = "warn" if was["state"] == "running" else "info"
                self._add("CTR", level, f"{item['name']} {was['state']} -> {item['state']}")
            if was.get("health") != item.get("health") and item.get("health"):
                level = "crit" if item["health"] == "unhealthy" else "info"
                self._add("CTR", level, f"{item['name']} is {item['health']}")
        for key, item in old.items():
            if key not in new:
                self._add("CTR", "warn", f"{item['name']} removed")

    def _network(self, before: dict, after: dict) -> None:
        if not self._collector("NET", "network", before, after):
            return
        was_link, link = before.get("link") or {}, after.get("link") or {}
        if was_link.get("state") != link.get("state") and link.get("state") and was_link.get("state"):
            level = "info" if link["state"] == "up" else "crit"
            self._add("NET", level, f"{link.get('name', 'link')} link {link['state']}")
        if before.get("gateway") != after.get("gateway") and before.get("gateway") and after.get("gateway"):
            self._add("NET", "warn", f"gateway now {after['gateway']}")

        if before.get("listening") is not None and after.get("listening") is not None:
            ours = (self._own_port, "tcp")
            old = {(p["port"], p["proto"]): p for p in before["listening"] if (p["port"], p["proto"]) != ours}
            new = {(p["port"], p["proto"]): p for p in after["listening"] if (p["port"], p["proto"]) != ours}
            for key, port in new.items():
                if key not in old:
                    # A port the whole network can reach deserves more attention than a local one.
                    level = "warn" if port["scope"] in ("*", "lan") else "info"
                    owner = f" by {port['who']}" if port.get("who") else ""
                    self._add("PORT", level, f"{key[0]}/{key[1]} opened{owner} ({port['scope']})")
            for key in old:
                if key not in new:
                    self._add("PORT", "info", f"{key[0]}/{key[1]} closed")

        old_pings = {p["name"]: p for p in before.get("ping") or []}
        for ping in after.get("ping") or []:
            was = old_pings.get(ping["name"])
            if not was or not was.get("history") or not ping.get("history"):
                continue
            if was.get("last") is not None and ping.get("last") is None:
                self._add("PING", "warn", f"{ping['name']} {ping['host']} not answering")
            elif was.get("last") is None and ping.get("last") is not None:
                self._add("PING", "info", f"{ping['name']} answering again ({ping['last']:.0f} ms)")

        was_dns, dns = before.get("dns") or {}, after.get("dns") or {}
        # "Not ok" without an error only means the resolver has not been asked yet.
        if dns.get("error") and not was_dns.get("error"):
            self._add("DNS", "crit", f"lookups failing: {dns['error']}")
        elif was_dns.get("error") and dns.get("ok") is True:
            self._add("DNS", "info", "lookups working again")

        was_wan, wan = before.get("wan") or {}, after.get("wan") or {}
        if was_wan.get("ip") and wan.get("ip") and was_wan["ip"] != wan["ip"]:
            self._add("WAN", "warn", f"public address now {wan['ip']}")

        was_speed, speed = before.get("speedtest") or {}, after.get("speedtest") or {}
        last = speed.get("last")
        if last and last != was_speed.get("last"):
            self._add("SPEED", "info", f"{_rate(last['down_bps'])} down, {_rate(last['up_bps'])} up")
        if speed.get("error") and speed["error"] != was_speed.get("error"):
            self._add("SPEED", "warn", f"test failed: {speed['error']}")

    def _hardware(self, before: dict, after: dict) -> None:
        if not self._collector("HW", "hardware", before, after):
            return
        was_ups, ups = before.get("ups") or {}, after.get("ups") or {}
        if ups.get("present") and was_ups.get("present"):
            if ups.get("on_battery") and not was_ups.get("on_battery"):
                self._add("UPS", "crit", "mains power lost, running on battery")
            elif was_ups.get("on_battery") and not ups.get("on_battery"):
                self._add("UPS", "info", "mains power is back")
        elif ups.get("present") != was_ups.get("present") and "present" in was_ups:
            self._add("UPS", "info", "UPS connected" if ups.get("present") else "UPS disconnected")

        old_temps = {temp["name"]: temp for temp in before.get("temps") or []}
        for temp in after.get("temps") or []:
            was = old_temps.get(temp["name"])
            if not was or _level(was) == _level(temp):
                continue
            level = _level(temp)
            if level == "ok":
                self._add("TEMP", "info", f"{temp['name']} back to {temp['c']:.0f} C")
            else:
                self._add("TEMP", level, f"{temp['name']} at {temp['c']:.0f} C")

        was_power, power = before.get("power") or {}, after.get("power") or {}
        if power.get("cpu_measured") and was_power.get("cpu_measured") is False:
            self._add("PWR", "info", "processor power is now measured")
        if after.get("board_sensors") and before.get("board_sensors") is False:
            self._add("HW", "info", "board fan and voltage sensors found")
