"""Review 2, step 15: reports, routines and Recos."""

from datetime import datetime, timedelta

import pytest

from agentic_os import briefing, feedback, jobs, leads, outcomes, recos, routines, skills

NOW = datetime(2026, 11, 6, 12, 0)


def test_lesson_jobs_are_not_successes(temp_jobs):
    """Item 42: 2 rejected jobs + their 2 lessons looked like 50% success."""
    for number in range(2):
        jobs.save(jobs.Job(id=f"bad{number}", task="x", status="done", cost=0.1, signals=["rejected"],
                           created_at="2026-11-05T10:00:00", finished_at="2026-11-05T10:10:00"))
        jobs.save(jobs.Job(id=f"les{number}", task="lesson", status="done", cost=0.01,
                           lesson_for={"job": f"bad{number}", "signal": "rejected"},
                           created_at="2026-11-05T10:20:00", finished_at="2026-11-05T10:21:00"))

    overall = outcomes.report(now=NOW)["overall"]

    assert overall["jobs"] == 2 and overall["succeeded"] == 0


def test_the_weekly_cap_counts_a_job_by_when_it_ran(temp_routines):
    """Item 43: a passive job queued 9 days ago that ran yesterday costs this week."""
    jobs.save(jobs.Job(id="late", task="x", status="done", cost=0.3, routine="hello",
                       created_at=(NOW - timedelta(days=9)).isoformat(),
                       finished_at=(NOW - timedelta(days=1)).isoformat()))

    assert routines.week_cost("hello", NOW) == pytest.approx(0.3)


def test_lesson_jobs_are_not_repeated_requests(temp_jobs, tmp_path):
    """Item 44: three lesson jobs in a week made a "make this a routine" card."""
    for number in range(3):
        job = jobs.add(f"Job {number} went wrong: the user rejected the result of the report task",
                       "haiku", use_memory=False, fields={"lesson_for": {"job": "x", "signal": "rejected"}})
        jobs.update(job.id, created_at=(NOW - timedelta(days=number + 1)).isoformat())

    assert recos.suggest(now=NOW, history=tmp_path / "none.jsonl") == []


def test_the_briefing_counts_a_job_by_when_it_finished(temp_briefing):
    """Item 77: an old job that failed today is news."""
    jobs.save(jobs.Job(id="old1", task="Old task", status="failed", error="boom",
                       created_at=(NOW - timedelta(days=3)).isoformat(),
                       finished_at=(NOW - timedelta(hours=2)).isoformat()))

    lines = briefing.jobs_part(jobs.all_jobs(), NOW - timedelta(days=1))

    assert "1 failed" in lines[0] and any("old1" in line for line in lines)


# --- Recos ---

@pytest.fixture
def office(tmp_path, monkeypatch, temp_routines):
    monkeypatch.setattr(leads, "LEADS_DIR", tmp_path / "leads")
    role = {"area": "quick tasks", "model": "haiku", "effort": "low", "folder": str(tmp_path),
            "tools": ["Read"], "keywords": [], "instructions": "Be short."}
    monkeypatch.setattr(leads, "roster", lambda: {"pip": role})
    monkeypatch.setattr(recos, "HISTORY_FILE", tmp_path / "none.jsonl")
    return temp_routines


def corrected(note, days_ago):
    job = jobs.add("Write the report.", "haiku", use_memory=False, lead="pip")
    jobs.update(job.id, status="done",
                review={"verdict": "rejected", "note": note, "at": (NOW - timedelta(days=days_ago)).isoformat()})


def test_every_group_of_corrections_gets_a_card(office):
    """Item 87: only the biggest group showed, and the others stayed hidden for good."""
    for days_ago in (1, 2, 3):
        corrected("Use the Bulgarian date format with the day first", days_ago)
    for days_ago in (4, 5):
        corrected("Write the totals in euros and not in leva please", days_ago)

    cards = [found for found in recos.memory_cards()]

    assert [found["count"] for found in cards] == [3, 2]


def test_a_handled_routine_card_does_not_come_back_with_new_words(office):
    """Item 88: one more similar request changed the top words, so the card got a new id."""
    for number in range(3):
        job = jobs.add(f"Summarize the new emails from the client into a short list {'now ' * number}",
                       "haiku", use_memory=False)
        jobs.update(job.id, created_at=(NOW - timedelta(days=number + 1)).isoformat())
    [found] = recos.refresh(NOW)[0]["cards"]
    recos.dismiss(found["id"])
    for number in range(3):  # the same request again, with other words that now lead the group
        job = jobs.add("Summarize client emails list today urgent urgent urgent", "haiku", use_memory=False)
        jobs.update(job.id, created_at=(NOW - timedelta(hours=number + 1)).isoformat())

    assert recos.refresh(NOW)[0]["cards"] == []


def test_a_request_with_pasted_text_is_not_a_routine(tmp_path, temp_jobs):
    """Item 89: the routine's task would point to text that is not there."""
    assert recos.request("Fix the bug in this log [Pasted text #2 +40 lines] please now", NOW, "x") is None


# --- routines ---

def write_routine(folder, name, schedule="daily 02:30", task="Say hello."):
    (folder / f"{name}.md").write_text(
        f"---\ndescription: test\nschedule: {schedule}\nmode: passive\nmodel: haiku\n---\n\n{task}\n")


def test_summer_time_end_does_not_run_a_routine_twice(temp_routines, monkeypatch):
    """Item 90: at 03:00 the clock goes back to 02:00, and 02:30 comes a second time."""
    monkeypatch.setattr(jobs, "run_job", lambda job: None)
    write_routine(temp_routines, "night")
    routines.tick(datetime(2026, 10, 24, 12, 0))  # first seen
    assert routines.tick(datetime(2026, 10, 25, 2, 30)) == ["night"]  # 02:30, the first time

    assert routines.tick(datetime(2026, 10, 25, 2, 15)) == []  # the clock went back
    assert routines.tick(datetime(2026, 10, 25, 2, 31)) == []  # 02:30 the second time: not again
    assert routines.tick(datetime(2026, 10, 26, 2, 30)) == ["night"]  # the next day as usual


def test_the_blanks_are_filled_when_the_job_starts(temp_routines, monkeypatch):
    """Item 91: a passive job that waited for days got an old report."""
    write_routine(temp_routines, "health", task="Check this: {memory_dir}")
    monkeypatch.setattr(routines, "render_task", lambda task: task.replace("{memory_dir}", "NEW"))

    assert routines.fresh_task("health") == "Check this: NEW"
    assert routines.fresh_task("gone") is None


def test_a_new_routine_with_the_name_of_a_deleted_one_waits(temp_routines, monkeypatch):
    """Item 92: the old state made the new routine run at the next tick."""
    monkeypatch.setattr(jobs, "run_job", lambda job: None)
    write_routine(temp_routines, "hello", schedule="daily 08:00")
    routines.tick(datetime(2026, 11, 1, 12, 0))
    (temp_routines / "hello.md").unlink()
    routines.tick(datetime(2026, 11, 3, 12, 0))

    write_routine(temp_routines, "hello", schedule="daily 08:00")

    assert routines.tick(datetime(2026, 11, 5, 12, 0)) == []  # new: it waits for 08:00 tomorrow


def test_an_absurd_every_value_is_a_clear_error():
    """Item 93: OverflowError."""
    with pytest.raises(ValueError, match="Unknown schedule"):
        routines.parse_schedule("every 99999999999999h")


# --- skills and feedback ---

def test_a_blank_inside_the_input_stays_as_written():
    """Item 95: the user's text "{memory_dir}" became a path."""
    skill = {"name": "x", "prompt": "Explain: {input}", "references": False}

    assert skills.render(skill, "what does {memory_dir} mean?") == "Explain: what does {memory_dir} mean?"


def test_accept_does_not_close_feedback_added_while_calibrate_worked(tmp_path, monkeypatch):
    """Item 94: the list was saved at the end of the run."""
    folder = tmp_path / "skills"
    folder.mkdir()
    (folder / "note.md").write_text("---\nname: note\ndescription: x\n---\n\nSave a note.\n")
    monkeypatch.setattr(feedback, "SKILLS_DIR", folder)
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", folder / "feedback")
    monkeypatch.setattr(feedback, "PROPOSALS_DIR", tmp_path / "proposals")
    (folder / "feedback").mkdir()
    (folder / "feedback" / "note.md").write_text(
        "# Feedback\n\n- [ ] 2026-11-06 10:00  Shorter titles.\n- [ ] 2026-11-06 10:30  Use dates.\n")

    feedback.save_proposals('<skill-file name="note">\n---\nnew\n</skill-file>', since="2026-11-06T10:05:00.000")
    feedback.accept("note")

    assert [item.split("  ", 1)[1] for item in feedback.open_items("note")] == ["Use dates."]
