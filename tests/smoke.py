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

    problems = []
    for path, marker in (("/", b"<title>"), ("/app.js", b"import"), ("/style.css", b"--bg"), ("/fonts/IBMPlexMono-Regular.woff2", b"wOF2")):
        try:
            status, body = fetch(base + path)
        except (urllib.error.URLError, OSError) as exc:
            status, body = 0, str(exc).encode()
        if status != 200 or marker not in body:
            problems.append(f"{path} is missing or wrong ({status})")

    print(f"host {snapshot.get('host')!r}, {len(snapshot.get('events') or [])} log lines")
    for name in ("containers", "network", "hardware"):
        section = snapshot.get(name) or {}
        state = "ok" if section.get("ok") else f"NOT OK: {section.get('error')}"
        print(f"{name:11} {state}")
        if name in required and not section.get("ok"):
            problems.append(f"{name} is required here but reports: {section.get('error')}")
    network, hardware = snapshot.get("network") or {}, snapshot.get("hardware") or {}
    link = network.get("link") or {}
    print("  link", link.get("name"), link.get("state"), "| gateway known:", bool(network.get("gateway")), "| listening ports:", len(network.get("listening") or []),
          "| talkers:", len(network.get("talkers") or []), "| pings:", [(p.get("name"), p.get("last") is not None, p.get("error")) for p in network.get("ping") or []])
    inventory = hardware.get("inventory") or {}
    print("  cpu", (inventory.get("cpu") or {}).get("model"), "| ram known:", bool(inventory.get("ram_bytes")), "| disks:", len(inventory.get("disks") or []),
          "| temps:", len(hardware.get("temps") or []), "| wall power known:", (hardware.get("power") or {}).get("wall_w") is not None)

    for problem in problems:
        print("FAIL ", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
