"""Leads: long-running agents. Each lead has a name, an area and its own session.

roster.json lists the leads. A lead keeps its session between jobs (--resume),
so it remembers the earlier jobs. Each lead has a folder data/leads/<name>/:

    state.json   the session id, its folder, the last use, the context size, replies, cost
    prompt.md    the system prompt of the current session
    handoff.md   a short summary for the next session (written after every job)
    lessons.json the lessons: "drafts" wait for the user, "kept" go into every fresh session's prompt

A lead gets a fresh session when its context is too long, or when the session
is cold and big. The fresh session reads the handoff.

Desks and twins: a lead's own session is its desk "Pip".
When that desk is busy, a new job may go to a twin desk "Pip~2": the same role, lessons and
memory, its own session in data/leads/Pip~2/, started fresh from the lead's handoff. A twin
only reads, unless the job has its own isolated workspace (then it may edit there). A twin
idle for TWIN_IDLE starts fresh again ("retires"). `take_desk` takes a free desk without
waiting; jobs.run_job puts the job back in the queue when every desk is busy.

Usage (from the project folder):
    python -m agentic_os.leads list
    python -m agentic_os.leads dispatch "your task"
    python -m agentic_os.leads reset <name>
    python -m agentic_os.leads lessons [<name>]          the drafts that wait for you, and the kept lessons
    python -m agentic_os.leads keep <name> <draft id> [--text "edited line"]
    python -m agentic_os.leads drop <name> <draft id>
"""

import argparse
import fcntl
import json
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from agentic_os import memory, secret_scan
from agentic_os.ask import ask
from agentic_os.router import ROUTER_DIR

PROJECT_DIR = Path(__file__).resolve().parent.parent
ROSTER_FILE = PROJECT_DIR / "roster.json"
LEADS_DIR = PROJECT_DIR / "data" / "leads"

# A long context costs more on every message, and the model remembers worse.
MAX_CONTEXT = 100_000  # tokens
# Claude Code's prompt cache lives about 1 hour. After that the session is "cold":
# resuming it pays again for the whole context, at the price of a cache write.
CACHE_TTL = 3600  # seconds
# A cold session smaller than this is still cheap to resume. (A fresh session
# starts with about 20k tokens anyway: Claude Code's own prompt and tools.)
COLD_LIMIT = 40_000  # tokens

HANDOFF_PATTERN = re.compile(r"<handoff>(.*?)</handoff>", re.DOTALL)
LESSON_PATTERN = re.compile(r"<lesson>(.*?)</lesson>", re.DOTALL)
MAX_LESSONS = 8  # kept lessons per lead; the oldest one drops off
MAX_LESSON_CHARS = 220  # one short line
MAX_DRAFTS = 20  # drafts per lead; the oldest one drops off
TWIN_MARK = "~"  # "Pip~2": the second desk of Pip
TWIN_IDLE = 1200  # seconds; a twin idle longer starts a fresh session

LEAD_PROMPT = """# Your role
You are {name}, the {area} lead in Agentic OS. {instructions}

# Handoff from your last session
{handoff}

# Lessons from earlier jobs (the user kept them; apply them to similar jobs)
{lessons}

# Writing a handoff
At the end of every answer, write a short handoff for your next session:

<handoff>
Decisions: ...
Open tasks: ...
Important details: ...
</handoff>

Keep it under 15 lines. Your next session sees only this handoff, not this conversation.
"""

TWIN_NOTE = """
# You are a second desk
{lead} is busy with another job, so you take this one. You have the same role, but this
session is new: the handoff above is from {lead}'s last session.
"""

DISPATCH_PROMPT = (
    "You are a dispatcher. Choose the lead whose area fits the task best. "
    'Reply only with JSON: {"lead": "...", "why": "<one short sentence>"}'
)


def roster():
    """Return the leads from roster.json as a dict: name -> role."""
    return json.loads(ROSTER_FILE.read_text())


def lead_dir(name):
    return LEADS_DIR / name


def base(desk):
    """Return the lead of a desk: "Pip~2" -> "Pip", "Pip" -> "Pip"."""
    return desk.split(TWIN_MARK)[0]


def is_twin(desk):
    return bool(desk) and TWIN_MARK in desk


def desks(name, twins):
    """Return the desks of a lead, its own first: ["Pip", "Pip~2", ...] with `twins` twins."""
    return [name] + [f"{name}{TWIN_MARK}{number}" for number in range(2, 2 + max(twins, 0))]


def take_desk(candidates):
    """Take the first free desk of the list without waiting. Return (desk, held lock) or (None, None).

    The lock is the same file lock as lock(desk); give the held lock to release_desk when done.
    """
    for desk in candidates:
        path = lead_dir(desk) / "lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        file = path.open("a")
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file.close()  # busy: try the next desk
            continue
        return desk, file
    return None, None


def release_desk(held):
    """Free a desk taken with take_desk."""
    if held is not None:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def folder(name):
    """Return the work folder of a lead, and create it if needed.

    A folder in roster.json can start with "~" (the home folder), or be relative
    to the project folder. Claude Code reads CLAUDE.md in the work folder and in
    all its parent folders, so a lead that must not see our CLAUDE.md needs a
    folder outside the project.
    """
    path = Path(roster()[base(name)]["folder"]).expanduser()
    path = (PROJECT_DIR / path).resolve()  # an absolute path stays the same
    path.mkdir(parents=True, exist_ok=True)
    return path


def lock(name):
    """Return the lock of a lead, for a `with` block. A lead does one job at a time (one session).

    It is a file lock, so it also works when the web server and `jobs run`
    run at the same time (two processes).
    """
    return memory.file_lock(lead_dir(name) / "lock")


def load_state(name):
    """Return the state of a lead, or a new state."""
    path = lead_dir(name) / "state.json"
    if not path.exists():
        return {"session_id": None, "last_used": 0, "context_tokens": 0, "replies": 0, "cost": 0.0}
    return json.loads(path.read_text())


def save_state(name, state):
    lead_dir(name).mkdir(parents=True, exist_ok=True)
    memory.write_atomic(lead_dir(name) / "state.json", json.dumps(state, indent=2))


def handoff(name):
    """Return the last handoff of a lead, or an empty string."""
    path = lead_dir(name) / "handoff.md"
    return path.read_text().strip() if path.exists() else ""


def fresh_reason(state, work_folder, twin=False):
    """Return why the lead needs a fresh session, or None to resume the old one."""
    if state["session_id"] is None:
        return "no session yet"
    if twin and time.time() - state["last_used"] > TWIN_IDLE:
        return f"the twin was idle over {TWIN_IDLE // 60} minutes (it retired)"
    # Claude Code saves sessions per folder, so it cannot resume from another folder.
    if state.get("folder") != str(work_folder):
        return "new work folder"
    if state["context_tokens"] > MAX_CONTEXT:
        return f"context too long ({state['context_tokens']} tokens)"
    idle = time.time() - state["last_used"]
    if idle > CACHE_TTL and state["context_tokens"] > COLD_LIMIT:
        return f"cold session ({idle / 3600:.1f} h idle, {state['context_tokens']} tokens)"
    return None


def prepare(name):
    """Get a lead's desk ready for a job. Return (session id or None, prompt file, why).

    name: a desk, "Pip" or a twin "Pip~2" (a twin starts from the lead's handoff and lessons).
    For a fresh session we write a new prompt file. For a resumed session we
    use the same file again: the prompt cache works only when the start of the
    context is exactly the same as before.
    """
    lead = base(name)
    role = roster()[lead]
    state = load_state(name)
    prompt_file = lead_dir(name) / "prompt.md"
    reason = fresh_reason(state, folder(name), twin=is_twin(name))
    if reason is None and prompt_file.exists():
        idle = time.time() - state["last_used"]
        return state["session_id"], prompt_file, f"resume session ({idle / 60:.0f} min idle)"
    lead_prompt = LEAD_PROMPT.format(
        name=lead,
        area=role["area"],
        instructions=role["instructions"],
        # A twin has no history of its own: it starts where the lead's last session ended.
        handoff=handoff(lead if is_twin(name) else name) or "None yet: this is your first session.",
        lessons="\n".join(f"- {lesson['text']}" for lesson in load_lessons(lead)["kept"]) or "None yet.",
    )
    if is_twin(name):
        lead_prompt += TWIN_NOTE.format(lead=lead)
    lead_dir(name).mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(lead_prompt + "\n" + memory.memory_prompt())
    return None, prompt_file, f"fresh session: {reason or 'no prompt file'}"


def context_tokens(reply):
    """Return the context size after a reply: all tokens of the last model call."""
    last = reply["usage"]["iterations"][-1]
    return (
        last["input_tokens"]
        + last["cache_read_input_tokens"]
        + last["cache_creation_input_tokens"]
        + last["output_tokens"]
    )


def finish(name, reply):
    """Save the lead's state and handoff after a reply. Return the answer without the handoff.

    We call this after every reply, not only at the end of a job,
    because a session can die at any moment.
    """
    state = load_state(name)
    state["session_id"] = reply["session_id"]
    state["folder"] = str(folder(name))
    state["last_used"] = time.time()
    state["context_tokens"] = context_tokens(reply)
    state["replies"] += 1
    state["cost"] += reply["total_cost_usd"]
    save_state(name, state)
    # A <handoff> inside Markdown code is an example (Ada works on this very code), not the handoff.
    found, rest = memory.blocks_outside_code(HANDOFF_PATTERN, reply["result"])
    if found:
        handoff = found[-1].group(1)
        what = secret_scan.find_secret(handoff)
        if what is None:
            memory.write_atomic(lead_dir(name) / "handoff.md", handoff.strip() + "\n")
        else:  # the old handoff stays
            print(secret_scan.refusal(what, f"the handoff of {name}"))
    return rest.strip()


def dispatch_rules(task):
    """Choose a lead by keywords. Return (name, why), or None if no word matches."""
    words = set(re.findall(r"[a-z]+", task.lower()))
    for name, role in roster().items():
        found = [word for word in role["keywords"] if word in words]
        if found:
            return name, f"rules, keyword: {found[0]}"
    return None


def dispatch_haiku(task):
    """Ask Claude Haiku which lead fits the task. Return (name, why)."""
    leads = roster()
    areas = "\n".join(f"- {name}: {role['area']}. {role['instructions']}" for name, role in leads.items())
    schema = {
        "type": "object",
        "properties": {"lead": {"type": "string", "enum": list(leads)}, "why": {"type": "string"}},
        "required": ["lead", "why"],
    }
    ROUTER_DIR.mkdir(parents=True, exist_ok=True)
    reply = ask(
        f"Leads:\n{areas}\n\nTask: {task}",
        model="haiku",
        effort="low",
        extra_args=[
            "--tools", "",
            "--system-prompt", DISPATCH_PROMPT,
            "--json-schema", json.dumps(schema),
        ],
        cwd=ROUTER_DIR,
    )
    choice = reply["structured_output"]
    return choice["lead"], f"haiku: {choice['why']}"


def dispatch(task):
    """Choose the lead for a task: rules first, then Haiku, then the first lead."""
    if not roster():
        raise ValueError("roster.json has no leads: add one, or start the job without a lead.")
    found = dispatch_rules(task)
    if found is not None:
        return found
    try:
        return dispatch_haiku(task)
    except Exception as error:
        print(f"Dispatcher haiku failed: {error}", file=sys.stderr)
        return next(iter(roster())), "default: the first lead"


def lessons_file(name):
    return lead_dir(name) / "lessons.json"


def load_lessons(name):
    """Return the lead's lessons: {"kept": [...], "drafts": [...]}, oldest first."""
    path = lessons_file(name)
    return json.loads(path.read_text()) if path.exists() else {"kept": [], "drafts": []}


def save_lessons(name, lessons):
    lead_dir(name).mkdir(parents=True, exist_ok=True)
    memory.write_atomic(lessons_file(name), json.dumps(lessons, indent=2))


def check_line(text):
    """Return the lesson as one clean line, or raise ValueError."""
    line = " ".join((text or "").split())
    if not line:
        raise ValueError("The lesson is empty.")
    if len(line) > MAX_LESSON_CHARS:
        raise ValueError(f"The lesson is too long ({len(line)} characters, at most {MAX_LESSON_CHARS}).")
    secret_scan.check(line, "the lesson")
    return line


def extract_lesson(text):
    """Find the last <lesson> block in an answer. Return (line, reason) or None.

    The first line of the block is the lesson; a line that starts with "Why:" is
    the reasoning (for the user, not for the prompt). A bad line raises ValueError.
    """
    blocks = memory.blocks_outside_code(LESSON_PATTERN, text)[0]  # a block inside code is an example
    if not blocks:
        return None
    lines = [line.strip() for line in blocks[-1].group(1).strip().splitlines() if line.strip()]
    reason = " ".join(line[4:].strip() for line in lines if line.lower().startswith("why:"))
    text_lines = [line for line in lines if not line.lower().startswith("why:")]
    return check_line(text_lines[0] if text_lines else ""), reason


def same_text(a, b):
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def add_draft(name, line, reason, job_id, trigger):
    """Save a lesson draft. It waits for the user. Return the draft, or None if the lead has it already."""
    with memory.file_lock(lead_dir(name) / "lessons.lock"):
        lessons = load_lessons(name)
        if any(same_text(kept["text"], line) for kept in lessons["kept"]):
            return None
        draft = {"id": uuid.uuid4().hex[:8], "text": line, "reason": reason, "job": job_id,
                 "trigger": trigger, "at": datetime.now().isoformat(timespec="seconds")}
        lessons["drafts"] = [d for d in lessons["drafts"] if not same_text(d["text"], line)] + [draft]
        lessons["drafts"] = lessons["drafts"][-MAX_DRAFTS:]
        save_lessons(name, lessons)
        return draft


def keep_lesson(name, draft_id, text=None):
    """Keep a draft (with the user's own words, if given). Return the kept lesson.

    At most MAX_LESSONS stay; the oldest drops off. A kept lesson reaches the
    lead's prompt in its next fresh session (a resumed session keeps its prompt,
    for the prompt cache).
    """
    with memory.file_lock(lead_dir(name) / "lessons.lock"):
        lessons = load_lessons(name)
        draft = next((d for d in lessons["drafts"] if d["id"] == draft_id), None)
        if draft is None:
            raise ValueError(f"No lesson draft {draft_id} for {name}.")
        lesson = {**draft, "text": check_line(text if text is not None else draft["text"]),
                  "edited": text is not None and not same_text(text, draft["text"])}
        lessons["drafts"].remove(draft)
        lessons["kept"] = [k for k in lessons["kept"] if not same_text(k["text"], lesson["text"])] + [lesson]
        lessons["kept"] = lessons["kept"][-MAX_LESSONS:]
        save_lessons(name, lessons)
        return lesson


def drop_lesson(name, draft_id):
    """Drop a draft: it never reaches the prompt."""
    with memory.file_lock(lead_dir(name) / "lessons.lock"):
        lessons = load_lessons(name)
        before = len(lessons["drafts"])
        lessons["drafts"] = [d for d in lessons["drafts"] if d["id"] != draft_id]
        if len(lessons["drafts"]) == before:
            raise ValueError(f"No lesson draft {draft_id} for {name}.")
        save_lessons(name, lessons)


def print_lessons(names):
    for name in names:
        lessons = load_lessons(name)
        if not lessons["drafts"] and not lessons["kept"]:
            continue
        print(f"{name}:")
        for draft in lessons["drafts"]:
            print(f"  draft {draft['id']}  {draft['text']}")
            print(f"        why: {draft['reason'] or '-'}  (job {draft['job']}, {draft['trigger']})")
        for lesson in lessons["kept"]:
            print(f"  kept   {lesson['text']}")


def print_list():
    """Print one line per lead."""
    for name, role in roster().items():
        state = load_state(name)
        session = (state["session_id"] or "-")[:8]
        print(
            f"{name:<6} {role['area']:<12} {role['model']:<6} session {session:<8}"
            f"  {state['context_tokens']:>7} tokens  {state['replies']:>3} replies  ${state['cost']:.4f}"
        )


def main():
    parser = argparse.ArgumentParser(description="Manage the leads.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list the leads and their sessions")
    dispatch_parser = commands.add_parser("dispatch", help="show which lead gets a task")
    dispatch_parser.add_argument("task")
    reset_parser = commands.add_parser("reset", help="start a fresh session next time")
    reset_parser.add_argument("name")
    lessons_parser = commands.add_parser("lessons", help="the lesson drafts that wait for you, and the kept lessons")
    lessons_parser.add_argument("name", nargs="?")
    keep_parser = commands.add_parser("keep", help="keep a lesson draft")
    keep_parser.add_argument("name")
    keep_parser.add_argument("draft_id")
    keep_parser.add_argument("--text", help="your own words for the lesson")
    drop_parser = commands.add_parser("drop", help="drop a lesson draft")
    drop_parser.add_argument("name")
    drop_parser.add_argument("draft_id")
    args = parser.parse_args()
    if getattr(args, "name", None) and args.name not in roster():
        raise SystemExit(f"No lead named {args.name}. Use 'list' to see the names.")

    if args.command == "list":
        print_list()
    elif args.command == "dispatch":
        name, why = dispatch(args.task)
        print(f"{name} ({why})")
    elif args.command == "lessons":
        print_lessons([args.name] if args.name else list(roster()))
    elif args.command in ("keep", "drop"):
        try:
            if args.command == "keep":
                lesson = keep_lesson(args.name, args.draft_id, args.text)
                print(f"Kept: {lesson['text']}\n{args.name} gets it in its next fresh session.")
            else:
                drop_lesson(args.name, args.draft_id)
                print(f"Dropped draft {args.draft_id}.")
        except ValueError as error:
            raise SystemExit(str(error)) from error
    elif args.command == "reset":
        # Under the lead's lock: a running job's finish() would write its session back.
        # So the reset waits until the lead's current job ends.
        with lock(args.name):
            state = load_state(args.name)
            state["session_id"] = None
            save_state(args.name, state)
        print(f"{args.name} starts a fresh session next time (the handoff stays).")


if __name__ == "__main__":
    main()
