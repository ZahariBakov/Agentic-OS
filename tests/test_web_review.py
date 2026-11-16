"""Tests for the review on the web page (review, stage 2).

We call the API functions directly. No agent runs: web.pool.submit is replaced.
"""

import pytest
from fastapi import HTTPException

from agentic_os import changes, job_store, jobs, web


@pytest.fixture
def submitted(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Replace web.pool.submit; temporary inbox and snapshots. Return the jobs that would start."""
    monkeypatch.setattr(job_store, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(changes, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    started = []
    monkeypatch.setattr(web.pool, "submit", lambda function, job: started.append(job))
    return started


@pytest.fixture
def edited(submitted, tmp_path):
    """A done job that changed app.py. Return (job, the file)."""
    job = jobs.add("Fix the app.", "haiku", use_memory=False)
    job.status, job.session_id, job.result = "done", "s1", "fixed"
    jobs.save(job)
    app = tmp_path / "app.py"
    app.write_text("old\n")
    changes.snapshot_before(job.id, app)
    app.write_text("new\n")
    return job, app


def test_approve(edited, submitted):
    job, _ = edited

    reply = web.api_review(job.id, web.Review(verdict="approve"))

    assert reply["job"]["review"]["verdict"] == "approved" and submitted == []


def test_request_changes_with_a_file_comment_starts_the_job(edited, submitted):
    job, app = edited

    reply = web.api_review(job.id, web.Review(verdict="changes", note="", files={str(app): "Keep the name.",
                                                                               "/empty": "  "}))

    assert reply["started"] and [started.id for started in submitted] == [job.id]
    assert reply["job"]["signals"] == ["rework"]
    assert "Keep the name." in jobs.read_inbox(job.id)[0]["text"]  # empty comments are left out


@pytest.mark.parametrize("review", [
    web.Review(verdict="maybe"),  # an unknown verdict
    web.Review(verdict="reject", note=" "),  # no reason
    web.Review(verdict="changes"),  # no comments
])
def test_wrong_reviews_get_400(edited, review):
    job, _ = edited

    with pytest.raises(HTTPException) as error:
        web.api_review(job.id, review)

    assert error.value.status_code == 400


def test_a_running_job_gets_409(edited):
    job, _ = edited
    job.status = "running"
    jobs.save(job)

    with pytest.raises(HTTPException) as error:
        web.api_review(job.id, web.Review(verdict="approve"))

    assert error.value.status_code == 409


def test_undo_on_the_web_is_a_bad_result(edited):
    job, app = edited

    web.api_undo(job.id, web.Undo())

    assert jobs.load(job.id).signals == ["undone"]
    assert app.read_text() == "old\n"
