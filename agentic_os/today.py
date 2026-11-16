"""Today: what needs the user, who works now, and what finished in the last 24 hours.

Python collects the facts (exact,
free); the web page only shows them (GET /api/today, the panel at the top).

Usage (from the project folder):
    python -m agentic_os.today      print the facts as JSON
"""

import json
from datetime import datetime, timedelta

from agentic_os import commands, feedback, job_store, jobs, leads, recos, workspaces

MAX_FINISHED = 8  # the newest finished jobs that the panel lists
PREVIEW = 140  # characters of an answer in the finished list


def preview(text):
    """Return the first line of an answer, cut to PREVIEW characters."""
    line = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    return line if len(line) <= PREVIEW else line[: PREVIEW - 3] + "..."


def short_job(job):
    """The fields that every item about a job needs."""
    return {"job": job.id, "lead": job.lead, "model": job.model, "task": preview(job.task)}


def needs_you(all_jobs):
    """Return what waits for the user, the oldest first: questions, plans, interrupted jobs, commands,
    then the lesson drafts, the skill proposals and the Recos cards (as counts)."""
    items = []
    for job in all_jobs:
        if job.status == "waiting" and job.question:
            items.append({"kind": "question", **short_job(job), "at": job.question["at"],
                          "text": job.question["text"], "options": job.question["options"],
                          "cold_warning": jobs.cold_warning(job)})  # one click must ask first, like the job view
        elif job.status == "waiting" and job.plan and job.plan["status"] == "pending":
            items.append({"kind": "plan", **short_job(job), "at": job.plan["at"], "estimate": job.plan["estimate"]})
        elif job.status == "interrupted":
            items.append({"kind": "interrupted", **short_job(job), "at": job.created_at,
                          "why": job.interrupted_why})
        if (job.status in job_store.FINISHED or job.status == "interrupted") and workspaces.active(job):
            # The job's changes wait in its isolated workspace: apply or discard them. An interrupted
            # job has both items (resume it, or keep what it did). A finished run that changed
            # nothing has no workspace any more (workspaces.drop_if_unchanged).
            items.append({"kind": "workspace", **short_job(job), "at": job.finished_at or job.created_at})
    for record in commands.pending():  # an agent waits for your OK to run a shell command
        if jobs.exists(record["job"]):
            items.append({"kind": "command", **short_job(jobs.load(record["job"])), "at": record["at"],
                          "id": record["id"], "command": record["command"], "reason": record["reason"]})
    items.sort(key=lambda item: item["at"])
    drafts = sum(len(leads.load_lessons(name)["drafts"]) for name in sorted(leads.roster()))
    if drafts:
        items.append({"kind": "lessons", "count": drafts})
    proposals = len(feedback.proposals())
    if proposals:
        items.append({"kind": "proposals", "count": proposals})
    cards = len(recos.load_recos()["cards"])
    if cards:
        items.append({"kind": "recos", "count": cards})
    return items


def finished_since(all_jobs, since):
    """Return the jobs that finished after `since`, the newest first. Old jobs have no
    finished_at: their created_at counts instead."""
    finished = [job for job in all_jobs if job.status in job_store.FINISHED
                and datetime.fromisoformat(job.finished_at or job.created_at) >= since]
    return sorted(finished, key=lambda job: job.finished_at or job.created_at, reverse=True)


def today(now=None):
    """Return the facts for the Today panel as a dict (it becomes JSON)."""
    now = now or datetime.now()
    all_jobs = jobs.all_jobs()
    finished = finished_since(all_jobs, now - timedelta(days=1))
    return {
        "now": now.isoformat(timespec="seconds"),
        "needs_you": needs_you(all_jobs),
        "working": [{**short_job(job), "cost": job.cost, "tries": job.tries}
                    for job in all_jobs if job.status == "running"],
        "queued": sum(1 for job in all_jobs if job.status == "queued"),
        "finished": [{**short_job(job), "status": job.status, "cost": job.cost,
                      "at": job.finished_at or job.created_at, "preview": preview(job.result),
                      "review": job.review["verdict"] if job.review else None,
                      "lesson": job.lesson_for is not None}
                     for job in finished[:MAX_FINISHED]],
        "finished_count": len(finished),
        # Done jobs that you have not approved, rejected or commented on (lesson jobs need no review).
        "not_reviewed": sum(1 for job in finished if job.status == "done" and not job.review and not job.lesson_for),
        # The whole cost of these jobs, also a part spent earlier (a long job, a follow-up): we
        # have no time per agent call. So it is not "money spent in 24 hours", and is not named so.
        "finished_cost": round(sum(job.cost for job in finished), 6),
    }


def main():
    print(json.dumps(today(), indent=2))


if __name__ == "__main__":
    main()
