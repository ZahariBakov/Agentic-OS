"""Tests for the Recos panel API (Recos stage 3). No agent runs: web.pool.submit is replaced."""

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from agentic_os import jobs, leads, recos, web


@pytest.fixture
def started(tmp_path, monkeypatch, temp_routines):
    """Temporary jobs, routines, Recos and leads. Return the jobs the web pool would start."""
    monkeypatch.setattr(leads, "LEADS_DIR", tmp_path / "leads")
    role = {"area": "quick tasks", "model": "haiku", "effort": "low", "folder": str(tmp_path),
            "tools": ["Read"], "keywords": [], "instructions": "Be short."}  # like tests/test_lessons.py
    monkeypatch.setattr(leads, "roster", lambda: {"pip": role})
    submitted = []
    monkeypatch.setattr(web.pool, "submit", lambda function, job: submitted.append(job))
    return submitted


def correction(note):
    job = jobs.add("Write the report.", "haiku", use_memory=False, lead="pip")
    job.status = "done"
    job.review = {"verdict": "rejected", "note": note, "at": (datetime.now() - timedelta(hours=1)).isoformat()}
    jobs.save(job)


def test_the_first_get_computes_the_cards(started):
    correction("Write the dates in the Bulgarian format, day first")
    correction("Use the Bulgarian date format: the day first")
    assert recos.load_recos()["at"] is None

    reply = web.api_recos()

    assert reply["at"] and [found["kind"] for found in reply["cards"]] == ["memory"]


def test_set_up_starts_the_lesson_job(started):
    correction("Write the dates in the Bulgarian format, day first")
    correction("Use the Bulgarian date format: the day first")
    card_id = web.api_recos_refresh()["cards"][0]["id"]

    reply = web.api_recos_setup(web.RecosSetup(ids=[card_id]))

    assert reply["cards"] == [] and "Lesson job" in reply["done"][0]
    assert len(started) == 1 and started[0].lead == "pip"


def test_dismiss(started):
    correction("Write the dates in the Bulgarian format, day first")
    correction("Use the Bulgarian date format: the day first")
    card_id = web.api_recos_refresh()["cards"][0]["id"]

    assert web.api_recos_dismiss(card_id)["cards"] == []
    with pytest.raises(HTTPException) as error:
        web.api_recos_dismiss(card_id)
    assert error.value.status_code == 404


@pytest.mark.parametrize("ids, code", [([], 400), (["nope"], 404)])
def test_wrong_set_up(started, ids, code):
    with pytest.raises(HTTPException) as error:
        web.api_recos_setup(web.RecosSetup(ids=ids))

    assert error.value.status_code == code and started == []
