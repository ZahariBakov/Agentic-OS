"""Isolated workspaces: a job on a git project edits its own git worktree, not the project.

Without it, an
agent (the lead Ada works in this very project) writes straight into the live
project, and two jobs on one project can overwrite each other.

How it works:
1. Before the run, Python makes a git worktree in WORKTREES_DIR/<job id>,
   detached (no branch). It starts from the project as it is now: HEAD plus the
   uncommitted changes and the untracked files. Ignored folders that a check
   needs (.venv, node_modules) are links to the project's own.
2. The agent still runs in its usual folder (Claude Code keeps sessions per
   folder, so a lead keeps its session), but it may edit only in the worktree
   (the allow rule and the edit hook use edit_folder). The check runs there too.
3. When the job ends, the user looks at the diff and applies it to the project
   (`git apply`; when the project changed meanwhile, a 3-way merge per file
   with conflict markers) or discards it. Nothing is committed.

job.workspace = {"path", "worktree", "repo", "sub", "base_tree", "status", "created_at", "files",
and "check_files": new files that only the check made (a build folder), never in the diff}
status: active, applied, conflicts (applied with conflict markers), discarded.

Modes (data/settings.json "isolate_jobs"): "always" (the default), "shared" (only when another unfinished job works
on the same project) or "off".

This module does not import jobs (jobs imports it); the caller saves the job.

Usage (from the project folder):
    python -m agentic_os.workspaces show <job id>       the changed files and the diff
    python -m agentic_os.workspaces apply <job id>      put the changes into the project
    python -m agentic_os.workspaces discard <job id>    remove the workspace, change nothing
    python -m agentic_os.workspaces mode [always|shared|off]
"""

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from agentic_os import safety

WORKTREES_DIR = Path.home() / ".cache" / "agentic-os" / "worktrees"
MODES = ("always", "shared", "off")
DEFAULT_MODE = "always"
LINKED = (".venv", "node_modules")  # ignored folders that checks need: links, not copies
MAX_UNTRACKED = 2000  # untracked files copied at most
MAX_DIFF = 400_000  # characters of diff shown
UNFINISHED = ("queued", "running", "waiting", "interrupted")
WORKSPACE_NOTE = """

[Isolated workspace: edit files ONLY in {path} (a separate copy of the project; the
original folder is read-only for you). Read from there too, so you see your own
changes. Do not commit; the user reviews your changes and applies them.]"""


class WorkspaceError(ValueError):
    """A workspace that cannot be made, applied or discarded."""


def git(folder, *args, stdin=None, env=None, text_mode=True):
    """Run git in a folder. Return the CompletedProcess (never raises for an exit code).

    text_mode=False: stdout is bytes (file contents).
    """
    return subprocess.run(["git", *args], cwd=folder, input=stdin, capture_output=True, text=text_mode,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}, timeout=300)


def repo_of(folder):
    """Return the top folder of the git repository that holds `folder`, or None.

    A folder that does not exist (deleted after its job was added) is in no repository.
    """
    if not Path(folder).is_dir():
        return None  # git cannot even start there (FileNotFoundError)
    done = git(folder, "rev-parse", "--show-toplevel")
    return Path(done.stdout.strip()) if done.returncode == 0 else None


def active(job):
    """True when the job has a workspace whose changes are not applied or discarded yet."""
    return bool(job.workspace) and job.workspace["status"] == "active"


def wanted(job, folder, mode, other_jobs):
    """Return True if the job should get a workspace.

    folder: where the job's agent works. other_jobs: a function that returns all jobs
    (called only for "shared": reading every job file is not free).
    Only an agent that can edit, in a folder inside a git repository.
    """
    if mode == "off" or not folder:
        return False
    repo = repo_of(folder)  # git runs once here
    if repo is None:
        return False
    if mode == "always":
        return True
    return any(other.id != job.id and other.status in UNFINISHED and other.folder
               and repo_of(other.folder) == repo for other in other_jobs())


def check_link(worktree, repo):
    """Refuse a worktree whose .git file does not point into the repository's own .git folder.

    The agent edits the worktree. If it changed .git to point to another git folder,
    that folder's config could run commands (core.fsmonitor) at our next `git add`.
    """
    link = Path(worktree) / ".git"
    expected = f"gitdir: {Path(repo).resolve() / '.git' / 'worktrees'}/"
    try:
        text = "" if link.is_symlink() else link.read_text()
    except OSError:
        text = ""
    if not text.startswith(expected) or "\n" in text.strip() or ".." in text:
        raise WorkspaceError(f"The workspace {worktree} is not safe to read: its .git file was changed.")


def snapshot_tree(worktree, repo, skip=()):
    """Return the worktree's files now as a git tree id.

    skip: paths (relative to the worktree) left out of the tree: the files the check made.
    A throwaway index in its own new folder: each call has its own, so a page
    refresh during Apply cannot delete the index that Apply is using.
    """
    check_link(worktree, repo)
    temp = Path(tempfile.mkdtemp(prefix=f".index-{worktree.name}-", dir=worktree.parent))
    env = {"GIT_INDEX_FILE": str(temp / "index")}
    try:
        # Start from the files git tracks (HEAD). With an empty index, `add -A` would drop a
        # tracked file that a new .gitignore line ignores, and Apply would delete it.
        if git(worktree, "read-tree", "HEAD", env=env).returncode != 0:
            raise WorkspaceError(f"git could not read the workspace {worktree}")
        done = git(worktree, "add", "-A", "--", ".", env=env)
        if done.returncode != 0:
            raise WorkspaceError(f"git could not read the workspace {worktree}: {done.stderr.strip()[:300]}")
        # The linked folders (LINKED) and the check's files are taken out after the add. Not with an
        # ':(exclude).venv' pathspec: git 2.25 refuses it when .venv is an ignored link (job f694068c).
        # On stdin, not as arguments: a build folder can have thousands of files.
        done = git(worktree, "update-index", "--force-remove", "-z", "--stdin", env=env,
                   stdin="".join(f"{path}\0" for path in (*LINKED, *skip)))
        if done.returncode != 0:
            raise WorkspaceError(f"git could not leave out the linked folders or the check's files: "
                                 f"{done.stderr.strip()[:300]}")
        done = git(worktree, "write-tree", env=env)
        if done.returncode != 0:
            raise WorkspaceError(f"git could not read the workspace {worktree}")
        return done.stdout.strip()
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def create(job, folder, now):
    """Make the job's worktree from the project as it is now. Return the workspace dict.

    When a step fails, the half-made worktree is removed, so no git record stays behind.
    """
    check_rerun(job)
    folder = Path(folder).resolve()
    repo = repo_of(folder)
    if repo is None:
        raise WorkspaceError(f"{folder} is not in a git repository")
    if git(repo, "rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
        # After `git init` and before the first commit there is no HEAD to start from
        # (git would only say "invalid reference: HEAD").
        raise WorkspaceError(f"The git repository {repo} has no commit yet. Make the first commit "
                             "(git add, then git commit -m \"...\"), then run the job again.")
    worktree = WORKTREES_DIR / job.id
    if worktree.exists():  # an old one of this job (for example after a crash)
        remove_worktree(repo, worktree)
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    done = git(repo, "worktree", "add", "--detach", "--quiet", str(worktree), "HEAD")
    if done.returncode != 0:
        raise WorkspaceError(f"git worktree add failed: {done.stderr.strip()[:300]}")
    try:
        copy_uncommitted(repo, worktree)
        sub = folder.relative_to(repo)
        return {"path": str(worktree / sub), "worktree": str(worktree), "repo": str(repo), "sub": str(sub),
                "base_tree": snapshot_tree(worktree, repo), "status": "active", "created_at": now, "files": []}
    except (OSError, WorkspaceError) as error:
        remove_worktree(repo, worktree)
        raise WorkspaceError(f"The workspace could not be made: {error}") from error


def copy_uncommitted(repo, worktree):
    """Copy what is not committed yet into a new worktree: the job starts from the project
    as the user sees it. Raise WorkspaceError (or OSError) when a part cannot be copied:
    a job that silently starts from HEAD would undo the user's work at Apply."""
    diff = git(repo, "diff", "HEAD", "--binary")
    if diff.returncode != 0:
        raise WorkspaceError(f"git diff failed: {diff.stderr.strip()[:300]}")
    if diff.stdout:
        done = git(worktree, "apply", "--binary", "--whitespace=nowarn", stdin=diff.stdout)
        if done.returncode != 0:
            raise WorkspaceError(f"your uncommitted changes did not apply: {done.stderr.strip()[:300]}")
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
    for name in [name for name in untracked if name][:MAX_UNTRACKED]:
        source = repo / name
        if source.is_file() and not source.is_symlink():
            target = worktree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for name in LINKED:
        if (repo / name).is_dir() and not (worktree / name).exists():
            (worktree / name).symlink_to(repo / name)


def check_rerun(job):
    """Refuse a new run while the job's workspace has conflicts.

    A new run makes a new worktree; the old one (with the job's version of the
    conflicting files) and the list of conflicts would be lost without a word.
    """
    if job.workspace and job.workspace["status"] == "conflicts":
        raise WorkspaceError(f"Job {job.id} has conflicts in the project ({', '.join(job.workspace.get('conflicts', []))}). "
                             "Fix the <<<<<<< markers, then remove its workspace (Discard), then write again.")


def note(job):
    """Return the text added to every prompt of a job with an active workspace, else ""."""
    return WORKSPACE_NOTE.format(path=job.workspace["path"]) if active(job) else ""


def patch(job):
    """Return (the changed files [{"state", "path"}], the binary patch) since the worktree was made.

    state is git's letter: A added, M modified, D deleted.
    """
    space = job.workspace
    worktree = Path(space["worktree"])
    if not worktree.is_dir():
        raise WorkspaceError("The workspace folder is gone.")
    tree = snapshot_tree(worktree, space["repo"], space.get("check_files", []))
    lines = git(worktree, "diff", "--name-status", "--no-renames", space["base_tree"], tree).stdout.splitlines()
    files = [{"state": line.split("\t", 1)[0], "path": line.split("\t", 1)[-1]} for line in lines if line.strip()]
    text = git(worktree, "diff", "--binary", "--no-renames", space["base_tree"], tree).stdout
    return files, text


def new_files(job):
    """Return the files in the job's worktree that git does not know and does not ignore.

    Call it before and after the check: what is new after it, the check made (see check_made).
    """
    done = git(job.workspace["worktree"], "ls-files", "--others", "--exclude-standard", "-z")
    if done.returncode != 0:
        raise WorkspaceError(f"git could not list the workspace's files: {done.stderr.strip()[:300]}")
    return {name for name in done.stdout.split("\0") if name}


def check_made(job, before):
    """Remember the files that the check made (new since `before`): they never go into the diff.

    A check like `cmake -B build && ctest` builds inside the worktree. Without this, the build
    folder would be a change of the job, and Apply would copy it into the project (with a
    CMakeCache.txt that points to the worktree and breaks the next build there).
    A file the agent made is new before the check, so it stays in the diff. The caller saves the job.
    """
    made = new_files(job) - before
    if made:
        job.workspace["check_files"] = sorted(set(job.workspace.get("check_files", [])) | made)


def review(job):
    """Return what the workspace changed: {"status", "path", "files", "diff"}."""
    space = job.workspace
    if not space:
        raise WorkspaceError(f"Job {job.id} has no workspace.")
    if space["status"] != "active":
        return {"status": space["status"], "path": space["path"], "files": space.get("files", []), "diff": "",
                "conflicts": space.get("conflicts", [])}
    files, text = patch(job)
    if len(text) > MAX_DIFF:
        text = text[:MAX_DIFF] + "\n... (diff cut here)\n"
    return {"status": "active", "path": space["path"], "files": files, "diff": text}


def remove_worktree(repo, worktree):
    """Remove a worktree folder and git's record of it. Only our own folders.

    Not `git worktree prune`: it would also delete the records of the user's own
    worktrees whose folder is missing (for example on a USB disk that is not plugged in).
    """
    worktree = Path(worktree)
    if not worktree.resolve().is_relative_to(WORKTREES_DIR.resolve()):
        raise WorkspaceError(f"Not a workspace folder: {worktree}")
    git(repo, "worktree", "remove", "--force", str(worktree))
    if worktree.exists():
        shutil.rmtree(worktree)
    forget_record(repo, worktree)


def forget_record(repo, worktree):
    """Delete git's record (.git/worktrees/<name>) of this one worktree, if it is still there.

    `git worktree remove` deletes it, but not when the folder was already gone.
    Each record has a gitdir file with the path of the worktree's .git file.
    """
    records = Path(repo) / ".git" / "worktrees"
    if not records.is_dir():
        return
    wanted_link = Path(worktree).resolve() / ".git"
    for record in records.iterdir():
        try:
            target = Path((record / "gitdir").read_text().strip())
        except OSError:
            continue
        if target == wanted_link and not record.is_symlink():
            shutil.rmtree(record)


def check_finished(job, action):
    if job.status in ("queued", "running", "waiting"):
        raise WorkspaceError(f"Job {job.id} is still working: {action} its changes when it has finished (or stop it).")
    if not active(job):
        raise WorkspaceError(f"Job {job.id} has no workspace to {action}.")


def merge_files(space, files):
    """Merge the workspace's changed files into the project, one by one. Return the conflicting paths.

    For each file: base = the file when the workspace was made, ours = the project's
    file now, theirs = the workspace's file. `git merge-file` writes <<<<<<< markers
    where both sides changed the same lines. When it cannot merge (a binary file, or a
    file one side deleted and the other changed), the project file stays and the job's
    version is written next to it as <path>.from-job.
    """
    repo, worktree = Path(space["repo"]), Path(space["worktree"])
    conflicts = []
    for item in files:
        path = item["path"]
        ours, theirs = repo / path, worktree / path
        # None = the file did not exist (a file the job added has no base)
        found = git(worktree, "cat-file", "blob", f"{space['base_tree']}:{path}", text_mode=False)
        base = found.stdout if found.returncode == 0 else None
        mine = ours.read_bytes() if ours.is_file() else None
        job_version = theirs.read_bytes() if theirs.is_file() else None
        if mine == base or mine == job_version:
            # The project did not change this file (or has the job's version already).
            if job_version is not None:
                ours.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(theirs, ours)
            elif ours.is_file():
                ours.unlink()
            continue
        if mine is not None and job_version is not None:
            # Both changed it (or both added it): an added file merges against an empty base.
            merged = merge_file(mine, base or b"", job_version, worktree)
            if merged is not None:
                ours.write_bytes(merged[0])
                if merged[1]:
                    conflicts.append(path)
                continue
        if job_version is not None:
            ours.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(theirs, ours.with_name(ours.name + ".from-job"))
        conflicts.append(path)
    return conflicts


def merge_file(ours, base, theirs, folder):
    """Return (the merged bytes, True when it has conflict markers), or None when git cannot merge."""
    temp = folder.parent / f".merge-{folder.name}-{os.getpid()}"
    temp.mkdir(exist_ok=True)
    try:
        names = []
        for name, data in (("project", ours), ("base", base), ("job", theirs)):
            (temp / name).write_bytes(data)
            names.append(str(temp / name))
        done = git(temp, "merge-file", "-p", "-L", "project", "-L", "base", "-L", "job", *names, text_mode=False)
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    if done.returncode < 0 or done.returncode > 127:  # an error (a binary file), not a conflict count
        return None
    return done.stdout, done.returncode > 0


def apply(job, now):
    """Put the workspace's changes into the project. Change job.workspace; the caller saves the job.

    Return a message. Plain `git apply` first; when the project changed meanwhile,
    a 3-way merge leaves conflict markers (status "conflicts") to resolve by hand.
    """
    check_finished(job, "apply")
    space = job.workspace
    files, text = patch(job)
    repo = Path(space["repo"])
    if repo.resolve() == safety.PROJECT_DIR:
        # The deny rules cover the workspace too; this is the last line if they ever miss.
        refused = [item["path"] for item in files if safety.is_protected(safety.PROJECT_DIR / item["path"])]
        if refused:
            raise WorkspaceError(f"Job {job.id} changed protected files ({', '.join(refused)}). Nothing was "
                                 f"applied; look at the workspace in {space['worktree']} or discard it.")
    space["files"] = files
    if text.strip() and git(repo, "apply", "--binary", "--whitespace=nowarn", stdin=text).returncode != 0:
        # The project changed meanwhile. `git apply --3way` needs a clean index, and the
        # user's uncommitted work is the usual case here, so merge file by file instead.
        conflicts = merge_files(space, files)
        if conflicts:
            space.update(status="conflicts", applied_at=now, conflicts=conflicts)
            return (f"Applied {len(files)} file change(s) to {repo} WITH CONFLICTS in {', '.join(conflicts)}: "
                    f"the project changed meanwhile. Look for <<<<<<< markers (a binary or deleted file: "
                    f"the job's version is next to it as .from-job). "
                    f"The workspace stays in {space['worktree']}.")
    space.update(status="applied", applied_at=now)
    remove_worktree(repo, space["worktree"])
    return f"Applied {len(files)} file change(s) to {repo}. Nothing was committed."


def drop_if_unchanged(job, now):
    """Remove an active workspace in which the job changed nothing. Return True if removed.

    Called at the end of a finished run: such a workspace has nothing to apply, and
    the Today panel would ask you to apply or discard it for nothing.
    """
    if not active(job) or patch(job)[0]:
        return False
    remove_worktree(job.workspace["repo"], job.workspace["worktree"])
    job.workspace.update(status="discarded", discarded_at=now)
    return True


def discard(job, now):
    """Remove the workspace; the project does not change. The caller saves the job."""
    space = job.workspace
    if not space or space["status"] not in ("active", "conflicts"):
        raise WorkspaceError(f"Job {job.id} has no workspace to discard.")
    if job.status in ("queued", "running", "waiting"):
        raise WorkspaceError(f"Job {job.id} is still working: stop it before you discard its workspace.")
    remove_worktree(space["repo"], space["worktree"])
    space.update(status="discarded" if space["status"] == "active" else "applied", discarded_at=now)
    return "The workspace was removed; the project did not change."


def main():
    # Imported here: jobs imports this module.
    from agentic_os import jobs

    parser = argparse.ArgumentParser(description="Isolated workspaces (git worktrees) of jobs.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("show", "the changed files and the diff"), ("apply", "put the changes into the project"),
                       ("discard", "remove the workspace, change nothing")):
        commands.add_parser(name, help=text).add_argument("job_id")
    mode_parser = commands.add_parser("mode", help="show or set when jobs get a workspace")
    mode_parser.add_argument("mode", nargs="?", choices=MODES)
    args = parser.parse_args()

    try:
        if args.command == "mode":
            if args.mode:
                jobs.set_setting("isolate_jobs", args.mode)
            print(f"Isolated workspaces: {jobs.settings()['isolate_jobs']}")
            return
        if args.command == "show":
            result = review(jobs.load(args.job_id))
            print(f"Workspace {result['status']}: {result['path']}")
            for item in result["files"]:
                print(f"{item['state']:<3} {item['path']}")
            print(result["diff"])
            return
        print(jobs.workspace_action(args.job_id, args.command)[1])
    except (WorkspaceError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
