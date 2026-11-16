"""Tests for Recos: repeated requests become suggested routines (recos.py).

A fake history file and a fixed `now`, so the tests never read your real
Claude Code history and never depend on the clock.
"""

import json
from datetime import datetime, timedelta

from agentic_os import jobs, recos, routines

NOW = datetime(2026, 11, 6, 12, 0)  # a Friday


def write_history(path, entries):
    """entries: (text, datetime). Write them like Claude Code does: one JSON object per line."""
    lines = [json.dumps({"display": text, "timestamp": int(at.timestamp() * 1000), "project": "/x"})
             for text, at in entries]
    path.write_text("\n".join(lines) + "\n")
    return path


def suggest(tmp_path, entries, **options):
    return recos.suggest(now=NOW, history=write_history(tmp_path / "history.jsonl", entries), **options)


def test_words_skip_small_words_links_paths_and_numbers():
    text = "Please check the logs in /var/log/app.log for 3 errors, see https://example.com"
    assert recos.words(text) == {"check", "logs", "errors", "see"}


def test_jaccard():
    assert recos.jaccard(frozenset("ab"), frozenset("ab")) == 1.0
    assert recos.jaccard(frozenset("ab"), frozenset("bc")) == 1 / 3
    assert recos.jaccard(frozenset(), frozenset()) == 0.0


def test_three_similar_requests_make_one_card(tmp_path, temp_jobs):
    monday = NOW - timedelta(days=4)  # Monday 2 November, 12:00
    entries = [
        ("Summarize the new emails from the client into a short list", monday.replace(hour=9)),
        ("Summarize new client emails into a short list", monday.replace(hour=9, minute=30)),
        ("Summarize the new emails of the client as a short list", monday - timedelta(days=7, hours=3)),
        ("Fix the failing test in the parser module", monday),  # different: no card
    ]
    cards = suggest(tmp_path, entries, days=14)

    assert len(cards) == 1
    found = cards[0]
    assert found["count"] == 3
    assert {"summarize", "emails", "client"} <= set(found["title"].split(" | "))
    assert found["routine"]["schedule"] == "weekly mon 09:00"
    assert found["routine"]["mode"] == "active"
    assert found["routine"]["task"] == entries[1][0]  # the newest request
    routines.parse_schedule(found["routine"]["schedule"])  # a schedule our routines understand


def test_a_task_on_many_days_is_daily_and_checking_is_passive(tmp_path, temp_jobs):
    entries = [(f"Check the server status page for new errors", NOW - timedelta(days=day, hours=4))
               for day in range(5)]
    found = suggest(tmp_path, entries)[0]
    assert found["routine"]["schedule"] == "daily 08:00"
    assert found["routine"]["mode"] == "passive"


def test_skipped_requests(tmp_path, temp_jobs):
    old = NOW - timedelta(days=30)
    entries = (
        [("/compact keep the decisions please", NOW)] * 3  # slash commands
        + [("What does the router do with long tasks?", NOW)] * 3  # questions
        + [("Next step.", NOW)] * 3  # too short
        + [("Summarize the new emails from the client", old)] * 3  # too old
    )
    assert suggest(tmp_path, entries) == []


def test_a_broken_history_line_is_skipped(tmp_path, temp_jobs):
    path = write_history(tmp_path / "history.jsonl", [("Summarize the client emails today", NOW)] * 3)
    path.write_text("not json\n{}\n" + path.read_text())
    assert recos.suggest(now=NOW, history=path)[0]["count"] == 3


def test_jobs_count_but_not_automatic_ones(tmp_path, temp_jobs):
    task = "Write the weekly report about the sales numbers"
    for extra in [{}, {}, {"routine": "weekly-report"}, {"skill": "summarize"}]:
        job = jobs.add(task, "haiku", use_memory=False)
        for key, value in extra.items():
            setattr(job, key, value)
        jobs.save(job)
    missing = tmp_path / "no-history.jsonl"

    assert recos.suggest(now=datetime.now(), history=missing) == []  # only 2 by hand
    write_history(missing, [(task, datetime.now())])
    found = recos.suggest(now=datetime.now(), history=missing)[0]
    assert found["count"] == 3
    assert "Agent Office" in found["what"] and "Claude Code" in found["what"]
