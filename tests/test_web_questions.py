"""Tests for questions on the web page (questions, stage 2).

We call the API functions directly. No agent runs: web.pool.submit is replaced.
"""

import pytest

from agentic_os import job_store, jobs, web


@pytest.fixture
def submitted(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Replace web.pool.submit and use a temporary inbox. Return the jobs that would start."""
    monkeypatch.setattr(job_store, "INBOX_DIR", tmp_path / "inbox")
    started = []
    monkeypatch.setattr(web.pool, "submit", lambda function, job: started.append(job))
    return started


def waiting_job():
    job = jobs.add("Save the data.", "haiku", use_memory=False)
    job.status, job.session_id, job.lead = "waiting", "s1", "pip"
    job.question = {"text": "Which storage?", "options": ["SQLite", "JSON"], "at": jobs.now()}
    jobs.save(job)
    return job


def test_the_list_and_the_view_show_the_question(submitted):
    job = waiting_job()

    summary = next(summary for summary in web.jobs_list() if summary["id"] == job.id)

    assert summary["question"]["options"] == ["SQLite", "JSON"]
    assert web.api_job(job.id)["question"]["text"] == "Which storage?"


def test_an_option_click_answers_the_question(submitted):
    job = waiting_job()

    reply = web.api_follow_up(job.id, web.Message(text="SQLite"))

    assert reply["started"] and [started.id for started in submitted] == [job.id]
    assert jobs.read_inbox(job.id)[0]["answer"] is True  # the agent gets "The user answered..."


def test_stop_on_a_waiting_job_starts_the_held_jobs(submitted):
    job = waiting_job()
    held = jobs.add("Write the docs.", "haiku", use_memory=False)
    held.lead, held.held_for = "pip", job.id
    jobs.save(held)

    web.api_stop(job.id)

    assert jobs.load(job.id).status == "stopped"
    assert [started.id for started in submitted] == [held.id]
