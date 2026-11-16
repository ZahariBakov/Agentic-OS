"""Tests for approved shell commands (commands.py) and the run_command office tool.

No agent runs. The tool is called through office_mcp.handle() in this process; the
user's answer comes from a thread that calls commands.decide, like the web page.
"""

import json
import threading
import time

import pytest

from agentic_os import commands, job_run, job_store, jobs, office_mcp


@pytest.fixture
def quick(monkeypatch):
    """Short waits, so the tests do not sleep long."""
    monkeypatch.setattr(commands, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(job_store, "SETTINGS_FILE", job_store.SETTINGS_FILE.parent / "settings-test.json")


@pytest.fixture
def editing_job(temp_jobs, tmp_path, quick):
    """A queued job with its own folder (so it may edit, and so run commands)."""
    folder = tmp_path / "shop"
    folder.mkdir()
    (folder / "hello.txt").write_text("hi from the file\n")
    return jobs.add("Run the tests.", "haiku", use_memory=False, folder=str(folder))


def call(arguments, job_id):
    reply = office_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "run_command", "arguments": arguments}}, job_id)
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def answer_when_asked(decision):
    """Answer the first waiting command from another thread, like the web page."""
    def answer():
        for _ in range(250):
            waiting = commands.pending()
            if waiting:
                commands.decide(waiting[0]["id"], decision, jobs.settings(), jobs.set_setting)
                return
            time.sleep(0.02)
    thread = threading.Thread(target=answer)
    thread.start()
    return thread


@pytest.mark.parametrize("command, word", [
    ("rm -rf build", "rm"),
    ("cd src && rm x", "rm"),
    ("sudo apt install x", "sudo"),
    ("git   push origin main", "git push"),
    ("echo ok; git reset --hard", "git reset"),
    ("cat ../data/mail.json", "mail.json"),
    ("cat ~/.ssh/id_rsa", ".ssh"),
    ("/bin/rm x", "rm"),
])
def test_blocked_commands(command, word):
    assert commands.blocked(command) == word


@pytest.mark.parametrize("command", ["python -m pytest -q", "ls -la", "git status", "git diff", "grep -rn rmdir_x ."])
def test_ordinary_commands_are_not_blocked(command):
    assert commands.blocked(command) is None


def test_your_list_lets_only_simple_local_commands_run_without_asking():
    allow = ["python -m pytest", "make *"]

    assert commands.allowed_by("python -m pytest -q", allow) == "python -m pytest"
    assert commands.allowed_by("make test", allow) == "make *"
    assert commands.allowed_by("python -m pytestx", allow) is None  # not at a word end
    assert commands.allowed_by("python -m pytest; cat secret", allow) is None  # a chain
    assert commands.allowed_by("python -m pytest > out.txt", allow) is None
    assert commands.allowed_by("python -m pytest $(cat x)", allow) is None
    assert commands.allowed_by("pip install requests", ["pip"]) is None  # the network always asks


def test_an_allowed_command_runs_in_the_job_folder(editing_job):
    thread = answer_when_asked("allow")

    found, error = call({"command": "cat hello.txt", "reason": "to see the file"}, editing_job.id)
    thread.join()

    assert not error and found["exit_code"] == 0 and found["output"] == "hi from the file\n"
    assert found["folder"] == editing_job.folder
    record = commands.of_job(editing_job.id)[0]
    assert record["status"] == "done" and record["decided_at"]


def test_a_denied_command_does_not_run(editing_job, tmp_path):
    thread = answer_when_asked("deny")

    found, error = call({"command": "touch made.txt", "reason": "test"}, editing_job.id)
    thread.join()

    assert error and "denied" in found["error"]
    assert not (tmp_path / "shop" / "made.txt").exists()


def test_always_allow_saves_the_command_and_the_next_one_does_not_ask(editing_job):
    thread = answer_when_asked("always")
    call({"command": "cat hello.txt", "reason": "first"}, editing_job.id)
    thread.join()

    assert jobs.settings()["commands_allow"] == ["cat hello.txt"]
    found, error = call({"command": "cat hello.txt", "reason": "again"}, editing_job.id)  # no thread answers

    assert not error and found["exit_code"] == 0
    assert commands.of_job(editing_job.id)[-1]["allowed_by"] == "cat hello.txt"


def test_a_chain_cannot_be_always_allowed(editing_job):
    record = commands.request(editing_job.id, "ls; cat x", "test", editing_job.folder, 10)

    with pytest.raises(ValueError, match="simple command"):
        commands.decide(record["id"], "always", jobs.settings(), jobs.set_setting)

    assert commands.load(record["id"])["status"] == "pending"  # you can still allow it once


def test_no_answer_in_time(editing_job, monkeypatch):
    monkeypatch.setattr(commands, "WAIT_SECONDS", 0.1)

    found, error = call({"command": "ls", "reason": "test"}, editing_job.id)

    assert error and "did not answer" in found["error"]
    assert commands.of_job(editing_job.id)[0]["status"] == "no answer"
    with pytest.raises(ValueError, match="not waiting"):
        commands.decide(commands.of_job(editing_job.id)[0]["id"], "allow")  # too late


def test_a_command_that_runs_too_long_is_stopped(editing_job):
    jobs.set_setting("commands_allow", ["sleep"])

    found, error = call({"command": "sleep 5", "reason": "test", "timeout_seconds": 1}, editing_job.id)

    assert not error and found["exit_code"] != 0 and "stopped after 1 seconds" in found["output"]
    assert found["seconds"] < 4


def test_who_may_not_run_commands(temp_jobs, editing_job):
    reader = jobs.add("Explain.", "haiku", use_memory=False)  # no folder: it cannot edit
    assert "cannot change files" in call({"command": "ls", "reason": "x"}, reader.id)[0]["error"]
    assert "blocked" in call({"command": "rm -rf .", "reason": "x"}, editing_job.id)[0]["error"]
    assert "reason" in call({"command": "ls"}, editing_job.id)[0]["error"]
    assert "timeout_seconds" in call({"command": "ls", "reason": "x", "timeout_seconds": 0}, editing_job.id)[0]["error"]

    jobs.set_setting("commands_mode", "off")
    assert "turned shell commands off" in call({"command": "ls", "reason": "x"}, editing_job.id)[0]["error"]
    assert commands.of_job(editing_job.id) == []  # nothing was even asked


def test_the_settings_check_their_values(quick, temp_jobs):
    with pytest.raises(ValueError):
        jobs.set_setting("commands_mode", "always")
    with pytest.raises(ValueError):
        jobs.set_setting("commands_allow", "pytest")
    assert jobs.settings()["commands_mode"] == "ask" and jobs.settings()["commands_allow"] == []


def test_the_agent_gets_a_long_enough_mcp_tool_timeout(editing_job, monkeypatch):
    seen = {}

    def ask(prompt, model, **options):
        seen.update(options["env"])
        return {"result": "ok", "total_cost_usd": 0.0, "session_id": "s1"}

    monkeypatch.setattr(job_run, "ask", ask)
    jobs.call_agent(editing_job, "hi", None, [], on_start=lambda pid: None)

    assert int(seen["MCP_TOOL_TIMEOUT"]) > (commands.WAIT_SECONDS + commands.MAX_TIMEOUT) * 1000
