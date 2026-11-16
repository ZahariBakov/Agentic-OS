"""Review 2, step 14: bad inputs (documents, the router log, settings, the roster) stop nothing else."""

import json

import pytest

from agentic_os import documents, job_store, jobs, leads, memory, router, web


def test_one_document_with_an_os_error_does_not_stop_the_others(temp_memory, monkeypatch):
    """Item 36: an OSError (a long name, a read-only folder) stopped convert_new on every run."""
    raw = memory.RAW_DIR
    raw.mkdir(parents=True)
    (raw / "a.html").write_text("<p>A</p>")
    (raw / "b.html").write_text("<p>B</p>")
    real_convert = documents.convert

    def convert(document):
        if document.name == "a.html":
            raise PermissionError(13, "Permission denied")
        return real_convert(document)

    monkeypatch.setattr(documents, "convert", convert)
    converted, problems = documents.convert_new()

    assert [path.name for path in converted] == ["b.html.md"]
    assert problems == ["a.html: Permission denied"]


def test_a_router_log_line_with_a_null_cost(temp_jobs, tmp_path, monkeypatch):
    """Item 40: Jev keeps OpenRouter's null cost; /api/costs was a 500 for good."""
    log = tmp_path / "router_log.jsonl"
    log.write_text(json.dumps({"router": "jev", "model": "haiku", "cost": None}) + "\n"
                   + json.dumps({"router": "haiku", "model": "sonnet", "cost": 0.001}) + "\n")
    monkeypatch.setattr(web, "ROUTER_LOG", log)

    costs = web.api_costs()

    assert costs["total"] == pytest.approx(0.001)


def test_a_broken_settings_file_can_be_fixed(temp_jobs, tmp_path, monkeypatch):
    """Item 56: a hand-broken data/settings.json blocked every editing job and every fix."""
    monkeypatch.setattr(job_store, "SETTINGS_FILE", tmp_path / "settings.json")
    job_store.SETTINGS_FILE.write_text("{ not json")

    assert jobs.settings()["auto_approve_plans_usd"] == 0.0  # the defaults
    jobs.set_auto_approve(0.25)  # writes a good file

    assert json.loads(job_store.SETTINGS_FILE.read_text())["auto_approve_plans_usd"] == 0.25


def test_a_failed_router_log_keeps_the_free_decision(monkeypatch):
    """Item 97: a log error made route() fall through to the paid Haiku router."""
    def broken_log(decision):
        raise OSError("disk full")

    def paid(task):
        raise AssertionError("the paid router must not run")

    monkeypatch.setattr(router, "log", broken_log)
    monkeypatch.setattr(router, "ROUTERS", {
        "rules": lambda task: ({"model": "haiku", "effort": "low", "why": "short"}, 0.0),
        "haiku": paid,
    })

    assert router.route("Say hi.")["router"] == "rules"


def test_an_empty_roster_is_a_clear_error(temp_jobs, monkeypatch):
    """Item 98: dispatch raised StopIteration (a 500 in the web)."""
    monkeypatch.setattr(leads, "roster", lambda: {})
    # Never a real agent call, even on code without the fix (the Haiku dispatcher runs `claude`).
    monkeypatch.setattr(leads, "dispatch_haiku", lambda task: pytest.fail("the paid dispatcher ran"))

    with pytest.raises(ValueError, match="no leads"):
        jobs.add("Write a poem.", lead="auto")
