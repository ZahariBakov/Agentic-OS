"""Tests for the message box on the web page (follow-up, stage 2).

We call the API functions directly, as plain Python functions. No agent runs:
web.pool.submit is replaced, so a job never starts after the test ends.
"""

import pytest
from fastapi import HTTPException

from agentic_os import job_store, jobs, web


@pytest.fixture
def submitted(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Replace web.pool.submit and use a temporary inbox. Return the jobs that would start."""
    monkeypatch.setattr(job_store, "INBOX_DIR", tmp_path / "inbox")
    started = []
    monkeypatch.setattr(web.pool, "submit", lambda function, job: started.append(job))
    return started


def saved_job(status):
    job = jobs.add("Write a poem.", "haiku", use_memory=False)
    job.status, job.session_id, job.result = status, "s1", "a poem"
    jobs.save(job)
    return job


def test_a_message_to_a_done_job_starts_it_again(submitted):
    job = saved_job("done")

    reply = web.api_follow_up(job.id, web.Message(text="Shorter, please."))

    assert reply["started"] and reply["job"]["status"] == "queued"
    assert [started.id for started in submitted] == [job.id]


def test_a_message_to_a_running_job_waits_in_the_inbox(submitted):
    job = saved_job("running")

    reply = web.api_follow_up(job.id, web.Message(text="Add a title."))

    assert not reply["started"] and submitted == []
    assert [message["text"] for message in web.api_job(job.id)["inbox"]] == ["Add a title."]


@pytest.mark.parametrize("status, text, code", [
    ("done", "  ", 400),
    ("interrupted", "Hi.", 409),
])
def test_wrong_messages_are_refused(submitted, status, text, code):
    job = saved_job(status)

    with pytest.raises(HTTPException) as error:
        web.api_follow_up(job.id, web.Message(text=text))

    assert error.value.status_code == code
    assert submitted == []


def test_a_message_to_an_unknown_job(submitted):
    with pytest.raises(HTTPException) as error:
        web.api_follow_up("nosuchjob", web.Message(text="Hi."))

    assert error.value.status_code == 404


def test_the_job_view_has_no_warning_for_a_fresh_session(submitted):
    job = saved_job("done")

    assert web.api_job(job.id)["cold_warning"] is None
