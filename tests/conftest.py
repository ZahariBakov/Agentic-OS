"""Shared test setup: point memory/ and data/jobs/ to a temporary folder.

Tests must never change the real memory or the real jobs.
"""

import os
import sys
from pathlib import Path

import pytest

from agentic_os import feedback, job_store, jobs, limits, mail, memory, project_memory, recos, routines, weather, workspaces

# Where the real personal data lives. No test may read or write there.
REAL_DATA = job_store.DATA_DIR.resolve()
REAL_CACHE = Path.home() / ".cache" / "agentic-os"


@pytest.fixture(autouse=True)
def no_real_data(tmp_path, monkeypatch):
    """Every test: each path in an agentic_os module that points into the real data/ or
    ~/.cache/agentic-os points into tmp_path instead.

    Without this, every fixture must remember every path (INBOX_DIR, SETTINGS_FILE, the
    lead locks, ...), and a forgotten one writes into your real data/. A path that a
    function builds from DATA_DIR at call time moves too, because DATA_DIR moves.
    The fixtures below still move some paths again, to the folders their tests look in.
    """
    moves = ((REAL_DATA, tmp_path / "real-data"), (REAL_CACHE, tmp_path / "real-cache"))
    modules = [module for name, module in sys.modules.items()
               if module is not None and (name == "agentic_os" or name.startswith("agentic_os."))]
    for module in modules:
        for name, value in list(vars(module).items()):
            if not isinstance(value, Path):
                continue
            for real, fake in moves:
                if value.is_relative_to(real):
                    monkeypatch.setattr(module, name, fake / value.relative_to(real))
                    break


    def no_network(*args, **kwargs):
        raise AssertionError("a test tried to connect to Gmail; use a fake IMAP (tests/test_mail.py)")

    monkeypatch.setattr(mail, "IMAP", no_network)

    def no_weather_network(*args, **kwargs):
        raise AssertionError("a test tried to connect to Open-Meteo; use a fake OPEN (tests/test_weather.py)")

    monkeypatch.setattr(weather, "OPEN", no_weather_network)


@pytest.fixture
def temp_memory(tmp_path, monkeypatch):
    """Use an empty memory folder in tmp_path. Return its path."""
    folder = tmp_path / "memory"
    folder.mkdir()
    monkeypatch.setattr(memory, "MEMORY_DIR", folder)
    monkeypatch.setattr(memory, "INDEX_FILE", folder / "MEMORY.md")
    monkeypatch.setattr(memory, "LOG_FILE", folder / "log.md")
    monkeypatch.setattr(memory, "WIKI_DIR", folder / "wiki")
    monkeypatch.setattr(memory, "IDENTITY_DIR", folder / "identity")
    monkeypatch.setattr(memory, "RAW_DIR", folder / "raw")
    return folder


@pytest.fixture
def temp_jobs(tmp_path, monkeypatch):
    """Use an empty jobs folder in tmp_path. Return its path.

    The Recos cards and the Claude Code history are read with the jobs, so they move too,
    and so do the jobs' isolated workspaces (git worktrees) and the project memory.
    """
    folder = tmp_path / "jobs"
    monkeypatch.setattr(job_store, "JOBS_DIR", folder)
    monkeypatch.setattr(workspaces, "WORKTREES_DIR", tmp_path / "worktrees")
    monkeypatch.setattr(project_memory, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(recos, "RECOS_FILE", tmp_path / "recos.json")
    monkeypatch.setattr(recos, "HISTORY_FILE", tmp_path / "no-history.jsonl")
    return folder


@pytest.fixture
def temp_routines(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Use an empty routines folder and state file. Return the routines folder.

    It also uses temp_jobs and temp_memory, because routines add jobs.
    """
    folder = tmp_path / "routines"
    folder.mkdir()
    monkeypatch.setattr(routines, "ROUTINES_DIR", folder)
    monkeypatch.setattr(routines, "STATE_FILE", tmp_path / "routines.json")
    return folder


@pytest.fixture
def temp_briefing(tmp_path, monkeypatch, temp_routines):
    """Everything the briefing reads, in tmp_path: jobs, memory, routines,
    skill feedback, proposals and limits. Return tmp_path."""
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", tmp_path / "feedback")
    monkeypatch.setattr(feedback, "PROPOSALS_DIR", tmp_path / "proposals")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    return tmp_path


@pytest.fixture
def open_umask():
    """The usual umask 022 for one test: a file made without an explicit mode is then 0644.

    A test that checks a 0600 mode would pass by itself under umask 077.
    """
    old = os.umask(0o022)
    yield
    os.umask(old)
