"""Memory: the "second brain" as plain Markdown files in memory/.

    memory/MEMORY.md    the map: one line per note, grouped by area (made by Python)
    memory/log.md       a dated list of every change (written by Python)
    memory/identity/    user.md, soul.md, anti-ai-style.md
    memory/raw/         sources; agents read them, but never change them
    memory/wiki/<area>/ the notes

Agents never write memory files. They put <note> blocks in their answer,
and Python saves them, updates the map and writes the log.

Usage (from the project folder):
    python -m agentic_os.memory note "Title" "The note text." [--area personal]
    python -m agentic_os.memory index
    python -m agentic_os.memory check <file>
    python -m agentic_os.memory prompt
    python -m agentic_os.memory health          find problems with exact rules (free)
"""

import argparse
import fcntl
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(__file__).resolve().parent.parent / "memory"
INDEX_FILE = MEMORY_DIR / "MEMORY.md"
LOG_FILE = MEMORY_DIR / "log.md"
IDENTITY_DIR = MEMORY_DIR / "identity"
RAW_DIR = MEMORY_DIR / "raw"
WIKI_DIR = MEMORY_DIR / "wiki"
def memory_lock():
    """The lock for writing notes, the map and the log, for a `with` block.

    A file lock, not a threading.Lock: the web server and `jobs run` are two
    processes, and both save notes. The lock file is in data/, not in memory/
    (memory/ is in git).
    """
    return file_lock(MEMORY_DIR.parent / "data" / ".memory.lock")

# A note block: <note name="value" ...>text</note>. The head has only name="value"
# pairs, so a ">" inside a value (for example "raw -> wiki") does not end the tag.
NOTE_PATTERN = re.compile(r'<note((?:\s+\w+="[^"]*")*)\s*>(.*?)</note>', re.DOTALL)
ATTRIBUTE_PATTERN = re.compile(r'(\w+)="([^"]*)"')
# Code in Markdown: ```fenced blocks``` and `inline code`. memory health removes it
# before it looks for links (a link inside code is an example).
CODE_PATTERN = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
# A fenced block: a line that starts with ``` or ~~~ up to a line with the same fence.
FENCE_PATTERN = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$", re.DOTALL | re.MULTILINE)
INLINE_CODE_PATTERN = re.compile(r"`[^`\n]+`")


def code_spans(text):
    """Return the (start, end) places of the Markdown code in a text: fenced blocks and inline code."""
    spans = [match.span() for match in FENCE_PATTERN.finditer(text)]
    for match in INLINE_CODE_PATTERN.finditer(text):
        if not any(start <= match.start() < end for start, end in spans):
            spans.append(match.span())
    return spans


def blocks_outside_code(pattern, text):
    """Find an agent's tagged blocks (<note>, <question>, <plan>, <handoff>, ...).

    Return (the matches, the text without them). A block whose tag starts inside
    Markdown code is an example, not a real block: it is skipped, and it stays in
    the text. The text is never changed before the search, so code INSIDE a real
    block stays as it is (an older version removed all code first, and a note
    "Run `pytest -q` first" was saved as "Run  first").
    """
    text = text or ""
    spans = code_spans(text)
    found = [match for match in pattern.finditer(text)
             if not any(start <= match.start() < end for start, end in spans)]
    rest = text
    for match in reversed(found):
        rest = rest[:match.start()] + rest[match.end():]
    return found, rest


def slug(text):
    """Turn a title into a safe file name, for example "User's name" -> "user-s-name"."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "note"


def write_atomic(path, text):
    """Write a file so that a reader never sees it half-written.

    We write a temporary file first and then rename it. A rename is atomic.
    The temporary name is unique, so two writers (threads or processes) never
    share it: with one fixed name, the second rename would find no file.
    """
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())  # on the disk before the rename: a power loss leaves the old file, not an empty one
        try:
            mode = path.stat().st_mode & 0o777  # keep the old file's rights (a private 0600 file stays private)
        except FileNotFoundError:
            mode = 0o644  # mkstemp makes the file private (0600); a new file gets the usual rights
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


@contextmanager
def file_lock(path):
    """Hold a lock on a file while the `with` block runs.

    threading.Lock works only inside one process. flock works between processes
    too (the web server and `jobs run` in a terminal), and between threads,
    because each call opens the file again and gets its own lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX)  # waits until nobody else holds the lock
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def read_note(path):
    """Read a note. Return (front matter as a dict, body text)."""
    text = path.read_text()
    meta = {}
    body = text
    if text.startswith("---\n"):
        head, _, body = text[4:].partition("\n---\n")
        for line in head.splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, body.strip()


def log_change(action, path, detail=""):
    """Add one dated line to memory/log.md."""
    if not LOG_FILE.exists():
        LOG_FILE.write_text("# Memory log\n\nEvery change to the memory, oldest first.\n\n")
    time = datetime.now().isoformat(timespec="seconds")
    with LOG_FILE.open("a") as file:
        file.write(f"- {time}  {action}  {path.relative_to(MEMORY_DIR)}  {detail}\n")


def index_text():
    """Return the text of the map (MEMORY.md): one section per area, one line per note."""
    areas = {}
    for path in sorted(WIKI_DIR.glob("*/*.md")):
        meta, _ = read_note(path)
        title = meta.get("title", path.stem)
        line = f"- [{title}]({path.relative_to(MEMORY_DIR)}) - {meta.get('description', '')}"
        areas.setdefault(path.parent.name, []).append(line)

    lines = [
        "# Memory map",
        "",
        "Made by Python (`python -m agentic_os.memory index`). Do not edit by hand.",
        "",
    ]
    for area in sorted(areas):
        lines += [f"## {area}", "", *areas[area], ""]
    return "\n".join(lines)


def rebuild_index():
    """Write memory/MEMORY.md from the notes."""
    write_atomic(INDEX_FILE, index_text())


LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")  # [text](target)
WIKI_LINK_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")  # [[Title]]
URL_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)  # http:, mailto:, ftp:, file:, tel: ...


def broken_links(path, body, titles):
    """Return the links in a note that point to nothing."""
    broken = []
    for target in LINK_PATTERN.findall(CODE_PATTERN.sub("", body)):
        if target.startswith("#") or URL_SCHEME.match(target):
            continue  # a heading in this note, or a web, mail, ftp, file or tel link: not a note
        file = target.split("#")[0]
        # A link can be relative to the note's folder, or to the memory folder (like the map).
        if not (path.parent / file).exists() and not (MEMORY_DIR / file).exists():
            broken.append(target)
    for title in WIKI_LINK_PATTERN.findall(CODE_PATTERN.sub("", body)):
        if title.split("|")[0].split("#")[0].strip() not in titles:  # [[Note#Heading|text]]
            broken.append(f"[[{title}]]")
    return broken


def health():
    """Check the memory with exact rules (no AI). Return a list of (file, problem).

    The AI part of a health check (notes that disagree, old facts) is a routine;
    it gets this list in its prompt, so it does not spend tokens on exact work.
    """
    problems = []
    if not WIKI_DIR.exists():
        return problems
    for path in sorted(WIKI_DIR.glob("*.md")):
        problems.append((path, "not in an area folder, so the map does not show it"))
    notes = [(path, *read_note(path)) for path in sorted(WIKI_DIR.glob("*/*.md"))]
    titles = {}
    for path, meta, _ in notes:
        titles.setdefault(meta.get("title", path.stem), []).append(path)
    for path, meta, body in notes:
        for field in ("title", "description"):
            if not meta.get(field):
                problems.append((path, f"no {field} in the front matter"))
        if not body:
            problems.append((path, "the note is empty"))
        others = [other for other in titles[meta.get("title", path.stem)] if other != path]
        if others:
            names = ", ".join(str(other.relative_to(MEMORY_DIR)) for other in others)
            problems.append((path, f"the same title as {names}"))
        for target in broken_links(path, body, titles):
            problems.append((path, f"broken link: {target}"))
        found = style_problems(body)
        if found:
            problems.append((path, f"banned phrases: {', '.join(found)}"))
    if not INDEX_FILE.exists() or INDEX_FILE.read_text() != index_text():
        problems.append((INDEX_FILE, "the map is out of date (run: python -m agentic_os.memory index)"))
    return problems


def health_report():
    """Return the health problems as text, one line per problem."""
    problems = health()
    if not problems:
        return "No problems found by the exact checks."
    return "\n".join(f"- {path.relative_to(MEMORY_DIR)}: {problem}" for path, problem in problems)


def note_path(title, area):
    """Return the file for a note with this title.

    If a note with the same title exists (in any area), return its file, so
    the note is updated. Otherwise return a new file in the area. If two
    titles give the same file name ("C++" and "C" both give "c"), add a number.
    """
    for path in WIKI_DIR.glob("*/*.md"):
        if read_note(path)[0].get("title") == title:
            return path
    folder = WIKI_DIR / slug(area or "general")
    path = folder / f"{slug(title)}.md"
    number = 2
    while path.exists():
        path = folder / f"{slug(title)}-{number}.md"
        number += 1
    return path


def save_notes(notes, source="you"):
    """Save many notes, then rebuild the map once. Return the paths of the notes.

    Each note is a dict with title, body, and optional area and description.
    A note that looks like it holds a secret (secret_scan) stops the save with
    ValueError before anything is written: memory/ is in git.
    """
    # Imported here: the edit hook loads memory, and every module the hook loads must be
    # protected (safety.HOOK_MODULES); this one is not needed there.
    from agentic_os import secret_scan
    for note in notes:
        what = note_secret(note)
        if what is not None:
            raise ValueError(secret_scan.refusal(what, f"the note \"{note['title']}\""))
    paths = []
    with memory_lock():
        for note in notes:
            title = " ".join(note["title"].split())
            path = note_path(title, note.get("area"))
            old = read_note(path)[0].get("description") if path.exists() else None
            description = " ".join((note.get("description") or old or title).split())
            action = "updated" if path.exists() else "created"
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, (
                f"---\ntitle: {title}\ndescription: {description}\n"
                f"source: {source}\n---\n\n{note['body'].strip()}\n"
            ))
            log_change(action, path, f"(by {source})")
            paths.append(path)
        if paths:
            rebuild_index()
    return paths


def note_secret(note):
    """Return what kind of secret a note seems to hold, or None (secret_scan)."""
    from agentic_os import secret_scan  # here: see save_notes
    return secret_scan.find_secret(f"{note['title']}\n{note.get('description') or ''}\n{note['body']}")


def save_note(title, body, area="general", description="", source="you"):
    """Save one note, then update the map and the log. Return the path of the note."""
    note = {"title": title, "body": body, "area": area, "description": description}
    return save_notes([note], source)[0]


def extract_notes(text):
    """Find the <note ...>...</note> blocks in an agent's answer. Return a list of dicts.

    Blocks inside Markdown code are examples, so we skip them.
    """
    notes = []
    for head, body in (match.groups() for match in blocks_outside_code(NOTE_PATTERN, text)[0]):
        attributes = dict(ATTRIBUTE_PATTERN.findall(head))
        if attributes.get("title") and body.strip():
            notes.append({
                "title": attributes["title"],
                "area": attributes.get("area") or "general",
                "description": attributes.get("description", ""),
                "body": body,
            })
    return notes


def phrase_text(phrase):
    """Return the text of one banned phrase. "U+2014" means that one character.

    Files in the repo are ASCII only, so a non-ASCII character is written by its code.
    """
    code = re.fullmatch(r"U\+([0-9A-Fa-f]{4,6})", phrase)
    return chr(int(code.group(1), 16)) if code else phrase


def banned_phrases():
    """Return the banned phrases: the "- " lines in identity/anti-ai-style.md."""
    path = IDENTITY_DIR / "anti-ai-style.md"
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    return [phrase_text(line[2:].strip()) for line in lines if line.startswith("- ")]


def style_problems(text):
    """Return the banned phrases that appear in the text (exact work, so no AI)."""
    lower = (text or "").lower()
    return [phrase for phrase in banned_phrases() if phrase.lower() in lower]


def identity(name):
    """Return the body of an identity file, or an empty string."""
    path = IDENTITY_DIR / f"{name}.md"
    return read_note(path)[1] if path.exists() else ""


def memory_prompt():
    """Build the memory part of an agent's system prompt.

    It has the identity and the map, but not the notes. The agent reads only
    the notes it needs, so the context stays small.
    """
    with memory_lock():
        if not INDEX_FILE.exists():
            rebuild_index()
    banned = ", ".join(f'"{phrase}"' for phrase in banned_phrases())
    return f"""# Memory

You have a long-term memory: Markdown files in {MEMORY_DIR}.

## Who the user is
{identity("user")}

## Who you are
{identity("soul")}

## Writing style
Never use these words or phrases: {banned}.

## Map of the notes (paths are relative to the memory folder)
{INDEX_FILE.read_text()}
To read a note, open its file with the Read tool. Read only the notes you need.
Files in raw/ are sources: read them, but never change them.

## Saving a note
Never write memory files yourself. To save something, put this block in your answer:

<note title="Short title" area="one-word-area" description="One-line summary">
The note text.
</note>

Save only facts that will be useful later. To update a note, use its exact title.

## Learning from corrections
One correction is normal: fix your answer. But when the user corrects the same
thing a second time (for example "again", "I already told you"), the memory
missed a rule. In that same answer, fix the work and also save the rule as a
lesson, so no future session needs the correction:

<note title="Answers in one sentence" area="lessons" description="The user wants one-sentence answers">
Keep answers to one sentence. The user corrected this twice.
</note>
"""


def write_prompt(prompt_file):
    """Write the memory prompt into a file for --append-system-prompt-file.

    We use a file, not a command line argument, because Linux allows at most
    128 KB in one argument.
    """
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(memory_prompt())


def main():
    parser = argparse.ArgumentParser(description="Manage the memory.")
    commands = parser.add_subparsers(dest="command", required=True)

    note_parser = commands.add_parser("note", help="save a note")
    note_parser.add_argument("title")
    note_parser.add_argument("text")
    note_parser.add_argument("--area", default="general")
    note_parser.add_argument("--description", default="")

    commands.add_parser("index", help="rebuild MEMORY.md from the notes")

    check_parser = commands.add_parser("check", help="find banned phrases in a file")
    check_parser.add_argument("file")

    commands.add_parser("prompt", help="show the memory prompt for agents")
    commands.add_parser("health", help="check the memory with exact rules (free, no AI)")

    args = parser.parse_args()
    if args.command == "note":
        path = save_note(args.title, args.text, args.area, args.description)
        print(f"Saved {path.relative_to(MEMORY_DIR)}")
    elif args.command == "index":
        with memory_lock():
            rebuild_index()
        print(f"Rebuilt {INDEX_FILE.relative_to(MEMORY_DIR)}")
    elif args.command == "check":
        problems = style_problems(Path(args.file).read_text())
        print("No banned phrases." if not problems else f"Banned phrases: {problems}")
    elif args.command == "prompt":
        print(memory_prompt())
    elif args.command == "health":
        print(health_report())


if __name__ == "__main__":
    main()
