"""Tests for ingest: raw files and meeting transcripts -> ingest jobs. No real agent runs."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agentic_os import job_store, jobs, memory, routines

MONDAY_9 = datetime(2026, 11, 2, 9, 0)


def raw_file(relative, text="Some source text."):
    path = memory.RAW_DIR / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def names(paths):
    return [str(path.relative_to(memory.RAW_DIR)) for path in paths]


def test_a_source_gets_the_ingest_task(temp_memory, temp_jobs):
    path = raw_file("article.md")

    job = jobs.ingest(path)

    assert job.task == jobs.INGEST_TASK.format(path=path.resolve())
    assert (job.source, job.model, job.effort) == (str(path.resolve()), "sonnet", "medium")
    assert jobs.load(job.id).source == job.source  # saved, not only in memory


def test_a_meeting_gets_the_meeting_task(temp_memory, temp_jobs):
    path = raw_file("meetings/2026-11-02-planning.txt")

    job = jobs.ingest(path)

    assert job.task == jobs.MEETING_TASK.format(path=path.resolve())
    assert 'area="meetings"' in job.task


def test_only_files_in_raw_can_be_ingested(temp_memory, temp_jobs, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("no")

    with pytest.raises(ValueError, match="Not a file in"):
        jobs.ingest(outside)


def test_new_sources(temp_memory, temp_jobs):
    for name in ["a-done.md", "b-queued.md", "c-failed.md", "d-old-job.md", "e-new.md",
                 "meetings/f-new.txt", "notes.pdf", ".gitkeep"]:
        raw_file(name)
    for name, status in [("a-done.md", "done"), ("b-queued.md", "queued"), ("c-failed.md", "failed")]:
        job = jobs.ingest(memory.RAW_DIR / name)
        job.status = status
        jobs.save(job)
    # An old ingest job, from before the `source` field: the path is only in its task.
    old = jobs.Job(id="old", status="done", created_at=MONDAY_9.isoformat(),
                   task=jobs.INGEST_TASK.format(path=(memory.RAW_DIR / "d-old-job.md").resolve()))
    jobs.save(old)

    # done, queued and the old job count as handled; a failed ingest is tried again;
    # only .md and .txt files are sources.
    assert names(jobs.new_sources()) == ["c-failed.md", "e-new.md", "meetings/f-new.txt"]


def test_ingest_new_has_a_limit(temp_memory, temp_jobs):
    for number in range(4):
        raw_file(f"source-{number}.md")

    added = jobs.ingest_new(limit=3)

    assert len(added) == 3
    assert names(jobs.new_sources()) == ["source-3.md"]  # the queued ones are handled


# --- the routine action ---

def write_inbox(folder, max_jobs=2, mode="passive"):
    (folder / "inbox.md").write_text(
        f"---\nschedule: daily 08:00\nmode: {mode}\naction: ingest-new\nmax_jobs: {max_jobs}\n---\n")


def test_ingest_routine_adds_jobs_for_new_sources(temp_routines, monkeypatch):
    monkeypatch.setattr(jobs, "run_job", lambda job: None)
    write_inbox(temp_routines, max_jobs=2)
    for name in ["one.md", "two.md", "three.md"]:
        raw_file(name)

    first = routines.run_now("inbox", MONDAY_9)
    second = routines.run_now("inbox", MONDAY_9)
    third = routines.run_now("inbox", MONDAY_9)

    assert [len(first), len(second), len(third)] == [2, 1, 0]  # the limit, then the rest, then nothing
    assert all(job.routine == "inbox" and job.source for job in jobs.all_jobs())
    assert routines.summary(MONDAY_9)[0]["what"] == "action ingest-new"


def test_ingest_routine_with_nothing_to_do_does_not_count_as_started(temp_routines, monkeypatch):
    monkeypatch.setattr(jobs, "run_job", lambda job: None)
    write_inbox(temp_routines)
    routines.tick(MONDAY_9)

    assert routines.tick(MONDAY_9 + timedelta(days=1)) == []
    assert jobs.all_jobs() == []


def test_unknown_action_is_a_mistake(temp_routines, capsys):
    (temp_routines / "bad.md").write_text("---\nschedule: daily 08:00\naction: delete-all\n---\n")

    assert routines.all_routines() == []
    assert "unknown action delete-all" in capsys.readouterr().out


def test_ingest_routine_marks_its_jobs_in_the_one_save(temp_routines, monkeypatch):
    """Review 2, item 52: no second write after jobs.add (a worker may hold the job already)."""
    monkeypatch.setattr(jobs, "run_job", lambda job: None)

    def no_update(job_id, **fields):
        raise AssertionError("a second save")

    monkeypatch.setattr(job_store, "update", no_update)
    monkeypatch.setattr(jobs, "update", no_update)  # the old name, for callers outside the job modules
    write_inbox(temp_routines, max_jobs=2)
    raw_file("one.md")

    [job] = routines.run_now("inbox", MONDAY_9)

    assert jobs.load(job.id).routine == "inbox"


# --- review 2, step 14 ---

def test_a_link_out_of_raw_is_skipped_and_the_rest_is_ingested(temp_memory, temp_jobs, tmp_path):
    """Item 34: a symlink to a file outside raw/ stopped every run (SystemExit)."""
    outside = tmp_path / "secret.md"
    outside.write_text("Not for the memory.")
    raw_file("a-ok.md")
    (memory.RAW_DIR / "b-link.md").symlink_to(outside)
    raw_file("c-ok.md")

    added = jobs.ingest_new()

    assert sorted(Path(job.source).name for job in added) == ["a-ok.md", "c-ok.md"]


def test_upper_case_suffixes_are_sources(temp_memory, temp_jobs):
    """Item 55: Notes.TXT and Notes.MD are sources too."""
    raw_file("Notes.TXT")
    raw_file("Plan.MD")

    assert names(jobs.new_sources()) == ["Notes.TXT", "Plan.MD"]
