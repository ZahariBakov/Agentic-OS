"""Tests for follow-up messages: write to a job during or after its run.

No real agent runs: job_run.ask is replaced with a fake that records each call.
"""

import os
import time
from datetime import datetime

import pytest

from agentic_os import artifacts, changes, job_run, job_store, jobs, leads, limits


@pytest.fixture
def calls(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Replace the agent call, and move every file a run writes into tmp_path.

    Return the list of calls: {"prompt", "session"}. Each answer is "answer <n>".
    """
    monkeypatch.setattr(job_run, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(job_store, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(job_run, "DEFAULT_WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "artifacts" / "INDEX.md")
    monkeypatch.setattr(changes, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    made = []

    def ask(prompt, model, session_id=None, **options):
        made.append({"prompt": prompt, "session": session_id})
        return {"result": f"answer {len(made)}", "total_cost_usd": 0.01, "session_id": session_id or "s1"}

    monkeypatch.setattr(job_run, "ask", ask)
    return made


def finished_job():
    job = jobs.add("Write a poem.", model="haiku", use_memory=False)
    jobs.run_job(job)
    return jobs.load(job.id)


def test_a_message_after_the_job_continues_the_same_session(calls):
    job = finished_job()

    queued, start, warning = jobs.follow_up(job.id, "Make it shorter.")
    assert start and warning is None and queued.status == "queued"
    jobs.run_job(queued)

    assert len(calls) == 2
    assert calls[1]["session"] == "s1"  # the session of the first run
    assert "Make it shorter." in calls[1]["prompt"]
    saved = jobs.load(job.id)
    assert saved.status == "done" and saved.result == "answer 2"
    assert [(m["from"], m["text"]) for m in saved.messages] == [("agent", "answer 1"), ("user", "Make it shorter.")]
    assert saved.cost == pytest.approx(0.02)  # the cost adds up


def test_a_message_during_the_run_is_answered_in_the_same_run(calls, monkeypatch):
    job = jobs.add("Write a poem.", model="haiku", use_memory=False)
    fake = job_run.ask

    def ask_and_get_a_message(prompt, model, **options):
        if not calls:  # the user writes while the first call works
            _, start, _ = jobs.follow_up(job.id, "Add a title.")
            assert not start  # the job is running: the message waits in the inbox
        return fake(prompt, model, **options)

    monkeypatch.setattr(job_run, "ask", ask_and_get_a_message)
    jobs.run_job(job)

    assert len(calls) == 2 and "Add a title." in calls[1]["prompt"]
    assert jobs.load(job.id).result == "answer 2"


def test_a_message_before_the_first_run_goes_with_the_task(calls):
    job = jobs.add("Write a poem.", model="haiku", use_memory=False)
    jobs.follow_up(job.id, "About the sea.")

    jobs.run_job(job)

    assert len(calls) == 1
    assert "Write a poem." in calls[0]["prompt"] and "About the sea." in calls[0]["prompt"]


def test_a_message_at_the_very_end_runs_the_job_again(calls, monkeypatch):
    job = jobs.add("Write a poem.", model="haiku", use_memory=False)
    save_artifact = artifacts.save

    def save_and_get_a_message(finished):
        # After the last agent call, before the job's final save: the file still says "running".
        if len(calls) == 1:
            jobs.follow_up(job.id, "One more line.")
        save_artifact(finished)

    monkeypatch.setattr(artifacts, "save", save_and_get_a_message)
    jobs.run_job(job)

    assert len(calls) == 2 and "One more line." in calls[1]["prompt"]
    assert jobs.load(job.id).status == "done"


def test_a_follow_up_gets_new_tries_for_its_check(calls, tmp_path, monkeypatch):
    folder = tmp_path / "project"
    folder.mkdir()
    job = jobs.add("Fix it.", model="haiku", use_memory=False, folder=folder, check="true")
    jobs.run_job(job)
    saved = jobs.load(job.id)
    saved.tries = job_run.MAX_TRIES  # an old job that used all its tries
    jobs.save(saved)
    results = [(1, "1 failed"), (0, "ok")]
    monkeypatch.setattr(job_run, "run_check", lambda running: results.pop(0))

    queued, _, _ = jobs.follow_up(job.id, "Also add a test.")
    jobs.run_job(queued)

    assert len(calls) == 3  # the first run, the follow-up, and one fix
    assert jobs.load(job.id).status == "done"


def test_a_cold_session_gives_a_warning(calls):
    job = finished_job()
    old = datetime.fromtimestamp(time.time() - leads.CACHE_TTL - 60).isoformat(timespec="milliseconds")
    jobs.update(job.id, finished_at=old)

    _, _, warning = jobs.follow_up(job.id, "Again.")

    assert "cold" in warning


def test_a_review_save_does_not_make_a_cold_session_warm(calls):
    """Review 2, item 23: the idle time is from the last run, not from the file's last save."""
    job = finished_job()
    old = datetime.fromtimestamp(time.time() - leads.CACHE_TTL - 60).isoformat(timespec="milliseconds")
    jobs.update(job.id, finished_at=old)
    jobs.approve(job.id)  # saves the job file now

    assert "cold" in jobs.cold_warning(jobs.load(job.id))


@pytest.mark.parametrize("status, text, error", [
    ("interrupted", "Hi.", "resume it first"),
    ("done", "   ", "empty"),
])
def test_wrong_follow_ups_are_refused(calls, status, text, error):
    job = jobs.add("Write a poem.", model="haiku", use_memory=False)
    job.status = status
    jobs.save(job)

    with pytest.raises(ValueError, match=error):
        jobs.follow_up(job.id, text)


def test_a_lead_with_a_new_session_gets_the_job_again(calls, tmp_path, monkeypatch):
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are Pip.")
    monkeypatch.setattr(leads, "prepare", lambda name: ("s-new", prompt_file, "resume session"))
    monkeypatch.setattr(leads, "roster", lambda: {"pip": {"tools": ["Read"]}})
    monkeypatch.setattr(leads, "finish", lambda name, reply: reply["result"])
    job = jobs.add("Write a poem.", model="haiku", use_memory=False)
    job.lead, job.folder = "pip", str(tmp_path)
    job.status, job.session_id, job.result = "done", "s-old", "the old poem"
    jobs.save(job)

    queued, _, _ = jobs.follow_up(job.id, "Make it rhyme.")
    jobs.run_job(queued)

    assert calls[0]["session"] == "s-new"  # the lead's current session
    assert "the old poem" in calls[0]["prompt"] and "Make it rhyme." in calls[0]["prompt"]
