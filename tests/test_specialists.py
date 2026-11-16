"""Tests for specialists (specialists.py) and the delegate office tool.

No agent runs: job_run.ask is replaced with a fake that answers each sub-job (and
records the options it got), or run is a fake that only marks the jobs.
"""

import json
import threading

import pytest

from agentic_os import artifacts, job_run, jobs, limits, office_mcp, specialists


@pytest.fixture
def parent(temp_jobs, tmp_path, monkeypatch):
    """A job with a folder that delegates; the agent calls are faked."""
    monkeypatch.setattr(job_run, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "artifacts" / "INDEX.md")
    monkeypatch.setattr(specialists, "POLL_SECONDS", 0.02)
    folder = tmp_path / "shop"
    folder.mkdir()
    return jobs.add("Review the shop.", "sonnet", use_memory=False, folder=str(folder))


@pytest.fixture
def fake_agent(monkeypatch):
    """Answer every agent call; return the list of (task, extra_args) it got."""
    calls, lock = [], threading.Lock()

    def ask(prompt, model, extra_args=(), **options):
        with lock:
            calls.append((prompt, list(extra_args)))
        return {"result": f"Report for: {prompt.splitlines()[0]}", "total_cost_usd": 0.05, "session_id": "s"}

    monkeypatch.setattr(job_run, "ask", ask)
    return calls


def delegate(arguments, job_id):
    reply = office_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "delegate", "arguments": arguments}}, job_id)
    return json.loads(reply["result"]["content"][0]["text"]), reply["result"]["isError"]


def assignments(count):
    return [{"title": f"Part {number}", "task": f"Read part {number} and summarize it."} for number in range(1, count + 1)]


def test_sub_jobs_run_and_report_back(parent, fake_agent):
    found, error = delegate({"assignments": assignments(2)}, parent.id)

    assert not error
    assert [item["title"] for item in found["sub_jobs"]] == ["Part 1", "Part 2"]
    assert all(item["status"] == "done" and item["report"].startswith("Report for: Read part") for item in found["sub_jobs"])
    assert found["cost_usd"] == 0.1 and found["budget_left_usd"] == 0.4
    sub = jobs.load(found["sub_jobs"][0]["job"])
    assert sub.parent == parent.id and sub.folder == parent.folder and sub.max_budget_usd == 0.25


def test_a_sub_job_only_reads_and_gets_its_budget(parent, fake_agent):
    delegate({"assignments": assignments(1)}, parent.id)

    task, args = fake_agent[0]
    allowed = args[args.index("--allowedTools") + 1:args.index("--disallowedTools")]
    assert not [tool for tool in allowed if tool.startswith(("Edit", "Write"))]
    assert args[args.index("--max-budget-usd") + 1] == "0.5000"
    assert "cannot ask the user" in task and "<question>" not in task  # no ASK_RULES


def test_the_cap_is_for_all_sub_jobs_of_a_job(parent, fake_agent):
    for _ in range(9):  # $0.05 each: $0.45 spent, $0.05 left
        delegate({"assignments": assignments(1)}, parent.id)

    found, error = delegate({"assignments": assignments(3)}, parent.id)  # $0.0167 each: too little
    assert error and "used up" in found["error"]
    assert specialists.spent(parent.id) == pytest.approx(0.45)


def test_what_cannot_be_delegated(parent, fake_agent):
    found, _ = delegate({"assignments": assignments(1)}, parent.id)
    sub_id = found["sub_jobs"][0]["job"]

    assert "cannot delegate again" in delegate({"assignments": assignments(1)}, sub_id)[0]["error"]
    assert "1 to 3" in delegate({"assignments": assignments(4)}, parent.id)[0]["error"]
    assert "1 to 3" in delegate({"assignments": []}, parent.id)[0]["error"]
    assert "task" in delegate({"assignments": [{"title": "x", "task": " "}]}, parent.id)[0]["error"]
    assert "no job" in delegate({"assignments": assignments(1)}, None)[0]["error"]


def test_a_sub_job_cannot_run_commands(parent, fake_agent):
    found, _ = delegate({"assignments": assignments(1)}, parent.id)
    sub = jobs.load(found["sub_jobs"][0]["job"])

    assert not jobs.can_edit(sub) and not jobs.can_ask(sub)


def test_choose_model_never_opus_and_follows_haikus_track_record(temp_jobs):
    assert specialists.choose_model("Debug the login.")[0] == "sonnet"  # the rule says opus
    assert specialists.choose_model("Review the diff.")[0] == "sonnet"
    assert specialists.choose_model("Find the TODO lines.")[0] == "haiku"

    for number in range(5):  # haiku: 1 of 5 succeeded
        job = jobs.add(f"Task {number}", "haiku", use_memory=False)
        job.status, job.finished_at = ("done" if number == 0 else "failed"), jobs.now()
        jobs.save(job)
    model, why = specialists.choose_model("Find the TODO lines.")
    assert model == "sonnet" and "20%" in why


def test_a_job_that_does_not_start_is_stopped_not_waited_for(parent, monkeypatch):
    monkeypatch.setattr(specialists, "WAIT_SECONDS", 5)

    found = specialists.delegate(parent, assignments(2), run=lambda job: None)  # runs nothing

    assert [item["status"] for item in found["sub_jobs"]] == ["stopped", "stopped"]


def test_the_parent_stops_its_sub_jobs(parent, monkeypatch):
    started = threading.Event()

    def run(job):
        jobs.update(job.id, status="running")
        started.set()

    found = specialists.delegate(parent, assignments(1), stopped=lambda: started.is_set(), run=run)

    assert found["sub_jobs"][0]["status"] == "stopped"


def test_no_delegation_while_the_usage_limits_pause_jobs(parent, monkeypatch):
    monkeypatch.setattr(limits, "pause_reason", lambda: "the 5-hour window is at 95%")

    assert "95%" in delegate({"assignments": assignments(1)}, parent.id)[0]["error"]


def test_sub_jobs_are_not_your_requests_for_recos(parent, fake_agent):
    from agentic_os import recos
    for _ in range(3):
        delegate({"assignments": [{"title": "x", "task": "Summarize the new emails from the client into a short list"}]},
                 parent.id)

    assert recos.suggest() == []
