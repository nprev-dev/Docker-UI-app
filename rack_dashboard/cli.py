"""The rack-dashboard command: reads its options and runs the server."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser

from rack_dashboard import __version__, paths
from rack_dashboard.logfile import CappedLog


def port(text: str) -> int:
    number = int(text)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("a port is a number from 1 to 65535")
    return number


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=paths.NAME,
        description="A one-page status screen for a home server: containers, network, power and hardware.",
        epilog="Every other setting is an environment variable (DASH_...); the readme lists them.",
    )
    parser.add_argument("--host", help="address to listen on (default 127.0.0.1; 0.0.0.0 lets other machines see the page)")
    parser.add_argument("--port", type=port, help="port to listen on (default 8787)")
    parser.add_argument("--open", action="store_true", help="open the page in the default browser once the server is up")
    parser.add_argument("--version", action="version", version=f"{paths.NAME} {__version__}")
    return parser.parse_args(argv)


def load_settings(environ=os.environ) -> None:
    """Settings from the settings file become environment variables, unless one is set already:
    what was typed for this one run wins over what was written down for every run."""
    for key, value in paths.read_settings(paths.settings_file()).items():
        environ.setdefault(key, value)


def keep_a_log() -> None:
    """Started by the system instead of from a terminal, there is nobody to print to. What would
    have been printed then goes to a log file: always when there is no window at all (as Windows
    starts it at sign-in), and for an installed copy whenever its output is not a terminal."""
    windowless = sys.stdout is None or sys.stderr is None
    if not windowless and (paths.home() is None or sys.stdout.isatty()):
        return
    try:
        path = paths.log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        log = CappedLog(path)
        log.write(f"--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    except OSError:
        log = open(os.devnull, "w")
    sys.stdout = sys.stderr = log


def main(argv: list[str] | None = None) -> int:
    options = parse(argv)
    keep_a_log()
    load_settings()
    # Settings travel as environment variables, because the app reads them once, as it is loaded.
    if options.host:
        os.environ["DASH_HOST"] = options.host
    if options.port:
        os.environ["DASH_PORT"] = str(options.port)
    host = os.environ.get("DASH_HOST", "127.0.0.1")
    number = int(os.environ.get("DASH_PORT", "8787"))

    import uvicorn

    from rack_dashboard import app as dashboard

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            # Live streams never end on their own; tell them to, so stopping is quick and quiet.
            dashboard.hub.closing = True
            super().handle_exit(sig, frame)

    # Safety net in case a stream does not notice the stop in time.
    config = uvicorn.Config(dashboard.app, host=host, port=number, timeout_graceful_shutdown=3)
    if options.open:
        # A browser cannot open "every address"; this machine's own one always works.
        shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        threading.Timer(1.5, webbrowser.open, [f"http://{shown}:{number}"]).start()
    try:
        Server(config).run()
    except KeyboardInterrupt:
        # uvicorn re-raises Ctrl-C once it has finished shutting down.
        pass
    return 0
