"""Documents: convert PDF, Office and HTML files in memory/raw/ to Markdown.

Converting is exact work, so Python does it (with markitdown), not an agent.
The Markdown goes next to the original, with ".md" added to the name:

    memory/raw/report.pdf                ->  memory/raw/report.pdf.md
    memory/raw/meetings/2026-11-02.docx  ->  memory/raw/meetings/2026-11-02.docx.md

Then the normal ingest reads the .md file (a meeting stays in meetings/).
`jobs ingest-new` converts new documents first, so the ingest-inbox routine
handles documents too.

markitdown runs in a child process with a time limit and a memory limit, so a
broken or huge file cannot block the web server or fill its memory.

Usage (from the project folder):
    python -m agentic_os.documents convert <file in memory/raw/>
    python -m agentic_os.documents convert-new      convert every document without a .md
"""

import argparse
import resource
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from agentic_os import memory

DOCUMENT_SUFFIXES = (".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm")
MAX_BYTES = 20 * 1024 * 1024  # bigger files are skipped: slow to convert, too long for an agent
# Word, PowerPoint and Excel files are zip files. A small "zip bomb" unpacks to gigabytes
# and would fill the memory of the web server (the ingest routine runs there).
ZIP_SUFFIXES = (".docx", ".pptx", ".xlsx")
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
TIMEOUT_SECONDS = 120  # one conversion; markitdown itself needs about 1 second to start
MAX_MEMORY_BYTES = 1024 * 1024 * 1024  # the child's address space (markitdown needs under 512 MB)
PROJECT_DIR = Path(__file__).resolve().parent.parent


def markdown_path(document):
    """Return where the Markdown of a document goes: the same name plus ".md"."""
    return document.with_name(document.name + ".md")


def to_markdown(document):
    """Convert one document in a child process and return its Markdown text.

    Raise ValueError when the child fails, runs too long or needs too much memory.
    """
    command = [sys.executable, "-m", "agentic_os.documents", "child", str(document)]
    try:
        # cwd: the child imports agentic_os from the project. The document is only data.
        done = subprocess.run(command, cwd=PROJECT_DIR, capture_output=True, encoding="utf-8", errors="replace",
                              timeout=TIMEOUT_SECONDS, start_new_session=True)
    except subprocess.TimeoutExpired as error:  # run() has killed the child
        raise ValueError(f"Took too long (over {TIMEOUT_SECONDS} seconds)") from error
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines()
        raise ValueError(lines[-1] if lines else f"the converter stopped (exit code {done.returncode})")
    return done.stdout


def child(document):
    """The child process: limit the memory, convert, print the Markdown. Errors go to stderr."""
    resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
    try:
        from markitdown import MarkItDown  # imported here: only the child needs it
        text = MarkItDown().convert(str(document)).text_content
    except MemoryError:
        raise SystemExit(f"Needs too much memory (over {MAX_MEMORY_BYTES // (1024 * 1024)} MB)") from None
    except Exception as error:  # markitdown raises many kinds of errors for broken files
        raise SystemExit(f"{type(error).__name__}: {error}".splitlines()[0]) from None
    sys.stdout.reconfigure(encoding="utf-8")  # the parent reads UTF-8, whatever the locale
    sys.stdout.write(text or "")


def check_unzipped_size(document):
    """Raise ValueError when an Office file (a zip) is broken or unpacks to too much.

    The sizes come from the zip's table of contents; nothing is unpacked here.
    """
    try:
        with zipfile.ZipFile(document) as archive:
            size = sum(member.file_size for member in archive.infolist())
    except (zipfile.BadZipFile, OSError) as error:
        raise ValueError(f"Could not convert {document.name}: {error}") from error
    if size > MAX_UNZIPPED_BYTES:
        raise ValueError(f"Too big when unpacked (over {MAX_UNZIPPED_BYTES // (1024 * 1024)} MB): {document.name}")


def convert(path):
    """Convert one document in memory/raw/ and write its .md file. Return the .md path.

    Raise ValueError with a clear message when the file cannot be converted.
    """
    document = Path(path).resolve()
    if not document.is_relative_to(memory.RAW_DIR) or not document.is_file():
        raise ValueError(f"Not a file in {memory.RAW_DIR}: {path}")
    if document.suffix.lower() not in DOCUMENT_SUFFIXES:
        raise ValueError(f"Not a document ({', '.join(DOCUMENT_SUFFIXES)}): {document.name}")
    if document.stat().st_size > MAX_BYTES:
        raise ValueError(f"Too big (over {MAX_BYTES // (1024 * 1024)} MB): {document.name}")
    if document.suffix.lower() in ZIP_SUFFIXES:
        check_unzipped_size(document)
    try:
        text = to_markdown(document)
    except Exception as error:  # markitdown raises many kinds of errors for broken files
        raise ValueError(f"Could not convert {document.name}: {error}") from error
    if not (text or "").strip():
        raise ValueError(f"No text found in {document.name} (a scanned PDF needs OCR first)")
    target = markdown_path(document)
    time = datetime.now().isoformat(timespec="seconds")
    # The comment says where the text came from. It is the first line, so the
    # agent that ingests the file sees it too.
    memory.write_atomic(target, f"<!-- Converted from {document.name} by markitdown, {time}. -->\n\n{text.strip()}\n")
    return target


def new_documents():
    """Return the documents in memory/raw/ that have no .md file yet."""
    if not memory.RAW_DIR.exists():
        return []
    return [path for path in sorted(memory.RAW_DIR.rglob("*"))
            if path.is_file() and path.suffix.lower() in DOCUMENT_SUFFIXES
            and not markdown_path(path).exists()]


def convert_new():
    """Convert every new document. Return (converted .md paths, problems as text).

    One bad file never stops the others.
    """
    converted, problems = [], []
    for document in new_documents():
        try:
            converted.append(convert(document))
        except ValueError as error:
            problems.append(str(error))
        except OSError as error:  # a very long name, a read-only folder: skip this one only
            problems.append(f"{document.name}: {error.strerror or error}")
    return converted, problems


def main():
    parser = argparse.ArgumentParser(description="Convert documents in memory/raw/ to Markdown.")
    commands = parser.add_subparsers(dest="command", required=True)
    convert_parser = commands.add_parser("convert", help="convert one document")
    convert_parser.add_argument("path")
    commands.add_parser("convert-new", help="convert every document that has no .md file yet")
    child_parser = commands.add_parser("child", help=argparse.SUPPRESS)  # used by to_markdown only
    child_parser.add_argument("path")
    args = parser.parse_args()

    if args.command == "child":
        child(args.path)
        return

    if args.command == "convert":
        try:
            target = convert(args.path)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print(f"Wrote {target.relative_to(memory.RAW_DIR)}")
    elif args.command == "convert-new":
        converted, problems = convert_new()
        for target in converted:
            print(f"Wrote {target.relative_to(memory.RAW_DIR)}")
        for problem in problems:
            print(f"Skipped: {problem}")
        if not converted and not problems:
            print("No new documents.")


if __name__ == "__main__":
    main()
