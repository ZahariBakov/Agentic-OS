"""Changes: a copy of each file before an agent's first edit in a job, the diff, and undo.

A PreToolUse hook (edit_hook.py) calls snapshot_before() before every Edit or
Write. Only the first edit of a file in a job saves a copy, so the copy is the
file as it was before the job. Later we can show what the job changed and put
the old files back.

Data, one folder per job:
    data/snapshots/<job id>/index.json   the files: path, copy name, existed
    data/snapshots/<job id>/<copy name>  the file before the first edit

Limits: an edit through Bash (for example `sed -i`) does not go through the
hook, so it is not saved. Undo puts back the file as it was before the job,
so it also removes changes that someone made after the job.

Old copies are deleted (`prune`, also the daily `prune-snapshots` routine
action): a job's folder goes when the job has finished (done, failed, stopped)
more than KEEP_DAYS days ago and nothing in the folder changed since then. After that the job's
changes can no longer be shown or undone.

Usage (from the project folder):
    python -m agentic_os.changes list <job id>
    python -m agentic_os.changes diff <job id> [file]
    python -m agentic_os.changes undo <job id> [file]
    python -m agentic_os.changes prune [--days 30]
"""

import argparse
import difflib
import hashlib
import json
import shutil
import time
from datetime import datetime
from pathlib import Path

from agentic_os import memory, safety

PROJECT_DIR = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = PROJECT_DIR / "data" / "snapshots"
MAX_DIFF = 150_000  # characters; a longer diff is cut
KEEP_DAYS = 30  # a finished job's copies are deleted after this many days


def job_folder(job_id):
    """Return the snapshot folder of a job. The id must be letters and digits only."""
    if not job_id or not job_id.isalnum():
        raise ValueError(f"bad job id: {job_id!r}")
    return SNAPSHOTS_DIR / job_id


def read_index(folder):
    """Return the list of saved files of a job (empty if there are none)."""
    index = folder / "index.json"
    return json.loads(index.read_text())["files"] if index.exists() else []


def write_index(folder, files):
    memory.write_atomic(folder / "index.json", json.dumps({"files": files}, indent=2))


def skipped(path):
    """Return True for files we never save: our own data and the protected files.

    Agents cannot edit the protected files (a deny rule). But the hook runs
    before that rule, and undo would later write back an old copy over your
    own newer changes. So we do not save them at all.
    """
    folders, files = safety.protected()
    if path in [Path(file).resolve() for file in files]:
        return True
    return any(path.is_relative_to(Path(folder).resolve()) for folder in [*folders, SNAPSHOTS_DIR])


def snapshot_before(job_id, file):
    """Save a copy of the file before the job's first edit of it.

    Return True if this call saved it, False if it was saved before or skipped.
    The agent can run tools in parallel, so the index is changed under a lock.
    """
    path = Path(file).resolve()
    if skipped(path):
        return False
    folder = job_folder(job_id)
    with memory.file_lock(folder / ".lock"):
        files = read_index(folder)
        if any(entry["path"] == str(path) for entry in files):
            return False  # only the first edit counts
        copy = hashlib.sha1(str(path).encode()).hexdigest()[:16]
        existed = path.is_file()
        if existed:
            shutil.copy2(path, folder / copy)
        files.append({"path": str(path), "copy": copy, "existed": existed,
                      "at": datetime.now().isoformat(timespec="seconds")})
        write_index(folder, files)
    return True


def forget(job_id, folder):
    """Delete the job's saved copies of files inside `folder`. Return how many.

    For a job's isolated workspace (workspaces.py): when it is made again, applied or
    discarded, the copies are of a worktree that is gone. A new worktree has the same
    path, so an old copy would look like "before the job" there, and Undo (and the next
    Apply) would remove the work of an earlier run.
    """
    folder = Path(folder).resolve()
    snapshots = job_folder(job_id)
    if not snapshots.is_dir():
        return 0
    with memory.file_lock(snapshots / ".lock"):
        files = read_index(snapshots)
        kept = [entry for entry in files if not Path(entry["path"]).is_relative_to(folder)]
        for entry in files:
            if entry not in kept:
                (snapshots / entry["copy"]).unlink(missing_ok=True)
        if len(kept) != len(files):
            write_index(snapshots, kept)
    return len(files) - len(kept)


def read_text(path):
    """Return the text of a file, or "" if it does not exist."""
    return path.read_text(errors="replace") if path.is_file() else ""


def changes(job_id, with_diff=True):
    """Return what the job changed: one dict per file.

    state: "added" (the agent made it), "deleted", "modified" or "unchanged".
    diff: a unified diff from "before" to "now" (None when with_diff is False: a caller
    that needs only the paths and states does not pay for the diffs).
    """
    folder = job_folder(job_id)
    result = []
    for entry in read_index(folder):
        path = Path(entry["path"])
        before = read_text(folder / entry["copy"]) if entry["existed"] else ""
        now = read_text(path)
        exists = path.is_file()
        diff = None
        if with_diff:
            diff = "".join(difflib.unified_diff(
                before.splitlines(keepends=True), now.splitlines(keepends=True), "before", "now"))
            if len(diff) > MAX_DIFF:
                diff = diff[:MAX_DIFF] + "\n... (diff cut here)\n"
        if not entry["existed"]:
            state = "added" if exists else "unchanged"
        elif not exists:
            state = "deleted"
        else:
            state = "modified" if before != now else "unchanged"
        result.append({"path": str(path), "state": state, "diff": diff, "at": entry["at"]})
    return result


def undo(job_id, file=None):
    """Put the files back as they were before the job. Return the paths that changed.

    With a file, undo only that file. A file the agent made is deleted.
    Files that are the same as before are not touched.
    """
    folder = job_folder(job_id)
    wanted = None if file is None else str(Path(file).resolve())
    done = []
    with memory.file_lock(folder / ".lock"):
        for entry, change in zip(read_index(folder), changes(job_id, with_diff=False)):
            if wanted is not None and entry["path"] != wanted:
                continue
            if change["state"] == "unchanged":
                continue
            path = Path(entry["path"])
            if entry["existed"]:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Not copy2: it would give the file its old time, and make or ninja would
                # think it is older than the build and skip the rebuild.
                shutil.copyfile(folder / entry["copy"], path)
                shutil.copymode(folder / entry["copy"], path)
            else:
                path.unlink(missing_ok=True)
            done.append(str(path))
    return done


def last_change(folder):
    """Return the time (seconds) of the newest file in a job's snapshot folder.

    The .lock file is left out: taking the lock (undo) creates it new. A file that
    goes away while we look (a temp file of an atomic write) is left out too.
    """
    times = []
    for path in folder.iterdir():
        if path.name == ".lock":
            continue
        try:
            times.append(path.stat().st_mtime)
        except FileNotFoundError:
            continue
    return max(times, default=folder.stat().st_mtime)


def finished_time(job):
    """Return when the job finished, in seconds, or None (old jobs have no finished_at)."""
    try:
        return datetime.fromisoformat(job.finished_at).timestamp()
    except (TypeError, ValueError):
        return None


def prune(days=KEEP_DAYS, now=None):
    """Delete the snapshot folders of finished jobs older than `days`. Return the job ids.

    Old means: nothing in the folder changed, and the job did not finish, in the
    last `days` days. A job that is queued, running, waiting or interrupted keeps
    its copies, whatever their age: it may still change files, and its undo still
    needs them. A job file we cannot read also keeps them (we do not know its
    state). A folder whose job file is gone is deleted when it is old.
    """
    from agentic_os import job_store, jobs  # here, not at the top: they import this module
    if days < 1:
        raise ValueError("days must be 1 or more")
    cutoff = (now if now is not None else time.time()) - days * 86400
    removed = []
    if not SNAPSHOTS_DIR.is_dir() or not job_store.JOBS_DIR.is_dir():
        return removed  # no jobs folder: every job would look "gone", so do nothing
    for folder in sorted(SNAPSHOTS_DIR.iterdir()):
        if folder.is_symlink() or not folder.is_dir() or not folder.name.isalnum():
            continue  # not a job folder: never delete what we did not make
        if last_change(folder) >= cutoff:
            continue  # the cheap check first, without a lock
        # The claim lock: no job can go back to the queue (follow-up, answer, resume)
        # or be undone while we look at it and delete. A finished job's agent does
        # not run, so no edit hook writes to the folder either.
        with jobs.claim_lock():
            try:
                job = jobs.load(folder.name)
            except FileNotFoundError:
                job = None  # the job is gone; its copies are of no use
            except (OSError, ValueError, TypeError):
                continue  # a broken or newer job file: keep the copies
            if job is not None:
                if job.status not in job_store.FINISHED:
                    continue
                finished = finished_time(job)
                if finished is not None and finished >= cutoff:
                    continue  # it finished lately (for example after a long wait)
            shutil.rmtree(folder)
        removed.append(folder.name)
    return removed


def prune_message(removed):
    """Return the text that says what prune deleted."""
    return f"Snapshots: deleted the copies of {len(removed)} old job(s)" + (
        f": {', '.join(removed)}" if removed else ".")


def routine_action(limit):
    """The daily `prune-snapshots` routine action. It adds no jobs (it costs nothing)."""
    print(prune_message(prune()))
    return []


def none_message(job_id, text):
    """Return `text` ("Nothing to undo." etc.), plus a note when the copies may have been pruned."""
    from agentic_os import jobs  # here, not at the top: jobs imports this module
    try:
        finished = finished_time(jobs.load(job_id))
    except (OSError, ValueError, TypeError):
        return text
    if finished is not None and finished < time.time() - KEEP_DAYS * 86400:
        return f"{text} (The copies of a job are deleted {KEEP_DAYS} days after it finishes.)"
    return text


def main():
    parser = argparse.ArgumentParser(description="See and undo the file changes of a job.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="the changed files").add_argument("job_id")
    diff_parser = commands.add_parser("diff", help="the diff of every file, or of one file")
    diff_parser.add_argument("job_id")
    diff_parser.add_argument("file", nargs="?")
    undo_parser = commands.add_parser("undo", help="put the files back as they were before the job")
    undo_parser.add_argument("job_id")
    undo_parser.add_argument("file", nargs="?")
    prune_parser = commands.add_parser("prune", help="delete the copies of finished jobs older than --days")
    prune_parser.add_argument("--days", type=int, default=KEEP_DAYS, help="keep copies this many days (1 or more)")
    args = parser.parse_args()

    if args.command == "prune":
        try:
            print(prune_message(prune(args.days)))
        except ValueError as error:
            raise SystemExit(str(error)) from error
        return

    if args.command == "undo":
        from agentic_os import jobs  # here, not at the top: jobs imports this module
        try:
            done = jobs.undo(args.job_id, args.file)  # checks the status; marks the job "undone"
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print("\n".join(f"Undone: {path}" for path in done) or none_message(args.job_id, "Nothing to undo."))
        return
    found = changes(args.job_id)
    if args.command == "diff" and args.file:
        found = [change for change in found if change["path"] == str(Path(args.file).resolve())]
    if not found:
        print(none_message(args.job_id, "No saved changes for this job."))
    for change in found:
        print(f"{change['state']:<10} {change['path']}")
        if args.command == "diff" and change["diff"]:
            print(change["diff"])


if __name__ == "__main__":
    main()
