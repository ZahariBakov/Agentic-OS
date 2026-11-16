"""Tests for routines: the schedule, tick, active and passive, and the cost cap.

No real agent runs: jobs.run_job is replaced with a fake that only records the call.
"""

from datetime import datetime, timedelta

import pytest

from agentic_os import job_run, jobs, memory, routines

# 2026-11-02 is a Monday.
MONDAY_9 = datetime(2026, 11, 2, 9, 0)


def write_routine(folder, name, schedule="daily 08:00", mode="passive", cap="0.50",
                  task="Say hello."):
    (folder / f"{name}.md").write_text(
        f"---\ndescription: test\nschedule: {schedule}\nmode: {mode}\n"
        f"model: haiku\nmax_week_usd: {cap}\n---\n\n{task}\n"
    )


@pytest.fixture
def fake_run(monkeypatch):
    """Replace jobs.run_job. Return the list of job ids that were 'run'."""
    ran = []
    monkeypatch.setattr(jobs, "run_job", lambda job: ran.append(job.id))
    return ran


# --- the schedule (pure functions) ---

@pytest.mark.parametrize("text, expected", [
    ("daily 08:00", {"kind": "daily", "hour": 8, "minute": 0}),
    ("weekly sun 18:30", {"kind": "weekly", "day": 6, "hour": 18, "minute": 30}),
    ("every 6h", {"kind": "every", "step": timedelta(hours=6)}),
    ("every 30m", {"kind": "every", "step": timedelta(minutes=30)}),
    ("  Daily   08:00 ", {"kind": "daily", "hour": 8, "minute": 0}),
])
def test_parse_good_schedules(text, expected):
    assert routines.parse_schedule(text) == expected


@pytest.mark.parametrize("text", ["", "daily", "daily 8:00", "daily 25:00", "weekly xyz 08:00",
                                  "every 0h", "every 5d", "hourly"])
def test_parse_bad_schedules(text):
    with pytest.raises(ValueError, match="Unknown schedule"):
        routines.parse_schedule(text)


@pytest.mark.parametrize("schedule, after, expected", [
    # daily: later today, or tomorrow when today's time has passed
    ("daily 10:00", MONDAY_9, datetime(2026, 11, 2, 10, 0)),
    ("daily 08:00", MONDAY_9, datetime(2026, 11, 3, 8, 0)),
    ("daily 09:00", MONDAY_9, datetime(2026, 11, 3, 9, 0)),  # exactly now -> the next one
    # weekly: forward to the right day, or a week later
    ("weekly wed 08:00", MONDAY_9, datetime(2026, 11, 4, 8, 0)),
    ("weekly mon 08:00", MONDAY_9, datetime(2026, 11, 9, 8, 0)),
    ("weekly sun 18:00", MONDAY_9, datetime(2026, 11, 8, 18, 0)),
    # every: a fixed step after the last run
    ("every 6h", MONDAY_9, datetime(2026, 11, 2, 15, 0)),
])
def test_next_run(schedule, after, expected):
    assert routines.next_run(routines.parse_schedule(schedule), after) == expected


# --- tick ---

def test_new_routine_waits_for_its_first_time(temp_routines, fake_run):
    write_routine(temp_routines, "hello")

    assert routines.tick(MONDAY_9) == []  # first seen at 09:00, after today's 08:00
    assert jobs.all_jobs() == []
    assert routines.tick(MONDAY_9 + timedelta(hours=22)) == []  # Tuesday 07:00: not yet
    assert routines.tick(MONDAY_9 + timedelta(hours=23)) == ["hello"]  # Tuesday 08:00


def test_passive_routine_adds_a_queued_job(temp_routines, fake_run):
    write_routine(temp_routines, "hello", mode="passive")
    routines.tick(MONDAY_9)

    routines.tick(MONDAY_9 + timedelta(days=1))

    [job] = jobs.all_jobs()
    assert (job.status, job.routine, job.task, job.model) == ("queued", "hello", "Say hello.", "haiku")
    assert fake_run == []  # passive: nothing runs by itself
    assert routines.load_state()["hello"]["last_job"] == job.id


def test_active_routine_runs_its_job(temp_routines, fake_run):
    write_routine(temp_routines, "hello", mode="active")
    routines.tick(MONDAY_9)

    routines.tick(MONDAY_9 + timedelta(days=1))

    [job] = jobs.all_jobs()
    assert fake_run == [job.id]


def test_due_routine_starts_only_once(temp_routines, fake_run):
    write_routine(temp_routines, "hello")
    routines.tick(MONDAY_9)
    tuesday = MONDAY_9 + timedelta(days=1)

    routines.tick(tuesday)
    routines.tick(tuesday)
    routines.tick(tuesday + timedelta(minutes=10))

    assert len(jobs.all_jobs()) == 1


def test_missed_runs_start_once(temp_routines, fake_run):
    write_routine(temp_routines, "hello")
    routines.tick(MONDAY_9)

    routines.tick(MONDAY_9 + timedelta(days=5))  # the computer was off for 5 days

    assert len(jobs.all_jobs()) == 1


def test_routine_with_a_mistake_is_skipped(temp_routines, fake_run, capsys):
    write_routine(temp_routines, "broken", schedule="sometimes")
    write_routine(temp_routines, "hello")
    routines.tick(MONDAY_9)

    assert routines.tick(MONDAY_9 + timedelta(days=1)) == ["hello"]
    assert "Skipped routine broken.md" in capsys.readouterr().out


# --- the cost cap ---

def old_job(routine, cost, created):
    """Save a finished job of a routine with this cost and creation time."""
    jobs.save(jobs.Job(id=f"old-{cost}", task="x", status="done", cost=cost,
                       routine=routine, created_at=created.isoformat()))


def test_cost_cap_skips_the_routine(temp_routines, fake_run, capsys):
    write_routine(temp_routines, "hello", cap="0.10")
    old_job("hello", 0.12, MONDAY_9 - timedelta(days=2))

    job = routines.run_now("hello", MONDAY_9)

    assert job is None
    assert len(jobs.all_jobs()) == 1  # only the old job
    assert "Skipped hello" in capsys.readouterr().out
    assert "hello" not in routines.load_state()  # a skipped run does not move the schedule


def test_cost_cap_counts_only_the_last_7_days(temp_routines, fake_run):
    write_routine(temp_routines, "hello", cap="0.10")
    old_job("hello", 0.12, MONDAY_9 - timedelta(days=8))  # too old to count
    old_job("other", 0.50, MONDAY_9 - timedelta(days=1))  # another routine

    [job] = routines.run_now("hello", MONDAY_9)

    assert job.routine == "hello"


# --- for the web server ---

def test_active_routine_uses_the_given_runner(temp_routines, fake_run):
    write_routine(temp_routines, "hello", mode="active")
    routines.tick(MONDAY_9)
    given = []

    routines.tick(MONDAY_9 + timedelta(days=1), run=lambda job: given.append(job.id))

    [job] = jobs.all_jobs()
    assert given == [job.id]  # the web server's runner got the job
    assert fake_run == []  # jobs.run_job was not called


def test_summary_is_ready_for_json(temp_routines, fake_run):
    write_routine(temp_routines, "hello", schedule="weekly sun 18:00", cap="0.10")
    routines.tick(MONDAY_9)
    old_job("hello", 0.03, MONDAY_9 - timedelta(days=1))

    [row] = routines.summary(MONDAY_9)

    assert row == {
        "name": "hello", "description": "test", "schedule": "weekly sun 18:00",
        "mode": "passive", "what": "task", "next_run": "2026-11-08T18:00",
        "last_job": None, "week_cost": 0.03, "max_week_usd": 0.10,
    }


def test_task_blanks_are_filled_by_python(temp_routines, fake_run):
    write_routine(temp_routines, "health", task="Folder {memory_dir}.\nReport:\n{memory_health}\nKeep {this}.")
    note = memory.WIKI_DIR / "a" / "one.md"  # a note, but no map: one problem
    note.parent.mkdir(parents=True)
    note.write_text("---\ntitle: One\ndescription: d\n---\n\nText.\n")

    [job] = routines.run_now("health", MONDAY_9)

    report = "- MEMORY.md: the map is out of date (run: python -m agentic_os.memory index)"
    assert job.task == f"Folder {memory.MEMORY_DIR}.\nReport:\n{report}\nKeep {{this}}."


# --- found in the code review ---

def test_a_broken_routine_does_not_stop_the_others(temp_routines, fake_run):
    (temp_routines / "a-broken.md").write_text(
        "---\nschedule: daily 08:00\nmode: passive\nskill: gone-skill\nmax_week_usd: 0.50\n---\n")
    write_routine(temp_routines, "b-hello")
    routines.tick(MONDAY_9 - timedelta(days=1))  # both are seen for the first time

    started = routines.tick(MONDAY_9)  # a-broken raises SystemExit (no such skill)

    assert started == ["b-hello"]


def test_a_last_run_in_the_future_counts_as_now(temp_routines, fake_run):
    write_routine(temp_routines, "hello")
    routines.record_run("hello", MONDAY_9 + timedelta(days=300))  # the clock was wrong once

    assert routines.tick(MONDAY_9) == []  # the schedule starts again from now
    assert routines.tick(MONDAY_9 + timedelta(days=1)) == ["hello"]  # the next 08:00 has passed


def test_record_run_keeps_what_others_wrote(temp_routines):
    routines.record_run("one", MONDAY_9)
    routines.record_run("two", MONDAY_9)  # loads the file again: "one" stays

    assert set(routines.load_state()) == {"one", "two"}


def test_the_cap_is_checked_again_when_a_queued_job_starts(temp_routines, monkeypatch):
    write_routine(temp_routines, "hello", cap="0.10")
    first = routines.run_now("hello", datetime.now())[0]
    second = routines.run_now("hello", datetime.now())[0]  # passive: both wait, both cost $0 now
    first.cost, first.status = 0.20, "done"  # the first one ran and spent more than the cap
    jobs.save(first)
    monkeypatch.setattr(job_run, "ask", lambda *args, **options: pytest.fail("must not start"))

    jobs.run_job(second)

    assert jobs.load(second.id).status == "queued"


def test_jobs_of_one_routine_started_together_run_one_at_a_time(temp_routines, tmp_path, monkeypatch):
    # Each job costs $0.08, the cap is $0.10. Started at the same time, all three would pass
    # the cap check (a running job costs $0 until it ends). One at a time, the third waits.
    from concurrent.futures import ThreadPoolExecutor
    from agentic_os import artifacts, limits
    monkeypatch.setattr(job_run, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "artifacts" / "INDEX.md")
    write_routine(temp_routines, "hello", cap="0.10")
    added = [routines.run_now("hello", datetime.now())[0] for _ in range(3)]  # passive: queued
    calls = []

    def ask(prompt, model, extra_args=(), **options):
        calls.append(prompt)
        return {"result": "Hello.", "total_cost_usd": 0.08, "session_id": f"s{len(calls)}"}

    monkeypatch.setattr(job_run, "ask", ask)

    with ThreadPoolExecutor(max_workers=3) as pool:  # like `jobs run` or the web pool
        list(pool.map(jobs.run_job, added))

    statuses = sorted(jobs.load(job.id).status for job in added)
    assert len(calls) == 2 and statuses == ["done", "done", "queued"]
