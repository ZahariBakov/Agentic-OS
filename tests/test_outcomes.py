"""Tests for the cost per successful job (outcomes.py). Job files only, no agent."""

from datetime import datetime

import pytest

from agentic_os import jobs, outcomes

NOW = datetime(2026, 11, 5, 12, 0)


def ended(job_id, status="done", cost=0.10, lead=None, model="haiku", signals=(), review=None, day=4):
    job = jobs.Job(id=job_id, task=f"Task {job_id}", status=status, cost=cost, lead=lead, model=model,
                   signals=list(signals), review=review,
                   created_at=f"2026-11-0{day}T10:00:00", finished_at=f"2026-11-0{day}T10:30:00")
    jobs.save(job)
    return job


def test_cost_per_success_counts_the_failures_too(temp_jobs):
    ended("a1")
    ended("a2")
    ended("a3", status="failed")
    ended("a4", signals=["rework"])  # done, but the user asked for changes
    jobs.save(jobs.Job(id="a5", task="still running", status="running", created_at="2026-11-04T10:00:00"))

    overall = outcomes.report(now=NOW)["overall"]

    assert (overall["jobs"], overall["succeeded"], overall["failed"], overall["reworked"]) == (4, 2, 1, 1)
    assert overall["cost_per_job"] == pytest.approx(0.10)
    assert overall["cost_per_success"] == pytest.approx(0.20)  # $0.40 for 2 good jobs
    assert overall["median_minutes"] == pytest.approx(30)


def test_rejected_and_undone_jobs_are_not_successes(temp_jobs):
    ended("b1", review={"verdict": "rejected", "note": "wrong", "at": ""}, signals=["rejected"])
    ended("b2", signals=["undone"])
    ended("b3", review={"verdict": "approved", "note": "", "at": ""})

    result = outcomes.report(now=NOW)

    assert result["overall"]["succeeded"] == 1
    assert sorted(job["why"] for job in result["went_wrong"]) == ["changes undone", "rejected"]


def test_groups_by_lead_and_model_and_old_jobs_are_left_out(temp_jobs):
    ended("c1", lead="Ada", model="sonnet")
    ended("c2", lead="Ada", model="sonnet", status="failed")
    ended("c3")
    jobs.save(jobs.Job(id="c4", task="old", status="done", cost=5.0,
                       created_at="2026-09-01T10:00:00", finished_at="2026-09-01T10:10:00"))

    result = outcomes.report(now=NOW)

    by_lead = {row["name"]: row for row in result["by_lead"]}
    assert by_lead["Ada"]["succeeded"] == 1 and by_lead["Ada"]["jobs"] == 2
    assert by_lead["(one-time agent)"]["jobs"] == 1  # c4 is older than 30 days
    assert [row["name"] for row in result["by_model"]] == ["sonnet", "haiku"]


def test_no_jobs(temp_jobs):
    result = outcomes.report(now=NOW)
    assert result["overall"]["cost_per_success"] is None
    assert outcomes.text(result) == "No jobs ended in the last 30 days."
