"""Briefing: the facts for the morning briefing, collected by Python (free, exact).

The routine morning-briefing gets these facts through the {briefing_facts}
blank. The agent only turns them into a short text with priorities.

Usage (from the project folder):
    python -m agentic_os.briefing      print the facts (no AI, no cost)
"""

import time
from datetime import datetime, timedelta

from agentic_os import documents, feedback, jobs, limits, memory, routines

MAX_ITEMS = 5  # list at most this many items per part, then "and N more"


def short(text, size=70):
    """Return the first line of a text, cut to `size` characters."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= size else line[: size - 3] + "..."


def capped(lines):
    """Return at most MAX_ITEMS lines, plus one line that says how many were left out."""
    if len(lines) <= MAX_ITEMS:
        return lines
    return lines[:MAX_ITEMS] + [f"- ... and {len(lines) - MAX_ITEMS} more"]


def waited(since_text, now):
    """Return how long ago a time was, as short text: "40 min" or "3.5 hours"."""
    minutes = (now - datetime.fromisoformat(since_text)).total_seconds() / 60
    return f"{minutes:.0f} min" if minutes < 90 else f"{minutes / 60:.1f} hours"


def waiting_item(job, now):
    """Return (when it was asked, one line) for a job that waits for the user: a question or a plan."""
    if job.question:
        return job.question["at"], f"- {job.id} waits {waited(job.question['at'], now)}: {short(job.question['text'], 70)}"
    estimate = jobs.usd(job.plan["estimate"]) if job.plan["estimate"] is not None else "no estimate"
    return job.plan["at"], (f"- {job.id} waits {waited(job.plan['at'], now)}: a plan ({estimate}, a guess) "
                            f"for: {short(job.task, 50)}")


def questions_part(all_jobs, now):
    """The questions and plans that agents sent and that still wait for the user (the reminder)."""
    waiting = [job for job in all_jobs if job.status == "waiting"
               and (job.question or (job.plan and job.plan["status"] == "pending"))]
    if not waiting:
        return ["Questions for you: none."]
    items = sorted(waiting_item(job, now) for job in waiting)  # the oldest first: it waited longest
    lines = [f"Questions for you: {len(waiting)} (the agents wait; their other jobs may be held)"]
    return lines + capped([line for _, line in items])


def jobs_part(all_jobs, since):
    """The jobs of the last 24 hours, and the jobs that wait in the queue.

    A job counts by when it finished (not finished yet: when it was made), like today.py:
    an old job that failed today is news.
    """
    recent = [job for job in all_jobs if datetime.fromisoformat(job.finished_at or job.created_at) >= since]
    counts = {}
    for job in recent:
        counts[job.status] = counts.get(job.status, 0) + 1
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items())) or "none"
    lines = [f"Jobs in the last 24 hours: {summary}; cost ${sum(job.cost for job in recent):.4f}"]
    failed = [f"- failed {job.id}: {short(job.task, 50)} (error: {short(job.error, 60)})"
              for job in recent if job.status == "failed"]
    lines += capped(failed)
    queued = [job for job in all_jobs if job.status == "queued"]
    if queued:
        lines.append(f"Waiting in the queue: {len(queued)}")
        lines += capped([f"- {job.id}: {('routine ' + job.routine + ': ') if job.routine else ''}"
                         f"{short(job.task, 60)}" for job in queued])
    return lines


def skills_part():
    """Proposals from calibrate that wait for a decision, and open feedback."""
    lines = [f"- a new version of the skill {name} waits: accept or reject it"
             for name in feedback.proposals()]
    if feedback.FEEDBACK_DIR.exists():
        for path in sorted(feedback.FEEDBACK_DIR.glob("*.md")):
            count = len(feedback.open_items(path.stem))
            if count:
                lines.append(f"- the skill {path.stem} has {count} open feedback (run calibrate)")
    return ["Skills:", *capped(lines)] if lines else ["Skills: nothing waits."]


def routines_part(now):
    """Routines that run today, and routines near their cost cap."""
    lines = []
    for row in routines.summary(now):
        if row["next_run"][:10] == now.date().isoformat():
            lines.append(f"- {row['name']} runs today at {row['next_run'][11:16]} ({row['mode']})")
        if row["week_cost"] >= 0.8 * row["max_week_usd"]:
            lines.append(f"- {row['name']} used ${row['week_cost']:.4f} of its ${row['max_week_usd']:.2f} weekly cap")
    return ["Routines:", *capped(lines)] if lines else ["Routines: none today."]


def memory_part(since):
    """Changes to the memory, raw sources that were never ingested, and health problems."""
    lines = []
    if memory.LOG_FILE.exists():
        changes = []
        for line in memory.LOG_FILE.read_text().splitlines():
            parts = line[2:].split() if line.startswith("- ") else []
            if len(parts) >= 3 and parts[0] >= since.isoformat(timespec="seconds"):
                changes.append(f"- {parts[1]} {parts[2]}")
        if changes:
            lines.append(f"Memory changes in the last 24 hours: {len(changes)}")
            lines += capped(changes)
    new = [f"- {path.relative_to(memory.RAW_DIR)}" for path in jobs.new_sources()]
    if new:
        lines.append(f"Raw sources not ingested yet: {len(new)} (run: python -m agentic_os.jobs ingest-new)")
        lines += capped(new)
    waiting = [f"- {path.relative_to(memory.RAW_DIR)}" for path in documents.new_documents()]
    if waiting:
        lines.append(f"Documents without Markdown: {len(waiting)} (run: python -m agentic_os.documents convert-new)")
        lines += capped(waiting)
    problems = memory.health()
    if problems:
        lines.append(f"Memory health: {len(problems)} problems (run: python -m agentic_os.memory health)")
    return lines or ["Memory: no changes, nothing new in raw/, no health problems."]


def limits_part():
    """The usage limits of the plan."""
    windows = {name: window for name, window in limits.load().items()
               if window.get("resetsAt", 0) >= time.time()}
    if not windows:
        return ["Usage limits: no data yet."]
    parts = [f"{name.replace('_', ' ')} {window.get('utilization', 0):.0%}"
             f" (resets {limits.clock(window['resetsAt'])})" for name, window in windows.items()]
    reason = limits.pause_reason()
    return [f"Usage limits: {', '.join(parts)}" + (f". Jobs wait: {reason}" if reason else "")]


def facts(now=None):
    """Return all the facts as text, one part after another."""
    now = now or datetime.now()
    since = now - timedelta(days=1)
    all_jobs = jobs.all_jobs()
    parts = [
        questions_part(all_jobs, now),
        jobs_part(all_jobs, since),
        skills_part(),
        routines_part(now),
        memory_part(since),
        limits_part(),
    ]
    return f"Facts for {now:%A %Y-%m-%d %H:%M}\n\n" + "\n\n".join("\n".join(part) for part in parts)


def main():
    print(facts())


if __name__ == "__main__":
    main()
