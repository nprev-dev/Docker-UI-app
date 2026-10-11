"""Asks a running dashboard for its page and one reading, and says what each section reports.

    python tests/smoke.py http://127.0.0.1:8787

Fails if the server does not answer or the page is incomplete. What the sections can read depends on
the machine, so that is only printed; pass --require containers,network to insist on some of them.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request


def fetch(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as reply:
        return reply.status, reply.read()


def main(argv: list[str]) -> int:
    base = argv[1].rstrip("/")
    required = argv[argv.index("--require") + 1].split(",") if "--require" in argv else []

    # The first reading is collected just after start-up; give it a little while.
    snapshot = None
    for _ in range(30):
        try:
            status, body = fetch(f"{base}/api/snapshot")
            if status == 200:
                snapshot = json.loads(body)
                break
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(1)
    if snapshot is None:
        print("FAIL  the server never answered with a reading")
        return 1
    # Rates and load need two samples, and some systems take a while to name their hardware.
    wait = float(argv[argv.index("--settle") + 1]) if "--settle" in argv else 12.0
    time.sleep(wait)
    snapshot = json.loads(fetch(f"{base}/api/snapshot")[1])

    problems = []
    for path, marker in (("/", b"<title>"), ("/app.js", b"import"), ("/style.css", b"--bg"), ("/fonts/IBMPlexMono-Regular.woff2", b"wOF2")):
        try:
            status, body = fetch(base + path)
        except (urllib.error.URLError, OSError) as exc:
            status, body = 0, str(exc).encode()
        if status != 200 or marker not in body:
            problems.append(f"{path} is missing or wrong ({status})")

    print(f"host {snapshot.get('host')!r}, system {snapshot.get('system')}, {len(snapshot.get('events') or [])} log lines")
    for name in ("containers", "network", "hardware"):
        section = snapshot.get(name) or {}
        state = "ok" if section.get("ok") else f"NOT OK: {section.get('error')}"
        print(f"{name:11} {state}")
        if name in required and not section.get("ok"):
            problems.append(f"{name} is required here but reports: {section.get('error')}")
    network, hardware = snapshot.get("network") or {}, snapshot.get("hardware") or {}
    link = network.get("link") or {}
    listening = network.get("listening") or []
    print("  link", link.get("name"), link.get("state"), link.get("speed"), "| address known:", bool(link.get("address")), "| gateway known:", bool(network.get("gateway")),
          "| rates known:", network.get("rx_bps") is not None, "| sockets:", network.get("sockets"))
    print("  listening ports:", len(listening), "named:", sum(1 for port in listening if port.get("who")), [f"{p['port']}/{p['proto']} {p.get('who')}" for p in listening[:5]],
          "| talkers:", network.get("talkers") if network.get("talkers") is None else len(network["talkers"]))
    print("  pings:", [(p.get("name"), p.get("last"), p.get("error")) for p in network.get("ping") or []], "| dns:", network.get("dns"), "| wan known:", bool((network.get("wan") or {}).get("ip")))
    speed = network.get("speedtest") or {}
    last = speed.get("last") or {}
    print("  speed test:", "on" if speed.get("enabled") else "off", "| down Mbps:", last.get("down_bps") and round(last["down_bps"] / 1e6, 1),
          "| up Mbps:", last.get("up_bps") and round(last["up_bps"] / 1e6, 1), "| error:", speed.get("error"))
    inventory = hardware.get("inventory") or {}
    power = hardware.get("power") or {}
    print("  board", inventory.get("board"), "| cpu", inventory.get("cpu"), "| ram:", inventory.get("ram_bytes"), "| disks:", [(d.get("name"), d.get("kind"), d.get("model")) for d in inventory.get("disks") or []])
    print("  cpu load:", power.get("cpu_load"), "| cpu W:", power.get("cpu_w"), "measured" if power.get("cpu_measured") else "estimated", "| wall W:", power.get("wall_w"),
          "| temps:", len(hardware.get("temps") or []), "| ups:", hardware.get("ups"))

    for problem in problems:
        print("FAIL ", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
