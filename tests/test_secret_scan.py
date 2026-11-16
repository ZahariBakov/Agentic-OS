"""Tests for the secret scan (secret_scan.py) and the places that use it."""

import pytest

from agentic_os import feedback, leads, memory, secret_scan

# Built from parts, so this file itself does not look like it holds real keys.
GITHUB = "ghp_" + "a1B2" * 9
ANTHROPIC = "sk-ant-" + "x9Y8" * 6


@pytest.mark.parametrize("text, what", [
    (f"The token is {GITHUB}.", "a GitHub token"),
    (f"key {ANTHROPIC}", "an Anthropic API key"),
    ("AKIA" + "ABCDEFGHIJKLMNOP", "an AWS access key"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "a private key"),
    ("password = hunter2hunter2", "a password or secret value"),
    ("API_KEY: 0123456789abcdef", "a password or secret value"),
    # review 2, item 15: forms the scan missed
    ('{"password": "hunter2hunter2"}', "a password or secret value"),
    ('{"api_key": "abcd1234efgh"}', "a password or secret value"),
    ("DB_PASSWORD=s3cr3tvalue1", "a password or secret value"),
    ("AWS_SECRET_ACCESS_KEY=" + "wJalrXUt" * 3, "a password or secret value"),
    ("client_secret: abcdefgh12345", "a password or secret value"),
    ("-----BEGIN PGP PRIVATE KEY BLOCK-----", "a private key"),
    ("-----BEGIN ENCRYPTED PRIVATE KEY-----", "a private key"),
    ("token " + "0a1b2c3d" * 6, "a hex token"),
])
def test_secrets_are_found(text, what):
    assert secret_scan.find_secret(text) == what


@pytest.mark.parametrize("text", [
    "The key is in ~/.config/agentic-os/openrouter.key.",
    "password = ${DB_PASSWORD}",
    "api_key: <your key here>",
    "secret: changeme123",
    "Use a strong password for the database.",
    "",
    # review 2, item 15: false alarms (they point to where the secret is kept)
    "password: ~/.config/agentic-os/db-password",
    'api_key = os.getenv("OPENROUTER_KEY")',
    "token = process.env.GITHUB_TOKEN",
    "api_key = None",
    "commit " + "e08c957a" * 5,  # 40 hex: a git commit id
    "sha256 " + "0a1b2c3d" * 8,  # 64 hex: a checksum
])
def test_ordinary_text_is_not_a_secret(text):
    assert secret_scan.find_secret(text) is None


def test_a_note_with_a_secret_is_not_saved(temp_memory):
    notes = [{"title": "Fine", "body": "Nothing secret."},
             {"title": "Server", "body": f"Use the token {GITHUB} to log in."}]
    with pytest.raises(ValueError, match="the note \"Server\" looks like it contains a GitHub token"):
        memory.save_notes(notes)
    assert list((temp_memory / "wiki").rglob("*.md")) == []  # nothing is written, not even the fine one


def test_a_lesson_with_a_secret_is_refused():
    with pytest.raises(ValueError, match="the lesson"):
        leads.check_line(f"Log in with {ANTHROPIC} before the tests.")


def test_a_handoff_with_a_secret_keeps_the_old_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(leads, "LEADS_DIR", tmp_path)
    monkeypatch.setattr(leads, "folder", lambda name: tmp_path)
    reply = {"session_id": "s1", "total_cost_usd": 0.0, "usage": {}, "result": "Done.\n<handoff>Next: fix X.</handoff>"}
    monkeypatch.setattr(leads, "context_tokens", lambda reply: 0)
    leads.finish("Ada", reply)
    reply["result"] = f"Done.\n<handoff>Token {GITHUB}</handoff>"
    leads.finish("Ada", reply)
    assert leads.handoff("Ada").strip() == "Next: fix X."
    assert "the handoff of Ada" in capsys.readouterr().out


def test_a_proposal_with_a_secret_is_not_saved(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "note.md").write_text("---\nname: note\n---\n")
    monkeypatch.setattr(feedback, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(feedback, "PROPOSALS_DIR", tmp_path / "proposals")
    text = f'<skill-file name="note">\n---\nname: note\n---\nUse {GITHUB}\n</skill-file>'
    assert feedback.save_proposals(text) == []


def test_a_job_saves_the_good_notes_when_one_note_holds_a_secret(temp_memory):
    """Review 2, item 15: one refused note must not drop the others of the reply."""
    notes = [{"title": "Fine", "body": "Nothing secret."},
             {"title": "Server", "body": f"Use the token {GITHUB} to log in."}]

    assert memory.note_secret(notes[0]) is None
    assert memory.note_secret(notes[1]) == "a GitHub token"
