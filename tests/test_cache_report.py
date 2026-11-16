"""Tests for the cache report (agentic_os/cache_report.py). Fake session files, no agent."""

import json
from datetime import datetime, timezone

import pytest

from agentic_os import cache_report, jobs

START = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc).timestamp()
NOW = START + 2 * 86400


def line(minutes, written=0, model="claude-opus-5-5", hour_cache=True, message_id=None,
         session="s1", cwd="/home/someone/project"):
    """Return one answer line of a session file, `minutes` after START."""
    moment = datetime.fromtimestamp(START + minutes * 60, timezone.utc)
    split = {"ephemeral_1h_input_tokens": written if hour_cache else 0,
             "ephemeral_5m_input_tokens": 0 if hour_cache else written}
    usage = {"input_tokens": 2, "cache_creation_input_tokens": written, "cache_read_input_tokens": 500,
             "output_tokens": 100, "cache_creation": split}
    return {"type": "assistant", "timestamp": moment.isoformat().replace("+00:00", "Z"),
            "sessionId": session, "cwd": cwd, "uuid": f"u{minutes}",
            "message": {"id": message_id or f"m{minutes}", "model": model, "usage": usage}}


def write_session(folder, name, lines):
    """Write a session file with the given answer lines (and a user line, which is skipped)."""
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
    text += "".join(json.dumps(item) + "\n" for item in lines)
    (folder / name).write_text(text, encoding="utf-8")


@pytest.fixture
def projects(tmp_path, monkeypatch, temp_jobs):
    """A temporary ~/.claude/projects and Agent Office work folder."""
    monkeypatch.setattr(cache_report, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(cache_report, "OFFICE_DIR", tmp_path / "office")
    return tmp_path / "projects"


def test_cold_reload_after_the_hour_with_pings(projects):
    write_session(projects / "p", "a.jsonl", [line(0, 50_000), line(90, 100_000)])
    result = cache_report.report(now=NOW)
    chats = result["chats"]
    assert (chats["sessions"], chats["calls"], chats["cold"], chats["savable"]) == (1, 2, 1, 1)
    # 100k tokens on opus ($5): write 2x minus read 0.1x = 1.9 * 0.5 = $0.95
    assert chats["cold_usd"] == pytest.approx(0.95)
    # 90 minutes / 50 -> 1 ping: a read of 100k (0.1 * 0.5 = $0.05) plus the small extra
    assert chats["ping_usd"] == pytest.approx(0.05 + cache_report.PING_EXTRA_USD)
    assert chats["net_usd"] == pytest.approx(0.95 - 0.0505)
    assert chats["pays"] is True
    assert result["worst"][0]["pings"] == 1 and result["worst"][0]["model"] == "opus"


def test_no_reload_inside_the_cache_life_or_when_small(projects):
    write_session(projects / "p", "a.jsonl", [line(0, 50_000), line(55, 100_000), line(200, 5_000)])
    chats = cache_report.report(now=NOW)["chats"]
    assert chats["calls"] == 3
    assert chats["cold"] == 0  # 55 min < 1 hour; 145 min but only 5k tokens
    assert chats["pays"] is False


def test_five_minute_cache_and_long_gap(projects):
    write_session(projects / "p", "a.jsonl", [line(0, 20_000, model="claude-haiku-5-5", hour_cache=False),
                                              line(10, 20_000, model="claude-haiku-5-5", hour_cache=False),
                                              line(10 + 300, 20_000, model="claude-haiku-5-5", hour_cache=False)])
    chats = cache_report.report(now=NOW)["chats"]
    assert chats["cold"] == 2
    assert chats["savable"] == 1  # 10 min: pings every 4 min -> 2 pings
    assert chats["long_gaps"] == 1  # 5 hours: too long to ping
    # haiku ($1), 5 minute cache: (1.25 - 0.1) * 0.02 per reload
    assert chats["cold_usd"] == pytest.approx(2 * 1.15 * 0.02)
    assert chats["ping_usd"] == pytest.approx(2 * (0.1 * 0.02 + cache_report.PING_EXTRA_USD))


def test_one_message_on_several_lines_counts_once(projects):
    write_session(projects / "p", "a.jsonl", [line(0, 50_000), line(1, 50_000, message_id="m0")])
    assert cache_report.report(now=NOW)["chats"]["calls"] == 1


def test_office_by_folder_and_by_job_session(projects):
    write_session(projects / "o", "a.jsonl",
                  [line(0, 30_000, cwd=str(cache_report.OFFICE_DIR / "workspaces" / "Pip")), line(70, 30_000)])
    write_session(projects / "p", "b.jsonl", [line(0, 30_000, session="lead-1"), line(70, 30_000, session="lead-1")])
    jobs.save(jobs.Job(id="j1", task="x", session_id="lead-1"))
    result = cache_report.report(now=NOW)
    assert result["office"]["sessions"] == 2 and result["office"]["cold"] == 2
    assert result["chats"]["sessions"] == 0
    assert all(item["office"] for item in result["worst"])


def test_old_calls_and_bad_lines_are_skipped(projects):
    folder = projects / "p"
    write_session(folder, "a.jsonl", [line(0, 50_000), line(90, 100_000)])
    with open(folder / "a.jsonl", "a", encoding="utf-8") as file:
        file.write('{"usage": "assistant", broken\n')
    # 30 days back from NOW does not reach START when NOW is 40 days later
    assert cache_report.report(now=START + 40 * 86400)["chats"]["calls"] == 0
    assert cache_report.report(now=NOW)["chats"]["calls"] == 2


def test_text_says_the_verdict(projects):
    write_session(projects / "p", "a.jsonl", [line(0, 50_000), line(90, 100_000)])
    text = cache_report.text(cache_report.report(now=NOW))
    assert "Agent Office agents: no model calls." in text
    assert "Verdict: keep-warm pays." in text
    assert "The most expensive reloads:" in text
    assert text.isascii()


def test_no_projects_folder(projects):
    result = cache_report.report(now=NOW)
    assert result["chats"]["calls"] == 0 and result["worst"] == []


def test_one_ping_covers_a_gap_up_to_the_cache_life_plus_one_ping(projects):
    # 1 hour cache: the last call keeps it to 60 min, a ping at 50 min keeps it to 110 min.
    write_session(projects / "p", "a.jsonl", [line(0, 50_000), line(105, 100_000)])
    assert cache_report.report(now=NOW)["worst"][0]["pings"] == 1
    write_session(projects / "p", "a.jsonl", [line(0, 50_000), line(115, 100_000)])
    assert cache_report.report(now=NOW)["worst"][0]["pings"] == 2
