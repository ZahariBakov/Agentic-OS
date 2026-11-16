"""Artifacts: the outputs of finished jobs, as Markdown files in data/artifacts/.

    data/artifacts/<job id>.md   one file per job: a short header, then the result
    data/artifacts/INDEX.md      one line per artifact, newest first (made by Python)

With the index, you can find an old result without spending tokens:
no agent has to search, Python reads one small file.

Usage (from the project folder):
    python -m agentic_os.artifacts
"""

from pathlib import Path

from agentic_os.memory import file_lock, read_note, write_atomic

ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "data" / "artifacts"
INDEX_FILE = ARTIFACTS_DIR / "INDEX.md"


def title(task):
    """Return a short one-line title for a task."""
    line = " ".join(task.split())
    return line if len(line) <= 70 else line[:67] + "..."


def save(job):
    """Save a finished job's result as an artifact, then rebuild the index."""
    header = {
        "title": title(job.task),
        "job": job.id,
        "date": job.created_at[:16].replace("T", " "),
        "lead": job.lead or "-",
        "model": job.model,
        "cost": f"{job.cost:.4f}",
    }
    front = "".join(f"{key}: {value}\n" for key, value in header.items())
    # A file lock: the web server and `jobs run` (two processes) both save artifacts.
    with file_lock(ARTIFACTS_DIR / ".lock"):
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        write_atomic(ARTIFACTS_DIR / f"{job.id}.md", f"---\n{front}---\n\n{job.result.strip()}\n")
        rebuild_index()


def all_artifacts():
    """Return the headers of all artifacts as dicts, newest first.

    A Markdown file without our header (not made by save) is skipped, so one
    stray file cannot break the index and every later save.
    """
    items = []
    for path in ARTIFACTS_DIR.glob("*.md"):
        if path != INDEX_FILE:
            meta, _ = read_note(path)
            if all(key in meta for key in ("job", "date", "title", "lead", "cost")):
                items.append(meta)
    return sorted(items, key=lambda meta: meta.get("date", ""), reverse=True)


def read(job_id):
    """Return (header, text) of one artifact, or None."""
    path = ARTIFACTS_DIR / f"{job_id}.md"
    # A job id has only letters and digits, so nobody can read ../../secret files.
    if not job_id.isalnum() or not path.exists():
        return None
    return read_note(path)


def rebuild_index():
    """Write INDEX.md: one line per artifact."""
    lines = ["# Artifacts", "", "Made by Python. Do not edit by hand.", ""]
    for meta in all_artifacts():
        lines.append(f"- {meta['date']}  [{meta['title']}]({meta['job']}.md)  {meta['lead']}  ${meta['cost']}")
    write_atomic(INDEX_FILE, "\n".join(lines) + "\n")


def main():
    for meta in all_artifacts():
        print(f"{meta['date']}  {meta['job']}  ${meta['cost']}  {meta['title']}")


if __name__ == "__main__":
    main()
