"""Review 2, step 16: the minor list (ids, wording, links, scripts, the web guard)."""

import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from agentic_os import jobs, leads, memory, stream, two_questions, web, wildcards


def test_a_new_job_id_is_never_taken(temp_jobs, monkeypatch):
    jobs.save(jobs.Job(id="aaaaaaaa", task="x", created_at="2026-11-01T10:00:00"))
    ids = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(uuid, "uuid4", lambda: type("U", (), {"hex": next(ids) + "0" * 24})())

    assert jobs.new_id() == "bbbbbbbb"


def test_one_wildcard_is_singular():
    text = "1. WILDCARD: a\n2. b\n3. WILDCARD: c"

    [problem] = wildcards.check(text, 3, [1])

    assert "(only 1 is a wildcard)" in problem


def test_heading_links_and_other_url_schemes_are_not_broken():
    body = "[a](ftp://x) [b](tel:123) [c](file:///x) [[Editor#Vim]] [[Editor#Vim|vim]] [d](missing.md) [[Nope]]"

    assert memory.broken_links(Path("/x/a.md"), body, {"Editor"}) == ["missing.md", "[[Nope]]"]


def test_two_questions_help_costs_nothing(monkeypatch):
    monkeypatch.setattr(two_questions, "ask", lambda *args, **options: pytest.fail("a paid call"))
    monkeypatch.setattr(sys, "argv", ["two_questions", "--help"])

    with pytest.raises(SystemExit) as end:
        two_questions.main()

    assert end.value.code == 0


def test_stream_goes_through_ask(monkeypatch, capsys):
    """The rule: every claude call goes through ask.ask (timeout, limits, kill on error)."""
    def ask(task, model, on_event=None):
        on_event({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}})
        return {}

    monkeypatch.setattr(stream, "ask", ask)
    monkeypatch.setattr(sys, "argv", ["stream", "Say hello."])

    stream.main()

    assert "Hello" in capsys.readouterr().out


def test_keep_wildcard_of_an_unknown_job_is_404(temp_jobs):
    with pytest.raises(HTTPException) as error:
        web.api_keep_wildcard("nosuch", 1)

    assert error.value.status_code == 404


# --- the first day of real use (2026-11-09) ---

def test_auto_lead_gives_way_to_a_chosen_model(temp_jobs, monkeypatch):
    """"Auto" is the form's default; a model you chose must not be ignored by a lead."""
    monkeypatch.setattr(web, "run_in_pool", lambda job: None)
    monkeypatch.setattr(leads, "dispatch", lambda task: pytest.fail("the dispatcher ran"))

    job = web.api_add_job(web.NewJob(task="Say hi.", model="sonnet", lead="auto", memory=False))

    assert job["lead"] is None and job["model"] == "sonnet"


def test_auto_lead_alone_asks_the_dispatcher(temp_jobs, monkeypatch):
    monkeypatch.setattr(web, "run_in_pool", lambda job: None)
    monkeypatch.setattr(leads, "dispatch", lambda task: ("Pip", "rules, keyword: quick"))

    job = web.api_add_job(web.NewJob(task="A quick one.", lead="auto"))

    assert job["lead"] == "Pip"


def test_jev_without_a_key_is_skipped_quietly(monkeypatch, tmp_path, capsys):
    from agentic_os import router
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(router, "JEV_KEY_FILE", tmp_path / "no-key")

    assert router.route_jev("Say hi.") is None
    assert "failed" not in capsys.readouterr().err


def test_logs_are_not_in_a_backup(tmp_path, monkeypatch):
    """The server log holds the login link with the token."""
    from agentic_os import backups
    monkeypatch.setattr(backups, "DATA_DIR", tmp_path / "data")

    assert backups.skipped(tmp_path / "data" / "server.log")
    assert not backups.skipped(tmp_path / "data" / "jobs" / "abc.json")
