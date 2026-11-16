"""Web: a local web page for Agent Office (jobs, file changes, skills, routines, costs, artifacts).

The server listens only on 127.0.0.1, so other computers cannot connect.
The pages are static files in agentic_os/static/. Their JavaScript gets
JSON from the /api/... addresses and builds the page in the browser.

While the server runs, it starts the routines that are due (every minute).
Use --no-routines if cron already calls `routines tick`, so they do not start twice.

Usage (from the project folder):
    python -m agentic_os.web [--port 8000] [--no-routines]
Then open the link it prints (http://127.0.0.1:8000/__app?t=...). The link holds
the token (see access.py); after the first visit a cookie keeps you in.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agentic_os import (
    access, artifacts, brain, changes, commands, feedback, job_run, job_store, jobs, leads, limits, outcomes, project_memory, recos, routines, skills, today, weather,
    wildcards, workspaces,
)
from agentic_os.ask import kill_group
from agentic_os.router import LOG_FILE as ROUTER_LOG
from agentic_os.router import MODELS

STATIC_DIR = Path(__file__).with_name("static")
WORKERS = 3  # jobs at the same time, like `jobs run`
TICK_SECONDS = 60  # how often the server looks for routines that are due

pool = ThreadPoolExecutor(max_workers=WORKERS)


def run_logged(job):
    """Run a job in a pool thread. An error is printed: the pool would keep it silently."""
    try:
        jobs.run_job(job)
    except BaseException:  # the last place that can report it
        print(f"Job {job.id} crashed in the pool:\n{traceback.format_exc()}", flush=True)


def run_in_pool(job):
    """Start a job in the background, so the caller does not wait for it."""
    pool.submit(run_logged, job)


async def routine_loop():
    """Start the routines that are due, every TICK_SECONDS, while the server runs."""
    while True:
        try:
            # tick reads and writes files, so it runs in a thread, not in the event loop.
            started = await asyncio.to_thread(routines.tick, None, run_in_pool)
            if started:
                print(f"Routines started: {', '.join(started)}", flush=True)
        except (Exception, SystemExit) as error:  # one bad tick must not stop the loop (or the server)
            print(f"Routines: tick failed: {error}", flush=True)
        await asyncio.sleep(TICK_SECONDS)


def stop_work():
    """The server stops: end the work of this process, so nothing runs on without it.

    The agents run in their own process groups (ask.py), so Ctrl+C does not reach
    them; without this they ran on (and edited files) after the server was gone, and
    the pool's threads kept the terminal waiting for up to 30 minutes. The jobs that
    waited in the pool stay queued; a cut job stays "running" and recover() makes it
    "interrupted" at the next start (see job_run.SHUTDOWN).
    """
    if job_run.SHUTDOWN.is_set():
        return
    job_run.SHUTDOWN.set()
    pool.shutdown(wait=False, cancel_futures=True)
    for job in jobs.all_jobs():
        if job.status == "running" and job.worker == os.getpid() and job.pid is not None:
            kill_group(job.pid, signal.SIGTERM)
            print(f"Ended the agent of job {job.id}: resume it after the next start", flush=True)


class Server(uvicorn.Server):
    """uvicorn's server, with stop_work at the FIRST Ctrl+C.

    uvicorn runs the lifespan's end only after every connection is closed; an open
    page keeps its live-update stream open, and a second Ctrl+C skips the lifespan.
    """

    def handle_exit(self, sig, frame):
        stop_work()
        super().handle_exit(sig, frame)


@asynccontextmanager
async def lifespan(app):
    """Code that runs when the server starts (before yield) and stops (after it)."""
    # Jobs that were running when the server stopped wait for Resume (jobs.recover).
    for job in jobs.recover():
        print(f"Interrupted job {job.id}: the server stopped while it was running")
    task = asyncio.create_task(routine_loop()) if app.state.routines else None
    yield
    if task is not None:
        task.cancel()
    stop_work()  # also when the server stops in another way


app = FastAPI(title="Agent Office", lifespan=lifespan)
app.state.port = 8000
app.state.routines = True


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Refuse requests that do not come from our own page.

    Listening on 127.0.0.1 is not enough. Another web site that is open in
    your browser, or another program on this computer, can also send requests
    to 127.0.0.1. access.decide checks the Host, the Origin and the token.
    """
    port = app.state.port
    # Read the file on every request: delete data/app-token and the old token stops at once
    token = await asyncio.to_thread(access.token_or_none)
    verdict, text = access.decide(request.method, request.url.path, request.headers, request.query_params,
                                  request.cookies, port, token)
    if verdict == "deny":
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": text}, status_code=403)  # ui.js shows "detail"
        return PlainTextResponse(text, status_code=403)
    if verdict == "login":
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(access.cookie_name(port), token, max_age=access.COOKIE_DAYS * 86400,
                            httponly=True, samesite="strict")
        return response
    return await call_next(request)


# --- Pages ---

# The browser must ask for the pages and their CSS/JS each time ("no-cache"); an unchanged file is
# a short 304 answer (its ETag). Without it the browser guesses how long its copy is good (hours,
# for an old file) and shows a new page with the old ui.css: that happened on 2026-11-16.
NO_CACHE = {"Cache-Control": "no-cache"}


class StaticNoCache(StaticFiles):
    """The files in static/, with the NO_CACHE header."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


@app.get("/")
def page_jobs():
    return FileResponse(STATIC_DIR / "jobs.html", headers=NO_CACHE)


@app.get("/costs")
def page_costs():
    return FileResponse(STATIC_DIR / "costs.html", headers=NO_CACHE)


@app.get("/artifacts")
def page_artifacts():
    return FileResponse(STATIC_DIR / "artifacts.html", headers=NO_CACHE)


@app.get("/brain")
def page_brain():
    return FileResponse(STATIC_DIR / "brain.html", headers=NO_CACHE)


app.mount("/static", StaticNoCache(directory=STATIC_DIR), name="static")


# --- Jobs ---

def job_summary(job):
    """Return the short form of a job for the list (without the long result)."""
    return {
        "id": job.id, "task": job.task, "status": job.status, "model": job.model,
        "effort": job.effort, "lead": job.lead, "cost": job.cost,
        "created_at": job.created_at, "tries": job.tries, "check": job.check,
        "skill": job.skill, "routine": job.routine, "reference": job.reference,
        "interrupted_why": job.interrupted_why, "question": job.question, "held_for": job.held_for,
        "review": job.review, "signals": job.signals, "lesson_for": job.lesson_for, "plan": job.plan,
        "parent": job.parent, "title": job.title, "desk": job.desk, "waits_for": job.waits_for,
    }


def jobs_list():
    return [job_summary(job) for job in reversed(jobs.all_jobs())]  # newest first


@app.get("/api/jobs")
def api_jobs():
    return jobs_list()


def require_job(job_id):
    """Answer 404 for an id that is not a job (only letters and digits: it becomes a file name)."""
    if not job_id.isalnum() or not jobs.exists(job_id):
        raise HTTPException(404, "no such job")


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    require_job(job_id)
    job = jobs.load(job_id)
    data = asdict(job)
    # The page shows the reference next to the critic's verdicts.
    reference = Path(job.reference) if job.reference else None
    data["reference_text"] = reference.read_text() if reference and reference.is_file() else None
    data["inbox"] = jobs.read_inbox(job_id)  # messages the agent has not read yet
    data["cold_warning"] = jobs.cold_warning(job)
    # What earlier jobs on this model really cost: it helps to judge the plan's estimate.
    pending = job.plan and job.plan["status"] == "pending"  # shown only while the plan waits
    data["cost_history"] = jobs.cost_reference(job.model) if pending else None
    # The numbered items of a skill with wildcards: the page marks the wildcards and can keep them.
    data["wildcard_items"] = wildcards.items(job.result) if job.wildcards else None
    data["commands"] = commands.of_job(job_id)  # shell commands the agent asked to run (commands.py)
    # The sub-jobs it delegated (specialists.py), oldest first.
    data["sub_jobs"] = [job_summary(other) for other in jobs.all_jobs() if other.parent == job_id]
    return data


class NewJob(BaseModel):
    """The form for a new job. The browser sends it as JSON."""
    task: str
    model: str | None = None  # None: the router chooses
    lead: str | None = None  # "auto": the dispatcher chooses
    folder: str | None = None
    check: str | None = None
    reference: str | None = None  # the text of a good example answer, for the critic
    memory: bool = True
    plan: bool = False  # the agent proposes a plan with a cost first (opus jobs always do)


@app.post("/api/jobs")
def api_add_job(new: NewJob):
    """Add a job and start it in the background. Return the job."""
    if not new.task.strip():
        raise HTTPException(400, "the task is empty")
    if new.task.rstrip().endswith("Next:"):
        # The text of the Continue button with nothing after it: the agent would guess the work.
        raise HTTPException(400, 'Write the next step after "Next:" (the text above it only says '
                                 "where the project stands).")
    if new.model and new.model not in MODELS:  # the CLI checks this with argparse choices
        raise HTTPException(400, f"unknown model {new.model} (use {', '.join(MODELS)})")
    found = skills.triggered(new.task)
    if found and new.lead in (None, "", "auto") and not (new.folder or new.check or new.reference):
        # "check my email ..." runs the inbox skill (its trigger phrases; Python, free). A lead,
        # a folder, a check or a reference that you chose means a normal job.
        return api_run_skill(found[0], SkillRun(input=found[1], model=new.model or None))
    # jobs.add wants a file, so a pasted reference is saved to data/references/ first.
    reference = jobs.save_reference(new.reference) if new.reference and new.reference.strip() else None
    lead = new.lead or None
    if lead == "auto" and (new.model or new.folder):
        # "Auto" is the form's default. A model or a folder that you chose wins: a lead
        # brings its own model and folder, and would ignore (or refuse) yours.
        lead = None
    try:
        job = jobs.add(
            new.task, new.model or None, use_memory=new.memory,
            lead=lead, folder=new.folder or None, check=new.check or None,
            reference=reference, plan_first=new.plan,
        )
    except ValueError as error:  # wrong input (jobs.add)
        if reference is not None:
            reference.unlink(missing_ok=True)  # no job uses this file
        raise HTTPException(400, str(error)) from error
    run_in_pool(job)
    return job_summary(job)


@app.post("/api/run")
def api_run_queued():
    """Start all queued jobs (for example, jobs added in the terminal)."""
    queued = [job for job in jobs.all_jobs() if job.status == "queued"]
    for job in queued:
        run_in_pool(job)
    return {"started": len(queued)}


@app.post("/api/jobs/{job_id}/stop")
def api_stop(job_id: str):
    require_job(job_id)
    jobs.stop(job_id)
    for held in jobs.release_held(job_id):  # a stopped question frees the lead's other jobs
        run_in_pool(held)
    return job_summary(jobs.load(job_id))


@app.post("/api/jobs/{job_id}/resume")
def api_resume(job_id: str):
    """Continue an interrupted job in its own session (jobs.resume), in the background."""
    require_job(job_id)
    try:
        job = jobs.resume(job_id)
    except ValueError as error:  # the job is not interrupted
        raise HTTPException(409, str(error)) from error
    run_in_pool(job)
    return job_summary(job)


class Message(BaseModel):
    text: str


@app.post("/api/jobs/{job_id}/messages")
def api_follow_up(job_id: str, message: Message):
    """Write a follow-up message to a job (jobs.follow_up). A finished job starts again."""
    require_job(job_id)
    if not message.text.strip():
        raise HTTPException(400, "the message is empty")
    try:
        job, start, warning = jobs.follow_up(job_id, message.text)
    except ValueError as error:  # the job is interrupted: resume it first
        raise HTTPException(409, str(error)) from error
    if start:
        run_in_pool(job)
    return {"job": job_summary(job), "started": start, "warning": warning}


class Review(BaseModel):
    verdict: str  # approve, reject or changes
    note: str = ""  # approve: optional; reject: why (required); changes: what to change in the answer
    files: dict[str, str] = {}  # changes: a comment per changed file (path -> comment)


@app.post("/api/jobs/{job_id}/review")
def api_review(job_id: str, review: Review):
    """Review a finished job: approve it, reject it, or ask for changes (the job runs again)."""
    require_job(job_id)
    if review.verdict not in ("approve", "reject", "changes"):
        raise HTTPException(400, "verdict must be approve, reject or changes")
    if jobs.load(job_id).status not in job_store.FINISHED:
        raise HTTPException(409, "the job has not finished yet")
    try:
        if review.verdict == "approve":
            return {"job": job_summary(jobs.approve(job_id, review.note)), "started": False, "warning": None}
        if review.verdict == "reject":
            rejected = jobs.reject(job_id, review.note)
            start_lessons(job_id)
            return {"job": job_summary(rejected), "started": False, "warning": None}
        files = {path: note for path, note in review.files.items() if note.strip()}
        job, start, warning = jobs.comment(job_id, review.note, files)
    except ValueError as error:  # no reason, no comments, or a file the job did not change
        raise HTTPException(400, str(error)) from error
    if start:
        run_in_pool(job)
    start_lessons(job_id)
    return {"job": job_summary(job), "started": start, "warning": warning}


def start_lessons(job_id):
    """Start the lesson jobs that a bad result on job_id added (jobs.ask_lesson)."""
    for lesson_job in jobs.queued_lessons(job_id):
        run_in_pool(lesson_job)


class PlanDecision(BaseModel):
    decision: str  # approve, change or reject
    note: str = ""  # change: the changes (required); reject: why (optional)


@app.post("/api/jobs/{job_id}/plan")
def api_plan(job_id: str, plan: PlanDecision):
    """Decide about a job's plan. Approve and change start the work; reject stops the job."""
    require_job(job_id)
    job = jobs.load(job_id)
    if job.status != "waiting" or not job.plan or job.plan["status"] != "pending":
        raise HTTPException(409, "no plan waits for a decision on this job")
    try:
        job, start = jobs.decide_plan(job_id, plan.decision, plan.note)
    except ValueError as error:  # an unknown decision, or a change without a note
        raise HTTPException(400, str(error)) from error
    if start:
        run_in_pool(job)
    else:
        for held in jobs.release_held(job_id):  # a rejected plan frees the lead's other jobs
            run_in_pool(held)
    return {"job": job_summary(job), "started": start}


class Settings(BaseModel):
    auto_approve_plans_usd: float
    isolate_jobs: str | None = None  # always, shared or off (workspaces.py); None: no change
    commands_mode: str | None = None  # ask or off (commands.py); None: no change
    commands_allow: list[str] | None = None  # commands that run without asking; None: no change
    lead_twins: int | None = None  # second desks per lead (leads.py); None: no change


@app.get("/api/settings")
def api_settings():
    return jobs.settings()


@app.post("/api/settings")
def api_save_settings(new: Settings):
    """Save the user's settings: the amount up to which a plan needs no OK, and when jobs get a workspace."""
    try:
        jobs.set_auto_approve(new.auto_approve_plans_usd)
        if new.isolate_jobs is not None:
            jobs.set_setting("isolate_jobs", new.isolate_jobs)
        if new.commands_mode is not None:
            jobs.set_setting("commands_mode", new.commands_mode)
        if new.lead_twins is not None:
            jobs.set_setting("lead_twins", new.lead_twins)
        if new.commands_allow is not None:
            jobs.set_setting("commands_allow", [" ".join(entry.split()) for entry in new.commands_allow if entry.strip()])
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return jobs.settings()


class CommandDecision(BaseModel):
    decision: str  # allow, always or deny


@app.post("/api/commands/{command_id}")
def api_decide_command(command_id: str, new: CommandDecision):
    """Your answer to a shell command that an agent waits to run (commands.py)."""
    try:
        return commands.decide(command_id, new.decision, jobs.settings(), jobs.set_setting)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.get("/api/jobs/{job_id}/changes")
def api_changes(job_id: str):
    """The files the job's agent edited: path, state and diff (see changes.py)."""
    require_job(job_id)
    job = jobs.load(job_id)
    if job.workspace and not workspaces.active(job):
        return []  # the copies were of the workspace, which is applied or discarded now
    return changes.changes(job_id)


@app.get("/api/jobs/{job_id}/workspace")
def api_workspace(job_id: str):
    """The job's isolated workspace: status, changed files and the diff (see workspaces.py)."""
    require_job(job_id)
    try:
        return workspaces.review(jobs.load(job_id))
    except workspaces.WorkspaceError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/jobs/{job_id}/workspace/{action}")
def api_workspace_action(job_id: str, action: str):
    """Apply the workspace's changes to the project, or discard them."""
    require_job(job_id)
    if action not in ("apply", "discard"):
        raise HTTPException(404, "no such action")
    try:
        job, message = jobs.workspace_action(job_id, action)
    except ValueError as error:  # WorkspaceError too
        raise HTTPException(409, str(error)) from error
    return {"status": job.workspace["status"], "message": message}


class Undo(BaseModel):
    path: str | None = None  # None: undo every file of the job


@app.post("/api/jobs/{job_id}/undo")
def api_undo(job_id: str, request: Undo):
    """Put the job's files back as they were before the job. Return the undone paths."""
    require_job(job_id)
    try:
        undone = jobs.undo(job_id, request.path)  # an undo is a bad result (job.signals)
    except ValueError as error:  # the job is still working
        raise HTTPException(409, str(error)) from error
    start_lessons(job_id)
    return {"undone": undone}


def jobs_signature():
    """Return the names and change times of the job files.

    When this value changes, some job has changed.
    """
    if not job_store.JOBS_DIR.exists():
        return ()
    # A command request (commands.py) is not in the job's file: its changes count too.
    files = [*job_store.JOBS_DIR.glob("*.json"), *commands.COMMANDS_DIR.glob("*.json")]
    return tuple(sorted((path.name, path.stat().st_mtime_ns) for path in files))


@app.get("/events")
async def events(request: Request):
    """Server-sent events: send the job list each time a job file changes.

    The browser keeps this one connection open. Every message is a line
    "data: <JSON>" and an empty line. We look at the files once a second,
    so jobs started in the terminal appear here too.
    """
    given = access.sent(request.headers, request.cookies, app.state.port)

    async def stream():
        last = None
        # The token is checked when a request starts; this stream lives for hours,
        # so it looks again each second (the token file was deleted: stop sending).
        while (not job_run.SHUTDOWN.is_set() and not await request.is_disconnected()
               and await asyncio.to_thread(access.still_valid, given)):
            # Reading the files runs in a thread: in the event loop it would make
            # every other request wait while many job files are read.
            try:
                signature = await asyncio.to_thread(jobs_signature)
                if signature != last:
                    data = json.dumps(await asyncio.to_thread(jobs_list))
                    last = signature
                    yield f"data: {data}\n\n"
            except (OSError, ValueError, TypeError, KeyError) as error:
                # A file renamed while we looked, a broken job file: try again in a second,
                # never end the stream (every reconnect would fail the same way).
                print(f"Live updates: could not read the jobs ({error!r}); trying again")
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


# --- Skills ---

@app.get("/api/skills")
def api_skills():
    """All skills, with how often each one ran and what it cost."""
    runs = {}
    for job in jobs.all_jobs():
        if job.skill:
            add_cost(runs, job.skill, job.cost)
    return [
        {**skill, "runs": runs.get(skill["name"], {}).get("jobs", 0),
         "cost": runs.get(skill["name"], {}).get("cost", 0.0),
         "feedback": len(feedback.open_items(skill["name"]))}
        for skill in skills.all_skills()
    ]


class SkillRun(BaseModel):
    input: str
    model: str | None = None  # None: the skill's default
    effort: str | None = None


@app.post("/api/skills/{name}/run")
def api_run_skill(name: str, run: SkillRun):
    """Add a job for a skill and start it in the background."""
    try:
        job = skills.add(name, run.input, run.model or None, run.effort or None)
    except (SystemExit, ValueError) as error:  # ValueError: a mistake in the skill file
        raise HTTPException(400, str(error)) from error
    run_in_pool(job)
    return job_summary(job)


# --- Routines ---

@app.get("/api/routines")
def api_routines():
    """All routines: schedule, mode, next run, cost in 7 days and the cap."""
    return routines.summary()


@app.post("/api/routines/{name}/run")
def api_run_routine(name: str):
    """Start one routine now. Passive: queued jobs. Active: the jobs run in the background.

    Return {"jobs": [...]}; the list is empty when an action finds nothing to do.
    """
    try:
        added = routines.run_now(name, run=run_in_pool)
    except (SystemExit, ValueError) as error:
        raise HTTPException(400, str(error)) from error
    if added is None:
        raise HTTPException(400, f"Skipped: the routine {name} reached its weekly cost cap.")
    return {"jobs": [job_summary(job) for job in added]}


@app.post("/api/jobs/{job_id}/wildcards/{number}/keep")
def api_keep_wildcard(job_id: str, number: int):
    """Add a wildcard item of a done skill job to the skill's references (Python writes it)."""
    require_job(job_id)
    try:
        text = skills.keep_wildcard(job_id, number)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"kept": text, "job": job_summary(jobs.load(job_id)), "wildcards": jobs.load(job_id).wildcards}


class Feedback(BaseModel):
    text: str


@app.post("/api/skills/{name}/feedback")
def api_add_feedback(name: str, item: Feedback):
    """Park one feedback item for a skill (Python only, no agent, no cost)."""
    try:
        feedback.add(name, item.text)
    except SystemExit as error:
        raise HTTPException(400, str(error)) from error
    return {"open": len(feedback.open_items(name))}


@app.get("/api/proposals")
def api_proposals():
    """The proposals from calibrate, each with its diff."""
    return [{"name": name, "diff": feedback.diff(name)} for name in feedback.proposals()]


@app.post("/api/proposals/{name}/{action}")
def api_proposal_action(name: str, action: str):
    """Accept or reject a proposal. The user decides, not the agent."""
    if action not in ("accept", "reject"):
        raise HTTPException(404, "unknown action")
    try:
        feedback.accept(name) if action == "accept" else feedback.reject(name)
    except SystemExit as error:
        raise HTTPException(400, str(error)) from error
    return {"done": action}


# --- Today: what needs you, who works, what finished ---

@app.get("/api/brain")
def api_brain():
    """The map of notes, areas, leads and projects (brain.py; Python, free)."""
    return brain.graph()


@app.get("/api/today")
def api_today():
    return today.today()


# --- Project memory: instructions, decisions, known failures, past jobs per project ---

@app.get("/api/projects")
def api_projects():
    """Every project with a memory, newest first, with its entries (newest first)."""
    rows = project_memory.all_projects()
    checks = project_memory.last_checks(jobs.all_jobs())
    for row in rows:
        row["entries"] = list(reversed(project_memory.load(row["project"])["entries"]))
        row["last_check"] = checks.get(row["project"])  # Continue puts it into the Check field
    return rows


class ProjectEntry(BaseModel):
    folder: str  # any folder of the project; its git repository is the project
    kind: str = "instruction"
    text: str


@app.post("/api/projects/entries")
def api_add_project_entry(new: ProjectEntry):
    """The user adds an instruction (or a decision, a known failure) to a project."""
    if new.kind not in project_memory.USER_KINDS:
        raise HTTPException(400, f"The kind is one of: {', '.join(project_memory.USER_KINDS)}.")
    project = project_memory.project_of(new.folder)
    if project is None:
        raise HTTPException(400, project_memory.folder_problem(new.folder))
    try:
        return project_memory.add(project, new.kind, new.text)
    except ValueError as error:  # too short or long, or a secret
        raise HTTPException(400, str(error)) from error


@app.post("/api/projects/{name}/entries/{entry_id}/remove")
def api_remove_project_entry(name: str, entry_id: str):
    """Remove one entry of a project's memory."""
    try:
        project_memory.remove(project_memory.by_file(name), entry_id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    return {"removed": entry_id}


# --- Lessons of the leads (drafts wait for the user) ---

@app.get("/api/lessons")
def api_lessons():
    """The lesson drafts and the kept lessons of every lead (leads.load_lessons)."""
    return [{"lead": name, **leads.load_lessons(name)} for name in leads.roster()]


class KeepLesson(BaseModel):
    text: str | None = None  # None: keep the draft as the lead wrote it


@app.post("/api/lessons/{lead}/{draft_id}/{action}")
def api_lesson_action(lead: str, draft_id: str, action: str, keep: KeepLesson | None = None):
    """Keep (as written, or with your own words) or drop a lesson draft. The user decides."""
    if lead not in leads.roster() or not draft_id.isalnum():
        raise HTTPException(404, "no such lead or draft")
    if action not in ("keep", "drop"):
        raise HTTPException(404, "unknown action")
    try:
        if action == "keep":
            return {"kept": leads.keep_lesson(lead, draft_id, keep.text if keep else None)}
        leads.drop_lesson(lead, draft_id)
        return {"dropped": draft_id}
    except ValueError as error:  # no such draft, or the edited line is empty or too long
        raise HTTPException(400, str(error)) from error


# --- Recos: suggested routines and repeated corrections ---

@app.get("/api/recos")
def api_recos():
    """The saved Recos cards. The first time (no file yet) Python computes them (free, no AI)."""
    found = recos.load_recos()
    if found["at"] is None:
        found, _ = recos.refresh()
    return {"at": found["at"], "days": found["days"], "cards": found["cards"]}


@app.post("/api/recos/refresh")
def api_recos_refresh():
    found, new = recos.refresh()
    return {"at": found["at"], "days": found["days"], "cards": found["cards"], "new": new}


class RecosSetup(BaseModel):
    ids: list[str]  # the cards in the cart


@app.post("/api/recos/setup")
def api_recos_setup(chosen: RecosSetup):
    """Set up the cards in the cart: a routine file, or a lesson job (started here)."""
    if not chosen.ids:
        raise HTTPException(400, "The cart is empty.")
    try:
        messages, started = recos.setup(chosen.ids)
    except ValueError as error:  # no such card
        raise HTTPException(404, str(error)) from error
    for lesson_job in started:
        run_in_pool(lesson_job)
    return {"done": messages, "cards": recos.load_recos()["cards"]}


@app.post("/api/recos/{card_id}/dismiss")
def api_recos_dismiss(card_id: str):
    try:
        recos.dismiss(card_id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    return {"cards": recos.load_recos()["cards"]}


# --- Leads, limits, costs ---

@app.get("/api/leads")
def api_leads():
    return [
        {"name": name, "area": role["area"], "model": role["model"], **leads.load_state(name)}
        for name, role in leads.roster().items()
    ]


@app.get("/api/limits")
def api_limits():
    return {"windows": limits.load(), "pause_reason": limits.pause_reason()}


@app.get("/api/weather")
def api_weather():
    """The weather for the top bar. A plain `def`: FastAPI runs it in a thread, so the network call blocks nobody."""
    try:
        return weather.weather()
    except weather.WeatherError as error:
        raise HTTPException(503, str(error)) from error


def add_cost(totals, key, cost):
    entry = totals.setdefault(key, {"name": key, "jobs": 0, "cost": 0.0})
    entry["jobs"] += 1
    entry["cost"] += cost


@app.get("/api/costs")
def api_costs():
    """Costs: totals by model, lead, skill, routine and router, the most expensive
    jobs, and the unused leads, skills and routines."""
    all_jobs = jobs.all_jobs()
    by_model, by_lead, by_skill, by_routine = {}, {}, {}, {}
    for job in all_jobs:
        add_cost(by_model, job.model, job.cost)
        add_cost(by_lead, job.lead or "(no lead)", job.cost)
        add_cost(by_skill, job.skill or "(no skill)", job.cost)
        add_cost(by_routine, job.routine or "(no routine)", job.cost)
    by_router = {}
    if ROUTER_LOG.exists():
        for line in ROUTER_LOG.read_text().splitlines():
            try:
                decision = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line cut while it was written: skip it, do not fail the page
            if isinstance(decision, dict):  # a null cost (Jev) counts as 0
                add_cost(by_router, decision.get("router") or "(unknown)", decision.get("cost") or 0.0)
    lead_states = api_leads()
    for lead in lead_states:
        by_lead.setdefault(lead["name"], {"name": lead["name"], "jobs": 0, "cost": 0.0})

    def sort(totals):
        return sorted(totals.values(), key=lambda entry: entry["cost"], reverse=True)

    expensive = sorted(all_jobs, key=lambda job: job.cost, reverse=True)[:10]
    return {
        "total": sum(job.cost for job in all_jobs) + sum(entry["cost"] for entry in by_router.values()),
        "jobs": len(all_jobs),
        "by_model": sort(by_model),
        "by_lead": sort(by_lead),
        "by_router": sort(by_router),
        "by_skill": sort(by_skill),
        "by_routine": sort(by_routine),
        "expensive": [job_summary(job) for job in expensive],
        "unused_leads": [lead["name"] for lead in lead_states if lead["replies"] == 0],
        "unused_skills": [skill["name"] for skill in skills.all_skills() if skill["name"] not in by_skill],
        "unused_routines": [row["name"] for row in routines.summary(all_jobs=all_jobs) if row["name"] not in by_routine],
        "outcomes": outcomes.report(all_jobs=all_jobs),  # cost per successful job, the last 30 days
    }


# --- Artifacts ---

@app.get("/api/artifacts")
def api_artifacts():
    return artifacts.all_artifacts()


@app.get("/api/artifacts/{job_id}")
def api_artifact(job_id: str):
    found = artifacts.read(job_id)
    if found is None:
        raise HTTPException(404, "no such artifact")
    meta, text = found
    return {**meta, "text": text}


def main():
    parser = argparse.ArgumentParser(description="Start the local web page.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-routines", action="store_true",
                        help="do not start routines (use this when cron calls `routines tick`)")
    args = parser.parse_args()
    app.state.port = args.port
    app.state.routines = not args.no_routines
    # The link holds the token. It is printed only here, in your own terminal (like Jupyter does)
    print(f"Open {access.link(args.port)}")
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port)  # local only (project rule)
    logging.getLogger("uvicorn.access").addFilter(access.HideToken())  # after Config: it sets up the loggers
    try:
        Server(config).run()
    except KeyboardInterrupt:  # Ctrl+C: uvicorn.run hides it the same way
        pass


if __name__ == "__main__":
    main()
