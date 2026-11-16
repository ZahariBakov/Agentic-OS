"""Tests for the saved results (artifacts.py)."""

from agentic_os import artifacts, jobs


def test_a_stray_markdown_file_does_not_break_the_index(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path)
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "INDEX.md")
    (tmp_path / "notes.md").write_text("Just some notes, no header.\n")
    job = jobs.Job(id="abc123", task="Say hi.", created_at="2026-11-05T10:00:00", result="Hi!")

    artifacts.save(job)

    assert [meta["job"] for meta in artifacts.all_artifacts()] == ["abc123"]
    assert "abc123.md" in (tmp_path / "INDEX.md").read_text()
