"""Tests for the skill feedback backlog and the <skill-file> proposals (feedback.py)."""

import json

import pytest

from agentic_os import feedback

SKILL = "---\nname: calibrate\ndescription: x\n---\n\nImprove the skill.\n"


@pytest.fixture
def skills(tmp_path, monkeypatch):
    """A temporary skills folder with the skills calibrate and note."""
    folder = tmp_path / "skills"
    folder.mkdir()
    for name in ("calibrate", "note"):
        (folder / f"{name}.md").write_text(SKILL.replace("calibrate", name))
    monkeypatch.setattr(feedback, "SKILLS_DIR", folder)
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", folder / "feedback")
    monkeypatch.setattr(feedback, "PROPOSALS_DIR", tmp_path / "proposals")
    return folder


def test_a_skill_file_with_an_example_block_inside_is_kept_whole(skills):
    body = ("---\nname: calibrate\n---\n\nAnswer with:\n<skill-file name=\"{input}\">\n...\n</skill-file>\n"
            "5. Step five.\n6. Step six.\n")
    text = f"Here it is.\n<skill-file name=\"calibrate\">\n{body}</skill-file>\nThe end."

    assert feedback.save_proposals(text) == ["calibrate"]
    saved = (feedback.PROPOSALS_DIR / "calibrate.md").read_text()
    assert "6. Step six." in saved  # not cut at the example's closing tag
    assert feedback.without_skill_files(text) == "Here it is.\n\nThe end."


def test_two_blocks_and_a_block_without_an_end(skills):
    text = ('<skill-file name="note">\n---\na\n</skill-file>\n'
            '<skill-file name="calibrate">\n---\nb\n</skill-file>\n'
            '<skill-file name="note">\n---\nnever closed\n')
    assert [block[0] for block in feedback.skill_files(text)] == ["note", "calibrate"]


def test_accept_closes_only_the_feedback_the_proposal_used(skills):
    feedback.add("note", "Shorter titles.")
    feedback.save_proposals('<skill-file name="note">\n---\nnew\n</skill-file>')
    feedback.add("note", "Use dates.")  # came after the proposal: not used in it

    feedback.accept("note")

    assert [item.split("  ", 1)[1] for item in feedback.open_items("note")] == ["Use dates."]
    assert not (feedback.PROPOSALS_DIR / "note.feedback.json").exists()


def test_an_old_proposal_without_its_feedback_list_closes_all(skills):
    feedback.add("note", "Shorter titles.")
    feedback.PROPOSALS_DIR.mkdir()
    (feedback.PROPOSALS_DIR / "note.md").write_text("---\nnew\n")

    feedback.accept("note")

    assert feedback.open_items("note") == []


def test_reject_removes_the_feedback_list_too(skills):
    feedback.save_proposals('<skill-file name="note">\n---\nnew\n</skill-file>')
    assert json.loads((feedback.PROPOSALS_DIR / "note.feedback.json").read_text()) == []
    feedback.reject("note")
    assert list(feedback.PROPOSALS_DIR.iterdir()) == []
