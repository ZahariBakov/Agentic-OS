"""Tests for today.today(): the facts of the Today panel. No AI, no agent.

Also the conversation: it shows the user's words, while the agent gets the full prompt.
"""

from datetime import datetime, timedelta

import pytest

from agentic_os import job_store, jobs, leads, today, web

NOW = datetime(2026, 11, 4, 12, 0)


@pytest.fixture
def office(tmp_path, monkeypatch, temp_briefing):
    """Jobs, memory, skill proposals and leads in tmp_path. Return a function that saves a job."""
    monkeypatch.setattr(leads, "LEADS_DIR", tmp_path / "leads")
    monkeypatch.setattr(leads, "roster", lambda: {"pip": {"area": "quick tasks"}})
    monkeypatch.setattr(job_store, "INBOX_DIR", tmp_path / "inbox")

    def save_job(job_id, status, minutes_ago=0, **fields):
        created = (NOW - timedelta(minutes=minutes_ago)).isoformat()
        jobs.save(jobs.Job(id=job_id, task=fields.pop("task", f"Task {job_id}"), status=status,
                           created_at=created, **fields))
    return save_job


def at(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def test_a_quiet_day(office):
    facts = today.today(NOW)

    assert facts["needs_you"] == [] and facts["working"] == [] and facts["finished"] == []
    assert facts["not_reviewed"] == 0 and facts["finished_cost"] == 0


def test_what_needs_you_the_oldest_first(office):
    office("q", "waiting", question={"text": "Which language?", "options": ["English"], "at": at(10)})
    office("p", "waiting", plan={"text": "1. Do it.", "estimate": 0.4, "status": "pending", "at": at(30)})
    office("i", "interrupted", minutes_ago=60, interrupted_why="the server stopped")
    office("a", "waiting", plan={"text": "1. Do it.", "estimate": 0.4, "status": "approved", "at": at(5)})
    leads.add_draft("pip", "Ask first.", "", "q", "rework")

    needs = today.today(NOW)["needs_you"]

    assert [(item["kind"], item.get("job")) for item in needs] == [
        ("interrupted", "i"), ("plan", "p"), ("question", "q"), ("lessons", None)]
    assert needs[2]["options"] == ["English"] and needs[3]["count"] == 1


def test_working_and_finished(office):
    office("r", "running", cost=0.02)
    office("q2", "queued")
    office("d1", "done", minutes_ago=200, finished_at=at(5), cost=0.01, result="First line.\nSecond.")
    office("d2", "done", minutes_ago=100, finished_at=at(50), cost=0.02, review={"verdict": "approved"})
    office("l", "done", minutes_ago=90, finished_at=at(40), cost=0.03, lesson_for={"job": "d2", "signal": "rework"})
    office("old", "done", minutes_ago=3000, cost=5.0)  # no finished_at, created 2 days ago: not today

    facts = today.today(NOW)

    assert [job["job"] for job in facts["working"]] == ["r"] and facts["queued"] == 1
    assert [job["job"] for job in facts["finished"]] == ["d1", "l", "d2"]  # the newest first
    assert facts["finished"][0]["preview"] == "First line."
    assert facts["not_reviewed"] == 1  # d1: d2 is approved, a lesson job needs no review
    assert facts["finished_cost"] == pytest.approx(0.06)


def test_the_web_api(office):
    office("q", "waiting", question={"text": "Which language?", "options": [], "at": at(10)})

    assert web.api_today()["needs_you"][0]["job"] == "q"


def test_the_conversation_shows_your_words_for_a_plan_change(office):
    office("p", "waiting", session_id="s1",
           plan={"text": "1. Do it.", "estimate": 0.4, "status": "pending", "at": at(30)})

    jobs.decide_plan("p", "change", "Skip step 3.")

    [message] = jobs.read_inbox("p")
    assert message["note"] == "Skip step 3." and "approved your plan with these changes" in message["text"]


def test_a_plain_approve_shows_a_short_note(office):
    office("p", "waiting", session_id="s1",
           plan={"text": "1. Do it.", "estimate": 0.4, "status": "pending", "at": at(30)})

    jobs.decide_plan("p", "approve")

    assert jobs.read_inbox("p")[0]["note"] == "Approved the plan."



def test_a_question_item_carries_the_cold_warning(temp_jobs, monkeypatch):
    """Review 2, item 22: the one-click answers on Today must ask first, like the job view."""
    job = jobs.add("Write a poem.", model="haiku", use_memory=False)
    jobs.update(job.id, status="waiting", session_id="s1",
                question={"text": "Short or long?", "options": ["Short", "Long"], "at": jobs.now()})
    monkeypatch.setattr(jobs, "cold_warning", lambda found: "cold!")

    [item] = [item for item in today.needs_you(jobs.all_jobs()) if item["kind"] == "question"]

    assert item["cold_warning"] == "cold!"
