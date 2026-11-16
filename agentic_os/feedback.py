"""Feedback for skills: a backlog of fixes, and proposals for better skills.

    skills/feedback/<name>.md   the backlog: one line per feedback item
                                "- [ ] <date>  <text>" is open, "- [x] ..." is done
    data/proposals/<name>.md    a new version of a skill, written by the calibrate skill

The calibrate agent never changes a skill. It writes the new file in a
<skill-file name="..."> block, and Python saves it as a proposal. You look
at the diff and accept it (or not). Accepting marks the open feedback as done.

This module does not import skills.py or jobs.py: jobs.py imports this module,
and skills.py imports jobs.py, so an import back would be circular.
"""

import difflib
import json
import re
from datetime import datetime
from pathlib import Path

from agentic_os import secret_scan
from agentic_os.memory import write_atomic

PROJECT_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = PROJECT_DIR / "skills"
FEEDBACK_DIR = SKILLS_DIR / "feedback"
PROPOSALS_DIR = PROJECT_DIR / "data" / "proposals"
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")  # for example "note" or "code-review"
SKILL_FILE_OPEN = re.compile(r'<skill-file\s+name="([^"]*)"\s*>\n?')
SKILL_FILE_TAG = re.compile(r"<skill-file\b[^>]*>|</skill-file>")  # an opening or a closing tag
OPEN = "- [ ] "
DONE = "- [x] "


def skill_path(name):
    """Return the file of a skill, or None if the name is not a simple, existing name."""
    if not NAME_PATTERN.fullmatch(name):
        return None
    path = SKILLS_DIR / f"{name}.md"
    return path if path.exists() else None


# --- The backlog ---

def add(name, text):
    """Add one feedback line to a skill's backlog. Return the backlog file."""
    if skill_path(name) is None:
        raise SystemExit(f"No skill named {name}.")
    text = " ".join(text.split())
    if not text:
        raise SystemExit("The feedback is empty.")
    path = FEEDBACK_DIR / f"{name}.md"
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# Feedback for the skill {name}\n\n")
    time = datetime.now().strftime("%Y-%m-%d %H:%M")
    with path.open("a") as file:
        file.write(f"{OPEN}{time}  {text}\n")
    return path


def open_items(name):
    """Return the open feedback lines of a skill (without the "- [ ] ")."""
    path = FEEDBACK_DIR / f"{name}.md"
    if not path.exists():
        return []
    return [line[len(OPEN):] for line in path.read_text().splitlines() if line.startswith(OPEN)]


def close_items(name, items=None):
    """Mark open feedback lines of a skill as done: the lines in `items`, or all (None)."""
    path = FEEDBACK_DIR / f"{name}.md"
    if not path.exists():
        return
    lines = path.read_text().splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith(OPEN) and (items is None or line[len(OPEN):].rstrip("\n") in items):
            lines[index] = DONE + line[len(OPEN):]
    write_atomic(path, "".join(lines))


# --- Proposals ---

def skill_files(text):
    """Return the <skill-file> blocks of a text: (name, body, start, end), in order.

    The tags are counted, so a block may hold an example <skill-file> block (the
    calibrate skill does): the example stays in the body, and the block ends at
    its own closing tag. A block without a closing tag is not a block.
    """
    blocks, position = [], 0
    while (opening := SKILL_FILE_OPEN.search(text, position)) is not None:
        depth, scan = 1, opening.end()
        while depth and (tag := SKILL_FILE_TAG.search(text, scan)) is not None:
            depth += -1 if tag.group(0) == "</skill-file>" else 1
            scan = tag.end()
        if depth:
            break
        blocks.append((opening.group(1), text[opening.end():tag.start()], opening.start(), tag.end()))
        position = tag.end()
    return blocks


def without_skill_files(text):
    """Remove the <skill-file> blocks from a text.

    A skill file can contain an example <note> block. It must not be saved as a real note.
    """
    text = text or ""
    for _, _, start, end in reversed(skill_files(text)):
        text = text[:start] + text[end:]
    return text


def save_proposals(text, since=None):
    """Find <skill-file> blocks in an agent's answer and save them as proposals.

    Return the names of the skills that got a proposal. The open feedback lines
    that existed when the calibrate job was made (`since`, its created_at) are saved
    next to it: accept closes only those. A line added while the agent worked may
    not have been read, so it stays open.
    """
    saved = []
    for name, body, _, _ in skill_files(text or ""):
        # Only existing skills, and only a file that looks like a skill.
        if skill_path(name) is None or not body.startswith("---\n"):
            continue
        what = secret_scan.find_secret(body)
        if what is not None:  # skills/ is in git
            print(secret_scan.refusal(what, f"the proposal for {name}"))
            continue
        PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
        write_atomic(PROPOSALS_DIR / f"{name}.md", body.rstrip() + "\n")
        used = [item for item in open_items(name) if since is None or item[:16] <= since[:16].replace("T", " ")]
        write_atomic(PROPOSALS_DIR / f"{name}.feedback.json", json.dumps(used))
        saved.append(name)
    return saved


def proposals():
    """Return the names of the skills that have a proposal."""
    return sorted(path.stem for path in PROPOSALS_DIR.glob("*.md")) if PROPOSALS_DIR.exists() else []


def diff(name):
    """Return the difference between a skill and its proposal, or None."""
    proposal = PROPOSALS_DIR / f"{name}.md"
    if skill_path(name) is None or not proposal.exists():
        return None
    lines = difflib.unified_diff(
        skill_path(name).read_text().splitlines(keepends=True),
        proposal.read_text().splitlines(keepends=True),
        fromfile=f"skills/{name}.md", tofile=f"proposal for {name}",
    )
    return "".join(lines)


def accept(name):
    """Replace the skill with its proposal, and mark its open feedback as done."""
    proposal = PROPOSALS_DIR / f"{name}.md"
    if skill_path(name) is None or not proposal.exists():
        raise SystemExit(f"No proposal for the skill {name}.")
    write_atomic(skill_path(name), proposal.read_text())
    proposal.unlink()
    used = PROPOSALS_DIR / f"{name}.feedback.json"
    close_items(name, set(json.loads(used.read_text())) if used.exists() else None)  # None: an old proposal
    used.unlink(missing_ok=True)


def reject(name):
    """Delete a skill's proposal. The feedback stays open."""
    proposal = PROPOSALS_DIR / f"{name}.md"
    if not NAME_PATTERN.fullmatch(name) or not proposal.exists():
        raise SystemExit(f"No proposal for the skill {name}.")
    proposal.unlink()
    (PROPOSALS_DIR / f"{name}.feedback.json").unlink(missing_ok=True)
