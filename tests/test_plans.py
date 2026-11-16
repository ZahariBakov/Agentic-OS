"""Tests for plans with a cost: the planning call is read-only, the job waits for
the user's OK, approve/change/reject, and auto-approve under an amount.

No real agent runs: job_run.ask is replaced with a fake whose answers we choose.
"""

import pytest

from agentic_os import artifacts, changes, job_run, job_store, jobs, limits, safety

PLAN = """I read the code. Here is my plan.
<plan>
1. Add a parser.
2. Add tests.
Estimate: $0.40
</plan>"""


@pytest.fixture
def agent(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Replace the agent call. Return (calls, answers): put the next answers in `answers`.

    Each call is {"prompt", "session", "args"}. Without a planned answer the agent says "done".
    """
    monkeypatch.setattr(job_run, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(job_store, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(job_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(job_run, "DEFAULT_WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "artifacts" / "INDEX.md")
    monkeypatch.setattr(changes, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    calls, answers = [], []

    def ask(prompt, model, session_id=None, extra_args=(), env=None, **options):
        calls.append({"prompt": prompt, "session": session_id, "args": list(extra_args), "env": env or {}})
        result = answers.pop(0) if answers else "done"
        return {"result": result, "total_cost_usd": 0.10, "session_id": "s1"}

    monkeypatch.setattr(job_run, "ask", ask)
    return calls, answers


def can_edit(args):
    """Return True if --allowedTools has an edit tool (only that option: --settings has
    "Edit|Write" too, in the edit hook's matcher)."""
    allowed = args[args.index("--allowedTools") + 1:args.index("--disallowedTools")]
    return any(rule.startswith(tool) for rule in allowed for tool in safety.EDIT_TOOLS)


def planned_job(agent, tmp_path, **options):
    """Run a --plan job whose agent proposes PLAN. Return the job."""
    _, answers = agent
    folder = tmp_path / "project"
    folder.mkdir(exist_ok=True)
    answers.append(PLAN)
    job = jobs.add("Add a parser.", model="sonnet", use_memory=False, folder=folder, plan_first=True, **options)
    jobs.run_job(job)
    return jobs.load(job.id)


def test_the_plan_block_is_parsed():
    plan, rest = jobs.extract_plan(PLAN)

    assert plan == {"text": "1. Add a parser.\n2. Add tests.", "estimate": 0.40}
    assert rest == "I read the code. Here is my plan."
    assert jobs.extract_plan("<plan>\n1. Do it.\n</plan>")[0]["estimate"] is None
    assert jobs.extract_plan("No plan.")[0] is None


def test_a_plan_makes_the_job_wait_and_the_planning_call_is_read_only(agent, tmp_path):
    calls, _ = agent

    job = planned_job(agent, tmp_path)

    assert job.status == "waiting" and job.plan["status"] == "pending" and job.plan["estimate"] == 0.40
    assert "<plan>" in calls[0]["prompt"] and "no cost history" in calls[0]["prompt"]
    assert not can_edit(calls[0]["args"])  # nothing can change before the OK


def test_approve_runs_the_work_in_the_same_session_with_edit_tools(agent, tmp_path):
    calls, _ = agent
    job = planned_job(agent, tmp_path)

    queued, start = jobs.decide_plan(job.id, "approve")
    jobs.run_job(queued)

    assert start
    assert calls[1]["session"] == "s1" and "approved your plan" in calls[1]["prompt"]
    assert can_edit(calls[1]["args"])
    saved = jobs.load(job.id)
    assert saved.status == "done" and saved.plan["status"] == "approved"
    assert any(message["text"].startswith("Plan (estimate $0.40") for message in saved.messages)


def test_change_sends_the_note(agent, tmp_path):
    calls, _ = agent
    job = planned_job(agent, tmp_path)

    queued, _, _ = jobs.follow_up(job.id, "Skip the tests.")  # a message to a waiting plan changes it
    jobs.run_job(queued)

    assert "Skip the tests." in calls[1]["prompt"]
    assert jobs.load(job.id).plan["status"] == "changed"


def test_reject_stops_the_job_without_an_agent_call(agent, tmp_path):
    calls, _ = agent
    job = planned_job(agent, tmp_path)

    _, start = jobs.decide_plan(job.id, "reject", "Too expensive.")

    assert not start and len(calls) == 1
    saved = jobs.load(job.id)
    assert saved.status == "stopped" and saved.plan["status"] == "rejected"
    assert "Too expensive." in saved.error


def test_a_cheap_plan_is_approved_automatically(agent, tmp_path):
    calls, _ = agent
    jobs.set_auto_approve(0.50)

    job = planned_job(agent, tmp_path)

    assert len(calls) == 2  # the plan, then the work, with no OK from the user
    assert job.status == "done" and job.plan["auto"].startswith("approved automatically")


def test_an_expensive_plan_still_waits(agent, tmp_path):
    jobs.set_auto_approve(0.30)

    assert planned_job(agent, tmp_path).status == "waiting"


def test_opus_plans_without_the_flag_and_routines_never_plan(agent):
    opus = jobs.add("Big work.", model="opus", use_memory=False)
    routine = jobs.add("Big work.", model="opus", use_memory=False)
    routine.routine = "nightly"
    jobs.save(routine)

    assert jobs.needs_plan(opus) and not jobs.needs_plan(routine)
    assert not jobs.needs_plan(jobs.add("Small work.", model="haiku", use_memory=False))


@pytest.mark.parametrize("decision, note, error", [
    ("maybe", "", "approve, change or reject"),
    ("change", " ", "Write the changes"),
])
def test_wrong_decisions(agent, tmp_path, decision, note, error):
    job = planned_job(agent, tmp_path)

    with pytest.raises(ValueError, match=error):
        jobs.decide_plan(job.id, decision, note)


def test_no_plan_waits(agent):
    job = jobs.add("Say hi.", model="haiku", use_memory=False)

    with pytest.raises(ValueError, match="No plan waits"):
        jobs.decide_plan(job.id, "approve")


def test_the_cost_reference_uses_done_jobs_of_the_model(agent):
    for number, cost in enumerate([0.10, 0.20, 0.90]):
        job = jobs.add(f"Job {number}.", model="sonnet", use_memory=False)
        job.status, job.cost = "done", cost
        jobs.save(job)

    assert jobs.cost_reference("sonnet") == (
        "For reference, earlier jobs on your model (sonnet) really cost: median $0.20, the most $0.90 (3 jobs).")


def test_small_amounts_keep_4_decimals():
    assert (jobs.usd(0.0031), jobs.usd(0.25)) == ("$0.0031", "$0.25")


def test_usd_switches_to_4_decimals_just_below_one_cent():
    assert jobs.usd(0.01) == "$0.01"
    assert jobs.usd(0.0099) == "$0.0099"


def test_a_message_during_the_planning_call_does_not_skip_the_ok(agent, tmp_path, monkeypatch):
    """The message waits with the plan; the work starts only after the user's OK, and gets the message."""
    calls, answers = agent
    real_ask = job_run.ask

    def ask_and_write(prompt, model, **options):
        if not calls:  # during the planning call, the user writes to the running job
            jobs.follow_up(job.id, "Use the new parser library.")
        return real_ask(prompt, model, **options)

    monkeypatch.setattr(job_run, "ask", ask_and_write)
    folder = tmp_path / "project"
    folder.mkdir()
    answers.append(PLAN)
    job = jobs.add("Add a parser.", model="sonnet", use_memory=False, folder=folder, plan_first=True)
    jobs.run_job(job)

    job = jobs.load(job.id)
    assert job.status == "waiting" and job.plan["status"] == "pending" and len(calls) == 1

    job, start = jobs.decide_plan(job.id, "approve")
    jobs.run_job(job)

    assert "Use the new parser library." in calls[1]["prompt"] and "approved your plan" in calls[1]["prompt"]
    assert can_edit(calls[1]["args"]) and jobs.load(job.id).status == "done"


def test_a_failure_before_the_inbox_is_taken_does_not_run_again(agent, monkeypatch):
    """Before: run_job called itself until RecursionError."""
    calls, _ = agent
    job = jobs.add("Say hi.", model="haiku", use_memory=False)
    jobs.write_inbox(job.id, {"from": "user", "text": "And bye.", "at": jobs.now()})
    runs = []

    def broken(job):
        runs.append(job.id)
        raise RuntimeError("git is broken")

    monkeypatch.setattr(job_run, "prepare_workspace", broken)
    jobs.run_job(job)

    assert runs == [job.id] and calls == []
    job = jobs.load(job.id)
    assert job.status == "failed" and "git is broken" in job.error
    assert jobs.inbox_file(job.id).exists()  # the message is kept for the next run


def test_the_planning_call_gets_no_edit_folder(agent, tmp_path):
    """Review 2, item 58: a refused Edit in the planning call must not leave a copy for Undo."""
    calls, _ = agent
    job = planned_job(agent, tmp_path)

    queued, _ = jobs.decide_plan(job.id, "approve")
    jobs.run_job(queued)

    assert "AGENTIC_OS_EDIT_DIR" not in calls[0]["env"]  # the plan: read-only
    assert "AGENTIC_OS_EDIT_DIR" in calls[1]["env"]  # the work after the OK



@pytest.mark.parametrize("line, estimate", [
    ("Estimate: $0.30 - $0.90", 0.90),  # review 2, item 21: a range counts by its high end
    ("Estimate: $1,200", 1200.0),
    ("estimate: about 0.4 USD", 0.4),
    ("Estimate: unknown", None),
])
def test_the_estimate_is_the_biggest_amount(line, estimate):
    plan, _ = jobs.extract_plan(f"<plan>\n1. Do it.\n{line}\n</plan>")

    assert plan["estimate"] == estimate and plan["text"] == "1. Do it."


def test_auto_approve_refuses_infinity(agent):
    """Review 2, item 59: Infinity would approve every plan and break GET /api/settings."""
    for value in (float("inf"), float("nan"), -1):
        with pytest.raises(ValueError, match="dollars"):
            jobs.set_auto_approve(value)
    job_store.SETTINGS_FILE.write_text('{"auto_approve_plans_usd": Infinity}')  # written by hand

    assert jobs.settings()["auto_approve_plans_usd"] == 0.0
