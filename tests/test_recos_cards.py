"""Tests for Recos stage 2: saved cards, handled cards, memory cards, Set up and the weekly action.

No agent runs: Set up of a memory card only adds a queued lesson job.
"""

from datetime import datetime, timedelta

import pytest

from agentic_os import job_review, jobs, leads, recos, routines, today

NOW = datetime(2026, 11, 6, 12, 0)


@pytest.fixture
def office(tmp_path, monkeypatch, temp_routines):
    """Temporary jobs, routines, Recos file and leads (one lead: pip)."""
    monkeypatch.setattr(leads, "LEADS_DIR", tmp_path / "leads")
    role = {"area": "quick tasks", "model": "haiku", "effort": "low", "folder": str(tmp_path),
            "tools": ["Read"], "keywords": [], "instructions": "Be short."}  # like tests/test_lessons.py
    monkeypatch.setattr(leads, "roster", lambda: {"pip": role})
    return temp_routines


def repeated_jobs(count=3, task="Summarize the new emails from the client into a short list", now=NOW):
    for number in range(count):
        job = jobs.add(f"{task} {'please ' * number}", "haiku", use_memory=False)
        job.created_at = (now - timedelta(days=number + 1)).isoformat()
        jobs.save(job)


def corrected(note, verdict="rejected", lead="pip", days_ago=1, **fields):
    """Save a done job of the lead with a review note (a correction). Return it."""
    job = jobs.add("Write the report.", "haiku", use_memory=False, lead=lead)
    job.status = "done"
    job.review = {"verdict": verdict, "note": note, "at": (NOW - timedelta(days=days_ago)).isoformat()}
    for key, value in fields.items():
        setattr(job, key, value)
    jobs.save(job)
    return job


def routine_card():
    return next(found for found in recos.load_recos()["cards"] if found["kind"] == "routine")


def test_refresh_saves_the_cards_and_tells_the_new_ones(office):
    repeated_jobs()

    saved, new = recos.refresh(NOW)

    assert [found["kind"] for found in saved["cards"]] == ["routine"]
    assert new == [saved["cards"][0]["id"]]
    assert recos.load_recos()["cards"] == saved["cards"]
    assert recos.refresh(NOW)[1] == []  # the same card again is not new


def test_a_dismissed_card_does_not_come_back(office):
    repeated_jobs()
    found = recos.refresh(NOW)[0]["cards"][0]

    recos.dismiss(found["id"])

    assert recos.load_recos()["cards"] == []
    assert recos.refresh(NOW)[0]["cards"] == []
    with pytest.raises(ValueError, match="No such card"):
        recos.dismiss(found["id"])


def test_the_same_correction_twice_makes_a_memory_card(office):
    first = corrected("Write the dates in the Bulgarian format, day first", days_ago=3)
    second = corrected("Use the Bulgarian date format: the day first", verdict="changes", days_ago=1)
    corrected("Shorter answers, please, three lines at most", days_ago=2)  # a different correction

    [found] = recos.memory_cards()

    assert found["lead"] == "pip" and found["count"] == 2
    assert found["jobs"] == [second.id, first.id]  # the newest first
    assert "Bulgarian" in found["examples"][0]


def test_no_memory_card_for_one_correction_lesson_jobs_or_approvals(office):
    corrected("Write the dates in the Bulgarian format, day first")
    corrected("Write the dates in the Bulgarian format, day first", lesson_for={"job": "x", "signal": "rework"})
    corrected("Write the dates in the Bulgarian format, day first", verdict="approved")

    assert recos.memory_cards() == []


def test_a_new_correction_brings_a_handled_memory_card_back(office):
    corrected("Write the dates in the Bulgarian format, day first", days_ago=3)
    corrected("Use the Bulgarian date format: the day first", days_ago=2)
    old = recos.refresh(NOW)[0]["cards"][0]
    recos.dismiss(old["id"])

    corrected("Again: the Bulgarian date format, the day first", days_ago=1)

    # (The 3 jobs have the same task, so a routine card comes too: look at the memory card.)
    [found] = [card for card in recos.refresh(NOW)[0]["cards"] if card["kind"] == "memory"]
    assert found["count"] == 3 and found["id"] != old["id"]


def test_set_up_a_routine_card_writes_a_routine_with_a_cap(office):
    repeated_jobs()
    recos.refresh(NOW)
    found = routine_card()
    (office / f"{found['routine']['name']}.md").write_text("taken")  # the name is taken: a new one

    messages, started = recos.setup([found["id"]])

    name = found["routine"]["name"] + "-2"
    routine = routines.load(name)
    assert routine["max_week_usd"] == routines.DEFAULT_CAP and routine["model"] == "haiku"
    assert routine["schedule_text"] == found["routine"]["schedule"] and routine["mode"] == found["routine"]["mode"]
    assert routine["task"] == found["routine"]["task"]
    assert started == [] and f"Routine '{name}'" in messages[0]
    assert recos.load_recos()["cards"] == [] and found["id"] in recos.load_recos()["handled"]


def test_set_up_a_memory_card_adds_a_lesson_job(office):
    corrected("Write the dates in the Bulgarian format, day first", days_ago=3)
    newest = corrected("Use the Bulgarian date format: the day first", days_ago=1)
    [found] = recos.refresh(NOW)[0]["cards"]

    _, [lesson_job] = recos.setup([found["id"]])

    saved = jobs.load(lesson_job.id)
    assert saved.status == "queued" and saved.lead == "pip"
    assert saved.lesson_for == {"job": newest.id, "signal": "repeated"}
    assert "2 times" in saved.task and "Bulgarian format" in saved.task and "<lesson>" in saved.task
    # When it finishes, its <lesson> block waits as a draft, like any other lesson.
    saved.result = "<lesson>\nWrite dates day first (DD.MM.YYYY).\nWhy: three corrections.\n</lesson>"
    draft = jobs.save_lesson_draft(saved)
    assert draft["trigger"] == job_review.SIGNAL_WHY["repeated"]


def test_set_up_an_unknown_card(office):
    with pytest.raises(ValueError, match="No such card: nope"):
        recos.setup(["nope"])


def test_the_weekly_routine_action_refreshes_the_cards(office):
    repeated_jobs(now=datetime.now())  # the action looks back from the real clock

    assert routines.ACTIONS["recos"](3) == []  # no jobs, no cost
    assert len(recos.load_recos()["cards"]) == 1


def test_the_real_weekly_routine_file_is_valid(monkeypatch):
    monkeypatch.setattr(routines, "ROUTINES_DIR", routines.PROJECT_DIR / "routines")

    routine = routines.load("weekly-recos")

    assert routine["action"] == "recos" and routine["schedule_text"] == "weekly fri 09:00"


def test_today_counts_the_cards(office, temp_briefing):
    repeated_jobs()
    recos.refresh(NOW)

    assert {"kind": "recos", "count": 1} in today.today(NOW)["needs_you"]
