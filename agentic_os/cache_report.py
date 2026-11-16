"""Cache report: what cold cache reloads cost, and whether keep-warm pings would pay.

It is exact work, so it is Python only, no AI, and it costs nothing.

Background: Claude keeps a session's context in the prompt cache for a while
(1 hour, or 5 minutes). A call inside that time reads the context from the
cache at 0.1x the input price. A call after a longer pause finds the cache
"cold": the whole context is written to the cache again, at 2x (1 hour cache)
or 1.25x (5 minute cache) the input price. A "ping" (a tiny message) before the
cache dies would keep it warm, but every ping is a cache read of the whole
context. This report compares the two.

How it works:
1. Read Claude Code's session files (~/.claude/projects/**/*.jsonl) changed in
   the last days. Every answer line has a time, a model and the token usage.
   Our job files do not keep the time of each call, so they cannot be used.
2. In each session, a call that comes after a gap longer than the cache life
   and writes MIN_RELOAD tokens or more to the cache is a cold reload. Its
   extra cost = the tokens * (write price - read price).
3. A gap up to WARM_FOR could have been kept warm with pings (one every
   PING_EVERY of the cache life). A longer gap is not worth pinging.
4. Verdict: the pings pay when the saving minus the ping cost is more than
   PAYS_USD. Agent Office calls (agents that work under ~/.cache/agentic-os, or a
   job's session) are counted on their own too, because only those we could
   ping ourselves.

The prices are estimates at API list prices, not what a plan subscription
really charges.

Usage (from the project folder):
    python -m agentic_os.cache_report [--days 30] [--json]
"""

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import job_store, jobs

PROJECTS_DIR = Path.home() / ".claude" / "projects"
OFFICE_DIR = Path.home() / ".cache" / "agentic-os"  # where Agent Office agents work
DAYS_BACK = 30

MIN_RELOAD = 10_000  # tokens; a smaller cache write is not worth a ping
WARM_FOR = 4 * 3600  # seconds; pinging through a longer gap does not pay
HOUR_TTL = 3600  # seconds; the 1 hour cache
SHORT_TTL = 300  # seconds; the 5 minute cache
PING_EVERY = {HOUR_TTL: 50 * 60, SHORT_TTL: 4 * 60}  # seconds; a ping before the cache dies
PING_EXTRA_USD = 0.0005  # the ping's own small message and answer
PAYS_USD = {"chats": 0.50, "office": 0.25}  # the net saving that makes pings worth it
WORST = 8  # how many of the most expensive reloads to show

# USD per million input tokens. A cache write is 2x (1 hour) or 1.25x (5 minutes)
# of it, a cache read is 0.1x.
INPUT_PRICES = {"opus": 5.0, "sonnet": 3.0, "haiku": 1.0}
WRITE_FACTOR = {HOUR_TTL: 2.0, SHORT_TTL: 1.25}
READ_FACTOR = 0.1


def tier(model):
    """Return "opus", "sonnet" or "haiku" for a model name (opus when unknown)."""
    for name in ("haiku", "sonnet"):
        if name in (model or "").lower():
            return name
    return "opus"


def parse_time(text):
    """Return the seconds since 1970 for a time like 2026-11-04T09:04:06.228Z, or None."""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def session_files(root, since):
    """Return the .jsonl files under root that changed at or after `since` (seconds)."""
    if not root.is_dir():
        return []
    files = []
    for path in sorted(root.rglob("*.jsonl")):
        try:
            if path.stat().st_mtime >= since:
                files.append(path)
        except OSError:
            continue  # the file was deleted while we looked
    return files


def read_calls(path):
    """Return the model calls in one session file, oldest first.

    A call is {"t" (seconds), "model", "usage", "session", "cwd"}. One answer can
    be split over several lines (one per content block) with the same message
    id; it is counted once.
    """
    calls = {}
    with open(path, encoding="utf-8", errors="replace") as file:
        for line in file:
            if '"usage"' not in line or '"assistant"' not in line:
                continue  # fast skip: most lines are not answers
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = entry.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            moment = parse_time(entry.get("timestamp"))
            key = message.get("id") or entry.get("uuid")
            if moment is None or key in calls:
                continue
            calls[key] = {"t": moment, "model": message.get("model") or "", "usage": message["usage"],
                          "session": entry.get("sessionId"), "cwd": entry.get("cwd") or ""}
    return sorted(calls.values(), key=lambda call: call["t"])


def job_sessions():
    """Return the session ids of Agent Office jobs (a lead may work outside OFFICE_DIR)."""
    sessions = set()
    for path in sorted(job_store.JOBS_DIR.glob("*.json")):
        try:
            session = json.loads(path.read_text(encoding="utf-8")).get("session_id")
        except (OSError, json.JSONDecodeError):
            continue
        if session:
            sessions.add(session)
    return sessions


def is_office(call, sessions):
    """True when the call was made by an Agent Office agent."""
    return call["session"] in sessions or Path(call["cwd"]).is_relative_to(OFFICE_DIR)


def cache_life(call, previous):
    """Return the cache life of the call in seconds: 1 hour when either call used the 1 hour cache."""
    for usage in (call["usage"], previous["usage"]):
        if (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens"):
            return HOUR_TTL
    return SHORT_TTL


def cold_reload(call, previous):
    """Return the facts of a cold reload, or None when the call is not one.

    The facts: {"gap" (seconds), "ttl", "tokens", "usd" (the extra cost of the
    reload), "pings", "ping_usd" (both 0 when the gap is too long to ping)}.
    """
    gap = call["t"] - previous["t"]
    ttl = cache_life(call, previous)
    tokens = call["usage"].get("cache_creation_input_tokens") or 0
    if gap <= ttl or tokens < MIN_RELOAD:
        return None
    price = INPUT_PRICES[tier(call["model"])]
    usd = tokens * price * (WRITE_FACTOR[ttl] - READ_FACTOR) / 1e6
    pings, ping_usd = 0, 0.0
    if gap <= WARM_FOR:
        # A ping keeps the cache for one more cache life. The cache already lasts
        # `ttl` after the last call, so the pings must cover the rest of the gap.
        pings = max(1, math.ceil((gap - ttl) / PING_EVERY[ttl]))
        ping_usd = pings * (tokens * price * READ_FACTOR / 1e6 + PING_EXTRA_USD)
    return {"gap": gap, "ttl": ttl, "tokens": tokens, "usd": usd, "pings": pings, "ping_usd": ping_usd}


def empty_part():
    """Return the counters of one part of the report (your chats or Agent Office)."""
    return {"sessions": 0, "calls": 0, "cold": 0, "cold_tokens": 0, "cold_usd": 0.0,
            "savable": 0, "savable_usd": 0.0, "ping_usd": 0.0, "long_gaps": 0}


def report(days=DAYS_BACK, root=None, now=None):
    """Return the cache report for the last `days` days as a dict.

    {"days", "chats": part, "office": part, "worst": [reload, ...]}. A part has
    the counters of `empty_part` plus "net_usd" and "pays".
    """
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    since = now - timedelta(days=days).total_seconds()
    sessions = job_sessions()
    parts = {"chats": empty_part(), "office": empty_part()}
    worst = []
    # PROJECTS_DIR is read here, not as a default value: tests move it with monkeypatch.
    for path in session_files(root or PROJECTS_DIR, since):
        calls = [call for call in read_calls(path) if call["t"] >= since]
        if not calls:
            continue
        part = parts["office" if is_office(calls[0], sessions) else "chats"]
        part["sessions"] += 1
        part["calls"] += len(calls)
        for previous, call in zip(calls, calls[1:]):
            reload = cold_reload(call, previous)
            if reload is None:
                continue
            part["cold"] += 1
            part["cold_tokens"] += reload["tokens"]
            part["cold_usd"] += reload["usd"]
            if reload["pings"]:
                part["savable"] += 1
                part["savable_usd"] += reload["usd"]
                part["ping_usd"] += reload["ping_usd"]
            else:
                part["long_gaps"] += 1
            worst.append({**reload, "at": datetime.fromtimestamp(call["t"]).isoformat(timespec="minutes"),
                          "model": tier(call["model"]), "folder": path.parent.name,
                          "office": part is parts["office"]})
    for name, part in parts.items():
        part["net_usd"] = part["savable_usd"] - part["ping_usd"]
        part["pays"] = part["net_usd"] > PAYS_USD[name]
    worst.sort(key=lambda reload: reload["usd"], reverse=True)
    return {"days": days, **parts, "worst": worst[:WORST]}


def part_lines(title, part):
    """Return the text lines of one part of the report."""
    if not part["calls"]:
        return [f"{title}: no model calls."]
    lines = [f"{title}: {part['sessions']} session(s), {part['calls']} model call(s).",
             f"  Cold cache reloads: {part['cold']} ({part['cold_tokens'] / 1e6:.2f}M tokens written again),"
             f" about {jobs.usd(part['cold_usd'])} extra."]
    if part["savable"]:
        lines.append(f"  {part['savable']} came after a gap under {WARM_FOR // 3600} hours: pings would have"
                     f" saved {jobs.usd(part['savable_usd'])} and cost {jobs.usd(part['ping_usd'])},"
                     f" net {jobs.usd(part['net_usd'])}.")
    if part["long_gaps"]:
        lines.append(f"  {part['long_gaps']} came after a gap over {WARM_FOR // 3600} hours: pings would not help.")
    lines.append(f"  Verdict: keep-warm {'pays' if part['pays'] else 'does not pay'}.")
    return lines


def text(result):
    """Return the report as text for the terminal."""
    lines = [f"Cache report for the last {result['days']} days.", ""]
    lines += part_lines("Agent Office agents", result["office"])
    lines += part_lines("Your own Claude Code chats", result["chats"])
    if result["worst"]:
        lines += ["", "The most expensive reloads:"]
        for item in result["worst"]:
            who = "Agent Office" if item["office"] else "chat"
            lines.append(f"  {item['at']}  {jobs.usd(item['usd'])}  {item['tokens']} tokens after"
                         f" {item['gap'] / 60:.0f} min, {item['model']}, {who} ({item['folder']})")
    lines += ["", "Estimates at API list prices (input per million tokens: opus $5, sonnet $3, haiku $1;",
              "a cache write is 2x for the 1 hour cache or 1.25x for 5 minutes, a read is 0.1x)."]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="What cold cache reloads cost, and whether keep-warm pings pay.")
    parser.add_argument("--days", type=int, default=DAYS_BACK, help="how many days back")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args()
    result = report(args.days)
    print(json.dumps(result, indent=2) if args.json else text(result))


if __name__ == "__main__":
    main()
