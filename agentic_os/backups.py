"""Backups of data/: one compressed archive a day, the newest KEEP kept.

data/ holds the
jobs, the lead sessions and handoffs, the lessons, the undo copies and the
settings, and it is not in git. A backup is one .tar.gz file in backups/,
next to data/ (not inside it, so a restore can replace data/ as a whole).
backups/ is not in git either.

It is exact work, so it is Python (the tarfile module), no AI, no cost. The
daily routine action `backup` (routines/daily-backup.md) runs it.

Restore by hand, when nothing runs (stop the web server first):
    mv data data-before-restore
    tar -xzf backups/<name>.tar.gz

Usage (from the project folder):
    python -m agentic_os.backups now     make a backup now
    python -m agentic_os.backups list    the backups, newest first
"""

import argparse
import io
import os
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
BACKUPS_DIR = PROJECT_DIR / "backups"
KEEP = 7  # the newest backups that stay; older ones are deleted
PREFIX = "agentic-os-data-"


def skipped(path):
    """Return True for files that do not go into a backup: temporary files, locks, logs, and
    the web token (a key; the server makes a new one after a restore),
    and the mail password (mail.py: run `mail setup` again after a restore)."""
    name = path.name
    # Logs too: the server prints its login link (with the token), and `tee` may keep it in data/.
    return (name.endswith((".tmp", ".lock", ".log")) or name == "lock"
            or path in (DATA_DIR / "app-token", DATA_DIR / "mail.json"))


def backup_now(now=None):
    """Write a new backup of data/. Return its path.

    The archive is written under a temporary name and renamed at the end, so a
    half-written archive never looks like a backup. Each file is read whole first and
    then archived from memory: a file that is replaced meanwhile (a job is saved) goes
    in whole, old or new, never cut. A file that disappears while we read it is
    skipped; the next backup has it.
    """
    if not DATA_DIR.is_dir():
        raise ValueError(f"No data folder: {DATA_DIR}")
    BACKUPS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    BACKUPS_DIR.chmod(0o700)  # an old folder may have 0775: the jobs and sessions are private
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S")
    target = BACKUPS_DIR / f"{PREFIX}{stamp}.tar.gz"
    # A unique temp name (mode 0600): two backups in the same second never share one.
    handle, temp_name = tempfile.mkstemp(dir=BACKUPS_DIR, prefix=f".{target.name}.", suffix=".tmp")
    os.close(handle)
    temp = Path(temp_name)
    try:
        with tarfile.open(temp, "w:gz") as archive:
            for path in sorted(DATA_DIR.rglob("*")):
                if path.is_symlink() or not path.is_file() or skipped(path):
                    continue
                try:
                    info = archive.gettarinfo(path, arcname=str(path.relative_to(PROJECT_DIR)))
                    data = path.read_bytes()
                except FileNotFoundError:
                    continue
                info.size = len(data)  # what we read, not what stat saw a moment before
                archive.addfile(info, io.BytesIO(data))
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    prune()
    for path in all_backups():
        path.chmod(0o600)  # older archives may be 0664
    return target


def all_backups():
    """Return the backup files, newest first."""
    if not BACKUPS_DIR.is_dir():
        return []
    return sorted(BACKUPS_DIR.glob(f"{PREFIX}*.tar.gz"), reverse=True)  # the names sort by time


def prune(keep=KEEP):
    """Delete all but the newest `keep` backups. Return the deleted names."""
    old = all_backups()[keep:]
    for path in old:
        path.unlink(missing_ok=True)
    return [path.name for path in old]


def routine_action(limit):
    """The daily `backup` routine action. It adds no jobs (it costs nothing)."""
    path = backup_now()
    print(f"Backup: {path.name} ({path.stat().st_size // 1024} KB)")
    return []


def main():
    parser = argparse.ArgumentParser(description="Backups of data/ (jobs, sessions, lessons, undo copies).")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("now", help="make a backup now")
    commands.add_parser("list", help="the backups, newest first")
    args = parser.parse_args()
    if args.command == "now":
        try:
            path = backup_now()
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print(f"Saved {path.relative_to(PROJECT_DIR)} ({path.stat().st_size // 1024} KB). "
              f"The newest {KEEP} backups are kept.")
        return
    found = all_backups()
    if not found:
        print("No backups yet. Make one with: python -m agentic_os.backups now")
    for path in found:
        print(f"{path.name}  {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
