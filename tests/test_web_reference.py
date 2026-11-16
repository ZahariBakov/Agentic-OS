"""Tests for a reference pasted on the web page (builder and critic, stage 3).

We call the API functions directly, as plain Python functions, so no HTTP
client is needed. A fake pool.submit makes sure no real agent starts.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException

from agentic_os import job_store, web


@pytest.fixture
def started(monkeypatch, tmp_path, temp_jobs):
    """Use a temporary references folder and a fake pool. Return the started jobs."""
    monkeypatch.setattr(job_store, "REFERENCES_DIR", tmp_path / "references")
    submitted = []
    monkeypatch.setattr(web.pool, "submit", lambda function, job: submitted.append(job))
    return submitted


def new_job(**fields):
    return web.NewJob(task="Say hello", model="haiku", memory=False, **fields)


def test_pasted_reference_is_saved_to_a_file(started):
    summary = web.api_add_job(new_job(reference="Hello there!"))

    assert summary["reference"] is not None
    assert started[0].reference == summary["reference"]
    detail = web.api_job(summary["id"])
    assert detail["reference_text"] == "Hello there!"
    assert detail["verdicts"] == []


@pytest.mark.parametrize("reference", [None, "", "   \n"])
def test_empty_reference_means_no_critic(started, reference):
    summary = web.api_add_job(new_job(reference=reference))

    assert summary["reference"] is None
    assert web.api_job(summary["id"])["reference_text"] is None
    assert not job_store.REFERENCES_DIR.exists() or not any(job_store.REFERENCES_DIR.iterdir())


def test_failed_add_deletes_the_reference_file(started, tmp_path):
    with pytest.raises(HTTPException) as error:
        web.api_add_job(new_job(reference="Hello there!", folder=str(tmp_path / "missing")))

    assert error.value.status_code == 400
    assert started == []
    assert list(job_store.REFERENCES_DIR.iterdir()) == []


def test_missing_reference_file_does_not_break_the_job_view(started):
    summary = web.api_add_job(new_job(reference="Hello there!"))
    Path(summary["reference"]).unlink()  # someone deleted the file

    assert web.api_job(summary["id"])["reference_text"] is None
