"""Tests for the lesson drafts on the web page (review, stage 4).

We call the API functions directly. A lead "pip" lives in tmp_path; no agent runs.
"""

import pytest
from fastapi import HTTPException

from agentic_os import leads, web


@pytest.fixture
def pip(tmp_path, monkeypatch):
    """A lead "pip" with one draft. Return the draft."""
    monkeypatch.setattr(leads, "roster", lambda: {"pip": {"area": "quick tasks"}})
    monkeypatch.setattr(leads, "LEADS_DIR", tmp_path / "leads")
    return leads.add_draft("pip", "Ask before you delete.", "I deleted a file.", "j1", "the user undid your file changes")


def test_the_list_has_the_drafts(pip):
    [lead] = web.api_lessons()

    assert lead["lead"] == "pip" and [draft["id"] for draft in lead["drafts"]] == [pip["id"]]


def test_keep_in_your_own_words(pip):
    reply = web.api_lesson_action("pip", pip["id"], "keep", web.KeepLesson(text="Ask before you delete or move."))

    assert reply["kept"]["edited"]
    [lead] = web.api_lessons()
    assert lead["drafts"] == [] and [lesson["text"] for lesson in lead["kept"]] == ["Ask before you delete or move."]


def test_drop(pip):
    web.api_lesson_action("pip", pip["id"], "drop")

    [lead] = web.api_lessons()
    assert lead["drafts"] == [] and lead["kept"] == []


@pytest.mark.parametrize("lead, draft_id, action, text, code", [
    ("nobody", "x1", "keep", None, 404),  # an unknown lead
    ("pip", "../x", "keep", None, 404),  # a bad draft id
    ("pip", "x1", "eat", None, 404),  # an unknown action
    ("pip", "missing1", "keep", None, 400),  # no such draft
    ("pip", None, "keep", "x" * 300, 400),  # the edited line is too long
])
def test_wrong_requests(pip, lead, draft_id, action, text, code):
    with pytest.raises(HTTPException) as error:
        web.api_lesson_action(lead, draft_id or pip["id"], action, web.KeepLesson(text=text))

    assert error.value.status_code == code
    assert len(web.api_lessons()[0]["drafts"]) == 1  # nothing changed
