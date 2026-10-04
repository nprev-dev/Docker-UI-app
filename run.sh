#!/usr/bin/env bash
# Starts the dashboard server. Open http://127.0.0.1:8787 once it is up.
# DASH_HOST and DASH_PORT change where it listens; DASH_INTERVAL the seconds between updates.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m backend
