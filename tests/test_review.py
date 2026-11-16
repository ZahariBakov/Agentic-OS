"""Tests for the review of a finished job: approve, reject, comment, and undo as a bad result.

No real agent runs: job_run.ask is replaced with a fake that records each call.
"""

import pytest

from agentic_os import artifacts, changes, job_run, job_store, jobs, limits


@pytest.fixture
def calls(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Replace the agent call, and move every file a run writes into tmp_path. Return the calls."""
    monkeypatch.setattr(job_run, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(job_store, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(job_run, "DEFAULT_WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "artifacts" / "INDEX.md")
    monkeypatch.setattr(changes, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    made = []

    def ask(prompt, model, session_id=None, **options):
        made.append({"prompt": prompt, "session": session_id})
        return {"result": f"answer {len(made)}", "total_cost_usd": 0.01, "session_id": session_id or "s1"}

    monkeypatch.setattr(job_run, "ask", ask)
    return made


@pytest.fixture
def edited(calls, tmp_path):
    """A finished job whose agent changed app.py. Return (job, the changed file)."""
    job = jobs.add("Fix the app.", model="haiku", use_memory=False)
    jobs.run_job(job)
    app = tmp_path / "app.py"
    app.write_text("old\n")
    changes.snapshot_before(job.id, app)  # what the edit hook does before the first edit
    app.write_text("new\n")
    return jobs.load(job.id), app


def test_approve_keeps_the_verdict(edited):
    job, _ = edited

    jobs.approve(job.id, "Nice.")

    saved = jobs.load(job.id)
    assert saved.review["verdict"] == "approved" and saved.review["note"] == "Nice."
    assert saved.signals == []


def test_reject_is_a_bad_result_without_a_new_run(edited, calls):
    job, _ = edited

    jobs.reject(job.id, "Wrong file.")

    saved = jobs.load(job.id)
    assert saved.review["verdict"] == "rejected" and saved.signals == ["rejected"]
    assert saved.status == "done" and len(calls) == 1  # no new agent call


def test_reject_needs_a_reason(edited):
    job, _ = edited

    with pytest.raises(ValueError, match="Say why"):
        jobs.reject(job.id, "  ")


def test_comments_go_back_to_the_same_session(edited, calls):
    job, app = edited

    queued, start, _ = jobs.comment(job.id, "Also log the error.", {str(app): "Keep the old name."})
    jobs.run_job(queued)

    assert start
    prompt = calls[1]["prompt"]
    assert calls[1]["session"] == "s1"
    assert "asks for changes" in prompt and "Also log the error." in prompt
    assert f"{app}: Keep the old name." in prompt
    saved = jobs.load(job.id)
    assert saved.review["verdict"] == "changes" and saved.signals == ["rework"]
    assert saved.status == "done" and saved.result == "answer 2"


def test_the_conversation_shows_your_comment_not_the_prompt(edited):
    job, app = edited

    jobs.comment(job.id, "Also log the error.", {str(app): "Keep the old name."})

    [message] = jobs.read_inbox(job.id)
    assert message["note"] == f"Also log the error.\n{app.name}: Keep the old name."
    assert "asks for changes" in message["text"]  # the agent still gets the full prompt
    assert jobs.load(job.id).review["note"] == message["note"]  # Recos groups your words, not the prompt


def test_a_comment_on_a_file_the_job_did_not_change(edited, tmp_path):
    job, _ = edited

    with pytest.raises(ValueError, match="did not change"):
        jobs.comment(job.id, "", {str(tmp_path / "other.py"): "Why?"})


def test_a_running_job_cannot_be_reviewed(calls):
    job = jobs.add("Fix the app.", model="haiku", use_memory=False)
    job.status = "running"
    jobs.save(job)

    for review in (lambda: jobs.approve(job.id), lambda: jobs.comment(job.id, "More.")):
        with pytest.raises(ValueError, match="when it has finished"):
            review()


def test_undo_is_a_bad_result_once(edited):
    job, app = edited

    assert jobs.undo(job.id) == [str(app)]
    assert app.read_text() == "old\n"
    jobs.undo(job.id)  # nothing more to undo: no second signal

    assert jobs.load(job.id).signals == ["undone"]
