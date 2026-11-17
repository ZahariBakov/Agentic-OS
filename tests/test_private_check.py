"""Tests for the private check (private_check.py), the pre-commit hook's work."""

import subprocess

import pytest

from agentic_os import private_check

# Built from parts, so this file itself does not look like it holds a real key.
GITHUB = "ghp_" + "a1B2" * 9

DIFF = """diff --git a/notes.md b/notes.md
--- a/notes.md
+++ b/notes.md
@@ -3,0 +4,2 @@
+My name is Alex Example.
+Nothing personal here.
diff --git a/old.md b/old.md
--- a/old.md
+++ /dev/null
@@ -1 +0,0 @@
-Alex Example was here.
"""


def test_added_lines_have_path_and_number():
    lines = list(private_check.added_lines(DIFF))
    assert lines == [("notes.md", 4, "My name is Alex Example."), ("notes.md", 5, "Nothing personal here.")]


def test_private_word_and_secret_are_found():
    lines = [("a.md", 1, "Written by ALEX example"), ("b.py", 7, f"token = {GITHUB}"), ("c.md", 2, "fine")]
    found = private_check.problems_in_lines(lines, ["alex example"])
    assert found == ["a.md:1: a private word (alex example)", "b.py:7: looks like a GitHub token"]
    assert GITHUB not in " ".join(found)


def test_secret_pattern_files_skip_only_the_secret_check():
    lines = [("agentic_os/secret_scan.py", 1, f"key {GITHUB} by Alex Example")]
    assert private_check.problems_in_lines(lines, ["alex example"]) == [
        "agentic_os/secret_scan.py:1: a private word (alex example)"]


def test_private_words_file(tmp_path):
    words = tmp_path / "words"
    words.write_text("# my words\nAlex Example\n\n  alex@example.com  \n")
    assert private_check.private_words(words) == ["alex example", "alex@example.com"]
    assert private_check.private_words(tmp_path / "missing") == []


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("CLAUDE.local.md\n.private-words\n")
    (tmp_path / ".private-words").write_text("Alex Example\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run_check():
    try:
        private_check.main()
    except SystemExit as stop:
        return stop.code
    return 0


def test_clean_commit_passes(repo):
    (repo / "a.md").write_text("Hello\n")
    git(repo, "add", "a.md", ".gitignore")
    assert run_check() == 0


def test_forced_personal_file_and_word_stop_the_commit(repo, capsys):
    (repo / "CLAUDE.local.md").write_text("rules\n")
    (repo / "a.md").write_text("By Alex Example\n")
    git(repo, "add", "-f", "CLAUDE.local.md")
    git(repo, "add", "a.md")
    assert run_check() == 1
    out = capsys.readouterr().out
    assert "CLAUDE.local.md: a personal file" in out
    assert "a.md:1: a private word" in out
