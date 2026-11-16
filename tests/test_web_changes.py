"""Tests for the file changes in the job view (hooks, stage 2).

We call the API functions directly, as plain Python functions. No agent runs:
we save a job by hand and call changes.snapshot_before() as the hook would.
"""

import pytest
from fastapi import HTTPException

from agentic_os import changes, jobs, web


@pytest.fixture
def edited(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """A finished job whose agent changed one file and made a new one. Return (job, old, new)."""
    monkeypatch.setattr(changes, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    job = jobs.add("Edit the app", "haiku", use_memory=False)
    job.status = "done"
    jobs.save(job)
    old, new = tmp_path / "app.py", tmp_path / "new.py"
    old.write_text("print('old')\n")
    changes.snapshot_before(job.id, old)
    changes.snapshot_before(job.id, new)
    old.write_text("print('new')\n")
    new.write_text("x = 1\n")
    return job, old, new


def test_the_api_lists_the_changes(edited):
    job, old, new = edited
    states = {change["path"]: change["state"] for change in web.api_changes(job.id)}
    assert states == {str(old): "modified", str(new): "added"}


def test_undo_one_file_then_all(edited):
    job, old, new = edited

    assert web.api_undo(job.id, web.Undo(path=str(old))) == {"undone": [str(old)]}
    assert old.read_text() == "print('old')\n"
    assert new.exists()

    assert web.api_undo(job.id, web.Undo()) == {"undone": [str(new)]}
    assert not new.exists()
    assert web.api_undo(job.id, web.Undo()) == {"undone": []}


@pytest.mark.parametrize("status", ["queued", "running"])
def test_no_undo_while_the_job_works(edited, status):
    job, old, _ = edited
    job.status = status
    jobs.save(job)

    with pytest.raises(HTTPException) as error:
        web.api_undo(job.id, web.Undo())
    assert error.value.status_code == 409
    assert old.read_text() == "print('new')\n"


def test_a_job_without_edits_has_no_changes(edited):
    other = jobs.add("Just answer", "haiku", use_memory=False)
    assert web.api_changes(other.id) == []


@pytest.mark.parametrize("job_id", ["nosuchjob", "../etc"])
def test_unknown_job_is_404(edited, job_id):
    for call in (lambda: web.api_changes(job_id), lambda: web.api_undo(job_id, web.Undo())):
        with pytest.raises(HTTPException) as error:
            call()
        assert error.value.status_code == 404
