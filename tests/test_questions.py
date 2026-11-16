"""Tests for questions from the agent: a <question> block, the "waiting" status, the answer.

No real agent runs: job_run.ask is replaced with a fake whose answers we choose.
"""

import pytest

from agentic_os import artifacts, changes, job_run, job_store, jobs, leads, limits, workspaces

QUESTION = """I can store the data in two ways.
<question>
Which storage should I use?
- SQLite
- JSON files
</question>"""


@pytest.fixture
def agent(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Replace the agent call. Return (calls, answers): put the next answers in `answers`.

    Each call is {"prompt", "session"}. Without a planned answer the agent says "done".
    """
    monkeypatch.setattr(job_run, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(job_store, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(job_run, "DEFAULT_WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "artifacts" / "INDEX.md")
    monkeypatch.setattr(changes, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    calls, answers = [], []

    def ask(prompt, model, session_id=None, **options):
        calls.append({"prompt": prompt, "session": session_id})
        result = answers.pop(0) if answers else "done"
        return {"result": result, "total_cost_usd": 0.01, "session_id": session_id or "s1"}

    monkeypatch.setattr(job_run, "ask", ask)
    return calls, answers


def test_the_question_block_is_parsed():
    question, rest = jobs.extract_question(QUESTION)

    assert question == {"text": "Which storage should I use?", "options": ["SQLite", "JSON files"]}
    assert rest == "I can store the data in two ways."


@pytest.mark.parametrize("text", [
    "No question here.",
    "```\n<question>\nAn example in code.\n</question>\n```",
    "<question>\n- only an option\n</question>",
])
def test_no_question(text):
    assert jobs.extract_question(text)[0] is None


def test_a_question_makes_the_job_wait(agent):
    calls, answers = agent
    answers.append(QUESTION)
    job = jobs.add("Save the data.", model="haiku", use_memory=False)

    jobs.run_job(job)

    saved = jobs.load(job.id)
    assert saved.status == "waiting"
    assert saved.question["options"] == ["SQLite", "JSON files"]
    assert saved.result == "I can store the data in two ways."
    assert "<question>" in calls[0]["prompt"]  # the agent got the rules for asking


def test_the_answer_continues_the_same_session(agent):
    calls, answers = agent
    answers.append(QUESTION)
    job = jobs.add("Save the data.", model="haiku", use_memory=False)
    jobs.run_job(job)

    queued, start, _ = jobs.follow_up(job.id, "SQLite")
    jobs.run_job(queued)

    assert start
    assert calls[1]["session"] == "s1" and "answered your question" in calls[1]["prompt"]
    saved = jobs.load(job.id)
    assert saved.status == "done" and saved.question is None and saved.result == "done"
    assert [m["text"] for m in saved.messages] == [
        "I can store the data in two ways.", "Which storage should I use?", "SQLite"]


def test_a_routine_never_asks(agent):
    calls, answers = agent
    answers.append(QUESTION)
    job = jobs.add("Save the data.", model="haiku", use_memory=False)
    job.routine = "nightly"
    jobs.save(job)

    jobs.run_job(job)

    assert "<question>" not in calls[0]["prompt"]
    assert jobs.load(job.id).status == "done"  # a question block in its answer is only text


@pytest.fixture
def lead_pip(tmp_path, monkeypatch):
    """A fake lead "pip", so lead jobs run without roster.json."""
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are Pip.")
    monkeypatch.setattr(leads, "prepare", lambda name: ("s1", prompt_file, "resume session"))
    monkeypatch.setattr(leads, "roster", lambda: {"pip": {"tools": ["Read"]}})
    monkeypatch.setattr(leads, "finish", lambda name, reply: reply["result"])
    monkeypatch.setattr(leads, "LEADS_DIR", tmp_path / "leads")

    def lead_job(task):
        job = jobs.add(task, model="haiku", use_memory=False)
        job.lead, job.folder = "pip", str(tmp_path)
        jobs.save(job)
        return job
    return lead_job


def test_a_waiting_lead_holds_its_other_jobs(agent, lead_pip):
    jobs.set_setting("lead_twins", 0)  # one desk: the next job must wait for the answer
    calls, answers = agent
    answers.append(QUESTION)
    first, second = lead_pip("Save the data."), lead_pip("Write the docs.")
    jobs.run_job(first)

    jobs.run_job(second)  # the lead's session has an open question

    assert len(calls) == 1
    held = jobs.load(second.id)
    assert held.status == "queued" and held.held_for == first.id

    queued, _, _ = jobs.follow_up(first.id, "SQLite")
    jobs.run_job(queued)  # answers, finishes, then runs the held job

    assert [jobs.load(job.id).status for job in (first, second)] == ["done", "done"]
    assert "Write the docs." in calls[2]["prompt"]


def test_stopping_a_waiting_job_frees_the_lead(agent, lead_pip):
    jobs.set_setting("lead_twins", 0)
    calls, answers = agent
    answers.append(QUESTION)
    first, second = lead_pip("Save the data."), lead_pip("Write the docs.")
    jobs.run_job(first)
    jobs.run_job(second)

    jobs.stop(first.id)

    assert jobs.load(first.id).status == "stopped"
    assert [job.id for job in jobs.release_held(first.id)] == [second.id]


# --- Desks and twins (leads.py): a busy lead's new job goes to a second desk ---

def test_a_twin_takes_the_job_while_the_lead_waits_for_an_answer(agent, lead_pip):
    calls, answers = agent
    answers.append(QUESTION)
    first, second = lead_pip("Save the data."), lead_pip("Write the docs.")
    jobs.run_job(first)  # waits for the answer on the desk "pip"

    jobs.run_job(second)

    assert jobs.load(first.id).desk == "pip" and jobs.load(first.id).status == "waiting"
    assert jobs.load(second.id).desk == "pip~2" and jobs.load(second.id).status == "done"


def test_every_desk_busy_the_job_waits_without_a_pool_place(agent, lead_pip):
    calls, _ = agent
    job = lead_pip("Write the docs.")
    held = [leads.take_desk([desk])[1] for desk in ("pip", "pip~2")]  # two other jobs run there

    jobs.run_job(job)  # returns at once: it does not wait for a lock

    waiting = jobs.load(job.id)
    assert waiting.status == "queued" and waiting.waits_for == "pip" and waiting.worker is None
    assert calls == []

    leads.release_desk(held[1])
    other = lead_pip("Another job on the twin.")
    other.status, other.desk = "done", "pip~2"
    jobs.save(other)
    assert [queued.id for queued in jobs.release_held(other.id)] == [job.id]  # the freed desk wakes it
    jobs.run_job(job)
    assert jobs.load(job.id).status == "done" and jobs.load(job.id).desk == "pip~2"
    leads.release_desk(held[0])


def test_a_follow_up_goes_back_to_its_own_desk(agent, lead_pip):
    job = lead_pip("Write the docs.")
    job.desk, job.session_id, job.status = "pip~2", "s1", "done"
    jobs.save(job)

    assert jobs.desk_candidates(jobs.load(job.id)) == ["pip~2"]


def test_no_twin_for_an_editing_lead_without_a_workspace(agent, lead_pip, monkeypatch):
    monkeypatch.setattr(leads, "roster", lambda: {"pip": {"tools": ["Read", "Edit", "Write"]}})
    job = lead_pip("Fix the bug.")

    assert jobs.desk_candidates(job) == ["pip"]  # a twin would only read: it could not do this job
    job.workspace = {"status": "active", "path": "/tmp/x", "worktree": "/tmp/x", "repo": "/tmp"}
    monkeypatch.setattr(workspaces, "active", lambda job: bool(job.workspace))
    assert jobs.desk_candidates(job) == ["pip", "pip~2"]  # it edits its own copy: safe


def test_a_twin_without_a_workspace_only_reads(agent, lead_pip, monkeypatch):
    monkeypatch.setattr(leads, "roster", lambda: {"pip": {"tools": ["Read", "Edit", "Write"]}})
    job = lead_pip("Look at the bug.")
    job.desk = "pip~2"

    assert not jobs.can_edit(job)
    args = jobs.lead_args(job, "prompt.md")
    allowed = args[args.index("--allowedTools") + 1:args.index("--disallowedTools")]
    assert not [tool for tool in allowed if tool.startswith(("Edit", "Write"))]


def test_a_lesson_job_asks_the_lead_itself(agent, lead_pip):
    job = lead_pip("Write one lesson.")
    job.lesson_for = {"job": "x", "signal": "rejected"}

    assert jobs.desk_candidates(job) == ["pip"]


def test_the_twins_setting(agent):
    assert jobs.settings()["lead_twins"] == 1
    with pytest.raises(ValueError):
        jobs.set_setting("lead_twins", 5)
