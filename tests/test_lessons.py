"""Tests for lessons (review, stage 3): a bad result asks the lead for one line,
the line waits as a draft, and a kept lesson goes into the lead's fresh prompt.

No real agent runs: job_run.ask is replaced with a fake whose answers we choose.
"""

import pytest

from agentic_os import artifacts, changes, job_run, job_store, jobs, leads, limits

LESSON = """I edited the wrong file.
<lesson>
Read the task's file names twice before the first edit.
Why: I changed app.py, but the task named main.py.
</lesson>"""


@pytest.fixture
def lead_folder(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """A lead "pip" in tmp_path, without the real roster.json and data/leads/. Return its folder."""
    folder = tmp_path / "pip"
    folder.mkdir()
    role = {"area": "quick tasks", "model": "haiku", "effort": "low", "folder": str(folder),
            "tools": ["Read"], "keywords": [], "instructions": "Be short."}
    monkeypatch.setattr(leads, "roster", lambda: {"pip": role})
    monkeypatch.setattr(leads, "LEADS_DIR", tmp_path / "leads")
    return folder


@pytest.fixture
def agent(lead_folder, tmp_path, monkeypatch):
    """Replace the agent call. Return (calls, answers): put the next answers in `answers`."""
    monkeypatch.setattr(job_run, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(job_store, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "artifacts" / "INDEX.md")
    monkeypatch.setattr(changes, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(leads, "finish", lambda name, reply: reply["result"])
    calls, answers = [], []

    def ask(prompt, model, session_id=None, **options):
        calls.append(prompt)
        result = answers.pop(0) if answers else "done"
        return {"result": result, "total_cost_usd": 0.01, "session_id": "s1"}

    monkeypatch.setattr(job_run, "ask", ask)
    return calls, answers


def finished_lead_job():
    job = jobs.add("Fix main.py.", lead="pip")
    job.status, job.result = "done", "fixed"
    jobs.save(job)
    return job


def test_the_lesson_block_is_parsed():
    assert leads.extract_lesson(LESSON) == (
        "Read the task's file names twice before the first edit.",
        "I changed app.py, but the task named main.py.")
    assert leads.extract_lesson("No lesson.") is None


def test_a_long_lesson_is_refused():
    with pytest.raises(ValueError, match="too long"):
        leads.extract_lesson("<lesson>\n" + "x" * 300 + "\n</lesson>")


def test_a_rejected_lead_job_asks_the_lead_for_a_lesson(agent):
    calls, answers = agent
    job = finished_lead_job()

    jobs.reject(job.id, "Wrong file.")
    [lesson_job] = jobs.queued_lessons(job.id)
    answers.append(LESSON)
    jobs.run_job(lesson_job)

    assert "Wrong file." in calls[0] and "<question>" not in calls[0]  # a lesson job never asks
    [draft] = leads.load_lessons("pip")["drafts"]
    assert draft["text"] == "Read the task's file names twice before the first edit."
    assert draft["job"] == job.id and draft["trigger"] == "the user rejected the result"
    assert leads.load_lessons("pip")["kept"] == []  # it waits for the user


def test_a_one_time_agent_is_not_asked(agent):
    job = jobs.add("Fix main.py.", model="haiku", use_memory=False)
    job.status = "done"
    jobs.save(job)

    jobs.reject(job.id, "Wrong file.")

    assert jobs.queued_lessons(job.id) == []


def test_two_undos_ask_once(agent, lead_folder):
    job = finished_lead_job()
    app = lead_folder / "app.py"
    app.write_text("old\n")
    changes.snapshot_before(job.id, app)
    app.write_text("new\n")

    jobs.undo(job.id)
    jobs.undo(job.id)

    assert len(jobs.queued_lessons(job.id)) == 1


def test_keep_edit_and_drop(lead_folder):
    draft = leads.add_draft("pip", "Ask before you delete.", "I deleted a file.", "j1", "undone")
    other = leads.add_draft("pip", "Run the tests first.", "", "j2", "rework")

    kept = leads.keep_lesson("pip", draft["id"], text="Ask before you delete or move files.")
    leads.drop_lesson("pip", other["id"])

    lessons = leads.load_lessons("pip")
    assert [lesson["text"] for lesson in lessons["kept"]] == ["Ask before you delete or move files."]
    assert kept["edited"] and lessons["drafts"] == []
    assert leads.add_draft("pip", "ask before you delete or move files.", "", "j3", "undone") is None


def test_only_the_newest_lessons_stay(lead_folder):
    for number in range(leads.MAX_LESSONS + 2):
        draft = leads.add_draft("pip", f"Lesson {number}.", "", "j", "rework")
        leads.keep_lesson("pip", draft["id"])

    texts = [lesson["text"] for lesson in leads.load_lessons("pip")["kept"]]
    assert len(texts) == leads.MAX_LESSONS and texts[-1] == f"Lesson {leads.MAX_LESSONS + 1}."


def test_a_fresh_session_gets_the_kept_lessons(lead_folder):
    draft = leads.add_draft("pip", "Read the task's file names twice.", "", "j1", "rejected")
    leads.keep_lesson("pip", draft["id"])

    _, prompt_file, _ = leads.prepare("pip")

    assert "- Read the task's file names twice." in prompt_file.read_text()



def test_a_removed_lead_gets_no_lesson_and_nothing_crashes(agent, monkeypatch):
    """The lead was removed from roster.json after its job: Reject still works (before: SystemExit)."""
    job = finished_lead_job()
    monkeypatch.setattr(leads, "roster", lambda: {})

    jobs.reject(job.id, "Wrong file.")

    assert jobs.load(job.id).review["verdict"] == "rejected"
    assert jobs.queued_lessons(job.id) == []


def test_a_job_for_an_unknown_lead_is_a_value_error(agent):
    with pytest.raises(ValueError, match="No lead named Nobody"):
        jobs.add("Fix main.py.", lead="Nobody")


# --- Twin desks (leads.py): a second desk of a busy lead ---

def test_a_twin_starts_from_the_leads_handoff_and_lessons(lead_folder):
    (leads.lead_dir("pip")).mkdir(parents=True)
    (leads.lead_dir("pip") / "handoff.md").write_text("Decisions: use SQLite.\n")
    draft = leads.add_draft("pip", "Read the task's file names twice.", "", "j1", "rejected")
    leads.keep_lesson("pip", draft["id"])

    session, prompt_file, why = leads.prepare("pip~2")

    text = prompt_file.read_text()
    assert session is None and prompt_file == leads.lead_dir("pip~2") / "prompt.md"
    assert "You are pip," in text and "Decisions: use SQLite." in text
    assert "- Read the task's file names twice." in text and "You are a second desk" in text
    assert "second desk" not in leads.prepare("pip")[1].read_text()  # the lead's own prompt is as before


def test_an_idle_twin_retires_but_the_lead_does_not(lead_folder):
    state = {"session_id": "s1", "folder": str(lead_folder), "last_used": leads.time.time() - leads.TWIN_IDLE - 5,
             "context_tokens": 1000, "replies": 1, "cost": 0.0}

    assert "retired" in leads.fresh_reason(state, lead_folder, twin=True)
    assert leads.fresh_reason(state, lead_folder) is None


def test_take_desk_gives_the_next_free_desk(lead_folder):
    first, held = leads.take_desk(["pip", "pip~2"])
    second, held_too = leads.take_desk(["pip", "pip~2"])

    assert (first, second) == ("pip", "pip~2")
    assert leads.take_desk(["pip", "pip~2"]) == (None, None)
    leads.release_desk(held)
    assert leads.take_desk(["pip", "pip~2"])[0] == "pip"
    leads.release_desk(held_too)
