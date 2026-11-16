"""Tests for `jobs save <id> "Title"`: a good answer goes back into the memory."""

import pytest

from agentic_os import jobs, memory


def make_job(status, result="Sofia is the capital of Bulgaria."):
    """Save a job with this status and result, and return it."""
    job = jobs.Job(id="job-1", task="What is the capital of Bulgaria?",
                   status=status, result=result)
    jobs.save(job)
    return job


def test_done_job_becomes_a_note(temp_memory, temp_jobs):
    make_job("done")

    path = jobs.save_result("job-1", "Capital of Bulgaria", "geography")

    # The note is in the right area, with the job's result as its body.
    assert path == temp_memory / "wiki" / "geography" / "capital-of-bulgaria.md"
    meta, body = memory.read_note(path)
    assert meta["title"] == "Capital of Bulgaria"
    assert meta["source"] == "job job-1"
    assert body == "Sofia is the capital of Bulgaria."
    # The map lists the note, and the log has one line about it.
    assert "[Capital of Bulgaria](wiki/geography/capital-of-bulgaria.md)" in (
        temp_memory / "MEMORY.md").read_text()
    assert "created  wiki/geography/capital-of-bulgaria.md" in (
        temp_memory / "log.md").read_text()


def test_same_title_updates_the_same_note(temp_memory, temp_jobs):
    make_job("done", result="First answer.")
    first = jobs.save_result("job-1", "Capital of Bulgaria", "geography")
    make_job("done", result="Better answer.")

    # Another area, but the same title: the old note is updated, not copied.
    second = jobs.save_result("job-1", "Capital of Bulgaria", "general")

    assert second == first
    assert memory.read_note(first)[1] == "Better answer."
    assert len(list((temp_memory / "wiki").glob("*/*.md"))) == 1
    assert "updated  wiki/geography/capital-of-bulgaria.md" in (
        temp_memory / "log.md").read_text()


@pytest.mark.parametrize("status", ["queued", "running", "failed", "stopped"])
def test_unfinished_job_is_not_saved(temp_memory, temp_jobs, status):
    make_job(status)

    with pytest.raises(SystemExit, match="is not done"):
        jobs.save_result("job-1", "Capital of Bulgaria", "geography")

    assert not (temp_memory / "wiki").exists()
