"""Tests for recovery after a restart: interrupted jobs and Resume.

No real agent runs: job_run.ask is replaced with a fake. A "dead" process id comes
from a short process that has already ended.
"""

import os
import subprocess
import sys
import time

import pytest

from agentic_os import artifacts, changes, job_run, jobs, limits


@pytest.fixture
def fake_agent(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Replace the agent call, and move every file a run writes into tmp_path.

    Return the list of calls: (prompt, session id).
    """
    monkeypatch.setattr(job_run, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(job_run, "DEFAULT_WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "artifacts" / "INDEX.md")
    monkeypatch.setattr(changes, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    calls = []

    def ask(prompt, model, session_id=None, **options):
        calls.append((prompt, session_id))
        return {"result": "ok", "total_cost_usd": 0.01, "session_id": session_id or "s1"}

    monkeypatch.setattr(job_run, "ask", ask)
    return calls


def dead_pid():
    """Return the id of a process that has already ended."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def running_job(worker, session_id="s-old", pid=None):
    """Add a job and save it as if a worker were running it."""
    job = jobs.add("Write hello.txt.", model="haiku", use_memory=False)
    job.status = "running"
    job.worker = worker
    job.pid = pid
    job.session_id = session_id
    jobs.save(job)
    return job


def test_a_job_with_a_dead_worker_is_interrupted(fake_agent):
    job = running_job(worker=dead_pid())

    found = jobs.recover()

    assert [found_job.id for found_job in found] == [job.id]
    saved = jobs.load(job.id)
    assert saved.status == "interrupted"
    assert saved.worker is None and saved.interrupted_why


def test_a_job_with_a_live_worker_keeps_running(fake_agent):
    job = running_job(worker=os.getpid())  # this test process is alive

    assert jobs.recover() == []
    assert jobs.load(job.id).status == "running"


def test_a_leftover_agent_process_is_stopped(fake_agent):
    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        running_job(worker=dead_pid(), pid=agent.pid)

        jobs.recover()

        assert agent.wait(timeout=10) != 0  # ended by SIGTERM
    finally:
        agent.kill()


def test_only_an_interrupted_job_can_be_resumed(fake_agent):
    job = jobs.add("Say hi.", model="haiku", use_memory=False)

    with pytest.raises(ValueError, match="not interrupted"):
        jobs.resume(job.id)


def test_resume_continues_the_same_session_with_a_brief(fake_agent):
    job = running_job(worker=dead_pid())
    jobs.recover()

    jobs.run_job(jobs.resume(job.id))

    prompt, session_id = fake_agent[0]
    assert session_id == "s-old"  # the same session, not a new one
    assert "interrupted" in prompt and "Write hello.txt." in prompt
    assert "Do NOT repeat" in prompt
    saved = jobs.load(job.id)
    assert saved.status == "done" and saved.resumes == 1


def test_the_session_id_is_saved_during_the_call(fake_agent, monkeypatch):
    job = jobs.add("Say hi.", model="haiku", use_memory=False)

    def ask_that_dies(prompt, model, on_session=None, **options):
        on_session("s9")  # the stream showed the session id
        raise RuntimeError("the server stopped")  # and then the call never finished

    monkeypatch.setattr(job_run, "ask", ask_that_dies)
    jobs.run_job(job)

    assert jobs.load(job.id).session_id == "s9"


def test_an_interrupted_ingest_is_not_a_new_source(fake_agent, temp_memory):
    raw = temp_memory / "raw"
    raw.mkdir()
    (raw / "idea.md").write_text("An idea.")
    job = jobs.ingest(raw / "idea.md")
    job.status = "interrupted"
    jobs.save(job)

    assert jobs.new_sources() == []


def test_a_stop_ends_the_agent_and_its_tools(fake_agent):
    """The agent runs in its own process group: its tools (children) end with it."""
    agent = subprocess.Popen([sys.executable, "-c",
                              "import subprocess, sys, time; "
                              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                              "print(child.pid, flush=True); time.sleep(60)"],
                             stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        child = int(agent.stdout.readline())
        job = running_job(worker=os.getpid(), pid=agent.pid)
        jobs.stop(job.id)
        assert agent.wait(timeout=10) != 0
        for _ in range(100):  # the child is not ours, so we cannot wait() for it
            if not jobs.is_alive(child):
                break
            time.sleep(0.05)
        assert not jobs.is_alive(child)
    finally:
        agent.kill()


def test_resume_of_a_cut_planning_call_plans_again(fake_agent):
    job = jobs.add("Big change.", model="opus", use_memory=False)  # opus jobs plan first
    job.status, job.worker = "running", dead_pid()
    jobs.save(job)
    jobs.recover()

    resumed = jobs.resume(job.id)

    assert resumed.resuming is False  # a fresh planning call (read-only), not a resume that edits
    jobs.run_job(resumed)
    assert "This job needs a plan first" in fake_agent[-1][0]  # PLAN_RULES, not the resume brief


def test_an_answer_while_the_run_still_ends_does_not_start_a_second_run(fake_agent):
    job = jobs.add("Say hi.", model="haiku", use_memory=False)
    job.status, job.worker = "waiting", os.getpid()  # "waiting" is saved before the run's end
    job.question = {"text": "Which one?", "options": [], "at": jobs.now()}
    jobs.save(job)

    _, start, _ = jobs.follow_up(job.id, "The first one.")

    assert start is False  # the worker finds the message at its end and runs the job again
    assert jobs.load(job.id).status == "waiting"
    assert jobs.read_inbox(job.id)[0]["answer"] is True


def test_a_plan_decision_waits_until_the_run_has_ended(fake_agent):
    job = jobs.add("Big change.", model="haiku", use_memory=False)
    job.status, job.worker = "waiting", os.getpid()
    job.plan = {"text": "1. Do it.", "estimate": 0.1, "status": "pending", "at": jobs.now()}
    jobs.save(job)

    with pytest.raises(ValueError, match="try again in a moment"):
        jobs.decide_plan(job.id, "approve")
