"""Tests for the usage limits (limits.py): what is saved, and when new jobs wait."""

import time

import pytest

from agentic_os import limits

LATER = int(time.time()) + 3600


@pytest.fixture(autouse=True)
def limits_file(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(limits, "DATA_DIR", tmp_path)


def window(utilization, status="allowed"):
    return {"utilization": utilization, "resetsAt": LATER, "status": status}


def test_a_warning_is_not_a_pause():
    """Before: any status but "allowed" paused every job until the reset."""
    limits.record({"status": "allowed_warning", "unifiedWindows": {"five_hour": window(0.72, "allowed_warning")}})

    assert limits.pause_reason() is None


def test_a_window_without_a_status_pauses_only_when_full():
    limits.record({"unifiedWindows": {"five_hour": {"utilization": 0.05, "resetsAt": LATER}}})
    assert limits.pause_reason() is None

    limits.record({"unifiedWindows": {"five_hour": {"utilization": 0.95, "resetsAt": LATER}}})
    assert "five_hour limit at 95%" in limits.pause_reason()


def test_a_rejected_window_pauses():
    limits.record({"unifiedWindows": {"seven_day": window(0.5, "rejected")}})

    assert "seven_day" in limits.pause_reason()


def test_a_rejected_model_limit_pauses_until_its_reset():
    """Before: only unifiedWindows were read, so an Opus or overage "rejected" never paused."""
    limits.record({"status": "rejected", "rateLimitType": "seven_day_opus", "resetsAt": LATER,
                   "unifiedWindows": {"five_hour": window(0.3)}})

    assert "seven_day_opus limit reached" in limits.pause_reason()
    assert set(limits.load()) == {"five_hour"}  # the status is not a window (briefing, web)

    limits.record({"status": "allowed", "unifiedWindows": {"five_hour": window(0.31)}})
    assert limits.pause_reason() is None


def test_the_short_form_with_one_window():
    limits.record({"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.93, "resetsAt": LATER})

    assert limits.load()["five_hour"]["utilization"] == 0.93
    assert "five_hour limit at 93%" in limits.pause_reason()


def test_an_old_rejection_does_not_pause_forever():
    limits.record({"status": "rejected", "rateLimitType": "overage"})
    assert "overage" in limits.pause_reason()  # no reset time: one hour from the event

    data = limits.read_file()
    data[limits.LIMITED_KEY]["at"] -= 7200
    limits.LIMITS_FILE.write_text(limits.json.dumps(data))
    assert limits.pause_reason() is None
