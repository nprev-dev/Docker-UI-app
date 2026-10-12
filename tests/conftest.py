"""Keeps the tests away from the real machine's saved data and from the network."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Loading the app creates its state file and writes a start-up line into its event log. These must be
# set before any test imports it, or every test run would leave a line in the real dashboard's log.
os.environ["DASH_STATE"] = str(Path(tempfile.mkdtemp(prefix="rack-dashboard-tests-")) / "state.json")
os.environ["DASH_PROBES"] = "0"
