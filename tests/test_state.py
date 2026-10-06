"""The state file must never stop the dashboard from starting or running."""

from __future__ import annotations

import json

from backend.state import StateFile


def test_missing_file_starts_empty(tmp_path):
    state = StateFile(tmp_path / "nowhere" / "state.json")

    assert state.get("anything") is None
    assert state.get("anything", 5) == 5


def test_values_survive_a_restart(tmp_path):
    path = tmp_path / "data" / "state.json"
    StateFile(path).set("wan", {"ip": "203.0.113.4"})

    assert StateFile(path).get("wan") == {"ip": "203.0.113.4"}


def test_damaged_file_starts_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"half": ')

    state = StateFile(path)
    assert state.get("half") is None

    state.set("ok", 1)
    assert json.loads(path.read_text()) == {"ok": 1}


def test_file_holding_the_wrong_shape_starts_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]")

    assert StateFile(path).get("x") is None


def test_unwritable_location_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    state = StateFile(blocker / "state.json")

    state.set("key", "value")

    # Still usable in memory for this run.
    assert state.get("key") == "value"


def test_no_scratch_file_is_left_behind(tmp_path):
    path = tmp_path / "state.json"
    StateFile(path).set("a", 1)

    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
