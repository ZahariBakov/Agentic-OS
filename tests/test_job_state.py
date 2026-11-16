"""Tests for the job state rules (review 2, step 6).

The rule: while a live worker owns a job, only the worker writes the job's file.
A stop and a message go to their own files; other actions wait ("try again").
No real agent runs: job_run.ask is replaced with a fake.
"""

import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentic_os import artifacts, changes, job_run, job_store, job_talk, jobs, leads, limits, web
from agentic_os.ask import AgentError


@pytest.fixture
def agent(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Replace the agent call and use a lead "pip" in tmp_path. Return the prompts the agent got."""
    folder = tmp_path / "pip"
    folder.mkdir()
    role = {"area": "quick tasks", "model": "haiku", "effort": "low", "folder": str(folder),
            "tools": ["Read"], "keywords": [], "instructions": "Be short."}
    monkeypatch.setattr(leads, "roster", lambda: {"pip": role})
    monkeypatch.setattr(leads, "LEADS_DIR", tmp_path / "leads")
    monkeypatch.setattr(leads, "finish", lambda name, reply: reply["result"])
    monkeypatch.setattr(job_store, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "artifacts" / "INDEX.md")
    monkeypatch.setattr(changes, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    prompts = []

    def ask(prompt, model, **options):
        prompts.append(prompt)
        return {"result": "done", "total_cost_usd": 0.01, "session_id": "s1"}

    monkeypatch.setattr(job_run, "ask", ask)
    return prompts


def dead_pid():
    """The id of a process that has ended."""
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def saved(task="Say hi.", **fields):
    job = jobs.add(task, model="haiku", use_memory=False)
    for name, value in fields.items():
        setattr(job, name, value)
    jobs.save(job)
    return job


def test_a_review_waits_while_the_worker_ends_its_run(agent):
    """Before: the worker's last save (an old Job object) wiped the review."""
    job = saved(status="stopped", worker=os.getpid())  # this process is alive: it still owns the job

    with pytest.raises(ValueError, match="still ending its run"):
        jobs.approve(job.id)

    jobs.update(job.id, worker=None)
    assert jobs.approve(job.id).review["verdict"] == "approved"


def test_a_dead_worker_owns_nothing(agent):
    job = saved(status="done", worker=dead_pid())

    assert jobs.approve(job.id).review["verdict"] == "approved"


def test_recover_frees_a_waiting_job_whose_worker_died(agent):
    job = saved(status="waiting", worker=dead_pid(), pid=12345)

    jobs.recover()

    job = jobs.load(job.id)
    assert job.status == "waiting" and job.worker is None and job.pid is None


def test_a_held_job_that_was_stopped_stays_stopped(agent, monkeypatch):
    """Before: the held branch wrote "queued" over the stop, and the job made a paid run later."""
    job = jobs.add("Fix main.py.", lead="pip")

    def stopped_meanwhile(lead, other_than):
        jobs.stop(job.id)  # the user stops it while it looks for an open question
        return "other1"

    monkeypatch.setattr(job_run, "waiting_job", stopped_meanwhile)
    jobs.run_job(job)

    assert jobs.load(job.id).status == "stopped" and agent == []


def test_no_paid_call_after_a_stop_in_a_follow_up(agent, monkeypatch):
    """Before: the follow-up and resume branches had no stop check before the call."""
    job = saved(status="done", session_id="s1", result="Hi.")
    jobs.follow_up(job.id, "And bye.")
    real_add_messages = jobs.add_messages

    def add_and_stop(job, answer, messages):
        real_add_messages(job, answer, messages)
        jobs.stop(job.id)  # the user stops it just before the agent call

    monkeypatch.setattr(job_talk, "add_messages", add_and_stop)
    jobs.run_job(jobs.load(job.id))

    job = jobs.load(job.id)
    assert agent == [] and job.status == "stopped" and job.result == "Hi."


def test_a_stop_before_the_run_keeps_the_messages_for_later(agent):
    job = saved(status="done", session_id="s1", result="Hi.")
    jobs.follow_up(job.id, "And bye.")
    claimed = jobs.claim(job.id)
    jobs.stop(job.id)

    assert jobs.work(claimed, lambda pid: None) == (None, "stopped")
    assert jobs.read_inbox(job.id)[0]["text"] == "And bye."


def test_an_answered_question_holds_the_lead_until_it_runs(agent):
    """Before: another job of the lead could run first, and the two conversations mixed."""
    answered = jobs.add("Fix main.py.", lead="pip")
    jobs.update(answered.id, status="queued", question={"text": "Which file?", "options": [], "at": "x"})

    assert jobs.waiting_job("pip", other_than="x") == answered.id
    assert jobs.waiting_job("pip", other_than=answered.id) is None


@pytest.fixture
def shutdown(monkeypatch):
    """A fresh SHUTDOWN flag and pool for one test (the real ones must stay usable for other tests)."""
    flag = threading.Event()
    monkeypatch.setattr(job_run, "SHUTDOWN", flag)
    monkeypatch.setattr(web, "pool", ThreadPoolExecutor(max_workers=1))
    return flag


def test_no_new_run_while_the_server_stops(agent, shutdown):
    job = saved()
    shutdown.set()

    jobs.run_job(job)

    assert jobs.load(job.id).status == "queued" and agent == []


def test_a_job_cut_by_the_stop_is_not_failed(agent, shutdown, monkeypatch):
    """The agent ended because the server stops: recover() makes it "interrupted" later, not "failed"."""
    def cut(prompt, model, **options):
        shutdown.set()
        raise AgentError("claude failed (exit code -15)")

    monkeypatch.setattr(job_run, "ask", cut)
    job = saved()
    jobs.run_job(job)

    job = jobs.load(job.id)
    assert job.status == "running" and job.worker == os.getpid() and job.error is None


def test_stop_work_ends_only_this_process_agents_and_the_waiting_pool(agent, shutdown, monkeypatch):
    killed = []
    monkeypatch.setattr(web, "kill_group", lambda pid, sig: killed.append(pid))
    ours = saved(status="running", worker=os.getpid(), pid=4242)
    saved(status="running", worker=dead_pid(), pid=5353)  # another process's job

    web.stop_work()
    web.stop_work()  # a second Ctrl+C does nothing more

    assert killed == [4242] and shutdown.is_set()
    with pytest.raises(RuntimeError):
        web.pool.submit(print)  # the pool takes no more jobs
    assert jobs.load(ours.id).status == "running"


def test_one_broken_job_file_does_not_hide_the_others(temp_jobs, capsys):
    """Review 2, item 37: an empty file after a power loss must not break the whole list."""
    job = jobs.add("Say hi.", model="haiku", use_memory=False)
    (temp_jobs / "broken1.json").write_text("")

    assert [found.id for found in jobs.all_jobs()] == [job.id]
    assert "broken1.json" in capsys.readouterr().out
