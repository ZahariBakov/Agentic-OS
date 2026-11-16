"""Usage limits: watch the 5-hour and weekly limits of the Pro plan.

Every `claude` call sends a rate_limit_event with the usage of each window,
for example {"five_hour": {"utilization": 0.05, "resetsAt": 1793617200}}.
We save the newest numbers in data/limits.json. When a window is nearly
full, new jobs wait in the queue until the window resets.

The event also has a top-level "status" ("allowed", "allowed_warning" or
"rejected") and "rateLimitType" (which limit: a window, a model, overage).
"rejected" means that calls fail now, whatever the windows say: we save it
under the key "_limited", and jobs wait until its reset time. "allowed_warning"
is only a warning: jobs still run until PAUSE_AT.

Usage (from the project folder):
    python -m agentic_os.limits
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from agentic_os.memory import file_lock, write_atomic

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LIMITS_FILE = DATA_DIR / "limits.json"
LIMITS_LOCK = threading.Lock()
PAUSE_AT = 0.9  # do not start new jobs when a window is 90% full
LIMITED_KEY = "_limited"  # {"type", "resetsAt", "at"} while calls are rejected (not a window)


def read_file():
    """Return everything in data/limits.json, or an empty dict (also for a broken file:
    the next rate_limit_event writes a good one)."""
    try:
        saved = json.loads(LIMITS_FILE.read_text())
    except FileNotFoundError:
        return {}
    except ValueError:
        print(f"{LIMITS_FILE} is not valid JSON: it is ignored until the next usage event")
        return {}
    return saved if isinstance(saved, dict) else {}


def load():
    """Return the saved windows as a dict, or an empty dict."""
    return {name: window for name, window in read_file().items() if not name.startswith("_")}


def record(info):
    """Save the windows and the status from one rate_limit_event."""
    windows = dict(info.get("unifiedWindows") or {})
    if not windows and info.get("rateLimitType") and info.get("utilization") is not None:
        # The short form: one window, its numbers at the top level
        windows[info["rateLimitType"]] = {key: info[key] for key in ("utilization", "resetsAt", "status")
                                          if key in info}
    status = info.get("status")
    if not windows and status is None:
        return
    # The thread lock for this process, the file lock for the other one (web server + `jobs run`)
    with LIMITS_LOCK, file_lock(DATA_DIR / ".limits.lock"):
        saved = read_file()
        saved.update(windows)
        if status == "rejected":
            saved[LIMITED_KEY] = {"type": info.get("rateLimitType") or "a limit", "resetsAt": info.get("resetsAt"),
                                  "at": time.time()}
        elif status is not None:
            saved.pop(LIMITED_KEY, None)  # calls are allowed again
        write_atomic(LIMITS_FILE, json.dumps(saved, indent=2))


def clock(timestamp):
    """Turn a Unix time into text, for example 2026-11-02 14:00."""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def pause_reason():
    """Return why jobs must wait (a short text), or None if they can run.

    A window pauses at PAUSE_AT, or when its own status is "rejected". "allowed_warning"
    or no status does not pause by itself. A rejected call pauses until its reset time
    (no reset time: one hour after the event, a guess, so jobs do not wait forever).
    """
    limited = read_file().get(LIMITED_KEY)
    if limited:
        until = limited.get("resetsAt") or (limited.get("at", 0) + 3600)
        if until > time.time():
            return f"{limited['type']} limit reached, resets {clock(until)}"
    for name, window in load().items():
        if window.get("resetsAt", 0) < time.time():
            continue  # This window has already reset, so its numbers are old.
        if window.get("status") == "rejected" or window.get("utilization", 0) >= PAUSE_AT:
            return f"{name} limit at {window.get('utilization', 0):.0%}, resets {clock(window['resetsAt'])}"
    return None


def main():
    for name, window in load().items():
        print(f"{name:<10} {window['utilization']:>4.0%}  resets {clock(window['resetsAt'])}")
    print(f"Jobs wait: {pause_reason() or 'no'}")


if __name__ == "__main__":
    main()
