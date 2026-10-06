"""Dashboard server: collects a snapshot every tick and pushes it to the page."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.collectors.containers import ContainerCollector
from backend.collectors.network import NetworkCollector, label_ports
from backend.state import StateFile

ROOT = Path(__file__).resolve().parent.parent
INTERVAL = float(os.environ.get("DASH_INTERVAL", "1"))
# Idle proxies and browsers drop silent connections; a comment line keeps the stream open.
KEEPALIVE = 15
FRONTEND = ROOT / "frontend"


class Hub:
    """Holds the latest snapshot and wakes every connected page when a new one lands."""

    def __init__(self) -> None:
        self.snapshot: dict | None = None
        self.sequence = 0
        # Set when the server is stopping, so open streams end instead of holding it up.
        self.closing = False
        self._changed = asyncio.Condition()

    async def publish(self, snapshot: dict) -> None:
        async with self._changed:
            self.snapshot = snapshot
            self.sequence += 1
            self._changed.notify_all()

    async def newer_than(self, sequence: int) -> tuple[int, dict]:
        async with self._changed:
            await self._changed.wait_for(lambda: self.sequence > sequence)
            return self.sequence, self.snapshot


hub = Hub()
containers = ContainerCollector()
network = NetworkCollector(
    StateFile(os.environ.get("DASH_STATE", ROOT / "data" / "state.json")),
    iface=os.environ.get("DASH_IFACE") or None,
    ping_target=os.environ.get("DASH_PING_TARGET", "1.1.1.1"),
    dns_name=os.environ.get("DASH_DNS_NAME", "example.com"),
    # Each test moves up to about 75 MB; 0 switches them off.
    speedtest_hours=float(os.environ.get("DASH_SPEEDTEST_HOURS", "6")),
    # 0 keeps the dashboard from sending anything onto the network at all.
    probes_enabled=os.environ.get("DASH_PROBES", "1") != "0",
)


async def collect(name: str, collector, offline: dict) -> dict:
    try:
        return await asyncio.to_thread(collector.collect)
    except Exception as exc:
        # A collector bug must never take the stream down with it.
        return {**offline, "ok": False, "error": f"{name} collector failed: {exc}"}


async def collect_loop() -> None:
    while True:
        started = time.monotonic()
        container_data, network_data = await asyncio.gather(
            collect("containers", containers, {"total": 0, "states": {}, "items": []}),
            collect("network", network, {}),
        )
        label_ports(network_data, container_data)
        await hub.publish(
            {
                "ts": time.time(),
                "host": socket.gethostname(),
                "interval": INTERVAL,
                "containers": container_data,
                "network": network_data,
            }
        )
        await asyncio.sleep(max(0.0, INTERVAL - (time.monotonic() - started)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(collect_loop())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    network.close()


app = FastAPI(title="Rack dashboard", lifespan=lifespan)


@app.get("/api/snapshot")
async def snapshot():
    if hub.snapshot is None:
        return JSONResponse({"detail": "first sample not collected yet"}, status_code=503)
    return hub.snapshot


@app.get("/api/stream")
async def stream():
    async def events():
        sequence = 0
        # Tells the browser how long to wait before reconnecting after a drop.
        yield "retry: 2000\n\n"
        while not hub.closing:
            try:
                sequence, snap = await asyncio.wait_for(hub.newer_than(sequence), timeout=KEEPALIVE)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(snap, separators=(',', ':'))}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class FreshStaticFiles(StaticFiles):
    """Makes the browser ask for a newer copy every time, so an update never mixes old and new files."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Mounted last so it never shadows the API routes above.
app.mount("/", FreshStaticFiles(directory=FRONTEND, html=True), name="frontend")
