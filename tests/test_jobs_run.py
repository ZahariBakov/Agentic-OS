"""Tests for running jobs: a job runs only once, stop, and the web model check.

No real agent runs: job_run.ask is replaced with a fake that counts the calls.
"""

import os
import threading
import time

import pytest
from fastapi import HTTPException

from agentic_os import artifacts, critic, job_run, jobs, limits, memory, web
from agentic_os.ask import AgentError


@pytest.fixture
def fake_agent(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Replace the agent call, and move every file a run writes into tmp_path.

    Return the list of prompts the fake agent got.
    """
    monkeypatch.setattr(job_run, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(job_run, "DEFAULT_WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "artifacts" / "INDEX.md")
    prompts = []

    def ask(prompt, model, **options):
        prompts.append(prompt)
        time.sleep(0.1)  # long enough for other threads to try the same job
        return {"result": "ok", "total_cost_usd": 0.01, "session_id": "s1"}

    monkeypatch.setattr(job_run, "ask", ask)
    return prompts


def test_a_job_runs_only_once_with_parallel_workers(fake_agent):
    job = jobs.add("Say hi.", model="haiku", use_memory=False)

    # Five workers get the same (old) Job object, like a double click on "Run queued".
    workers = [threading.Thread(target=jobs.run_job, args=(job,)) for _ in range(5)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert len(fake_agent) == 1
    assert jobs.load(job.id).status == "done"


def test_a_finished_job_does_not_run_again(fake_agent):
    job = jobs.add("Say hi.", model="haiku", use_memory=False)
    jobs.run_job(job)

    jobs.run_job(job)  # job is the old object; its status still says "queued"

    assert len(fake_agent) == 1


def test_stop_during_the_check_ends_the_job(fake_agent, tmp_path, monkeypatch):
    folder = tmp_path / "project"
    folder.mkdir()
    job = jobs.add("Fix it.", model="haiku", use_memory=False, folder=folder, check="false")

    def check_that_gets_stopped(running_job):
        jobs.stop(running_job.id)  # the user clicks Stop while the check runs
        return 1, "1 test failed"

    monkeypatch.setattr(job_run, "run_check", check_that_gets_stopped)
    jobs.run_job(job)

    assert len(fake_agent) == 1  # no fix prompt after the stop
    assert jobs.load(job.id).status == "stopped"


def test_file_lock_works_between_threads(tmp_path):
    inside = []
    overlaps = []

    def hold():
        with memory.file_lock(tmp_path / "lock"):
            if inside:
                overlaps.append(True)
            inside.append(True)
            time.sleep(0.05)
            inside.pop()

    threads = [threading.Thread(target=hold) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert overlaps == []


def test_web_refuses_an_unknown_model(fake_agent):
    with pytest.raises(HTTPException) as error:
        web.api_add_job(web.NewJob(task="Say hi.", model="gpt-9"))

    assert error.value.status_code == 400
    assert jobs.all_jobs() == []  # no job was added


# --- the critic in the job loop ---

@pytest.fixture
def fake_critic(monkeypatch):
    """Replace critic.compare. Return the list of winners it will give, in order.

    The test fills the list; each comparison takes the first winner from it.
    """
    winners = []

    def compare(task, result, reference, on_start=None):
        return {"winner": winners.pop(0), "why": "too vague", "fix": "Add an example.", "cost": 0.02}

    monkeypatch.setattr(critic, "compare", compare)
    return winners


def reference_file(tmp_path):
    path = tmp_path / "good.md"
    path.write_text("A good example answer.")
    return path


def test_the_critic_fix_goes_back_to_the_builder(fake_agent, fake_critic, tmp_path):
    fake_critic += ["reference", "result"]  # the second answer beats the reference
    job = jobs.add("Explain X.", model="haiku", use_memory=False, reference=reference_file(tmp_path))

    jobs.run_job(job)

    saved = jobs.load(job.id)
    assert len(fake_agent) == 2
    assert "Add an example." in fake_agent[1]  # the fix, not the reference itself
    assert "A good example answer." not in fake_agent[1]
    assert [verdict["winner"] for verdict in saved.verdicts] == ["reference", "result"]
    assert saved.status == "done"
    assert saved.cost == pytest.approx(2 * 0.01 + 2 * 0.02)  # builder + critic


def test_the_critic_stops_after_max_tries(fake_agent, fake_critic, tmp_path):
    fake_critic += ["reference"] * job_run.MAX_TRIES
    job = jobs.add("Explain X.", model="haiku", use_memory=False, reference=reference_file(tmp_path))

    jobs.run_job(job)

    saved = jobs.load(job.id)
    assert len(fake_agent) == job_run.MAX_TRIES
    assert saved.status == "done"  # the critic gives advice; the job is still done
    assert len(saved.verdicts) == job_run.MAX_TRIES


def test_the_critic_runs_only_after_the_check_passes(fake_agent, fake_critic, tmp_path, monkeypatch):
    fake_critic += ["result"]
    folder = tmp_path / "project"
    folder.mkdir()
    results = [(1, "1 test failed"), (0, "all passed")]
    monkeypatch.setattr(job_run, "run_check", lambda job: results.pop(0))
    job = jobs.add("Fix it.", model="haiku", use_memory=False, folder=folder, check="pytest",
                   reference=reference_file(tmp_path))

    jobs.run_job(job)

    saved = jobs.load(job.id)
    assert len(fake_agent) == 2  # first answer, then the fix for the failed check
    assert len(saved.verdicts) == 1  # the critic saw only the answer that passed
    assert saved.status == "done"


def test_a_failing_critic_keeps_the_job_done(fake_agent, tmp_path, monkeypatch):
    """Review 2, item 24: the critic is advice; its failure does not fail a job, and its cost counts."""
    def broken_compare(task, result, reference, on_start=None):
        raise critic.NoChoice("the critic gave no clear choice", 0.02)

    monkeypatch.setattr(critic, "compare", broken_compare)
    job = jobs.add("Explain X.", model="haiku", use_memory=False, reference=reference_file(tmp_path))

    jobs.run_job(job)

    saved = jobs.load(job.id)
    assert saved.status == "done" and saved.error is None
    assert saved.result == "ok"
    [verdict] = saved.verdicts
    assert verdict["winner"] is None and "no clear choice" in verdict["why"]
    assert saved.cost == pytest.approx(0.01 + 0.02)  # builder + the failed critic


def test_the_critic_can_be_stopped(fake_agent, tmp_path, monkeypatch):
    """Review 2, item 24: the critic gives its pid, so Stop can end it."""
    job = jobs.add("Explain X.", model="haiku", use_memory=False, reference=reference_file(tmp_path))
    seen = []

    def compare(task, result, reference, on_start=None):
        on_start(4242)
        seen.append(jobs.load(job.id).pid)  # saved, so Stop finds it
        jobs.stop_file(job.id).touch()
        raise AgentError("killed", 0.01)

    monkeypatch.setattr(critic, "compare", compare)
    jobs.run_job(job)

    saved = jobs.load(job.id)
    assert seen == [4242]
    assert saved.status == "stopped" and saved.pid is None


def test_a_missing_reference_file_is_refused(temp_jobs, tmp_path):
    with pytest.raises(ValueError, match="No such reference file"):
        jobs.add("Explain X.", model="haiku", reference=tmp_path / "missing.md")


def test_an_old_job_file_without_the_new_fields_loads(temp_jobs):
    temp_jobs.mkdir()
    (temp_jobs / "old1.json").write_text('{"id": "old1", "task": "x", "status": "done"}')

    job = jobs.load("old1")

    assert job.reference is None and job.verdicts == []


# --- found in the code review ---

def test_stop_while_the_critic_runs_wins_over_the_worker_save(fake_agent, fake_critic, tmp_path, monkeypatch):
    """The critic has no pid, so Stop only marks the job; the worker's later saves must not undo it."""
    job = jobs.add("Explain X.", model="haiku", use_memory=False, reference=reference_file(tmp_path))

    def compare(task, result, reference, on_start=None):
        jobs.stop(job.id)  # the user clicks Stop while the critic works
        return {"winner": "reference", "why": "vague", "fix": "Add an example.", "cost": 0.02}

    monkeypatch.setattr(critic, "compare", compare)
    jobs.run_job(job)

    saved = jobs.load(job.id)
    assert len(fake_agent) == 1  # no second agent call after the stop
    assert saved.status == "stopped"
    assert saved.worker is None and saved.finished_at is not None


def test_stop_during_a_check_that_passes_ends_stopped(fake_agent, tmp_path, monkeypatch):
    folder = tmp_path / "project"
    folder.mkdir()
    job = jobs.add("Fix it.", model="haiku", use_memory=False, folder=folder, check="true")

    def check(running_job):
        jobs.stop(running_job.id)
        return 0, "all good"

    monkeypatch.setattr(job_run, "run_check", check)
    jobs.run_job(job)

    assert jobs.load(job.id).status == "stopped"  # not "done"


def test_a_new_run_forgets_an_old_stop(fake_agent):
    job = jobs.add("Say hi.", model="haiku", use_memory=False)
    jobs.stop_file(job.id).parent.mkdir(parents=True, exist_ok=True)
    jobs.stop_file(job.id).touch()  # left from an earlier run

    jobs.run_job(job)

    assert jobs.load(job.id).status == "done"
    assert not jobs.stop_file(job.id).exists()


def test_a_failed_call_still_counts_its_cost(fake_agent, monkeypatch):
    def ask(prompt, model, session_id=None, **options):
        raise AgentError("claude returned an error: too many turns", 0.05)

    monkeypatch.setattr(job_run, "ask", ask)
    job = jobs.add("Say hi.", model="haiku", use_memory=False)
    jobs.run_job(job)

    saved = jobs.load(job.id)
    assert saved.status == "failed"
    assert saved.cost == pytest.approx(0.05)


def test_the_check_ends_with_its_children_on_a_timeout(fake_agent, tmp_path, monkeypatch):
    monkeypatch.setattr(job_run, "CHECK_TIMEOUT", 1)
    job = jobs.add("x", model="haiku", use_memory=False, folder=tmp_path,
                   check="sleep 30 & echo $! > child.pid; wait")
    started = time.time()
    code, output = jobs.run_check(job)
    assert code == -1 and "more than 1 seconds" in output
    assert time.time() - started < 10
    child = int((tmp_path / "child.pid").read_text())
    for _ in range(100):
        if not jobs.is_alive(child):
            break
        time.sleep(0.05)
    assert not jobs.is_alive(child)


# --- review 2, step 10: job runs and agent calls ---

def test_the_fix_prompt_asks_for_the_whole_answer(fake_agent, tmp_path, monkeypatch):
    """Item 19: the fix reply replaces the answer, so it must not be only "Fixed."."""
    results = [(1, "1 test failed"), (0, "all passed")]
    monkeypatch.setattr(job_run, "run_check", lambda job: results.pop(0))
    job = jobs.add("Fix it.", model="haiku", use_memory=False, folder=tmp_path, check="pytest")

    jobs.run_job(job)

    assert "whole answer" in fake_agent[1]


def test_the_last_answer_for_a_fresh_session_is_never_empty(temp_jobs):
    """Item 20: a plan moved the answer into the conversation; a question waits for its answer."""
    job = jobs.add("Add a parser.", model="haiku", use_memory=False)
    job.result = None
    job.messages = [{"from": "user", "text": "Go on.", "at": "t"},
                    {"from": "agent", "text": "I read the code.", "at": "t"},
                    {"from": "agent", "text": "Plan: 1. Add it.", "at": "t"}]
    assert jobs.last_answer(job) == "I read the code.\n\nPlan: 1. Add it."

    job.result = "Half done."
    job.question = {"text": "Which file?", "options": [], "at": "t"}
    assert jobs.last_answer(job) == "Half done.\n\nYour question:\nWhich file?"


def test_a_process_that_started_after_the_save_is_not_ours(tmp_path):
    """Item 33: a pid from an earlier boot (or reused) belongs to another process now."""
    started = jobs.process_started(os.getpid())
    assert started is not None and 0 <= time.time() - started < 24 * 3600

    assert jobs.is_alive(os.getpid(), saved_at=time.time())
    assert not jobs.is_alive(os.getpid(), saved_at=started - 3600)  # saved before we started


def test_a_message_to_a_stopped_job_that_still_ends_is_refused(temp_jobs):
    """Item 49: the end of a stopped run never takes the inbox, so the message would wait for good."""
    job = jobs.add("Say hi.", model="haiku", use_memory=False)
    jobs.update(job.id, status="stopped", worker=os.getpid())  # this process: alive

    with pytest.raises(ValueError, match="still ending"):
        jobs.follow_up(job.id, "And then?")

    assert not jobs.inbox_file(job.id).exists()


def test_check_output_that_is_not_utf8_is_read(fake_agent, tmp_path):
    """Item 53: a check that prints other bytes does not fail with a codec error."""
    job = jobs.add("x", model="haiku", use_memory=False, folder=tmp_path, check="printf 'caf\\351 ok'; exit 1")

    code, output = jobs.run_check(job)

    assert code == 1 and output.startswith("caf") and output.endswith(" ok")


def test_a_check_child_that_keeps_the_output_open_does_not_block(fake_agent, tmp_path, monkeypatch):
    """Item 54: a child in its own session keeps the pipe open after the timeout kill."""
    monkeypatch.setattr(job_run, "CHECK_TIMEOUT", 1)
    job = jobs.add("x", model="haiku", use_memory=False, folder=tmp_path,
                   check="setsid sh -c 'echo $$ > child.pid; exec sleep 60' & wait")
    started = time.time()
    try:
        code, output = jobs.run_check(job)
        assert code == -1 and time.time() - started < 30
    finally:
        child = tmp_path / "child.pid"
        if child.exists():
            job_run.kill_group(int(child.read_text()))


def test_a_later_error_does_not_hide_the_first_one(fake_agent, tmp_path, monkeypatch):
    """Item 57: a failed note save keeps the reason why the job failed."""
    monkeypatch.setattr(job_run, "run_check", lambda job: (1, "1 test failed"))

    def broken(text):
        raise OSError("disk full")

    monkeypatch.setattr(memory, "extract_notes", broken)
    job = jobs.add("Fix it.", model="haiku", folder=tmp_path, check="pytest")

    jobs.run_job(job)

    saved = jobs.load(job.id)
    assert saved.status == "failed" and saved.error.startswith("the check")


def test_a_queued_job_can_be_cancelled(fake_agent):
    """Stop on a queued job cancels it; a pool thread that picks it later runs no agent."""
    job = jobs.add("Say hi.", model="haiku", use_memory=False)

    jobs.stop(job.id)
    jobs.run_job(job)  # the old object still says "queued", like a pool thread's

    saved = jobs.load(job.id)
    assert saved.status == "stopped" and saved.finished_at
    assert fake_agent == []  # no agent call
