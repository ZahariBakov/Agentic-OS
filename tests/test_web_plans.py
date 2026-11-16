"""Tests for plans on the web page (plan with a cost, stage 2).

We call the API functions directly. No agent runs: web.pool.submit is replaced.
"""

import pytest
from fastapi import HTTPException

from agentic_os import job_store, jobs, web


@pytest.fixture
def submitted(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Replace web.pool.submit; temporary inbox and settings. Return the jobs that would start."""
    monkeypatch.setattr(job_store, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(job_store, "SETTINGS_FILE", tmp_path / "settings.json")
    started = []
    monkeypatch.setattr(web.pool, "submit", lambda function, job: started.append(job))
    return started


def waiting_plan(lead=None):
    job = jobs.add("Add a parser.", "sonnet", use_memory=False)
    job.status, job.session_id, job.lead = "waiting", "s1", lead
    job.plan = {"text": "1. Add it.", "estimate": 0.4, "status": "pending", "at": jobs.now()}
    jobs.save(job)
    return job


def test_the_form_can_ask_for_a_plan(submitted):
    summary = web.api_add_job(web.NewJob(task="Add a parser.", model="sonnet", memory=False, plan=True))

    assert jobs.load(summary["id"]).plan_first


def test_the_view_shows_the_plan_and_the_cost_history(submitted):
    job = waiting_plan()

    view = web.api_job(job.id)

    assert view["plan"]["estimate"] == 0.4
    assert "no cost history" in view["cost_history"]


def test_approve_starts_the_work(submitted):
    job = waiting_plan()

    reply = web.api_plan(job.id, web.PlanDecision(decision="approve"))

    assert reply["started"] and [started.id for started in submitted] == [job.id]
    assert reply["job"]["plan"]["status"] == "approved"


def test_reject_stops_the_job_and_starts_the_held_jobs(submitted):
    job = waiting_plan(lead="pip")
    held = jobs.add("Write the docs.", "haiku", use_memory=False)
    held.lead, held.held_for = "pip", job.id
    jobs.save(held)

    reply = web.api_plan(job.id, web.PlanDecision(decision="reject", note="Too big."))

    assert not reply["started"] and reply["job"]["status"] == "stopped"
    assert [started.id for started in submitted] == [held.id]


def test_a_message_box_send_changes_the_plan(submitted):
    job = waiting_plan()

    reply = web.api_follow_up(job.id, web.Message(text="Skip the tests."))

    assert reply["started"] and jobs.load(job.id).plan["status"] == "changed"


@pytest.mark.parametrize("decision, note, code", [
    ("maybe", "", 400),
    ("change", "", 400),  # a change needs the changes
])
def test_wrong_decisions(submitted, decision, note, code):
    job = waiting_plan()

    with pytest.raises(HTTPException) as error:
        web.api_plan(job.id, web.PlanDecision(decision=decision, note=note))

    assert error.value.status_code == code


def test_no_plan_waits_gets_409(submitted):
    job = jobs.add("Say hi.", "haiku", use_memory=False)

    with pytest.raises(HTTPException) as error:
        web.api_plan(job.id, web.PlanDecision(decision="approve"))

    assert error.value.status_code == 409


def test_the_auto_approve_setting(submitted):
    assert web.api_settings()["auto_approve_plans_usd"] == 0

    assert web.api_save_settings(web.Settings(auto_approve_plans_usd=0.25))["auto_approve_plans_usd"] == 0.25
    with pytest.raises(HTTPException) as error:
        web.api_save_settings(web.Settings(auto_approve_plans_usd=-1))
    assert error.value.status_code == 400


def test_the_page_sets_when_jobs_get_a_workspace(submitted):
    assert web.api_settings()["isolate_jobs"] == "always"

    saved = web.api_save_settings(web.Settings(auto_approve_plans_usd=0, isolate_jobs="shared"))

    assert saved["isolate_jobs"] == "shared"
    with pytest.raises(HTTPException):
        web.api_save_settings(web.Settings(auto_approve_plans_usd=0, isolate_jobs="sometimes"))
