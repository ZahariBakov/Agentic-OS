"""Tests for the copies before an agent's edit, the diff and undo (changes.py, edit_hook.py)."""

import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

import pytest

from agentic_os import changes, edit_hook, job_store, jobs, routines, safety


@pytest.fixture
def snapshots(tmp_path, monkeypatch, temp_memory):
    """Use a temporary snapshots folder. Return a work folder with one file in it."""
    monkeypatch.setattr(changes, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    work = tmp_path / "work"
    work.mkdir()
    (work / "app.py").write_text("print('old')\n")
    return work


def test_undo_puts_back_a_changed_a_new_and_a_deleted_file(snapshots):
    changed, new, deleted = snapshots / "app.py", snapshots / "new.py", snapshots / "gone.py"
    deleted.write_text("keep me\n")
    for path in (changed, new, deleted):
        assert changes.snapshot_before("job1", path)
    changed.write_text("print('new')\n")
    new.write_text("x = 1\n")
    deleted.unlink()

    states = {change["path"]: change["state"] for change in changes.changes("job1")}
    assert states == {str(changed): "modified", str(new): "added", str(deleted): "deleted"}
    assert "+print('new')" in changes.changes("job1")[0]["diff"]

    assert sorted(changes.undo("job1")) == sorted(map(str, (changed, new, deleted)))
    assert changed.read_text() == "print('old')\n"
    assert not new.exists()
    assert deleted.read_text() == "keep me\n"
    assert all(change["state"] == "unchanged" for change in changes.changes("job1"))


def test_only_the_first_edit_saves_a_copy(snapshots):
    path = snapshots / "app.py"
    assert changes.snapshot_before("job1", path)
    path.write_text("print('first edit')\n")

    assert not changes.snapshot_before("job1", path)  # the second edit keeps the old copy
    changes.undo("job1")
    assert path.read_text() == "print('old')\n"


def test_undo_one_file_leaves_the_others(snapshots):
    one, two = snapshots / "app.py", snapshots / "two.py"
    for path in (one, two):
        changes.snapshot_before("job1", path)
        path.write_text("changed\n")

    assert changes.undo("job1", one) == [str(one)]
    assert one.read_text() == "print('old')\n"
    assert two.read_text() == "changed\n"


def test_protected_files_are_never_saved(snapshots):
    assert not changes.snapshot_before("job1", safety.PROJECT_DIR / "roster.json")
    assert not changes.snapshot_before("job1", changes.SNAPSHOTS_DIR / "x" / "index.json")
    assert changes.changes("job1") == []


def test_bad_job_id_is_refused(snapshots):
    with pytest.raises(ValueError):
        changes.snapshot_before("../evil", snapshots / "app.py")


def test_hook_saves_the_file_of_an_edit_event(snapshots):
    event = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": str(snapshots),
             "tool_input": {"file_path": "app.py", "old_string": "a", "new_string": "b"}}

    assert edit_hook.handle(event, "job1", str(snapshots))  # a relative path is relative to cwd
    assert changes.changes("job1")[0]["path"] == str(snapshots / "app.py")
    assert not edit_hook.handle(event, None, str(snapshots))  # not started by a job: nothing to do


def test_hook_saves_nothing_for_an_edit_that_is_refused(snapshots, tmp_path):
    """The hook runs before the permission check: a refused edit must leave no copy for undo."""
    outside = tmp_path / "outside.txt"
    event = {"hook_event_name": "PreToolUse", "tool_name": "Write", "cwd": str(snapshots),
             "tool_input": {"file_path": str(outside), "content": "x"}}
    assert not edit_hook.handle(event, "job2", str(snapshots))  # outside the agent's folder
    escape = dict(event, tool_input={"file_path": "../outside.txt", "content": "x"})
    assert not edit_hook.handle(escape, "job2", str(snapshots))  # ".." does not get out
    inside = dict(event, tool_input={"file_path": "app.py", "content": "x"})
    assert not edit_hook.handle(inside, "job2", None)  # a read-only agent: no edit folder
    assert changes.changes("job2") == []
    assert edit_hook.environment("j", None) == {"AGENTIC_OS_JOB_ID": "j"}
    assert edit_hook.environment("j", snapshots)["AGENTIC_OS_EDIT_DIR"] == str(snapshots.resolve())


def test_hook_never_fails(snapshots, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("this is not JSON"))
    with pytest.raises(SystemExit) as end:
        edit_hook.main()

    assert end.value.code == 0
    assert "edit_hook:" in capsys.readouterr().err


def test_hook_command_from_the_settings_runs(tmp_path):
    """Run the real command from the settings, the way Claude Code runs it.

    It runs in another process, where our temporary folders do not apply. So the
    event is one the hook ignores: the test proves that the command starts and
    finds our package, but it never writes to the real data/snapshots/.
    """
    command = json.loads(safety.settings())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    event = {"hook_event_name": "PostToolUse", "tool_name": "Write", "cwd": str(tmp_path),
             "tool_input": {"file_path": str(tmp_path / "app.py"), "content": "x"}}
    done = subprocess.run(command, shell=True, input=json.dumps(event), text=True,
                          cwd=tmp_path, capture_output=True,
                          env={"AGENTIC_OS_JOB_ID": "job1", "PATH": "/usr/bin:/bin"})

    assert done.returncode == 0, done.stderr
    assert done.stderr == ""


DAY = 86400


def make_old(folder, days):
    """Set the time of every file in a snapshot folder `days` days back."""
    moment = time.time() - days * DAY
    for path in folder.iterdir():
        os.utime(path, (moment, moment))


@pytest.fixture
def old_jobs(snapshots, temp_jobs):
    """Snapshot folders for jobs in different states, all 40 days old, plus a new one."""
    for job_id, status in [("done1", "done"), ("stopped1", "stopped"), ("waiting1", "waiting"),
                           ("interrupted1", "interrupted"), ("new1", "done"), ("gone1", None)]:
        if status:
            jobs.save(jobs.Job(id=job_id, task="x", status=status))
        assert changes.snapshot_before(job_id, snapshots / "app.py")
        if job_id != "new1":
            make_old(changes.SNAPSHOTS_DIR / job_id, 40)
    return changes.SNAPSHOTS_DIR


def test_prune_deletes_only_old_copies_of_finished_or_gone_jobs(old_jobs):
    assert changes.prune() == ["done1", "gone1", "stopped1"]
    left = sorted(folder.name for folder in old_jobs.iterdir())
    assert left == ["interrupted1", "new1", "waiting1"]  # not finished, or too new
    assert changes.changes("done1") == []  # the job view shows no changes any more
    assert changes.prune() == []  # a second run finds nothing


def test_prune_days_and_a_folder_that_is_not_a_job(old_jobs):
    (old_jobs / "notes.txt").write_text("not a job\n")
    (old_jobs / "bad-name").mkdir()
    (old_jobs / "bad-name" / "app.py").write_text("x\n")  # an old file: only the name guard keeps it
    make_old(old_jobs / "bad-name", 40)
    assert changes.prune(days=50) == []  # 40 days old is still young enough
    assert changes.prune(days=10) == ["done1", "gone1", "stopped1"]
    assert (old_jobs / "notes.txt").exists() and (old_jobs / "bad-name").exists()


def test_prune_after_undo_keeps_the_lock_out_of_the_age(old_jobs):
    (old_jobs / "done1" / ".lock").unlink()  # so that undo makes a NEW .lock (an old one keeps its time)
    changes.undo("done1")
    assert (old_jobs / "done1" / ".lock").stat().st_mtime > time.time() - DAY
    assert "done1" in changes.prune()


def test_prune_without_a_snapshots_folder(snapshots, temp_jobs):
    assert changes.prune() == []


def test_prune_snapshots_routine_action(old_jobs, capsys):
    assert routines.ACTIONS["prune-snapshots"](3) == []  # no jobs, no cost
    assert "deleted the copies of 3 old job(s)" in capsys.readouterr().out


def test_prune_keeps_a_job_that_finished_lately(old_jobs):
    job = jobs.load("done1")
    job.finished_at = datetime.now().isoformat()  # it waited long, and finished today
    jobs.save(job)
    assert "done1" not in changes.prune()
    assert (old_jobs / "done1").is_dir()


def test_prune_keeps_the_copies_of_a_broken_job_file(old_jobs):
    (job_store.JOBS_DIR / "done1.json").write_text("{broken")
    (job_store.JOBS_DIR / "stopped1.json").write_text('{"id": "stopped1", "task": "x", "new_field": 1}')
    assert changes.prune() == ["gone1"]


def test_prune_does_nothing_without_the_jobs_folder(old_jobs, monkeypatch, tmp_path):
    monkeypatch.setattr(job_store, "JOBS_DIR", tmp_path / "moved-away")
    assert changes.prune() == []  # otherwise every job would look "gone"
    assert len(list(old_jobs.iterdir())) == 6


def test_prune_needs_one_day_or_more(old_jobs):
    for days in (0, -1):
        with pytest.raises(ValueError):
            changes.prune(days=days)


def test_none_message_explains_pruned_copies(old_jobs):
    job = jobs.load("done1")
    job.finished_at = (datetime.now() - timedelta(days=40)).isoformat()
    jobs.save(job)
    assert "deleted 30 days after" in changes.none_message("done1", "Nothing to undo.")
    assert changes.none_message("new1", "Nothing to undo.") == "Nothing to undo."  # no finished_at
    assert changes.none_message("gone1", "Nothing to undo.") == "Nothing to undo."  # no job file


def test_undo_gives_the_file_a_new_time(snapshots):
    """make or ninja rebuild only files newer than the build: the old time would skip the rebuild."""
    app = snapshots / "app.py"
    old = time.time() - 10 * DAY
    os.utime(app, (old, old))
    assert changes.snapshot_before("job1", app)
    app.write_text("print('new')\n")

    changes.undo("job1")

    assert app.read_text() == "print('old')\n"
    assert app.stat().st_mtime > time.time() - DAY


def test_prune_skips_a_file_that_goes_away_while_it_looks(old_jobs):
    """A temp file of an atomic write can disappear between iterdir and stat (here: a broken link)."""
    (old_jobs / "done1" / "index.json.tmp").symlink_to(old_jobs / "missing")

    assert "done1" in changes.prune()


def test_forget_drops_only_the_copies_inside_a_folder(snapshots, tmp_path):
    inside = snapshots / "app.py"
    outside = tmp_path / "other.py"
    outside.write_text("x = 1\n")
    assert changes.snapshot_before("job1", inside) and changes.snapshot_before("job1", outside)
    copies = len(list((changes.SNAPSHOTS_DIR / "job1").iterdir()))

    assert changes.forget("job1", snapshots) == 1

    assert [change["path"] for change in changes.changes("job1")] == [str(outside)]
    assert len(list((changes.SNAPSHOTS_DIR / "job1").iterdir())) == copies - 1  # its copy is gone too
    assert changes.forget("job1", snapshots) == 0
    assert changes.forget("nojob", snapshots) == 0
