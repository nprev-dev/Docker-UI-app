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
    monkeypatch.setattr(cli.paths, "settings_file", lambda: tmp_path / "rack-dashboard" / "env")
    environ = {"DASH_PORT": "8800"}

    cli.load_settings(environ)

    assert environ == {"DASH_PORT": "8800", "DASH_HOST": "0.0.0.0"}


def test_no_settings_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.paths, "settings_file", lambda: tmp_path / "absent" / "env")
    environ = {}

    cli.load_settings(environ)

    assert environ == {}


def test_without_a_window_output_goes_to_a_log(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(cli.paths, "log_file", lambda: tmp_path / "data" / "rack-dashboard.log")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    cli.keep_a_log()
    print("started")
    sys.stderr.write("a warning\n")
    sys.stdout.close()

    lines = (tmp_path / "data" / "rack-dashboard.log").read_text().splitlines()
    assert lines[0].startswith("--- started 20") and lines[1:] == ["started", "a warning"]


def test_with_a_window_nothing_is_redirected(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(cli.paths, "log_file", lambda: tmp_path / "data" / "rack-dashboard.log")
    monkeypatch.setattr(cli.paths, "home", lambda: None)
    before = (sys.stdout, sys.stderr)

    cli.keep_a_log()

    assert (sys.stdout, sys.stderr) == before and not (tmp_path / "data").exists()


class Screen:
    """Stands in for a terminal, or for a pipe: something that takes text and says which it is."""

    def __init__(self, terminal: bool):
        self.terminal = terminal

    def isatty(self) -> bool:
        return self.terminal

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


def test_an_installed_copy_started_by_the_system_logs_into_its_own_folder(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(cli.paths, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.paths, "log_file", lambda: tmp_path / "rack-dashboard.log")
    monkeypatch.setattr(sys, "stdout", Screen(terminal=False))
    monkeypatch.setattr(sys, "stderr", Screen(terminal=False))

    cli.keep_a_log()
    print("listening")
    sys.stdout.close()

    assert (tmp_path / "rack-dashboard.log").read_text().splitlines()[1:] == ["listening"]


def test_an_installed_copy_started_in_a_terminal_prints_there(tmp_path, monkeypatch):
    import sys

    screen = Screen(terminal=True)
    monkeypatch.setattr(cli.paths, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.paths, "log_file", lambda: tmp_path / "rack-dashboard.log")
    monkeypatch.setattr(sys, "stdout", screen)
    monkeypatch.setattr(sys, "stderr", screen)

    cli.keep_a_log()

    assert sys.stdout is screen and not (tmp_path / "rack-dashboard.log").exists()


def test_a_log_that_cannot_be_written_does_not_stop_the_start(tmp_path, monkeypatch):
    import sys

    (tmp_path / "in-the-way").write_text("a file where a folder should be")
    monkeypatch.setattr(cli.paths, "log_file", lambda: tmp_path / "in-the-way" / "rack-dashboard.log")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    cli.keep_a_log()
    print("still running")

    assert sys.stdout is sys.stderr and sys.stdout is not None
    sys.stdout.close()
