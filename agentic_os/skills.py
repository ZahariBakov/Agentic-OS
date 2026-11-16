"""Skills: saved instructions with a blank for the input.

Each skill is a Markdown file in skills/. The front matter has the name,
a description, the default model and effort, and if the agent gets the memory.
The body is the prompt. These blanks are filled in:

    {input}        the text you give when you run the skill
    {memory_dir}   the absolute path of the memory folder
    {skills_dir}   the absolute path of the skills folder
    {references}   the skill's references: skills/references/<name>.md

With "input_kind: skill" in the front matter, the input must be a skill name;
with "input_kind: optional", the input may be empty.

More front matter:
    tools: none        the agent gets no tools at all (safety.no_tool_args), for a
                       skill whose task holds text written by strangers (mail)
    trigger: a, b      phrases: a new job on the web page whose task starts with one
                       runs this skill (free, Python), with the rest as the input

One more blank, {inbox_facts}, is filled when the job starts, not when it is
added (jobs.fill_inbox: the mail must be new, and a job may wait in the queue).

Wildcards (see wildcards.py): with "items: 10" and "wildcards: 2" in the front
matter, Python chooses which 2 of the 10 items break the pattern of the
references, adds the rules to the task and checks the answer. A wildcard you
like goes into the references ("keep-wildcard").

A skill run is a normal job with job.skill set, so costs, artifacts,
checks and live updates work for skills too.

Usage (from the project folder):
    python -m agentic_os.skills list
    python -m agentic_os.skills show <name>
    python -m agentic_os.skills run <name> "input" [--model sonnet] [--effort high]
    python -m agentic_os.skills feedback <name> ["text"]   add feedback, or show the open feedback
    python -m agentic_os.skills diff <name>                show the proposal from calibrate
    python -m agentic_os.skills accept <name>              use the proposal
    python -m agentic_os.skills reject <name>              delete the proposal
    python -m agentic_os.skills keep-wildcard <job id> <item number>   add a wildcard to the references
"""

import argparse
import re

from agentic_os import feedback, job_store, jobs, memory, secret_scan, wildcards
from agentic_os.feedback import NAME_PATTERN, SKILLS_DIR

REFERENCES_DIR = SKILLS_DIR / "references"
from agentic_os.router import EFFORTS, MODELS


def load(name):
    """Return one skill as a dict, or None if there is no such skill."""
    # Only simple names, so nobody can load "../../some/other/file".
    if not NAME_PATTERN.fullmatch(name):
        return None
    path = SKILLS_DIR / f"{name}.md"
    if not path.exists():
        return None
    meta, prompt = memory.read_note(path)
    items, wild = int(meta.get("items", 0)), int(meta.get("wildcards", 0))
    if wild and not 0 < wild < items:
        raise ValueError(f"skill {name}: wildcards must be between 1 and items - 1")
    return {
        "name": name,
        "description": meta.get("description", ""),
        "model": meta.get("model", "haiku"),
        "effort": meta.get("effort") or None,
        "memory": meta.get("memory", "false").lower() == "true",
        "input": meta.get("input", "The input"),
        "input_kind": meta.get("input_kind", "text"),
        "tools": meta.get("tools", "default"),
        "trigger": [" ".join(phrase.lower().split())
                    for phrase in meta.get("trigger", "").split(",") if phrase.strip()],
        "items": items,  # 0: the answer has no numbered items to check
        "wildcards": wild,  # 0: no wildcards
        "references": (REFERENCES_DIR / f"{name}.md").is_file(),
        "prompt": prompt,
    }


def references_file(name):
    return REFERENCES_DIR / f"{name}.md"


def all_skills():
    """Return all skills, sorted by name. A skill with a mistake is skipped with a message."""
    found = []
    for path in sorted(SKILLS_DIR.glob("*.md")):
        try:
            skill = load(path.stem)
        except ValueError as error:
            print(f"Skipped skill {path.name}: {error}")
            continue
        if skill is not None:
            found.append(skill)
    return found


BLANK = re.compile(r"\{(input|memory_dir|skills_dir|references)\}")


def render(skill, text):
    """Fill the blanks in the skill's prompt. Return the task for the agent.

    Not format(): with format(), every other "{" in the prompt (for example in a
    JSON example) would be an error. One pass over the prompt, not replace() after
    replace(): a "{memory_dir}" inside the user's input (or in the references) stays
    as the user wrote it.
    """
    def value(blank):
        if blank == "input":
            return text.strip()
        if blank == "memory_dir":
            return str(memory.MEMORY_DIR)
        if blank == "skills_dir":
            return str(SKILLS_DIR)
        return references_file(skill["name"]).read_text().strip() if skill["references"] else "(no references yet)"

    return BLANK.sub(lambda match: value(match[1]), skill["prompt"])


def add(name, text, model=None, effort=None, fields=None):
    """Add a job that runs a skill. model and effort replace the skill's defaults.

    fields: more Job fields, set before the job is saved (see jobs.add).
    """
    skill = load(name)
    if skill is None:
        raise SystemExit(f"No skill named {name}. Use 'python -m agentic_os.skills list'.")
    if not text.strip() and skill["input_kind"] != "optional":
        raise SystemExit(f"The skill {name} needs an input: {skill['input']}")
    text = text if text.strip() else "(nothing more)"
    if skill["input_kind"] == "skill" and load(text.strip()) is None:
        raise SystemExit(f"The skill {name} needs a skill name, and there is no skill named {text.strip()}.")
    model = model or skill["model"]
    effort = effort or skill["effort"]
    if model not in MODELS or (effort is not None and effort not in EFFORTS):
        raise SystemExit(f"Unknown model or effort: {model}, {effort}")
    task = render(skill, text)
    places = wildcards.pick(skill["items"], skill["wildcards"]) if skill["wildcards"] else None
    if places:
        task += wildcards.rules(skill["items"], places)
    fields = dict(fields or {})
    if places:
        fields["wildcards"] = {"count": skill["items"], "places": places, "kept": []}
    return jobs.add(task, model, use_memory=skill["memory"], effort=effort, skill=name, fields=fields)


# Polite words that may come before a trigger phrase ("Can you check my email?") or after it
# ("check my email please"). A fixed list: any other start means a normal job.
POLITE_START = r"(?:(?:hey|hi|hello|ok|okay|please|pls|can you|could you|would you|will you)(?![\w])[\s,!.]*)*"
POLITE_END = r"(?:(?:[\s,]*(?:please|pls|for me|thanks|thank you)(?![\w]))+[\s.!?]*)?"


def triggered(task):
    """Return (skill name, input) when the task starts with a skill's trigger phrase, else None.

    "check my email from GitHub" -> ("inbox", "from GitHub"). The phrase must end at a word
    end: "check my emails" is not "check my email" + "s". Polite words around it are allowed:
    "Hey, can you please check my email?" -> ("inbox", "").
    """
    text = " ".join(task.split())
    for skill in all_skills():
        for phrase in skill["trigger"]:
            pattern = rf"{POLITE_START}{re.escape(phrase)}(?![\w])[\s:,.!?-]*(.*?){POLITE_END}"
            found = re.fullmatch(pattern, text, re.IGNORECASE | re.DOTALL)
            if found:
                return skill["name"], found[1]
    return None


def no_tools(name):
    """Return True when the skill's agent gets no tools (front matter "tools: none")."""
    skill = load(name) if name else None
    return skill is not None and skill["tools"] == "none"


def keep_wildcard(job_id, number):
    """Add wildcard item `number` of a done skill job to the skill's references. Return its text.

    Python copies the line; the agent never writes the references.
    """
    if not job_id.isalnum() or not jobs.exists(job_id):
        raise ValueError(f"No job {job_id}.")
    with memory.file_lock(job_store.DATA_DIR / ".references.lock"):  # in data/: not in git
        job = jobs.load(job_id)
        if not job.wildcards or job.status != "done":
            raise ValueError(f"Job {job_id} is not a done job with wildcards.")
        if jobs.worker_busy(job):
            raise jobs.busy_error(job_id)
        item = next((item for item in wildcards.items(job.result) if item["number"] == number), None)
        if item is None or not item["wildcard"]:
            raise ValueError(f"Item {number} of job {job_id} is not a wildcard.")
        secret_scan.check(item["text"], "the wildcard")  # the references are in git
        REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
        wildcards.keep(references_file(job.skill), item["text"])  # False: the same line is there already
        # A fresh copy under the claim lock: never save an old Job object (a run may have saved since)
        with jobs.claim_lock():
            fresh = jobs.load(job_id)
            if fresh.wildcards and number not in fresh.wildcards["kept"]:
                fresh.wildcards["kept"].append(number)
                jobs.save(fresh)
    return item["text"]


def print_list():
    """Print one line per skill, with how often it ran and what it cost."""
    runs = {}
    for job in jobs.all_jobs():
        if job.skill:
            count, cost = runs.get(job.skill, (0, 0.0))
            runs[job.skill] = (count + 1, cost + job.cost)
    for skill in all_skills():
        count, cost = runs.get(skill["name"], (0, 0.0))
        waiting = len(feedback.open_items(skill["name"]))
        print(
            f"{skill['name']:<12} {skill['model']:<6} {skill['effort'] or '-':<6}"
            f" {count:>3} runs  ${cost:.4f}  {waiting} feedback  {skill['description']}"
        )
    for name in feedback.proposals():
        print(f"Proposal waiting for {name}: python -m agentic_os.skills diff {name}")


def main():
    parser = argparse.ArgumentParser(description="Run saved skills.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list the skills")
    show_parser = commands.add_parser("show", help="show one skill")
    show_parser.add_argument("name")
    run_parser = commands.add_parser("run", help="run a skill now")
    run_parser.add_argument("name")
    run_parser.add_argument("input")
    run_parser.add_argument("--model", choices=MODELS)
    run_parser.add_argument("--effort", choices=EFFORTS)
    feedback_parser = commands.add_parser("feedback", help="add feedback, or show the open feedback")
    feedback_parser.add_argument("name")
    feedback_parser.add_argument("text", nargs="?")
    for command, text in [("diff", "show the proposal"), ("accept", "use the proposal"),
                          ("reject", "delete the proposal")]:
        commands.add_parser(command, help=text).add_argument("name")
    keep_parser = commands.add_parser("keep-wildcard", help="add a wildcard of a job to the skill's references")
    keep_parser.add_argument("job")
    keep_parser.add_argument("number", type=int)
    args = parser.parse_args()

    if args.command == "list":
        print_list()
    elif args.command == "show":
        skill = load(args.name)
        if skill is None:
            raise SystemExit(f"No skill named {args.name}.")
        print(f"{skill['name']}: {skill['description']}")
        print(f"model {skill['model']}, effort {skill['effort'] or '-'}, memory {skill['memory']}")
        print(f"input: {skill['input']}")
        if skill["wildcards"]:
            print(f"items: {skill['items']}, wildcards: {skill['wildcards']} (references: {skill['references']})")
        print()
        print(skill["prompt"])
    elif args.command == "run":
        job = add(args.name, args.input, args.model, args.effort)
        jobs.run_job(job)
        print()
        print(jobs.load(job.id).result)
    elif args.command == "feedback":
        if args.text:
            feedback.add(args.name, args.text)
        items = feedback.open_items(args.name)
        print(f"Open feedback for {args.name}: {len(items)}")
        for item in items:
            print(f"  {item}")
    elif args.command == "diff":
        print(feedback.diff(args.name) or f"No proposal for {args.name}.")
    elif args.command == "accept":
        feedback.accept(args.name)
        print(f"Accepted. skills/{args.name}.md is updated, and its feedback is marked as done.")
    elif args.command == "reject":
        feedback.reject(args.name)
        print("Rejected. The proposal is deleted; the feedback stays open.")
    elif args.command == "keep-wildcard":
        try:
            text = keep_wildcard(args.job, args.number)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print(f"Kept in the references: {text}")


if __name__ == "__main__":
    main()
