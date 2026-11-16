"""Tests for the backups of data/ (backups.py). A temporary project folder."""

import stat
import tarfile
from datetime import datetime, timedelta

import pytest

from agentic_os import backups, routines


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A temporary project with a data folder."""
    monkeypatch.setattr(backups, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(backups, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(backups, "BACKUPS_DIR", tmp_path / "backups")
    (tmp_path / "data" / "jobs").mkdir(parents=True)
    (tmp_path / "data" / "jobs" / "abc.json").write_text('{"id": "abc"}')
    (tmp_path / "data" / "jobs" / ".claim.lock").write_text("")
    (tmp_path / "data" / "jobs" / ".abc.json.x1.tmp").write_text("half")
    return tmp_path


def test_a_backup_has_the_data_but_no_locks_or_temporary_files(project):
    path = backups.backup_now(datetime(2026, 11, 5, 3, 0))

    assert path.name == "agentic-os-data-2026-11-05_030000.tar.gz"
    with tarfile.open(path) as archive:
        assert archive.getnames() == ["data/jobs/abc.json"]  # restore with: tar -xzf <file> (in the project)
        assert archive.extractfile("data/jobs/abc.json").read() == b'{"id": "abc"}'
    assert [p.name for p in (project / "backups").iterdir()] == [path.name]  # no temporary file left


def test_only_the_newest_backups_stay(project):
    start = datetime(2026, 11, 1, 3, 0)
    for day in range(backups.KEEP + 2):
        backups.backup_now(start + timedelta(days=day))

    names = [path.name for path in backups.all_backups()]
    assert len(names) == backups.KEEP
    assert names[0] == "agentic-os-data-2026-11-09_030000.tar.gz"  # the newest first
    assert "agentic-os-data-2026-11-01_030000.tar.gz" not in names


def test_no_data_folder(project, monkeypatch):
    monkeypatch.setattr(backups, "DATA_DIR", project / "nothing")
    with pytest.raises(ValueError, match="No data folder"):
        backups.backup_now()


def test_the_backup_routine_action(project, capsys):
    assert routines.ACTIONS["backup"](1) == []  # no jobs, no cost
    assert "Backup: agentic-os-data-" in capsys.readouterr().out
    assert len(backups.all_backups()) == 1


def test_only_you_can_read_a_backup(project, open_umask):
    path = backups.backup_now(datetime(2026, 11, 5, 3, 0))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --- review 2, step 13 ---

def test_a_file_replaced_while_it_is_read_goes_in_whole(project, monkeypatch):
    """Item 12: stat sees the old size, the read gets the new file: the archive must not cut it."""
    job = project / "data" / "jobs" / "abc.json"
    real_read = type(job).read_bytes

    def read_new_version(path):
        if path.name == "abc.json":
            job.write_text('{"id": "abc", "status": "done", "result": "a longer answer"}')  # replaced now
        return real_read(path)

    monkeypatch.setattr(type(job), "read_bytes", read_new_version)
    path = backups.backup_now(datetime(2026, 11, 5, 3, 0))

    with tarfile.open(path) as archive:
        assert archive.extractfile("data/jobs/abc.json").read().endswith(b'"a longer answer"}')


def test_the_token_is_not_in_a_backup_and_old_rights_are_fixed(project, open_umask):
    """Item 72: data/app-token is a key; an old backups/ folder or archive may be readable by others."""
    (project / "data" / "app-token").write_text("0a1b" * 12)
    (project / "backups").mkdir(mode=0o775)
    old = project / "backups" / "agentic-os-data-2026-11-01_030000.tar.gz"
    old.write_bytes(b"")
    old.chmod(0o664)

    path = backups.backup_now(datetime(2026, 11, 5, 3, 0))

    with tarfile.open(path) as archive:
        assert "data/app-token" not in archive.getnames()
    assert stat.S_IMODE((project / "backups").stat().st_mode) == 0o700
    assert stat.S_IMODE(old.stat().st_mode) == 0o600


def test_two_backups_in_the_same_second_do_not_share_a_temp_file(project, monkeypatch):
    """Item 71: each backup gets its own temporary name."""
    names = []
    real_open = tarfile.open

    def remember(name, *args, **options):
        names.append(str(name))
        return real_open(name, *args, **options)

    monkeypatch.setattr(tarfile, "open", remember)
    backups.backup_now(datetime(2026, 11, 5, 3, 0))
    backups.backup_now(datetime(2026, 11, 5, 3, 0))

    assert len(set(names)) == 2
