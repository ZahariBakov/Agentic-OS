"""Job store: the Job record and its files in data/.

The lowest part of the jobs code (see jobs.py for the map of the parts). It owns:
- the `Job` dataclass and its JSON file in data/jobs/ (save, load, all_jobs, add, update)
- the claim lock, the stop flag file and "who owns a job now" (worker_busy, is_alive)
- the inbox: messages that no agent has read yet, one file per job
- the user's settings (data/settings.json)

It imports no other job_* module, so every one of them can import it.
"""

import json
import math
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from agentic_os import leads, memory, workspaces
from agentic_os.router import route


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
JOBS_DIR = DATA_DIR / "jobs"
REFERENCES_DIR = DATA_DIR / "references"  # references pasted on the web page
INBOX_DIR = DATA_DIR / "inbox"  # follow-up messages that no agent has read yet, one file per job
SETTINGS_FILE = DATA_DIR / "settings.json"  # the user's choices, for example the auto-approve amount

FINISHED = ("done", "failed", "stopped")  # a job in these states can be reviewed


@dataclass
class Job:
    id: str
    task: str
    model: str = "haiku"
    effort: str | None = None
    router: str | None = None  # who chose the model: rules, jev, haiku, or None (the user)
    status: str = "queued"  # queued, running, done, failed, stopped, interrupted
    created_at: str = ""
    finished_at: str | None = None  # when the last run ended (done, failed or stopped); old jobs: None
    result: str | None = None
    error: str | None = None
    cost: float = 0.0
    session_id: str | None = None
    pid: int | None = None  # the agent (claude) process, while it runs
    worker: int | None = None  # the Python process that runs the job (web server or `jobs run`)
    interrupted_why: str | None = None  # why the job was interrupted (the last time)
    resumes: int = 0  # how many times the user resumed the job
    resuming: bool = False  # the next run continues an interrupted call (set by resume)
    # The conversation after the task: {"from": "user" or "agent", "text", "at"}.
    # The last answer is not here: it is `result`.
    messages: list[dict] = field(default_factory=list)
    question: dict | None = None  # the agent's open question: {"text", "options", "at"}
    held_for: str | None = None  # a lead's job that waits until this other job's question is answered
    review: dict | None = None  # the user's last review: {"verdict": approved/changes/rejected, "note", "at"}
    signals: list[str] = field(default_factory=list)  # bad results (SIGNALS), each one once
    lesson_for: dict | None = None  # a lesson job: {"job": the bad job's id, "signal"}
    plan_first: bool = False  # the user asked for a plan before the work (--plan)
    # The agent's plan: {"text", "estimate" (USD or None), "status": pending/approved/changed/rejected,
    # "at", and after the decision "note", "auto" (why it needed no OK), "decided_at"}
    plan: dict | None = None
    use_memory: bool = True
    notes: list[str] = field(default_factory=list)  # notes that this job saved
    project_notes: list[str] = field(default_factory=list)  # <project-note> lines saved (project_memory.py)
    style_problems: list[str] = field(default_factory=list)  # banned phrases found
    lead: str | None = None  # the lead that does the job, or None (a one-time agent)
    folder: str | None = None  # where the agent works; None means DEFAULT_WORK_DIR
    check: str | None = None  # a command, for example "pytest -q"; exit code 0 = passed
    tries: int = 0  # agent calls so far
    check_output: str | None = None  # the end of the last check output
    skill: str | None = None  # the skill that made this job, or None
    # A skill with wildcards: {"count", "places", "kept", "problems"} (see wildcards.py)
    wildcards: dict | None = None
    routine: str | None = None  # the routine that made this job, or None
    source: str | None = None  # an ingest job: the raw file it reads
    reference: str | None = None  # a file with a good example answer, for the critic
    # An isolated git worktree where the agent edits (see workspaces.py), or None
    workspace: dict | None = None
    verdicts: list[dict] = field(default_factory=list)  # the critic's verdicts, one per round
    # A sub-job (specialists.py): the job that delegated it, its short title, why this model,
    # and its part of the budget (claude --max-budget-usd). It only reads; it cannot ask.
    parent: str | None = None
    title: str | None = None
    # A lead's job: the desk that runs it ("Pip", or a twin "Pip~2"; leads.py), and the lead
    # whose free desk a queued job waits for (every desk was busy).
    desk: str | None = None
    waits_for: str | None = None
    model_why: str | None = None
    max_budget_usd: float | None = None


def now():
    """Return the current time as text, for example 2026-10-30T10:15:30.123."""
    return datetime.now().isoformat(timespec="milliseconds")


def save(job):
    """Write the job to its JSON file.

    memory.write_atomic writes a temporary file (a unique name) and renames it.
    A rename is atomic, so a reader never sees a half-written file.
    """
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    memory.write_atomic(JOBS_DIR / f"{job.id}.json", json.dumps(asdict(job), indent=2))


def save_reference(text):
    """Save a reference text (a good example answer) to a new file. Return its path.

    The web form sends the reference as text, but a job keeps the path of a
    file (like `jobs add --reference <file>`), so both use the same code.
    """
    REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
    path = REFERENCES_DIR / f"{uuid.uuid4().hex[:8]}.md"
    memory.write_atomic(path, text)
    return path


def exists(job_id):
    """Return True if a job with this id exists."""
    return (JOBS_DIR / f"{job_id}.json").exists()


def load(job_id):
    """Read one job from its JSON file."""
    path = JOBS_DIR / f"{job_id}.json"
    return Job(**json.loads(path.read_text()))


def all_jobs():
    """Return all jobs, oldest first. A file that cannot be read (empty after a crash, a
    hand edit) is skipped with a message: one bad file must not hide every job."""
    jobs = []
    for path in JOBS_DIR.glob("*.json"):
        try:
            jobs.append(Job(**json.loads(path.read_text())))
        except FileNotFoundError:
            continue  # removed while we listed the folder
        except (ValueError, TypeError) as error:
            print(f"Skipped the broken job file {path.name}: {error}")
    return sorted(jobs, key=lambda job: job.created_at)


def new_id():
    """Return a job id that no job file has yet (8 hex characters can repeat)."""
    while True:
        job_id = uuid.uuid4().hex[:8]
        if not exists(job_id):
            return job_id


def add(task, model=None, use_memory=True, effort=None, lead=None, folder=None, check=None, skill=None,
        reference=None, plan_first=False, fields=None):
    """Create a new queued job and save it.

    With lead="auto", the dispatcher chooses a lead. A lead brings its own
    model, effort and folder. Without a lead or a model, the router chooses.
    fields: more Job fields (for example wildcards, lesson_for, routine). They are
    set before the one save: a second save later could come after another worker
    has claimed the job, and would write "queued" over "running".
    Raise ValueError for wrong input (the web server answers 400; SystemExit would stop it).
    """
    job = Job(id=new_id(), task=task, created_at=now(), use_memory=use_memory)
    job.check = check
    job.skill = skill
    job.plan_first = plan_first
    if reference is not None:
        if not Path(reference).is_file():
            raise ValueError(f"No such reference file: {reference}")
        job.reference = str(Path(reference).resolve())
    if folder is not None:
        if not Path(folder).is_dir():
            raise ValueError(f"No such folder: {folder}")
        job.folder = str(Path(folder).resolve())
    if lead is not None:
        if lead == "auto":
            lead, why = leads.dispatch(task)
            print(f"Dispatcher chose {lead} ({why})")
        role = leads.roster().get(lead)
        if role is None:
            raise ValueError(f"No lead named {lead}. Use 'python -m agentic_os.leads list'.")
        # A session belongs to one folder, so a lead always works in its own folder.
        if folder is not None:
            raise ValueError("A lead works in its own folder (see roster.json), so --folder is not allowed.")
        job.lead = lead
        job.model = role["model"]
        job.effort = role["effort"]
        job.folder = str(leads.folder(lead))
        job.use_memory = True
    elif model is None:
        decision = route(task)
        job.model = decision["model"]
        job.effort = decision["effort"]
        job.router = decision["router"]
    else:
        job.model = model
        job.effort = effort
    for name, value in (fields or {}).items():
        if not hasattr(job, name):
            raise ValueError(f"A job has no field {name}")
        setattr(job, name, value)
    save(job)
    return job


def update(job_id, **fields):
    """Change some fields of a saved job under the claim lock. Return the job.

    For a caller that holds an old Job object: it loads the file again, so a
    status that a worker wrote in the meantime stays.
    """
    with claim_lock():
        job = load(job_id)
        for name, value in fields.items():
            setattr(job, name, value)
        save(job)
        return job


def usd(value):
    """Return an amount as text: 2 decimals, but 4 below one cent (haiku jobs cost less)."""
    return f"${value:.2f}" if value >= 0.01 else f"${value:.4f}"


def settings():
    """Return the user's settings (data/settings.json), with the defaults."""
    values = {"auto_approve_plans_usd": 0.0,  # 0: every plan needs the user's OK
              "isolate_jobs": workspaces.DEFAULT_MODE,  # always, shared or off (see workspaces.py)
              "commands_mode": "ask",  # ask or off: may agents run shell commands (commands.py)
              "commands_allow": [],  # commands that run without asking (commands.allowed_by)
              "lead_twins": 1}  # second desks per lead when it is busy (leads.py); 0: none
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
    except FileNotFoundError:
        saved = {}
    except ValueError as error:
        # A hand-broken file: the defaults, so jobs still run and set_setting can write a good file.
        print(f"{SETTINGS_FILE} is not valid JSON ({error}): the defaults are used")
        saved = {}
    if isinstance(saved, dict):
        values.update(saved)
    limit = values["auto_approve_plans_usd"]
    if not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit < 0:
        values["auto_approve_plans_usd"] = 0.0  # Infinity or text in the file: every plan needs the OK
    if values["isolate_jobs"] not in workspaces.MODES:
        values["isolate_jobs"] = workspaces.DEFAULT_MODE
    if values["commands_mode"] not in ("ask", "off"):
        values["commands_mode"] = "ask"
    if not isinstance(values["commands_allow"], list):
        values["commands_allow"] = []
    values["commands_allow"] = [entry for entry in values["commands_allow"] if isinstance(entry, str)]
    if values["lead_twins"] not in TWIN_CHOICES:
        values["lead_twins"] = 1
    return values


TWIN_CHOICES = (0, 1, 2)


def set_setting(name, value):
    """Save one setting in data/settings.json."""
    if name == "isolate_jobs" and value not in workspaces.MODES:
        raise ValueError(f"isolate_jobs is one of: {', '.join(workspaces.MODES)}")
    if name == "lead_twins" and value not in TWIN_CHOICES:
        raise ValueError(f"lead_twins is one of {TWIN_CHOICES}")
    if name == "commands_mode" and value not in ("ask", "off"):
        raise ValueError("commands_mode is ask or off")
    if name == "commands_allow" and not (isinstance(value, list) and all(isinstance(entry, str) for entry in value)):
        raise ValueError("commands_allow is a list of commands")
    values = settings()
    values[name] = value
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    memory.write_atomic(SETTINGS_FILE, json.dumps(values, indent=2))


def set_auto_approve(usd):
    """Plans with an estimate up to `usd` need no OK (0 turns it off)."""
    if not math.isfinite(usd) or usd < 0:
        raise ValueError("The amount must be a number of dollars, 0 or more.")
    set_setting("auto_approve_plans_usd", usd)


def write_inbox(job_id, message):
    """Add a message to the job's inbox. The caller holds the claim lock."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    with inbox_file(job_id).open("a") as inbox:
        inbox.write(json.dumps(message) + "\n")


def claim_lock():
    """The lock for every change of a job's state and its inbox (web server, `jobs run`, threads)."""
    return memory.file_lock(JOBS_DIR / ".claim.lock")


def stop_file(job_id):
    """The file that says "the user stopped this run".

    A stop is its own file, not only the status in the job's file: the worker
    keeps a Job object in memory and saves it (status "running") after the
    critic or a follow-up, and that save would undo a stop written into the
    job's file in the meantime. The flag stays until the next run (claim).
    """
    return JOBS_DIR / f"{job_id}.stop"


def is_stopped(job):
    """Return True if someone stopped the job's run."""
    return stop_file(job.id).exists() or load(job.id).status == "stopped"


def worker_busy(job):
    """True while a live worker still owns the job's run.

    The rule: while a worker owns a job, only the worker writes the job's file
    (it saves its whole Job object at the end, and would undo other changes).
    A stop and a message go to their own files (stop_file, the inbox); other
    actions (review, undo, apply, plan decisions, ...) wait until the run ends.
    A dead worker (a crash) owns nothing, so nothing waits for it forever.
    """
    return job.worker is not None and is_alive(job.worker, saved_at(job.id))


def busy_error(job_id):
    return ValueError(f"Job {job_id} is still ending its run; try again in a moment.")


def is_alive(pid, saved_at=None):
    """Return True if a process with this id exists.

    saved_at: when the job file that holds the pid was saved. The real process started
    before that save, so a process that started later is another one that got the same
    pid (after a reboot, or pids came round again): it counts as dead.
    """
    try:
        os.kill(pid, 0)  # signal 0 sends nothing; it only checks that the process exists
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # it exists, but belongs to another user
    if saved_at is not None:
        started = process_started(pid)
        if started is not None and started > saved_at + 2:  # 2 s: the clocks are not exact
            return False
    return True


def process_started(pid):
    """Return when a process started (Unix time), or None when we cannot tell (not Linux)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        ticks = int(stat.rsplit(")", 1)[1].split()[19])  # field 22: start time, in clock ticks after boot
        boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                    if line.startswith("btime "))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def saved_at(job_id):
    """Return when the job's file was last saved, or None."""
    try:
        return (JOBS_DIR / f"{job_id}.json").stat().st_mtime
    except OSError:
        return None


def inbox_file(job_id):
    return INBOX_DIR / f"{job_id}.jsonl"


def read_inbox(job_id):
    """Return the job's unread messages, and leave them in the inbox (for the web page)."""
    path = inbox_file(job_id)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def take_inbox(job_id):
    """Return the job's unread messages and empty its inbox.

    The inbox is its own file, not a field in the job's file: the worker keeps a
    Job object in memory and saves it, and that save would delete a message that
    someone else wrote into the job's file in the meantime.
    """
    with claim_lock():
        path = inbox_file(job_id)
        messages = read_inbox(job_id)
        path.unlink(missing_ok=True)
        return messages


def finished_job(job_id):
    """Load a job that can be reviewed (FINISHED), or raise ValueError. The caller holds the claim lock."""
    job = load(job_id)
    if worker_busy(job):  # stopped, but the worker has not saved its end yet
        raise busy_error(job_id)
    if job.status not in FINISHED:
        raise ValueError(f"Job {job_id} is {job.status}: you can review it when it has finished.")
    return job
