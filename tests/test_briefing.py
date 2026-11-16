"""Tests for briefing.facts(): the exact facts for the morning briefing. No AI."""

import json
from datetime import datetime, timedelta

from agentic_os import briefing, jobs, memory

NOW = datetime(2026, 11, 2, 8, 0)  # a Monday morning


def save_job(job_id, status, created, cost=0.0, **fields):
    jobs.save(jobs.Job(id=job_id, task=fields.pop("task", f"Task {job_id}"), status=status,
                       created_at=created.isoformat(), cost=cost, **fields))


def test_a_quiet_morning(temp_briefing):
    assert briefing.facts(NOW) == (
        "Facts for Monday 2026-11-02 08:00\n\n"
        "Questions for you: none.\n\n"
        "Jobs in the last 24 hours: none; cost $0.0000\n\n"
        "Skills: nothing waits.\n\n"
        "Routines: none today.\n\n"
        "Memory: no changes, nothing new in raw/, no health problems.\n\n"
        "Usage limits: no data yet."
    )


def test_jobs_of_the_last_24_hours_and_the_queue(temp_briefing):
    save_job("a", "done", NOW - timedelta(hours=3), cost=0.02)
    save_job("b", "failed", NOW - timedelta(hours=2), cost=0.01, error="the check failed")
    save_job("c", "done", NOW - timedelta(days=2), cost=5.0)  # too old: not counted
    save_job("d", "queued", NOW - timedelta(days=3), routine="weekly-health")

    lines = briefing.jobs_part(jobs.all_jobs(), NOW - timedelta(days=1))

    assert lines == [
        "Jobs in the last 24 hours: 1 done, 1 failed; cost $0.0300",
        "- failed b: Task b (error: the check failed)",
        "Waiting in the queue: 1",
        "- d: routine weekly-health: Task d",
    ]


def test_questions_that_wait_the_oldest_first(temp_briefing):
    save_job("q1", "waiting", NOW - timedelta(hours=5),
             question={"text": "Which storage?", "options": [], "at": (NOW - timedelta(hours=4)).isoformat()})
    save_job("q2", "waiting", NOW - timedelta(hours=1),
             question={"text": "Which language?", "options": [], "at": (NOW - timedelta(minutes=20)).isoformat()})

    lines = briefing.questions_part(jobs.all_jobs(), NOW)

    assert lines == [
        "Questions for you: 2 (the agents wait; their other jobs may be held)",
        "- q1 waits 4.0 hours: Which storage?",
        "- q2 waits 20 min: Which language?",
    ]


def test_a_plan_that_waits_is_in_the_reminder(temp_briefing):
    save_job("p1", "waiting", NOW - timedelta(hours=1), task="Add a parser.",
             plan={"text": "1. Do it.", "estimate": 0.4, "status": "pending", "at": (NOW - timedelta(minutes=30)).isoformat()})

    assert briefing.questions_part(jobs.all_jobs(), NOW)[1] == (
        "- p1 waits 30 min: a plan ($0.40, a guess) for: Add a parser.")


def test_long_lists_are_cut(temp_briefing):
    for number in range(7):
        save_job(f"f{number}", "failed", NOW - timedelta(hours=1), error="boom")

    lines = briefing.jobs_part(jobs.all_jobs(), NOW - timedelta(days=1))

    assert len(lines) == 1 + briefing.MAX_ITEMS + 1
    assert lines[-1] == "- ... and 2 more"


def test_skills_that_wait(temp_briefing):
    (temp_briefing / "proposals").mkdir()
    (temp_briefing / "proposals" / "note.md").write_text("new version")
    (temp_briefing / "feedback").mkdir()
    (temp_briefing / "feedback" / "summarize.md").write_text(
        "# Feedback\n\n- [x] 2026-11-01 10:00  old\n- [ ] 2026-11-01 11:00  shorter\n")

    assert briefing.skills_part() == [
        "Skills:",
        "- a new version of the skill note waits: accept or reject it",
        "- the skill summarize has 1 open feedback (run calibrate)",
    ]


def test_routines_today_and_near_the_cap(temp_briefing, temp_routines):
    (temp_routines / "brief.md").write_text(
        "---\nschedule: daily 09:30\nmode: active\nmax_week_usd: 0.10\n---\n\nHi.\n")
    (temp_routines / "weekly.md").write_text(
        "---\nschedule: weekly fri 08:00\nmax_week_usd: 0.10\n---\n\nHi.\n")
    save_job("w", "done", NOW - timedelta(days=1), cost=0.09, routine="weekly")

    assert briefing.routines_part(NOW) == [
        "Routines:",
        "- brief runs today at 09:30 (active)",
        "- weekly used $0.0900 of its $0.10 weekly cap",
    ]


def test_memory_changes_new_sources_and_health(temp_briefing, temp_memory):
    memory.LOG_FILE.write_text(
        "# Memory log\n\n"
        "- 2026-10-30T10:00:00  created  wiki/a/old.md  (by you)\n"
        "- 2026-11-02T07:00:00  updated  wiki/a/new.md  (by job x)\n")
    memory.RAW_DIR.mkdir()
    (memory.RAW_DIR / "done.md").write_text("source")
    (memory.RAW_DIR / "fresh.md").write_text("source")
    save_job("i", "done", NOW - timedelta(days=5),
             task=jobs.INGEST_TASK.format(path=(memory.RAW_DIR / "done.md").resolve()))
    note = memory.WIKI_DIR / "a" / "new.md"  # a note, but no map: one health problem
    note.parent.mkdir(parents=True)
    note.write_text("---\ntitle: New\ndescription: d\n---\n\nText.\n")

    assert briefing.memory_part(NOW - timedelta(days=1)) == [
        "Memory changes in the last 24 hours: 1",
        "- updated wiki/a/new.md",
        "Raw sources not ingested yet: 1 (run: python -m agentic_os.jobs ingest-new)",
        "- fresh.md",
        "Memory health: 1 problems (run: python -m agentic_os.memory health)",
    ]


def test_usage_limits(temp_briefing):
    future = datetime.now().timestamp() + 3600
    (temp_briefing / "limits.json").write_text(json.dumps({
        "five_hour": {"utilization": 0.95, "resetsAt": future, "status": "allowed"},
        "old_window": {"utilization": 0.99, "resetsAt": 1000, "status": "allowed"},  # expired
    }))

    [line] = briefing.limits_part()

    assert line.startswith("Usage limits: five hour 95% (resets ")
    assert "old window" not in line
    assert line.endswith(". Jobs wait: five_hour limit at 95%, resets "
                         + datetime.fromtimestamp(future).strftime("%Y-%m-%d %H:%M"))
