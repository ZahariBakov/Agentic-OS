"""Jobs: tasks for agents, saved as JSON files in data/jobs/.

Usage (from the project folder):
    python -m agentic_os.jobs add "your task" [--model sonnet]
    python -m agentic_os.jobs add "your task" --lead [name]
    python -m agentic_os.jobs add "your task" --folder <path> --check "pytest -q"
    python -m agentic_os.jobs add "your task" --reference <file with a good example answer>
    python -m agentic_os.jobs list
    python -m agentic_os.jobs show <job id>
    python -m agentic_os.jobs run [--workers 3]
    python -m agentic_os.jobs stop <job id>
    python -m agentic_os.jobs recover             mark jobs whose process died as interrupted
    python -m agentic_os.jobs resume <job id>     continue an interrupted job in its session
    python -m agentic_os.jobs follow-up <job id> "message"   write to a job, during or after it
    python -m agentic_os.jobs answer <job id> "answer"       answer a job's question (the same as follow-up)
    python -m agentic_os.jobs approve <job id> [--note "..."] the result is good
    python -m agentic_os.jobs reject <job id> "why"           the result is bad, and no new try
    python -m agentic_os.jobs comment <job id> "what to change" [--on <file> "comment"]   ask for changes
    python -m agentic_os.jobs undo <job id> [<file>]          undo the job's file changes (a bad result)
    python -m agentic_os.jobs add "your task" --plan         the agent proposes a plan with a cost first
    python -m agentic_os.jobs plan <job id> approve|change|reject ["note"]   decide about a plan
    python -m agentic_os.jobs auto-approve [<usd>]           show or set: plans up to this cost need no OK
    python -m agentic_os.jobs ingest <file in memory/raw/>
    python -m agentic_os.jobs ingest-new          ingest every raw file that is new

The code is in four parts, each one imports only the parts before it:
    job_store.py    the Job record, its files, the claim lock, the inbox, the settings
    job_talk.py     questions, plans with a cost, follow-up messages
    job_review.py   approve, reject, comment, undo, lessons after a bad result
    job_run.py      agent calls, checks, run_job, desks, stop, recover, resume
This module has the commands (main), ingest, and the functions of the four parts
under their old names, so `jobs.add(...)` or `jobs.run_job(...)` still work.

Only functions and classes are here under the old name, never a constant or `ask`:
a test that replaces one (monkeypatch) must do it in the module that owns the name.
`monkeypatch.setattr(jobs, "ask", fake)` would change nothing in job_run.py, and the
test would call the real agent. Without the name here, that line fails at once.
"""

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

from agentic_os import documents, job_review, job_run, job_store, job_talk, memory
from agentic_os.job_store import (  # noqa: F401 (for jobs.<name>)
    Job, add, all_jobs, busy_error, claim_lock, exists, finished_job, inbox_file, is_alive, is_stopped, load,
    new_id, now, process_started, read_inbox, save, save_reference, saved_at, set_auto_approve, set_setting,
    settings, stop_file, take_inbox, update, usd, worker_busy, write_inbox,
)
from agentic_os.job_talk import (  # noqa: F401 (for jobs.<name>)
    add_messages, auto_approve_reason, can_ask, cold_warning, cost_reference, decide, decide_plan,
    estimate_of, extract_plan, extract_question, follow_up, last_activity, last_answer, message_text,
    needs_plan, plan_to_conversation, prompt_for,
)
from agentic_os.job_review import (  # noqa: F401 (for jobs.<name>)
    add_signal, approve, ask_lesson, comment, queued_lessons, reject, save_lesson_draft, undo,
    workspace_action,
)
from agentic_os.job_run import (  # noqa: F401 (for jobs.<name>)
    Waits, agent_args, call_agent, can_edit, claim, continue_call, desk_candidates, desk_free, edit_folder,
    fill_inbox, first_call, follow_up_call, lead_args, next_of_routine, no_tools, prepare_workspace, recover,
    release_held, resume, resume_brief, resume_call, routine_wait_reason, run_check, run_critic, run_job,
    run_queued, save_project_memory, stop, take_desk_for, tool_timeout_ms, waiting_job, work, work_folder,
)
from agentic_os.router import MODELS


INGEST_TASK = """Read the source file {path}. Find the key ideas that are useful to remember.
First look at the memory map. If a note about the same idea already exists, read it,
and write an updated note with the same title. For each idea, write one <note> block.
Do not change the source file."""

# "Read the source file /abs/path.md. Find the key ideas..." -> /abs/path.md
OLD_INGEST_PATTERN = re.compile(r"Read the source file (\S+?)\.(?:\s|$)")
MEETINGS_DIR_NAME = "meetings"  # transcripts go into memory/raw/meetings/
SOURCE_SUFFIXES = (".md", ".txt")

MEETING_TASK = """Read the meeting transcript {path}.
Write one <note> block for this meeting, with area="meetings" and the title
"Meeting <YYYY-MM-DD>: <topic>". Take the date from the transcript or the file name;
if there is none, write "date unknown". The note has four parts:
- Summary: 3 to 5 lines
- Decisions: one line each
- Action items: one line each: who, what, and when (if the transcript says it)
- Open questions: one line each
Write "none" for an empty part. Use only facts from the transcript: never invent
names, dates or numbers.
If a decision changes a fact in an existing note (see the memory map), read that
note and write an updated note with the same title.
Do not change the source file."""


def ingest(path, fields=None):
    """Add a job that turns a source file in memory/raw/ into notes.

    A file in memory/raw/meetings/ is a meeting transcript and gets MEETING_TASK.
    fields: more job fields, set before the one save (a routine's mark).
    """
    source = Path(path).resolve()
    if not source.is_relative_to(memory.RAW_DIR.resolve()) or not source.is_file():
        raise ValueError(f"Not a file in {memory.RAW_DIR}: {path}")
    meeting = source.is_relative_to(memory.RAW_DIR / MEETINGS_DIR_NAME)
    task = (MEETING_TASK if meeting else INGEST_TASK).format(path=source)
    # A fixed model: the keyword rules would look at words in the file name.
    return job_store.add(task, model="sonnet", effort="medium", fields={**(fields or {}), "source": str(source)})


def job_source(job):
    """Return the raw file that a job ingests, or None.

    Old ingest jobs have no `source` field; their first task line has the path.
    """
    if job.source:
        return job.source
    match = OLD_INGEST_PATTERN.match(job.task)
    return match[1] if match else None


def new_sources():
    """Return the raw files that still need an ingest job, oldest name first.

    A file needs one when no job for it is done, queued, running or interrupted
    (an interrupted job waits for Resume). A failed or stopped ingest does not
    count, so the file is tried again.
    """
    if not memory.RAW_DIR.exists():
        return []
    handled = {job_source(job) for job in job_store.all_jobs()
               if job.status in ("done", "queued", "running", "interrupted")}
    raw = memory.RAW_DIR.resolve()
    return [path for path in sorted(memory.RAW_DIR.rglob("*"))
            if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES
            and path.resolve().is_relative_to(raw)  # a link to a file outside raw/ is not a source
            and str(path.resolve()) not in handled]


def ingest_new(limit=None, fields=None):
    """Add one ingest job for each new raw file (at most `limit`). Return the jobs.

    New documents (PDF, Office, HTML) are converted to Markdown first, so their
    .md files are new sources too. A document that cannot be converted is skipped
    with a message.
    """
    _, problems = documents.convert_new()
    for problem in problems:
        print(f"Skipped: {problem}")
    added = []
    for path in new_sources()[:limit]:
        try:
            added.append(ingest(path, fields))
        except ValueError as error:  # one bad file never stops the others
            print(f"Skipped: {error}")
    return added


def save_result(job_id, title, area):
    """Save the result of a finished job as a note (a good answer goes back to memory)."""
    job = job_store.load(job_id)
    if job.status != "done":
        raise SystemExit(f"Job {job_id} is not done (status: {job.status}).")
    return memory.save_note(title, job.result, area, source=f"job {job.id}")


def print_list():
    """Print one line per job."""
    for job in job_store.all_jobs():
        effort = job.effort or "-"
        lead = job.lead or "-"
        print(
            f"{job.id}  {job.status:<8} {job.model:<6} {effort:<6} {lead:<6}"
            f"  ${job.cost:.4f}  {job.task[:50]}"
        )


def main():
    parser = argparse.ArgumentParser(description="Manage jobs for agents.")
    commands = parser.add_subparsers(dest="command", required=True)

    add_parser = commands.add_parser("add", help="add a new job")
    add_parser.add_argument("task", help="the task for the agent")
    add_parser.add_argument(
        "--model",
        choices=MODELS,
        help="the model to use (default: the router chooses)",
    )
    add_parser.add_argument("--no-memory", action="store_true", help="run without memory")
    add_parser.add_argument(
        "--lead", nargs="?", const="auto", help="give the job to a lead (default: the dispatcher chooses)"
    )
    add_parser.add_argument("--folder", help="the folder where the agent works and may edit files")
    add_parser.add_argument("--check", help='a command that must pass, for example "pytest -q"')
    add_parser.add_argument("--reference", help="a file with a good example answer; a critic compares")
    add_parser.add_argument("--plan", action="store_true", help="the agent proposes a plan with a cost first")

    ingest_parser = commands.add_parser("ingest", help="add a job: a raw source -> notes")
    ingest_parser.add_argument("path", help="a file in memory/raw/")
    commands.add_parser("ingest-new", help="add an ingest job for every new raw file")

    save_parser = commands.add_parser("save", help="save a job's result as a note")
    save_parser.add_argument("job_id")
    save_parser.add_argument("title")
    save_parser.add_argument("--area", default="general")

    commands.add_parser("list", help="list all jobs")

    show_parser = commands.add_parser("show", help="show one job")
    show_parser.add_argument("job_id")

    run_parser = commands.add_parser("run", help="run all queued jobs")
    run_parser.add_argument("--workers", type=int, default=3, help="jobs at the same time")

    stop_parser = commands.add_parser("stop", help="stop a running job, or cancel a queued one")
    stop_parser.add_argument("job_id")

    commands.add_parser("recover", help="mark running jobs whose process died as interrupted")
    resume_parser = commands.add_parser("resume", help="continue an interrupted job in its session")
    resume_parser.add_argument("job_id")
    follow_parser = commands.add_parser("follow-up", help="write a message to a job, during or after it")
    follow_parser.add_argument("job_id")
    follow_parser.add_argument("message")
    answer_parser = commands.add_parser("answer", help="answer a job's question (the same as follow-up)")
    answer_parser.add_argument("job_id")
    answer_parser.add_argument("message")
    approve_parser = commands.add_parser("approve", help="the result is good")
    approve_parser.add_argument("job_id")
    approve_parser.add_argument("--note", default="")
    reject_parser = commands.add_parser("reject", help="the result is bad, and no new try")
    reject_parser.add_argument("job_id")
    reject_parser.add_argument("note", help="why the result is bad")
    comment_parser = commands.add_parser("comment", help="ask for changes (the job runs again)")
    comment_parser.add_argument("job_id")
    comment_parser.add_argument("text", help="what to change in the answer (can be empty)")
    comment_parser.add_argument("--on", nargs=2, action="append", default=[], metavar=("FILE", "COMMENT"),
                                help="a comment on one changed file (can be given more times)")
    plan_parser = commands.add_parser("plan", help="decide about a job's plan")
    plan_parser.add_argument("job_id")
    plan_parser.add_argument("decision", choices=["approve", "change", "reject"])
    plan_parser.add_argument("note", nargs="?", default="", help="the changes (for change) or why (for reject)")
    auto_parser = commands.add_parser("auto-approve", help="show or set the amount for plans that need no OK")
    auto_parser.add_argument("usd", nargs="?", type=float, help="in US dollars; 0 turns it off")
    undo_parser = commands.add_parser("undo", help="undo the job's file changes (a bad result)")
    undo_parser.add_argument("job_id")
    undo_parser.add_argument("file", nargs="?")

    args = parser.parse_args()
    if args.command in ("show", "stop", "save", "resume", "follow-up", "answer", "approve", "reject",
                        "comment", "undo", "plan") and not job_store.exists(args.job_id):
        print(f"No job with id {args.job_id}. Use 'list' to see the ids.")
        sys.exit(1)

    if args.command == "add":
        try:
            job = job_store.add(
                args.task, args.model, use_memory=not args.no_memory,
                lead=args.lead, folder=args.folder, check=args.check, reference=args.reference,
                plan_first=args.plan,
            )
        except ValueError as error:
            raise SystemExit(str(error)) from error
        chosen_by = f"lead {job.lead}" if job.lead else job.router or "you"
        print(f"Added job {job.id}: {job.model}, effort {job.effort or '-'} (chosen by {chosen_by})")
    elif args.command == "ingest":
        try:
            job = ingest(args.path)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print(f"Added ingest job {job.id}: {job.model}, effort {job.effort or '-'}")
    elif args.command == "ingest-new":
        added = ingest_new()
        for job in added:
            print(f"Added ingest job {job.id} for {Path(job.source).relative_to(memory.RAW_DIR)}")
        print(f"{len(added)} new sources. Start them with 'jobs run'." if added else "No new sources.")
    elif args.command == "save":
        path = save_result(args.job_id, args.title, args.area)
        print(f"Saved {path.relative_to(memory.MEMORY_DIR)}")
    elif args.command == "list":
        print_list()
    elif args.command == "show":
        print(json.dumps(asdict(job_store.load(args.job_id)), indent=2))
    elif args.command == "run":
        job_run.run_queued(args.workers)
    elif args.command == "stop":
        job_run.stop(args.job_id)
    elif args.command == "recover":
        found = job_run.recover()
        for job in found:
            print(f"Interrupted: {job.id}  {job.task[:60]}")
        print(f"{len(found)} jobs interrupted. Continue one with 'jobs resume <id>'." if found
              else "No dead running jobs.")
    elif args.command == "resume":
        try:
            job = job_run.resume(args.job_id)
        except ValueError as error:
            print(error)
            sys.exit(1)
        print(f"Job {job.id} is queued again. Start it with 'jobs run'.")
    elif args.command == "plan":
        try:
            job, start = job_talk.decide_plan(args.job_id, args.decision, args.note)
        except ValueError as error:
            print(error)
            sys.exit(1)
        if start:
            print(f"Plan {job.plan['status']}. Job {job.id} is queued again. Start it with 'jobs run'.")
        else:
            print(f"Plan rejected. Job {job.id} is stopped (no agent call).")
            for held in job_run.release_held(job.id):
                print(f"Held job {held.id} of {held.lead} can run now: 'jobs run'.")
    elif args.command == "auto-approve":
        if args.usd is not None:
            try:
                job_store.set_auto_approve(args.usd)
            except ValueError as error:
                print(error)
                sys.exit(1)
        limit = job_store.settings()["auto_approve_plans_usd"]
        print(f"Plans with an estimate up to ${limit:.2f} need no OK." if limit > 0
              else "Every plan needs your OK (auto-approve is off).")
    elif args.command in ("approve", "reject", "undo"):
        try:
            if args.command == "approve":
                print(f"Approved job {job_review.approve(args.job_id, args.note).id}.")
            elif args.command == "reject":
                print(f"Rejected job {job_review.reject(args.job_id, args.note).id}.")
            else:
                print("\n".join(f"Undone: {path}" for path in job_review.undo(args.job_id, args.file)) or "Nothing to undo.")
            for lesson_job in job_review.queued_lessons(args.job_id):
                print(f"Lesson job {lesson_job.id} for {lesson_job.lead} is queued. Start it with 'jobs run'.")
        except ValueError as error:
            print(error)
            sys.exit(1)
    elif args.command in ("follow-up", "answer", "comment"):
        try:
            if args.command == "comment":
                job, start, warning = job_review.comment(args.job_id, args.text, dict(args.on))
                for lesson_job in job_review.queued_lessons(args.job_id):
                    print(f"Lesson job {lesson_job.id} for {lesson_job.lead} is queued too.")
            else:
                job, start, warning = job_talk.follow_up(args.job_id, args.message)
        except ValueError as error:
            print(error)
            sys.exit(1)
        if warning:
            print(f"Warning: {warning}")
        print(f"Job {job.id} is queued again. Start it with 'jobs run'." if start
              else f"Job {job.id} is {job.status}: the agent reads the message after its current call.")


if __name__ == "__main__":
    main()
