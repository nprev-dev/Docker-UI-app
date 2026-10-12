"""The log file an installed dashboard writes, which must not grow without end."""

from __future__ import annotations

import threading

from rack_dashboard.logfile import CappedLog


def test_text_is_added_at_the_end(tmp_path):
    path = tmp_path / "log"
    path.write_text("from the last run\n")

    log = CappedLog(path)
    assert log.write("one\n") == 4
    print("two", file=log)
    log.close()

    assert path.read_text() == "from the last run\none\ntwo\n"


def test_past_the_limit_the_older_half_goes(tmp_path):
    path = tmp_path / "log"
    log = CappedLog(path, limit=1000)

    for number in range(400):
        log.write(f"line {number:04}\n")
    log.close()

    lines = path.read_text().splitlines()
    assert path.stat().st_size <= 1000
    # The newest lines are all there, in order, and the file starts with a whole line.
    assert lines[-1] == "line 0399" and lines == [f"line {number:04}" for number in range(400 - len(lines), 400)]
    assert len(lines) >= 40


def test_a_file_already_too_long_is_cut_at_the_first_write(tmp_path):
    path = tmp_path / "log"
    path.write_text("old\n" * 1000)

    log = CappedLog(path, limit=1000)
    log.write("new\n")
    log.close()

    assert path.stat().st_size <= 1000 and path.read_text().endswith("old\nnew\n")


def test_cutting_never_leaves_half_a_character(tmp_path):
    path = tmp_path / "log"
    log = CappedLog(path, limit=300)

    for _ in range(100):
        log.write("température élevée: 85°C\n")
    log.close()

    assert set(path.read_text(encoding="utf-8").splitlines()) == {"température élevée: 85°C"}


def test_one_line_longer_than_the_limit_is_survived(tmp_path):
    path = tmp_path / "log"
    log = CappedLog(path, limit=100)

    log.write("x" * 500 + "\n")
    log.write("after\n")
    log.close()

    assert path.read_text().endswith("after\n") and path.stat().st_size <= 100 + 6


def test_many_writers_at_once(tmp_path):
    path = tmp_path / "log"
    log = CappedLog(path, limit=20_000)

    def chatter(name: str) -> None:
        for number in range(500):
            log.write(f"{name} {number:03}\n")

    threads = [threading.Thread(target=chatter, args=(name,)) for name in "abcd"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    log.close()

    lines = path.read_text().splitlines()
    assert path.stat().st_size <= 20_000
    # Whatever was kept is whole lines, none of them mixed with another.
    assert all(len(line) == 5 and line[0] in "abcd" and line[2:].isdigit() for line in lines)


def test_a_log_taken_away_does_not_raise(tmp_path):
    path = tmp_path / "log"
    log = CappedLog(path, limit=50)
    log._file.close()

    assert log.write("lost\n") == 5
    log.flush()


def test_it_is_not_a_terminal(tmp_path):
    log = CappedLog(tmp_path / "log")

    assert log.writable() and not log.isatty()
    log.close()
