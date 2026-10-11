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
