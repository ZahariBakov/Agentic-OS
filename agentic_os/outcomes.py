"""Outcomes: what it costs to get a job done well.

Cost per job hides
the jobs that failed or had to be done again; cost per successful job does not.
It is exact work from the job files (nothing new is logged), so it is Python,
no AI, and it costs nothing.

A job is a success when it ended "done" (so its check passed, if it had one),
the user did not reject it, did not ask for changes (rework) and did not undo
its changes (job.signals).

Usage (from the project folder):
    python -m agentic_os.outcomes [--days 30] [--json]
"""

import argparse
import json
from datetime import datetime, timedelta
from statistics import median

from agentic_os import jobs

DAYS_BACK = 30
ENDED = ("done", "failed", "stopped", "interrupted")
WORST = 8  # how many jobs that went wrong to show


def success(job):
    """True when the job ended done and the user did not reject, rework or undo it."""
    return job.status == "done" and not job.signals and not (job.review and job.review["verdict"] == "rejected")


def why_not(job):
    """Return why a job that ended is not a success, in a few words."""
    if job.status != "done":
        return job.status
    for signal, why in (("undone", "changes undone"), ("rework", "needed rework"), ("rejected", "rejected")):
        if signal in job.signals:
            return why
    return "rejected"


def minutes(job):
    """Return how long the job took (created to finished), or None for old jobs."""
    if not job.finished_at:
        return None
    return (datetime.fromisoformat(job.finished_at) - datetime.fromisoformat(job.created_at)).total_seconds() / 60


def summarize(group):
    """Return the numbers of a list of ended jobs."""
    good = [job for job in group if success(job)]
    cost = sum(job.cost for job in group)
    times = [m for m in map(minutes, group) if m is not None]
    return {
        "jobs": len(group),
        "succeeded": len(good),
        "failed": sum(job.status == "failed" for job in group),
        "stopped": sum(job.status == "stopped" for job in group),
        "interrupted": sum(job.status == "interrupted" for job in group),
        "reworked": sum("rework" in job.signals for job in group),
        "undone": sum("undone" in job.signals for job in group),
        "rejected": sum("rejected" in job.signals for job in group),
        "success_rate": len(good) / len(group) if group else None,
        "median_minutes": median(times) if times else None,
        "cost": cost,
        "cost_per_job": cost / len(group) if group else None,
        "cost_per_success": cost / len(good) if good else None,
    }


def grouped(ended, key):
    """Return one summary per value of key(job), the biggest groups first."""
    groups = {}
    for job in ended:
        groups.setdefault(key(job), []).append(job)
    rows = [{"name": name, **summarize(group)} for name, group in groups.items()]
    return sorted(rows, key=lambda row: (-row["jobs"], row["name"]))


def report(days=DAYS_BACK, now=None, all_jobs=None):
    """Return the outcomes of the jobs that ended in the last `days` days.

    all_jobs: the jobs, if the caller has them already (so the files are read once).
    """
    if all_jobs is None:
        all_jobs = jobs.all_jobs()
    since = (now or datetime.now()) - timedelta(days=days)
    # Lesson jobs are left out: they are the review's extra work after a bad result, not
    # a job you asked for, so as "successes" they would make the numbers look better.
    ended = [job for job in all_jobs if job.status in ENDED and not job.lesson_for
             and datetime.fromisoformat(job.finished_at or job.created_at) >= since]
    went_wrong = [job for job in ended if not success(job) and job.status != "stopped"]
    went_wrong.sort(key=lambda job: job.finished_at or job.created_at, reverse=True)
    return {
        "days": days,
        "overall": summarize(ended),
        "by_lead": grouped(ended, lambda job: job.lead or "(one-time agent)"),
        "by_model": grouped(ended, lambda job: job.model),
        "went_wrong": [{"id": job.id, "task": job.task[:90], "why": why_not(job), "cost": job.cost}
                       for job in went_wrong[:WORST]],
    }


def money(value):
    return "-" if value is None else jobs.usd(value)


def text(result):
    """Return the report as text for the terminal."""
    overall = result["overall"]
    if not overall["jobs"]:
        return f"No jobs ended in the last {result['days']} days."
    rate = overall["success_rate"]
    lines = [f"Outcomes of the last {result['days']} days: {overall['jobs']} job(s), "
             f"{overall['succeeded']} succeeded ({rate:.0%}).",
             f"Cost {money(overall['cost'])}: {money(overall['cost_per_job'])} per job, "
             f"{money(overall['cost_per_success'])} per successful job.", ""]
    for title, rows in (("By lead", result["by_lead"]), ("By model", result["by_model"])):
        lines.append(f"{title}:")
        for row in rows:
            lines.append(f"  {row['name']:<18} {row['succeeded']:>3} of {row['jobs']:<3} "
                         f"{money(row['cost_per_success']):>9} per success  ({money(row['cost'])} in all)")
        lines.append("")
    if result["went_wrong"]:
        lines.append("Went wrong lately:")
        for job in result["went_wrong"]:
            lines.append(f"  {job['id']}  {job['why']:<15} {money(job['cost']):>9}  {job['task']}")
    return "\n".join(lines).rstrip()


def main():
    parser = argparse.ArgumentParser(description="What it costs to get a job done well.")
    parser.add_argument("--days", type=int, default=DAYS_BACK, help="how many days back")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args()
    result = report(args.days)
    print(json.dumps(result, indent=2) if args.json else text(result))


if __name__ == "__main__":
    main()
