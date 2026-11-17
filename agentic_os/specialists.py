"""Specialists: an agent hands parts of its work to sub-agents that run in parallel.

The agent calls the office tool `delegate` (office_mcp.py) with 1 to MAX_AT_ONCE assignments;
each one becomes a normal job with `parent` set, and they run at the same time. The tool
returns when all are done, with each one's report.

The rules (Python checks them, not the agent):
    - a sub-job only reads (Read, Glob, Grep) in the parent's folder (its workspace when it
      has one); it cannot edit, run commands, ask you, or delegate again
    - its model is haiku or sonnet, never opus (choose_model: the router's free keyword
      rules, then the track record of haiku from outcomes.py)
    - all sub-jobs of one job together cost at most CAP_USD: each new one gets an equal
      part of what is left, as `claude --max-budget-usd`, so Claude Code stops it there
    - a sub-job that does not end in WAIT_SECONDS is stopped
"""

import time
from concurrent.futures import ThreadPoolExecutor

from agentic_os import jobs, limits, outcomes, router

CAP_USD = 0.50  # all sub-jobs of one job together (your choice, 2026-11-10)
MIN_BUDGET_USD = 0.02  # less than this is too little for one agent call
MAX_AT_ONCE = 3
MODELS = ("haiku", "sonnet")
WAIT_SECONDS = 1200
POLL_SECONDS = 1.0
REPORT_CHARS = 4000  # of each sub-job's result, back to the parent agent
# Haiku stays the choice while its track record is good enough (or too short to judge).
HAIKU_MIN_JOBS = 5
HAIKU_MIN_SUCCESS = 0.6

BRIEF = """

You are a specialist: another agent (job {parent}) gave you this part of its work. You can
read files in {folder} but not change them, and you cannot ask the user. Reply with a short,
complete report: what you found, with file paths and line numbers where they help."""


def choose_model(task, all_jobs=None):
    """Return (model, why) for a sub-job: haiku or sonnet.

    The router's keyword rules (free): opus becomes sonnet. No rule: haiku, unless haiku's
    track record (outcomes.py, the last 30 days) is poor; then sonnet.
    """
    decision = router.route_rules(task)
    if decision is not None:
        model = decision[0]["model"]
        if model == "opus":
            return "sonnet", f"{decision[0]['why']} (opus is too dear for a sub-job)"
        return model, decision[0]["why"]
    rows = {row["name"]: row for row in outcomes.report(all_jobs=all_jobs)["by_model"]}
    haiku = rows.get("haiku")
    if haiku and haiku["jobs"] >= HAIKU_MIN_JOBS and haiku["success_rate"] < HAIKU_MIN_SUCCESS:
        return "sonnet", f"haiku succeeded in only {haiku['success_rate']:.0%} of {haiku['jobs']} jobs"
    return "haiku", "no keyword rule; haiku's track record is good enough"


def spent(parent_id, all_jobs=None):
    """What the job's sub-jobs have cost so far."""
    return sum(job.cost for job in (all_jobs if all_jobs is not None else jobs.all_jobs()) if job.parent == parent_id)


def check_assignments(assignments):
    """Raise ValueError unless assignments is a list of 1..MAX_AT_ONCE {title, task} objects."""
    if not isinstance(assignments, list) or not 1 <= len(assignments) <= MAX_AT_ONCE:
        raise ValueError(f"assignments must be a list of 1 to {MAX_AT_ONCE} items")
    for number, item in enumerate(assignments, 1):
        if not isinstance(item, dict):
            raise ValueError(f"assignment {number} must be an object with title and task")
        for key in ("title", "task"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"assignment {number}: {key} must be a non-empty string")


def start(parent, assignments):
    """Add the sub-jobs (queued). Return them. Raise ValueError when the rules say no."""
    check_assignments(assignments)
    if parent.parent:
        raise ValueError("a sub-job cannot delegate again; do the work yourself")
    reason = limits.pause_reason()
    if reason is not None:
        raise ValueError(f"no new jobs start now ({reason})")
    all_jobs = jobs.all_jobs()
    left = CAP_USD - spent(parent.id, all_jobs)
    budget = round(left / len(assignments), 4)
    if budget < MIN_BUDGET_USD:
        raise ValueError(f"the budget for sub-jobs is used up (${CAP_USD:.2f} for each job, "
                         f"${max(left, 0):.4f} left); do the rest yourself")
    folder = str(jobs.edit_folder(parent))
    added = []
    for item in assignments:
        model, why = choose_model(item["task"], all_jobs)
        task = item["task"].strip() + BRIEF.format(parent=parent.id, folder=folder)
        added.append(jobs.add(task, model, use_memory=False, folder=folder, fields={
            "parent": parent.id, "title": item["title"].strip()[:80], "max_budget_usd": budget,
            "model_why": why}))
    return added


def wait_all(added, futures, stopped=lambda: False, clock=time.monotonic, sleep=time.sleep):
    """Wait until every sub-job has ended. Stop the rest when the parent stops or time is up.

    A job still queued after its run returned did not start (the usage limits, for example):
    it is stopped too, or it would wait here until the time is up.
    """
    end = clock() + WAIT_SECONDS
    while True:
        statuses = {job.id: jobs.load(job.id).status for job in added}
        for job, future in zip(added, futures):
            if statuses[job.id] == "queued" and future.done():
                jobs.stop(job.id)
                statuses[job.id] = "stopped"
        open_ids = [job_id for job_id, status in statuses.items() if status in ("queued", "running")]
        if not open_ids:
            return
        if stopped() or clock() >= end:
            for job_id in open_ids:
                jobs.stop(job_id)
            return
        sleep(POLL_SECONDS)


def report(job):
    """What the parent agent gets back about one sub-job."""
    job = jobs.load(job.id)
    result = job.result or ""
    if len(result) > REPORT_CHARS:
        result = result[:REPORT_CHARS] + " ..."
    return {"title": job.title, "job": job.id, "model": job.model, "status": job.status,
            "cost_usd": round(job.cost, 4), "report": result, "error": job.error}


def delegate(parent, assignments, stopped=lambda: False, run=None):
    """Run the assignments as sub-jobs at the same time. Return what the parent agent gets back.

    run: the function that runs one job (jobs.run_job; a test passes a fake). A job that
    another worker took first (the web page's "run queued") is only waited for.
    """
    added = start(parent, assignments)
    run = run or jobs.run_job
    with ThreadPoolExecutor(max_workers=len(added)) as pool:
        futures = [pool.submit(run, job) for job in added]
        wait_all(added, futures, stopped)
        for future in futures:
            future.result()  # a crash in a run shows here, after all have ended
    reports = [report(job) for job in added]
    used = spent(parent.id)
    return {"sub_jobs": reports, "cost_usd": round(sum(item["cost_usd"] for item in reports), 4),
            "budget_left_usd": round(max(CAP_USD - used, 0), 4)}
