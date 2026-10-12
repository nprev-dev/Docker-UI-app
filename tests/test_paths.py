"""Where the dashboard keeps its data, and where it finds its own page."""

from __future__ import annotations

from pathlib import Path

from rack_dashboard import paths


def test_data_folder_on_linux():
    home = Path("/home/someone")
    assert paths.data_dir("linux", {}, home) == home / ".local" / "share" / "rack-dashboard"
    assert paths.data_dir("linux", {"XDG_DATA_HOME": "/srv/data"}, home) == Path("/srv/data/rack-dashboard")


def test_a_relative_xdg_folder_is_ignored():
    home = Path("/home/someone")
    assert paths.data_dir("linux", {"XDG_DATA_HOME": "data"}, home) == home / ".local" / "share" / "rack-dashboard"
    assert paths.data_dir("linux", {"XDG_DATA_HOME": ""}, home) == home / ".local" / "share" / "rack-dashboard"


def test_data_folder_on_mac():
    home = Path("/Users/someone")
    # The Linux setting means nothing there, even if it happens to be set.
    assert paths.data_dir("darwin", {"XDG_DATA_HOME": "/srv/data"}, home) == home / "Library" / "Application Support" / "rack-dashboard"


def test_data_folder_on_windows():
    home = Path("C:/Users/someone")
    assert paths.data_dir("win32", {"LOCALAPPDATA": "D:/Local"}, home) == Path("D:/Local/rack-dashboard")
    assert paths.data_dir("win32", {}, home) == home / "AppData" / "Local" / "rack-dashboard"


def test_an_explicit_state_file_wins(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text("{}")

    assert paths.state_file({"DASH_STATE": "/somewhere/else.json"}, tmp_path / "rack_dashboard") == Path("/somewhere/else.json")


def test_a_data_folder_beside_the_source_keeps_being_used(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text("{}")

    assert paths.state_file({}, tmp_path / "rack_dashboard") == tmp_path / "data" / "state.json"


def test_otherwise_the_state_lives_in_the_users_data_folder(tmp_path):
    # An installed copy, or a fresh copy of the source: nothing beside it.
    assert paths.state_file({"XDG_DATA_HOME": str(tmp_path / "xdg")}, tmp_path / "rack_dashboard") == paths.data_dir(environ={"XDG_DATA_HOME": str(tmp_path / "xdg")}) / "state.json"
    # An empty data folder beside the source is not a reason to use it.
    (tmp_path / "data").mkdir()
    assert paths.state_file({"XDG_DATA_HOME": str(tmp_path / "xdg")}, tmp_path / "rack_dashboard").parent == paths.data_dir(environ={"XDG_DATA_HOME": str(tmp_path / "xdg")})


def test_the_page_is_shipped_inside_the_package():
    assert paths.FRONTEND == paths.PACKAGE / "frontend"
    for name in ("index.html", "app.js", "style.css", "fonts/LICENSE.txt"):
        assert (paths.FRONTEND / name).is_file(), name


def test_the_tests_do_not_touch_the_real_state_file():
    # conftest.py points the app at a scratch file; if this fails, test runs write into real data.
    from rack_dashboard import app

    assert "rack-dashboard-tests-" in str(app.state.path)
    assert app.state.path != paths.PACKAGE.parent / "data" / "state.json"


def test_settings_folder_on_each_system():
    assert paths.config_dir("linux", {}, Path("/home/someone")) == Path("/home/someone/.config/rack-dashboard")
    assert paths.config_dir("linux", {"XDG_CONFIG_HOME": "/etc/xdg-user"}, Path("/home/someone")) == Path("/etc/xdg-user/rack-dashboard")
    assert paths.config_dir("linux", {"XDG_CONFIG_HOME": "relative"}, Path("/home/someone")) == Path("/home/someone/.config/rack-dashboard")
    assert paths.config_dir("darwin", {"XDG_CONFIG_HOME": "/etc/xdg-user"}, Path("/Users/someone")) == Path("/Users/someone/.config/rack-dashboard")
    assert paths.config_dir("win32", {"APPDATA": "C:/Users/someone/AppData/Roaming"}, Path("C:/Users/someone")) == Path("C:/Users/someone/AppData/Roaming/rack-dashboard")
    assert paths.config_dir("win32", {}, Path("C:/Users/someone")) == Path("C:/Users/someone/AppData/Roaming/rack-dashboard")


def test_settings_file_is_read(tmp_path):
    file = tmp_path / "env"
    file.write_text(
        "# Settings for Rack Dashboard\n"
        "\n"
        "DASH_HOST=0.0.0.0\n"
        "  DASH_PORT = 9000  \n"
        "DASH_CURRENCY=\"CA$ \"\n"
        "DASH_ROOM_SENSOR='/sys/bus/w1/devices/28-00000a/temperature'\n"
        "DASH_KWH_PRICE=\n"
        "#DASH_PROBES=0\n"
        "not a setting\n"
        "=nothing\n"
        "bad key=1\n"
        "DASH_DNS_NAME=a=b\n"
    )

    assert paths.read_settings(file) == {
        "DASH_HOST": "0.0.0.0", "DASH_PORT": "9000", "DASH_CURRENCY": "CA$ ", "DASH_ROOM_SENSOR": "/sys/bus/w1/devices/28-00000a/temperature",
        "DASH_KWH_PRICE": "", "DASH_DNS_NAME": "a=b",
    }


def test_settings_file_missing_or_unreadable(tmp_path):
    assert paths.read_settings(tmp_path / "nope") == {}
    assert paths.read_settings(tmp_path) == {}
    (tmp_path / "binary").write_bytes(b"\xff\xfe\x00DASH")
    assert paths.read_settings(tmp_path / "binary") == {}
    # Notepad on Windows likes to put a marker at the start of a file; it is not part of the first key.
    (tmp_path / "marked").write_bytes(b"\xef\xbb\xbfDASH_PORT=9000\r\nDASH_HOST=0.0.0.0\r\n")
    assert paths.read_settings(tmp_path / "marked") == {"DASH_PORT": "9000", "DASH_HOST": "0.0.0.0"}
