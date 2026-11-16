"""Project memory: what Agent Office knows about each project, kept per project and given per job.

memory/ is
about the user and the world; a lead's lessons are about the lead. This is
about one project (a folder): how the user wants work done there, what was
decided, what broke, and what earlier jobs did there.

Four kinds of entries:
    instruction  how the user wants work done here ("use pytest", "never touch legacy/")  always in the brief
    decision     a design choice and why                                                 when relevant
    failure      a known failure and its cause or workaround                             when relevant
    outcome      one line per finished job, written by Python (status, check, cost)       when relevant

A job's prompt gets every instruction and only the few other entries most
relevant to its task: shared words, rarer words count more, decisions and
failures a bit more, newer entries win a tie. Not the whole history.

The project of a job is the git repository of its folder, or the folder itself
when it is not in git. Jobs without a folder, and the agents' own folders in
~/.cache/agentic-os, have no project.

Agents never write the file. The user adds instructions (web page or CLI); an
agent ends its answer with <project-note kind="decision|failure">...</project-note>
and Python checks the text (length, secrets) and saves it.

data/projects/<name>-<hash>.json = {"project": path, "name", "entries": [{"id", "at", "kind", "text", "by", "job"}]}

Usage (from the project folder):
    python -m agentic_os.project_memory list                              the projects
    python -m agentic_os.project_memory show <folder>                     the entries, newest first
    python -m agentic_os.project_memory add <folder> "text" [--kind instruction|decision|failure]
    python -m agentic_os.project_memory remove <folder> <entry id>
    python -m agentic_os.project_memory brief <folder> "a task"           what an agent would get
"""

import argparse
import hashlib
import json
import math
import re
import uuid
from datetime import datetime
from pathlib import Path

from agentic_os import memory, secret_scan

PROJECT_DIR = Path(__file__).resolve().parent.parent
PROJECTS_DIR = PROJECT_DIR / "data" / "projects"
AGENT_FOLDERS = Path.home() / ".cache" / "agentic-os"  # the agents' own folders: not projects
KINDS = ("instruction", "decision", "failure", "outcome")
AGENT_KINDS = ("decision", "failure")  # what an agent may add
USER_KINDS = ("instruction", "decision", "failure")  # what the user may add
MAX_ENTRIES = 300
MAX_OUTCOMES = 120
MIN_TEXT = 8
MAX_TEXT = 600
MAX_INSTRUCTIONS = 12  # the newest ones go into every brief
MAX_RELATED = 6
MIN_SCORE = 1.0  # one shared rare word is enough; one shared common word is not
BRIEF_CHARS = 1800
LABEL = {"instruction": "the user's instruction", "decision": "decision", "failure": "known failure",
         "outcome": "past job"}
STOP_WORDS = set("""the and for with are was were this that from not can will should would could have has had
    our your their its into about than then but just only also very more most some any all you use one two
    please make add new file files job""".split())
WORD = re.compile(r"[a-z0-9](?:[a-z0-9_./-]*[a-z0-9])?")  # "legacy/" -> legacy, ".env" -> env
NOTE_PATTERN = re.compile(r'<project-note(?:\s+kind="(\w+)")?\s*>(.*?)</project-note>', re.DOTALL)
RULES = """

[Project memory: this job works on the project {name}. If you made a design decision there,
or found a failure and its cause, add one line for later jobs at the end of your answer:
<project-note kind="decision">Use SQLite: one user, no server.</project-note>
<project-note kind="failure">pytest fails without .env: copy .env.example first.</project-note>
Only what a later job needs. Never secrets.]"""


def project_of(folder):
    """Return the project folder of a work folder (its git repository, or itself), or None."""
    if not folder:
        return None
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir() or folder.is_relative_to(AGENT_FOLDERS.resolve()):
        return None
    # Imported here: workspaces is the git helper, and a plain function keeps this module light.
    from agentic_os import workspaces
    return workspaces.repo_of(folder) or folder


def folder_problem(folder):
    """Return why a folder has no project memory (one clear reason), or None when it has one."""
    if not folder:
        return "No folder given."
    path = Path(folder).expanduser().resolve()
    if not path.exists():
        return f"The folder {path} does not exist. Create it first (mkdir -p {path})."
    if not path.is_dir():
        return f"{path} is a file, not a folder."
    if path.is_relative_to(AGENT_FOLDERS.resolve()):
        return f"{path} is an agent's own folder (in {AGENT_FOLDERS}), not a project."
    return None


def file_of(project):
    """Return the memory file of a project: its folder name plus a short hash of the full path."""
    name = re.sub(r"[^a-z0-9]+", "-", Path(project).name.lower()).strip("-")[:40] or "project"
    digest = hashlib.sha1(str(project).encode()).hexdigest()[:6]
    return PROJECTS_DIR / f"{name}-{digest}.json"


def lock():
    return memory.file_lock(PROJECTS_DIR / ".lock")


def load(project):
    """Return the memory of a project: {"project", "name", "entries"} (empty when there is none)."""
    path = file_of(project)
    if path.exists():
        return json.loads(path.read_text())
    return {"project": str(project), "name": Path(project).name, "entries": []}


def save(data):
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    memory.write_atomic(file_of(data["project"]), json.dumps(data, indent=1))


def words(text):
    """Return the words of a text that can tell entries apart (lower case, 3+ letters, no stop words)."""
    return [word for word in WORD.findall((text or "").lower()) if len(word) > 2 and word not in STOP_WORDS]


def tidy(text):
    """Return the text as one line."""
    return " ".join((text or "").split())


def check_text(text):
    """Raise ValueError when the text cannot be an entry. Return the tidy text."""
    line = tidy(text)
    if len(line) < MIN_TEXT:
        raise ValueError("Too short to be useful.")
    if len(line) > MAX_TEXT:
        raise ValueError(f"Too long ({len(line)} characters); keep it under {MAX_TEXT}.")
    secret_scan.check(line, "the entry")
    return line


def add(project, kind, text, by="user", job=None, now=None):
    """Add an entry to a project's memory. Return it.

    The same words of the same kind replace the old entry (a newer time). An
    outcome replaces only the old outcome of the same job (a job that ran
    again): two jobs with a similar task are two lines. Old outcomes, then old
    entries, go when there are too many.
    """
    if kind not in KINDS:
        raise ValueError(f"The kind is one of: {', '.join(KINDS)}.")
    line = check_text(text)
    entry = {"id": uuid.uuid4().hex[:8], "at": now or datetime.now().isoformat(timespec="seconds"),
             "kind": kind, "text": line, "by": by, "job": job}
    same = same_text(line)
    with lock():
        data = load(project)
        def replaced(old):
            if old["kind"] != kind:
                return False
            if kind == "outcome":
                return job is not None and old.get("job") == job
            return same_text(old["text"]) == same

        data["entries"] = [old for old in data["entries"] if not replaced(old)]
        data["entries"].append(entry)
        outcomes = [old["id"] for old in data["entries"] if old["kind"] == "outcome"]
        extra = set(outcomes[:max(0, len(outcomes) - MAX_OUTCOMES)])
        data["entries"] = trim([old for old in data["entries"] if old["id"] not in extra])
        save(data)
    return entry


def same_text(text):
    """Return the text for the "same entry" test: lower case, only letters and digits."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def trim(entries):
    """Keep at most MAX_ENTRIES. The oldest agent entries go first, so they never push out
    the user's instructions; the user's own entries go only when nothing else is left."""
    extra = len(entries) - MAX_ENTRIES
    if extra <= 0:
        return entries
    agents = [entry["id"] for entry in entries if entry.get("by") != "user"]
    users = [entry["id"] for entry in entries if entry.get("by") == "user"]
    dropped = set((agents + users)[:extra])  # each list is oldest first
    return [entry for entry in entries if entry["id"] not in dropped]


def remove(project, entry_id):
    """Remove one entry. Raise ValueError when there is no such entry."""
    with lock():
        data = load(project)
        kept = [entry for entry in data["entries"] if entry["id"] != entry_id]
        if len(kept) == len(data["entries"]):
            raise ValueError("No such entry.")
        data["entries"] = kept
        save(data)


def rank(entries, text, limit=MAX_RELATED):
    """Return the entries most relevant to a text, best first.

    Each shared word adds log(1 + N / df): a word in few entries counts more.
    Decisions and failures count 1.3 times (they prevent mistakes). A newer
    entry wins a tie. An entry below MIN_SCORE is not relevant.
    """
    wanted = set(words(text))
    if not wanted or not entries:
        return []
    sets = [set(words(entry["text"])) for entry in entries]
    count = {}
    for found in sets:
        for word in found:
            count[word] = count.get(word, 0) + 1
    scored = []
    for position, (entry, found) in enumerate(zip(entries, sets)):
        score = sum(math.log(1 + len(entries) / count[word]) for word in found & wanted)
        if entry["kind"] in ("decision", "failure"):
            score *= 1.3
        if score >= MIN_SCORE:
            scored.append((score, entry["at"], position, entry))
    scored.sort(key=lambda item: item[:3], reverse=True)
    return [item[3] for item in scored[:limit]]


def relevant(project, text, limit=MAX_RELATED):
    """Return (the newest instructions, the other entries most relevant to the text)."""
    entries = load(project)["entries"]
    instructions = [entry for entry in entries if entry["kind"] == "instruction"][-MAX_INSTRUCTIONS:]
    others = [entry for entry in entries if entry["kind"] != "instruction"]
    return instructions, rank(others, text, limit)


def line_of(entry):
    job = f", job {entry['job']}" if entry.get("job") else ""
    return f"- ({LABEL[entry['kind']]}{job}) {entry['text']}"


def brief(folder, task):
    """Return the text added to a job's task: what we know about its project, and the rules.

    "" for a job without a project. Always the rules for <project-note> when there is a project.
    """
    project = project_of(folder)
    if project is None:
        return ""
    instructions, related = relevant(project, task)
    text = ""
    if instructions or related:
        text = f"\n\nWhat Agent Office remembers about the project {project.name}:\n"
        text += "\n".join(line_of(entry) for entry in instructions + related)
        if len(text) > BRIEF_CHARS:
            text = text[:BRIEF_CHARS] + " ..."
    return text + RULES.format(name=project.name)


def extract_notes(text):
    """Return the agent's <project-note> blocks as [(kind, text)]. Blocks inside Markdown code are examples."""
    found = []
    for kind, body in (match.groups() for match in memory.blocks_outside_code(NOTE_PATTERN, text)[0]):
        found.append((kind or "decision", tidy(body)))
    return found


def save_agent_notes(folder, text, job_id, by):
    """Save an answer's <project-note> blocks. Return (saved lines, refused lines with why).

    A bad note (wrong kind, too short or long, a secret) is refused alone; the others are saved.
    """
    project = project_of(folder)
    if project is None:
        return [], []
    saved, refused = [], []
    for number, (kind, body) in enumerate(extract_notes(text), 1):
        try:
            if kind not in AGENT_KINDS:
                raise ValueError(f"An agent adds only {' or '.join(AGENT_KINDS)} notes.")
            saved.append(add(project, kind, body, by=by, job=job_id)["text"])
        except ValueError as error:
            refused.append(f"note {number} ({kind}): {error}")  # not its text: it may hold a secret
    return saved, refused


CONTINUE_START = "Continue the work on "  # the text of the Continue button (static/jobs.html)
NEXT = "Next:"


def task_words(task):
    """Return the part of a task that says what the job did.

    A task from the Continue button starts with where the project stood ("Continue the work
    on X. The last job: ..."); only the words after "Next:" are this job's own work. Without
    this, each outcome quoted the one before it, and every line began with the same words.
    """
    if task.startswith(CONTINUE_START) and NEXT in task:
        own = task.rsplit(NEXT, 1)[1].strip()
        if own:
            return own
    return task


def outcome_line(job, files):
    """Return one line about a finished job: its task, status, check, changed files and cost."""
    task = tidy(task_words(job.task))
    task = task[:100] + "..." if len(task) > 100 else task
    parts = [f'"{task}" ended {job.status}']
    if job.check:
        parts.append("the check passed" if job.status == "done" else f"the check was `{job.check}`")
    if files:
        parts.append(f"{files} file(s) changed")
    parts.append(f"${job.cost:.2f}")
    line = ", ".join(parts) + "."
    if job.error:
        line += " Error: " + tidy(job.error)[:200]
    return line


def record_outcome(job, files):
    """Save one outcome line for a finished job on a project (done or failed). Return the entry or None."""
    project = project_of(job.folder)
    if project is None or job.status not in ("done", "failed"):
        return None
    line = outcome_line(job, files)
    if secret_scan.find_secret(line):  # an error message can quote anything
        line = f'"{tidy(job.task)[:60]}" ended {job.status}.'
    try:
        return add(project, "outcome", line, by="python", job=job.id)
    except ValueError:  # a task that itself looks like a secret
        return None


def last_checks(jobs):
    """Return {project: check} with the check of each project's newest done job that had one.

    A done job with a check is a job whose check passed (rule 5), so it is a command that
    works in this project.
    The page's Continue button puts it into the Check field. jobs: Job objects (jobs.all_jobs()).
    """
    projects = {}  # folder -> project: git runs once per folder, not once per job
    newest = {}
    for job in jobs:
        if job.status != "done" or not job.check or not job.folder:
            continue
        if job.folder not in projects:
            projects[job.folder] = project_of(job.folder)
        project = projects[job.folder]
        if project is None:
            continue
        key = str(project)
        if key not in newest or (job.finished_at or "") > (newest[key].finished_at or ""):
            newest[key] = job
    return {key: job.check.strip() for key, job in newest.items()}


def all_projects():
    """Return every project with a memory: [{"project", "name", "file", "counts", "last",
    "latest_outcome"}], newest first. latest_outcome: the newest past-job entry, or None
    (where the project stands; the page's Continue button starts from it)."""
    if not PROJECTS_DIR.is_dir():
        return []
    rows = []
    for path in PROJECTS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        counts = {kind: sum(entry["kind"] == kind for entry in data["entries"]) for kind in KINDS}
        last = max((entry["at"] for entry in data["entries"]), default="")
        outcomes = [entry for entry in data["entries"] if entry["kind"] == "outcome"]
        rows.append({"project": data["project"], "name": data["name"], "file": path.stem,
                     "counts": counts, "last": last,
                     "latest_outcome": max(outcomes, key=lambda entry: entry["at"], default=None)})
    return sorted(rows, key=lambda row: row["last"], reverse=True)


def by_file(name):
    """Return the project folder of a memory file name (from all_projects), or raise ValueError."""
    for row in all_projects():
        if row["file"] == name:
            return Path(row["project"])
    raise ValueError("No such project.")


def main():
    parser = argparse.ArgumentParser(description="What Agent Office remembers about each project.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="the projects")
    commands.add_parser("show", help="the entries, newest first").add_argument("folder")
    adding = commands.add_parser("add", help="add an instruction (or a decision, a failure)")
    adding.add_argument("folder")
    adding.add_argument("text")
    adding.add_argument("--kind", choices=USER_KINDS, default="instruction")
    removing = commands.add_parser("remove", help="remove an entry")
    removing.add_argument("folder")
    removing.add_argument("entry_id")
    briefing = commands.add_parser("brief", help="what an agent would get for a task")
    briefing.add_argument("folder")
    briefing.add_argument("task")
    args = parser.parse_args()

    if args.command == "list":
        rows = all_projects()
        if not rows:
            print("No project memory yet. Add an instruction: python -m agentic_os.project_memory add <folder> \"...\"")
        for row in rows:
            counts = ", ".join(f"{count} {kind}" for kind, count in row["counts"].items() if count)
            print(f"{row['name']:<20} {row['project']}  ({counts})")
        return
    project = project_of(args.folder)
    if project is None:
        raise SystemExit(folder_problem(args.folder))
    try:
        if args.command == "show":
            entries = load(project)["entries"]
            print(f"{project}: {len(entries)} entries")
            for entry in reversed(entries):
                print(f"{entry['id']}  {entry['at']}  {line_of(entry)[2:]}")
        elif args.command == "add":
            entry = add(project, args.kind, args.text)
            print(f"Remembered for {project.name} ({entry['kind']}): {entry['text']}")
        elif args.command == "remove":
            remove(project, args.entry_id)
            print("Removed.")
        else:
            print(brief(project, args.task).strip())
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
