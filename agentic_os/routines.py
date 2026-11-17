"""Routines: jobs that start by themselves on a schedule, with a cost cap.

Each routine is a Markdown file in routines/. The front matter says when it
runs and what it does. It runs a skill, a built-in action, or its body is the task:

    ---
    description: One line about the routine
    schedule: daily 08:00          (or "weekly mon 08:00", "every 6h", "every 30m")
    mode: passive                  (passive: add a queued job; active: run it now)
    skill: summarize               (optional: run this skill ...)
    input: notes.md                (... with this input)
    action: ingest-new             (optional: a built-in action, see ACTIONS)
    max_jobs: 3                    (an action adds at most this many jobs per run)
    (for an action, the body is only a note for people)
    model: haiku                   (optional; for a skill, the skill's default)
    effort: low                    (optional)
    memory: false                  (a task routine: does the agent get the memory?)
    max_week_usd: 0.50             (cost cap: all runs in the last 7 days)
    ---
    The task, when there is no skill. Python fills these blanks when the job starts:
        {memory_dir}      the absolute path of the memory folder
        {memory_health}   the report of `memory health` (exact checks, no AI)
        {briefing_facts}  the facts for the morning briefing (see briefing.py)

The time of the last run is in data/routines.json. A new routine waits for
its first scheduled time. When many runs were missed (the computer was off),
the routine runs once, not once per missed time.

Something must call `tick` regularly: you, or cron, for example every 10 minutes:
    */10 * * * * cd <project> && .venv/bin/python -m agentic_os.routines tick

Usage (from the project folder):
    python -m agentic_os.routines list
    python -m agentic_os.routines tick         start the routines that are due
    python -m agentic_os.routines run <name>   start one routine now (the cost cap still counts)
"""

import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from agentic_os import backups, changes, jobs, memory, skills
from agentic_os.router import EFFORTS, MODELS

PROJECT_DIR = Path(__file__).resolve().parent.parent
ROUTINES_DIR = PROJECT_DIR / "routines"
STATE_FILE = PROJECT_DIR / "data" / "routines.json"
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")
DEFAULT_CAP = 0.50  # dollars per 7 days
DEFAULT_MAX_JOBS = 3  # jobs per run of an action
# Built-in actions: Python decides which jobs to add. Each takes the job limit and the
# fields for its jobs (the routine's mark, set in jobs.add: a second save could undo a claim).
def recos_action(limit):
    # Imported here: recos imports routines (a top import would be circular).
    from agentic_os import recos
    return recos.routine_action(limit)


ACTIONS = {
    "ingest-new": lambda limit, fields=None: jobs.ingest_new(limit, fields),  # one ingest job per new raw file
    "recos": lambda limit, fields=None: recos_action(limit),  # save new Recos cards (no jobs, no cost)
    "prune-snapshots": lambda limit, fields=None: changes.routine_action(limit),  # delete old file copies
    "backup": lambda limit, fields=None: backups.routine_action(limit),  # a compressed copy of data/
}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # the order of datetime.weekday()
MAX_EVERY = {"h": 24 * 366, "m": 60 * 24 * 366}  # at most a year
DST_SHIFT = timedelta(hours=2)  # a clock that goes back less than this is a summer-time change
SCHEDULE_PATTERNS = {
    "daily": re.compile(r"daily (\d\d):(\d\d)"),
    "weekly": re.compile(r"weekly (mon|tue|wed|thu|fri|sat|sun) (\d\d):(\d\d)"),
    "every": re.compile(r"every (\d+)(m|h)"),
}


def parse_schedule(text):
    """Turn a schedule text into a dict, or raise ValueError if it is wrong.

    "daily 08:00"      -> {"kind": "daily", "hour": 8, "minute": 0}
    "weekly mon 08:00" -> {"kind": "weekly", "day": 0, "hour": 8, "minute": 0}
    "every 6h"         -> {"kind": "every", "step": timedelta(hours=6)}
    """
    text = " ".join(text.split()).lower()
    for kind, pattern in SCHEDULE_PATTERNS.items():
        match = pattern.fullmatch(text)
        if match is None:
            continue
        if kind == "every":
            number, unit = int(match[1]), match[2]
            if not 1 <= number <= MAX_EVERY[unit]:  # "every 99999999999h" would overflow timedelta
                break
            step = timedelta(hours=number) if unit == "h" else timedelta(minutes=number)
            return {"kind": kind, "step": step}
        *day, hour, minute = match.groups()
        hour, minute = int(hour), int(minute)
        if hour > 23 or minute > 59:
            break
        schedule = {"kind": kind, "hour": hour, "minute": minute}
        if kind == "weekly":
            schedule["day"] = DAYS.index(day[0])
        return schedule
    raise ValueError(
        f'Unknown schedule "{text}". Use "daily 08:00", "weekly mon 08:00", "every 6h" or "every 30m".'
    )


def next_run(schedule, after):
    """Return the first scheduled time that is later than `after`."""
    if schedule["kind"] == "every":
        return after + schedule["step"]
    time = after.replace(hour=schedule["hour"], minute=schedule["minute"], second=0, microsecond=0)
    if schedule["kind"] == "daily":
        return time if time > after else time + timedelta(days=1)
    # weekly: go forward to the right day of the week.
    time += timedelta(days=(schedule["day"] - time.weekday()) % 7)
    return time if time > after else time + timedelta(days=7)


def is_due(schedule, last_run, now):
    """Return True if the routine must run now. last_run is when it ran (or was first seen)."""
    return next_run(schedule, last_run) <= now


def load(name):
    """Return one routine as a dict, or None if there is no such routine.

    Raise ValueError if the routine file has a mistake (a bad schedule, mode or cap).
    """
    if not NAME_PATTERN.fullmatch(name):
        return None
    path = ROUTINES_DIR / f"{name}.md"
    if not path.exists():
        return None
    meta, task = memory.read_note(path)
    routine = {
        "name": name,
        "description": meta.get("description", ""),
        "schedule_text": meta.get("schedule", ""),
        "schedule": parse_schedule(meta.get("schedule", "")),
        "mode": meta.get("mode", "passive"),
        "skill": meta.get("skill") or None,
        "input": meta.get("input", ""),
        "action": meta.get("action") or None,
        "max_jobs": int(meta.get("max_jobs", DEFAULT_MAX_JOBS)),
        "model": meta.get("model") or None,
        "effort": meta.get("effort") or None,
        "memory": meta.get("memory", "false").lower() == "true",
        "max_week_usd": float(meta.get("max_week_usd", DEFAULT_CAP)),
        "task": task,
    }
    if routine["mode"] not in ("passive", "active"):
        raise ValueError(f"{name}: mode must be passive or active, not {routine['mode']}")
    if routine["model"] not in (None, *MODELS) or routine["effort"] not in (None, *EFFORTS):
        raise ValueError(f"{name}: unknown model or effort: {routine['model']}, {routine['effort']}")
    if routine["action"] not in (None, *ACTIONS):
        raise ValueError(f"{name}: unknown action {routine['action']} (known: {', '.join(ACTIONS)})")
    if routine["skill"] is None and routine["action"] is None and not task:
        raise ValueError(f"{name}: a routine needs a skill, an action or a task (the text under the front matter)")
    return routine


def all_routines():
    """Return all routines, sorted by name. A routine with a mistake is skipped with a message."""
    found = []
    for path in sorted(ROUTINES_DIR.glob("*.md")):
        try:
            routine = load(path.stem)
        except ValueError as error:
            print(f"Skipped routine {path.name}: {error}")
            continue
        if routine is not None:
            found.append(routine)
    return found


def load_state():
    """Return the saved state: routine name -> {"last_run": "...", "last_job": "..."}."""
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text())


def save_state(state):
    """Write the state file atomically (a temporary file, then a rename)."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    memory.write_atomic(STATE_FILE, json.dumps(state, indent=2))


def state_lock():
    """The lock for the state file: the web loop, `routines run` and cron `tick` all write it."""
    return memory.file_lock(STATE_FILE.with_name(".routines.lock"))


def week_cost(name, now, all_jobs=None):
    """Return what the routine's jobs cost in the 7 days before now.

    all_jobs: the jobs, if the caller has them already (so the files are read once).
    """
    since = now - timedelta(days=7)
    if all_jobs is None:
        all_jobs = jobs.all_jobs()
    # By when the job ended (a job still running: when it was made). A passive routine's job
    # may wait a week in the queue: by created_at, its cost would never count.
    return sum(
        job.cost for job in all_jobs
        if job.routine == name and datetime.fromisoformat(job.finished_at or job.created_at) >= since
    )


def cap_reason(routine, now=None):
    """Return why the routine's weekly cost cap stops it, or None.

    Checked when a routine starts, and again when one of its jobs starts
    (jobs.run_job): a queued job costs $0 until it runs, so a passive routine's
    jobs that wait a week would all pass the first check.
    """
    spent = week_cost(routine["name"], now or datetime.now())
    if spent >= routine["max_week_usd"]:
        return f"${spent:.4f} in 7 days, the cap is ${routine['max_week_usd']:.2f}"
    return None


def job_cap_reason(name):
    """Return why the cap stops a job of the routine `name` now, or None (also for a routine that is gone)."""
    try:
        routine = load(name)
    except (ValueError, SystemExit):
        return None
    return cap_reason(routine) if routine is not None else None


BLANK = re.compile(r"\{(?:memory_health|briefing_facts|memory_dir)\}")


def render_task(task):
    """Fill the blanks in a routine's task. Exact work (the health report) is done here, in Python.

    We use replace(), not format(), like skills.render: other "{" in the task stay as they are.
    """
    if "{memory_health}" in task:
        task = task.replace("{memory_health}", memory.health_report())
    if "{briefing_facts}" in task:
        # Imported here, not at the top: briefing imports routines, so a top
        # import would be circular (each module would wait for the other).
        from agentic_os import briefing
        task = task.replace("{briefing_facts}", briefing.facts())
    return task.replace("{memory_dir}", str(memory.MEMORY_DIR))


def fresh_task(name):
    """Return the routine's task with its blanks filled now, or None.

    jobs.run_job calls it when a routine's job starts: a passive job can wait in the
    queue for days, and its {memory_health} or {briefing_facts} from that time is old.
    None: no such routine, or not a task routine with blanks (nothing to fill again).
    """
    try:
        routine = load(name)
    except ValueError:
        return None
    if routine is None or routine["action"] or routine["skill"] or not BLANK.search(routine["task"]):
        return None
    return render_task(routine["task"])


def start(routine, now, run=None):
    """Add the routine's jobs, and run them if the routine is active.

    Return the list of jobs (empty when an action finds nothing to do),
    or None when the cost cap stops the routine.

    run is the function that starts an active job. The default, jobs.run_job,
    waits until the job is done. The web server passes a function that gives
    the job to its thread pool, so its loop does not wait.

    The cost cap is checked first. The cost of a job is known only after it
    ran, so one run can go a little over the cap; the next run is then skipped.
    """
    reason = cap_reason(routine, now)
    if reason is not None:
        print(f"Skipped {routine['name']}: {reason}")
        return None
    mark = {"routine": routine["name"]}
    if routine["action"]:
        added = ACTIONS[routine["action"]](routine["max_jobs"], fields=mark)
    elif routine["skill"]:
        added = [skills.add(routine["skill"], routine["input"], routine["model"], routine["effort"], fields=mark)]
    else:
        added = [jobs.add(render_task(routine["task"]), routine["model"] or "haiku",
                          use_memory=routine["memory"], effort=routine["effort"], fields=mark)]
    # Every job is marked and saved before the first one runs (running one can take long).
    for job in added:
        if routine["mode"] == "active":
            (run or jobs.run_job)(job)
        else:
            print(f"Queued job {job.id} for {routine['name']} (passive: start it with 'jobs run')")
    return added


def record_run(name, now, added=None):
    """Remember when a routine ran (or was first seen), and its last job.

    It loads the state again under the lock and changes only this routine: a
    caller that holds an old copy (a routine that ran for a minute) would undo
    what others wrote in the meantime.
    """
    with state_lock():
        state = load_state()
        state[name] = {"last_run": now.isoformat(timespec="seconds"),
                       "last_job": added[-1].id if added else state.get(name, {}).get("last_job")}
        save_state(state)


def forget_gone():
    """Drop the state of routines whose file is gone. A new routine with the same name is
    then new (it waits for its first time); with the old state it would run at once."""
    with state_lock():
        state = load_state()
        gone = [name for name in state if not (ROUTINES_DIR / f"{name}.md").exists()]
        for name in gone:
            del state[name]
        if gone:
            save_state(state)


def tick(now=None, run=None):
    """Start every routine that is due. Return the names of the routines that started.

    run: see start().
    """
    now = now or datetime.now()
    forget_gone()
    state = load_state()
    started = []
    for routine in all_routines():
        name = routine["name"]
        if name not in state:
            # A new routine waits for its first scheduled time.
            record_run(name, now)
            continue
        last_run = datetime.fromisoformat(state[name]["last_run"])
        if last_run > now:
            if last_run - now <= DST_SHIFT:
                # The clocks went back (summer time ended): the same hour comes again. Wait
                # until it has passed; a new start from now would run the routine twice.
                continue
            # The clock was wrong once: start the schedule again from now. Otherwise
            # the routine would stay silent until that date.
            record_run(name, now)
            continue
        if not is_due(routine["schedule"], last_run, now):
            continue
        # Save the run before the job, so a crash or a slow job never starts it twice.
        record_run(name, now)
        try:
            added = start(routine, now, run)
        except (Exception, SystemExit) as error:  # SystemExit: for example, the routine's skill is gone
            # One broken routine must not stop the others in this tick (or the web server).
            print(f"Routine {name} failed: {error}")
            continue
        if added:
            record_run(name, now, added)
            started.append(name)
    return started


def run_now(name, now=None, run=None):
    """Start one routine now, without waiting for its schedule. Return what start() returns.

    Raise SystemExit if there is no such routine, ValueError if its file has a mistake.
    """
    now = now or datetime.now()
    routine = load(name)
    if routine is None:
        raise SystemExit(f"No routine named {name}. Use 'python -m agentic_os.routines list'.")
    added = start(routine, now, run)
    if added is not None:  # None: the cost cap skipped it, so the schedule stays as it was
        record_run(name, now, added)
    return added


def summary(now=None, all_jobs=None):
    """Return one dict per routine, for the list and the web page (JSON-safe, no timedelta).

    all_jobs: the jobs, if the caller has them already (so the files are read once).
    """
    now = now or datetime.now()
    state = load_state()
    if all_jobs is None:
        all_jobs = jobs.all_jobs()
    rows = []
    for routine in all_routines():
        saved = state.get(routine["name"], {})
        last = datetime.fromisoformat(saved["last_run"]) if saved.get("last_run") else now
        rows.append({
            "name": routine["name"],
            "description": routine["description"],
            "schedule": routine["schedule_text"],
            "mode": routine["mode"],
            "what": (f"action {routine['action']}" if routine["action"]
                     else f"skill {routine['skill']}" if routine["skill"] else "task"),
            "next_run": next_run(routine["schedule"], last).isoformat(timespec="minutes"),
            "last_job": saved.get("last_job"),
            "week_cost": week_cost(routine["name"], now, all_jobs),
            "max_week_usd": routine["max_week_usd"],
        })
    return rows


def print_list(now=None):
    """Print one line per routine: schedule, mode, next run, cost in 7 days and the cap."""
    for row in summary(now):
        print(
            f"{row['name']:<16} {row['schedule']:<17} {row['mode']:<7}"
            f" next {row['next_run'].replace('T', ' ')}  ${row['week_cost']:.4f}"
            f" / ${row['max_week_usd']:.2f} a week  {row['what']}  {row['description']}"
        )


def main():
    parser = argparse.ArgumentParser(description="Routines: scheduled jobs with a cost cap.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list the routines")
    commands.add_parser("tick", help="start the routines that are due")
    run_parser = commands.add_parser("run", help="start one routine now")
    run_parser.add_argument("name")
    args = parser.parse_args()

    if args.command == "list":
        print_list()
    elif args.command == "tick":
        started = tick()
        print(f"Started: {', '.join(started) or 'nothing is due'}")
    elif args.command == "run":
        try:
            run_now(args.name)
        except ValueError as error:
            raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
