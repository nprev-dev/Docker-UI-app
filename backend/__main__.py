"""Starts the dashboard server: python -m backend"""

from __future__ import annotations

import os

import uvicorn

from backend import app as dashboard


class Server(uvicorn.Server):
    def handle_exit(self, sig, frame):
        # Live streams never end on their own; tell them to, so stopping is quick and quiet.
        dashboard.hub.closing = True
        super().handle_exit(sig, frame)


def main() -> None:
    config = uvicorn.Config(
        dashboard.app,
        host=os.environ.get("DASH_HOST", "127.0.0.1"),
        port=int(os.environ.get("DASH_PORT", "8787")),
        # Safety net in case a stream does not notice the stop in time.
        timeout_graceful_shutdown=3,
    )
    try:
        Server(config).run()
    except KeyboardInterrupt:
        # uvicorn re-raises Ctrl-C once it has finished shutting down.
        pass


if __name__ == "__main__":
    main()
