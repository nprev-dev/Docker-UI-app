"""The hub hands the latest snapshot to every connected page."""

from __future__ import annotations

import asyncio

from backend.app import Hub


def test_new_viewer_gets_the_latest_snapshot_at_once():
    async def scenario():
        hub = Hub()
        await hub.publish({"n": 1})
        await hub.publish({"n": 2})
        return await asyncio.wait_for(hub.newer_than(0), timeout=1)

    assert asyncio.run(scenario()) == (2, {"n": 2})


def test_waiting_viewers_all_wake_on_publish():
    async def scenario():
        hub = Hub()
        await hub.publish({"n": 1})
        waiters = [asyncio.create_task(hub.newer_than(1)) for _ in range(3)]
        await asyncio.sleep(0.05)
        assert not any(w.done() for w in waiters)
        await hub.publish({"n": 2})
        return await asyncio.wait_for(asyncio.gather(*waiters), timeout=1)

    assert asyncio.run(scenario()) == [(2, {"n": 2})] * 3


def test_viewer_waits_when_nothing_is_newer():
    async def scenario():
        hub = Hub()
        await hub.publish({"n": 1})
        try:
            await asyncio.wait_for(hub.newer_than(1), timeout=0.1)
        except asyncio.TimeoutError:
            return "still waiting"

    assert asyncio.run(scenario()) == "still waiting"


def test_hub_survives_a_viewer_that_gave_up():
    async def scenario():
        hub = Hub()
        gone = asyncio.create_task(hub.newer_than(0))
        await asyncio.sleep(0.05)
        gone.cancel()
        await asyncio.gather(gone, return_exceptions=True)
        await hub.publish({"n": 1})
        return await asyncio.wait_for(hub.newer_than(0), timeout=1)

    assert asyncio.run(scenario()) == (1, {"n": 1})
