"""Tests for the Resume button (recovery, stage 2).

We call the API functions directly, as plain Python functions. No agent runs:
web.pool.submit is replaced, so a job never starts after the test ends.
"""

import pytest
from fastapi import HTTPException

from agentic_os import jobs, web


@pytest.fixture
def submitted(monkeypatch, temp_jobs, temp_memory):
    """Replace web.pool.submit. Return the list of jobs that would start."""
    started = []
    monkeypatch.setattr(web.pool, "submit", lambda function, job: started.append(job))
    return started


def saved_job(status):
    job = jobs.add("Write hello.txt.", "haiku", use_memory=False)
    job.status = status
    job.session_id = "s-old"
    job.interrupted_why = "the server stopped while the job was running"
    jobs.save(job)
    return job


def test_resume_queues_and_starts_an_interrupted_job(submitted):
    job = saved_job("interrupted")

    summary = web.api_resume(job.id)

    assert summary["status"] == "queued"
    assert [started.id for started in submitted] == [job.id]
    assert jobs.load(job.id).resumes == 1


@pytest.mark.parametrize("status", ["done", "running", "queued"])
def test_only_an_interrupted_job_can_be_resumed(submitted, status):
    job = saved_job(status)

    with pytest.raises(HTTPException) as error:
        web.api_resume(job.id)

    assert error.value.status_code == 409
    assert submitted == []


def test_resume_of_an_unknown_job(submitted):
    with pytest.raises(HTTPException) as error:
        web.api_resume("nosuchjob")

    assert error.value.status_code == 404


def test_the_list_shows_why_a_job_was_interrupted(submitted):
    job = saved_job("interrupted")

    summary = next(summary for summary in web.jobs_list() if summary["id"] == job.id)

    assert summary["interrupted_why"] == job.interrupted_why
