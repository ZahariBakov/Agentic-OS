"""Tests for ask(): the question goes through stdin, a stuck agent is stopped.

A fake `claude` script is put first in PATH, so no real agent runs.
The FAKE_MODE environment variable says what the fake does.
"""

import os
import sys
import time

import pytest

from agentic_os.ask import AgentError, ask

FAKE_CLAUDE = """#!{python}
import json, os, sys, time
mode = os.environ["FAKE_MODE"]
question = "" if mode == "early-exit" else sys.stdin.read()
if mode == "echo":
    print(json.dumps({{"type": "result", "is_error": False, "result": question,
                      "session_id": "s1", "total_cost_usd": 0.0}}), flush=True)
elif mode == "session":
    print(json.dumps({{"type": "system", "subtype": "init", "session_id": "s7"}}), flush=True)
    print(json.dumps({{"type": "result", "is_error": False, "result": "ok",
                      "session_id": "s7", "total_cost_usd": 0.0}}), flush=True)
elif mode == "hang":
    time.sleep(60)
elif mode == "bad-line":
    print("this is not JSON", flush=True)
    print(json.dumps({{"type": "result", "is_error": False, "result": "still here",
                      "session_id": "s1", "total_cost_usd": 0.01}}), flush=True)
elif mode == "limits":
    print(json.dumps({{"type": "rate_limit_event", "rate_limit_info": {{"status": "allowed"}}}}), flush=True)
    print(json.dumps({{"type": "result", "is_error": False, "result": "ok",
                      "session_id": "s1", "total_cost_usd": 0.01}}), flush=True)
elif mode == "error-result":
    print(json.dumps({{"type": "result", "subtype": "error_max_turns", "is_error": True,
                      "errors": ["too many turns"], "session_id": "s1", "total_cost_usd": 0.05}}), flush=True)
    sys.exit(1)
elif mode == "early-exit":
    sys.stderr.write("unknown option --bad")
    sys.exit(2)
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Put a fake `claude` first in PATH. Return a function that sets its mode."""
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return lambda mode: monkeypatch.setenv("FAKE_MODE", mode)


def is_alive(pid):
    try:
        os.kill(pid, 0)  # signal 0 only checks that the process exists
    except ProcessLookupError:
        return False
    return True


def test_a_question_that_starts_with_a_dash_is_a_question(fake_claude):
    fake_claude("echo")

    reply = ask("--version")

    assert reply["result"] == "--version"


def test_a_stuck_agent_is_stopped(fake_claude):
    fake_claude("hang")
    pids = []
    start = time.monotonic()

    with pytest.raises(RuntimeError, match="more than 1 seconds"):
        ask("hello", on_start=pids.append, timeout=1)

    assert time.monotonic() - start < 10
    assert not is_alive(pids[0])


def test_a_line_that_is_not_json_is_skipped(fake_claude):
    """Review 2, item 100: our bookkeeping must not kill a working agent and lose its cost."""
    fake_claude("bad-line")

    reply = ask("hello")

    assert reply["result"] == "still here"


def test_a_broken_limits_save_does_not_stop_the_agent(fake_claude, monkeypatch):
    def broken(info):
        raise OSError("disk full")

    monkeypatch.setattr("agentic_os.limits.record", broken)
    fake_claude("limits")

    reply = ask("hello")

    assert reply["result"] == "ok"


def test_an_error_on_start_does_not_leave_the_agent_running(fake_claude):
    fake_claude("hang")
    pids = []

    def on_start(pid):
        pids.append(pid)
        raise OSError("could not save the job")

    with pytest.raises(OSError):
        ask("hello", on_start=on_start)

    assert not is_alive(pids[0])


def test_an_error_result_keeps_its_text_and_cost(fake_claude):
    """Review 2, item 38: error results have "errors", not "result"."""
    fake_claude("error-result")

    with pytest.raises(AgentError, match="too many turns") as error:
        ask("hello")

    assert error.value.cost == 0.05


def test_claude_that_ends_before_it_reads_a_big_question_says_why(fake_claude):
    """Review 2, item 99: the broken pipe must not hide stderr."""
    fake_claude("early-exit")

    with pytest.raises(AgentError, match="unknown option --bad"):
        ask("x" * 200_000)


def test_the_session_id_comes_before_the_result(fake_claude):
    fake_claude("session")
    seen = []

    def on_session(session_id):
        seen.append(session_id)

    reply = ask("hello", on_session=on_session)

    assert seen == ["s7"]
    assert reply["session_id"] == "s7"
