"""The rack-dashboard command's options."""

from __future__ import annotations

import pytest

import rack_dashboard
from rack_dashboard import cli


def test_no_options_changes_nothing():
    options = cli.parse([])

    assert options.host is None and options.port is None and options.open is False


def test_options_are_read():
    options = cli.parse(["--host", "0.0.0.0", "--port", "9000", "--open"])

    assert (options.host, options.port, options.open) == ("0.0.0.0", 9000, True)


@pytest.mark.parametrize("bad", ["0", "65536", "-1", "http", ""])
def test_a_port_must_be_a_port(bad, capsys):
    with pytest.raises(SystemExit) as stop:
        cli.parse(["--port", bad])

    assert stop.value.code == 2
    assert "port" in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.parse(["--version"])

    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"rack-dashboard {rack_dashboard.__version__}"


def test_unknown_option_is_refused(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.parse(["--nope"])

    assert stop.value.code == 2


def test_help_names_the_settings(capsys):
    with pytest.raises(SystemExit):
        cli.parse(["--help"])

    text = capsys.readouterr().out
    assert "--host" in text and "--port" in text and "--open" in text and "DASH_" in text


def test_written_settings_do_not_override_what_is_set_for_this_run(tmp_path, monkeypatch):
    (tmp_path / "rack-dashboard").mkdir()
    (tmp_path / "rack-dashboard" / "env").write_text("DASH_HOST=0.0.0.0\nDASH_PORT=9000\n")
    monkeypatch.setattr(cli.paths, "config_dir", lambda: tmp_path / "rack-dashboard")
    environ = {"DASH_PORT": "8800"}

    cli.load_settings(environ)

    assert environ == {"DASH_PORT": "8800", "DASH_HOST": "0.0.0.0"}


def test_no_settings_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.paths, "config_dir", lambda: tmp_path / "absent")
    environ = {}

    cli.load_settings(environ)

    assert environ == {}


def test_without_a_window_output_goes_to_a_log(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(cli.paths, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    cli.keep_a_log()
    print("started")
    sys.stderr.write("a warning\n")
    sys.stdout.close()

    assert (tmp_path / "data" / "rack-dashboard.log").read_text() == "started\na warning\n"


def test_with_a_window_nothing_is_redirected(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(cli.paths, "data_dir", lambda: tmp_path / "data")
    before = (sys.stdout, sys.stderr)

    cli.keep_a_log()

    assert (sys.stdout, sys.stderr) == before and not (tmp_path / "data").exists()
