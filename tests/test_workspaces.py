"""Tests for isolated workspaces (workspaces.py): a job edits a git worktree, not the project.

Every test makes its own small git repository in tmp_path. No real agent runs:
job_run.ask is replaced with a fake that edits a file in the folder named by the
workspace note in its prompt.
"""

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from agentic_os import artifacts, changes, job_run, job_store, jobs, leads, limits, safety, today, web, workspaces


def git(folder, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                          cwd=folder, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    """A git repository with one commit (a.txt), one uncommitted change and one untracked file."""
    folder = tmp_path / "project"
    folder.mkdir()
    git(folder, "init", "-q")
    (folder / "a.txt").write_text("one\n")
    (folder / "b.txt").write_text("bee\n")
    git(folder, "add", ".")
    git(folder, "commit", "-q", "-m", "start")
    (folder / "b.txt").write_text("bee changed\n")  # not committed
    (folder / "new.txt").write_text("untracked\n")  # not in git
    return folder


@pytest.fixture
def fake_agent(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Replace the agent call; it appends a line to a.txt where the prompt says it may edit.

    Return the list of prompts the fake agent got.
    """
    monkeypatch.setattr(job_run, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(job_run, "DEFAULT_WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(job_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "artifacts" / "INDEX.md")
    prompts = []

    def ask(prompt, model, **options):
        prompts.append(prompt)
        found = re.search(r"edit files ONLY in (\S+)", prompt)
        if found:
            target = workspaces.Path(found.group(1)) / "a.txt"
            target.write_text(target.read_text() + "agent line\n")
        return {"result": "ok", "total_cost_usd": 0.01, "session_id": "s1"}

    monkeypatch.setattr(job_run, "ask", ask)
    return prompts


def run_in(folder, check=None):
    job = jobs.add("Add a line to a.txt.", model="haiku", use_memory=False, folder=str(folder), check=check)
    jobs.run_job(job)
    return jobs.load(job.id)


def test_a_job_edits_its_workspace_not_the_project(fake_agent, repo):
    job = run_in(repo)

    assert job.status == "done"
    assert workspaces.active(job)
    assert "edit files ONLY in" in fake_agent[0]
    assert (repo / "a.txt").read_text() == "one\n"  # the project did not change
    space = workspaces.Path(job.workspace["path"])
    assert (space / "a.txt").read_text() == "one\nagent line\n"
    # The workspace started from the project as the user sees it.
    assert (space / "b.txt").read_text() == "bee changed\n"
    assert (space / "new.txt").read_text() == "untracked\n"


def test_review_shows_only_the_agents_changes(fake_agent, repo):
    job = run_in(repo)

    result = workspaces.review(job)

    assert result["files"] == [{"state": "M", "path": "a.txt"}]
    assert "+agent line" in result["diff"]
    assert "bee changed" not in result["diff"]  # it was there before the job


def test_apply_puts_the_changes_into_the_project(fake_agent, repo):
    job = run_in(repo)

    job, message = jobs.workspace_action(job.id, "apply")

    assert "Applied 1 file change(s)" in message
    assert (repo / "a.txt").read_text() == "one\nagent line\n"
    assert (repo / "b.txt").read_text() == "bee changed\n"  # the user's own change stays
    assert job.workspace["status"] == "applied"
    assert jobs.load(job.id).workspace["status"] == "applied"
    assert not workspaces.Path(job.workspace["worktree"]).exists()
    assert "worktrees" not in git(repo, "worktree", "list")  # git forgot it too
    assert git(repo, "log", "--oneline").count("\n") == 1  # nothing was committed


def test_apply_with_conflicts_keeps_both_sides(fake_agent, repo):
    job = run_in(repo)
    (repo / "a.txt").write_text("one\nthe user's line\n")  # the project changed meanwhile

    job, message = jobs.workspace_action(job.id, "apply")

    assert "WITH CONFLICTS" in message
    assert job.workspace["status"] == "conflicts"
    text = (repo / "a.txt").read_text()
    assert "<<<<<<<" in text and "agent line" in text and "the user's line" in text


def test_discard_changes_nothing(fake_agent, repo):
    job = run_in(repo)

    job, message = jobs.workspace_action(job.id, "discard")

    assert "did not change" in message
    assert job.workspace["status"] == "discarded"
    assert (repo / "a.txt").read_text() == "one\n"
    assert not workspaces.Path(job.workspace["worktree"]).exists()
    with pytest.raises(workspaces.WorkspaceError):
        jobs.workspace_action(job.id, "apply")  # nothing left to apply


def test_a_working_job_cannot_be_applied(fake_agent, repo):
    job = run_in(repo)
    jobs.update(job.id, status="running")

    with pytest.raises(workspaces.WorkspaceError, match="still working"):
        jobs.workspace_action(job.id, "apply")


def test_the_check_runs_in_the_workspace(fake_agent, repo):
    job = run_in(repo, check="grep -q 'agent line' a.txt")

    assert job.status == "done"  # it would fail in the project, where a.txt has no such line


def test_mode_off_and_folders_outside_git_get_no_workspace(fake_agent, repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert run_in(plain).workspace is None

    jobs.set_setting("isolate_jobs", "off")
    assert run_in(repo).workspace is None

    with pytest.raises(ValueError):
        jobs.set_setting("isolate_jobs", "sometimes")


def test_files_that_the_check_makes_are_not_changes_of_the_job(fake_agent, repo):
    """A check like `cmake -B build && ctest` builds in the workspace: the build is not the job's work."""
    job = run_in(repo, check="mkdir -p build-tests/sub && echo cache > build-tests/CMakeCache.txt "
                             "&& echo obj > build-tests/sub/x.o && grep -q 'agent line' a.txt")

    assert job.status == "done"
    assert job.workspace["check_files"] == ["build-tests/CMakeCache.txt", "build-tests/sub/x.o"]
    assert [item["path"] for item in workspaces.review(job)["files"]] == ["a.txt"]

    jobs.workspace_action(job.id, "apply")

    assert (repo / "a.txt").read_text() == "one\nagent line\n"
    assert not (repo / "build-tests").exists()


def test_a_file_the_agent_makes_stays_a_change_when_the_check_runs(fake_agent, repo, monkeypatch):
    def agent_adds_a_file(prompt, model, **options):
        space = workspaces.Path(re.search(r"edit files ONLY in (\S+)", prompt).group(1))
        (space / "tests.cpp").write_text("int main() {}\n")
        return {"result": "ok", "total_cost_usd": 0.01, "session_id": "s1"}

    monkeypatch.setattr(job_run, "ask", agent_adds_a_file)
    job = run_in(repo, check="mkdir -p build && echo obj > build/tests.o")

    assert job.workspace["check_files"] == ["build/tests.o"]
    assert [item["path"] for item in workspaces.review(job)["files"]] == ["tests.cpp"]


def test_a_repository_without_a_commit_fails_with_a_clear_message(fake_agent, tmp_path):
    """After `git init` and `git add`, but before the first commit, there is no HEAD."""
    folder = tmp_path / "fresh"
    folder.mkdir()
    git(folder, "init", "-q")
    (folder / "a.txt").write_text("one\n")
    git(folder, "add", ".")

    job = run_in(folder)

    assert job.status == "failed"
    assert "has no commit yet" in job.error and "invalid reference" not in job.error
    assert fake_agent == []  # no agent call: it would have edited the live project
    assert (folder / "a.txt").read_text() == "one\n"
    assert not (workspaces.WORKTREES_DIR / job.id).exists()


def test_shared_mode_isolates_only_next_to_another_job(fake_agent, repo):
    jobs.set_setting("isolate_jobs", "shared")
    alone = jobs.add("Task.", model="haiku", use_memory=False, folder=str(repo))
    assert not workspaces.wanted(alone, repo, "shared", jobs.all_jobs)

    other = jobs.add("Other task.", model="haiku", use_memory=False, folder=str(repo))
    assert workspaces.wanted(other, repo, "shared", jobs.all_jobs)


def test_today_and_web_show_a_waiting_workspace(fake_agent, repo, monkeypatch):
    monkeypatch.setattr(leads, "roster", lambda: {})  # not the real roster.json
    job = run_in(repo)

    items = [item for item in today.needs_you(jobs.all_jobs()) if item["kind"] == "workspace"]
    assert [item["job"] for item in items] == [job.id]
    assert web.api_workspace(job.id)["files"] == [{"state": "M", "path": "a.txt"}]

    reply = web.api_workspace_action(job.id, "discard")

    assert reply["status"] == "discarded"
    assert not [item for item in today.needs_you(jobs.all_jobs()) if item["kind"] == "workspace"]
    assert web.api_changes(job.id) == []  # its file copies were of the workspace
    with pytest.raises(HTTPException) as error:
        web.api_workspace_action(job.id, "discard")
    assert error.value.status_code == 409


def test_apply_merges_when_the_project_changed_other_lines(fake_agent, repo):
    job = run_in(repo)
    (repo / "a.txt").write_text("zero\none\n")  # the user's own change, not committed, other lines

    job, message = jobs.workspace_action(job.id, "apply")

    assert "CONFLICTS" not in message
    assert job.workspace["status"] == "applied"
    assert (repo / "a.txt").read_text() == "zero\none\nagent line\n"


def test_snapshots_at_the_same_time_do_not_break_each_other(fake_agent, repo):
    """A page refresh during Apply: each snapshot needs its own index (the old shared one gave an empty tree)."""
    job = run_in(repo)
    worktree = workspaces.Path(job.workspace["worktree"])
    expected = workspaces.snapshot_tree(worktree, repo)

    with ThreadPoolExecutor(max_workers=8) as pool:
        trees = list(pool.map(lambda _: workspaces.snapshot_tree(worktree, repo), range(24)))

    assert set(trees) == {expected}
    assert not [path for path in worktree.parent.iterdir() if path.name.startswith(".index-")]


def test_a_changed_git_link_is_refused(fake_agent, repo, tmp_path):
    job = run_in(repo)
    other = tmp_path / "other"
    git(tmp_path, "init", "-q", str(other))
    (workspaces.Path(job.workspace["worktree"]) / ".git").write_text(f"gitdir: {other / '.git'}\n")

    with pytest.raises(workspaces.WorkspaceError, match="not safe"):
        workspaces.review(job)


def test_apply_refuses_protected_files_of_this_project(fake_agent, repo, monkeypatch):
    monkeypatch.setattr(safety, "PROJECT_DIR", repo.resolve())  # pretend the repo is Agent Office
    job = run_in(repo)
    (workspaces.Path(job.workspace["worktree"]) / "roster.json").write_text("{}\n")

    with pytest.raises(workspaces.WorkspaceError, match="protected files"):
        jobs.workspace_action(job.id, "apply")

    assert not (repo / "roster.json").exists() and (repo / "a.txt").read_text() == "one\n"


def test_copies_of_an_old_worktree_are_forgotten(fake_agent, repo, tmp_path):
    """The next run's worktree has the same path: an old copy would make Undo (and the
    next Apply) remove the work of run 1, which is already in the project."""
    job = run_in(repo)
    space = workspaces.Path(job.workspace["worktree"])
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n")
    assert changes.snapshot_before(job.id, space / "a.txt")  # as the edit hook does
    assert changes.snapshot_before(job.id, outside)

    jobs.workspace_action(job.id, "apply")

    assert [change["path"] for change in changes.changes(job.id)] == [str(outside)]

    # A stale copy in a worktree path (for example from a crash) is forgotten by the next run.
    (space / "sub").mkdir(parents=True)
    assert changes.snapshot_before(job.id, space / "sub" / "old.txt")
    _, start, _ = jobs.follow_up(job.id, "One more line.")
    assert start
    jobs.run_job(jobs.load(job.id))

    assert [change["path"] for change in changes.changes(job.id)] == [str(outside)]


def test_conflicting_apply_writes_added_files_in_new_folders(fake_agent, repo):
    job = run_in(repo)
    space = workspaces.Path(job.workspace["worktree"])
    (space / "deep" / "dir").mkdir(parents=True)
    (space / "deep" / "dir" / "new.py").write_text("x = 1\n")  # a file the job added
    (repo / "a.txt").write_text("one\nthe user's line\n")  # so the plain apply fails

    job, message = jobs.workspace_action(job.id, "apply")

    assert job.workspace["conflicts"] == ["a.txt"]  # the added file is not a conflict
    assert (repo / "deep" / "dir" / "new.py").read_text() == "x = 1\n"
    assert not (repo / "deep" / "dir" / "new.py.from-job").exists()


def test_shared_mode_with_a_deleted_folder(fake_agent, repo, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    jobs.add("Old task.", model="haiku", use_memory=False, folder=str(gone))
    gone.rename(tmp_path / "moved")  # the user deleted (moved) the folder; the job is still queued
    job = jobs.add("Task.", model="haiku", use_memory=False, folder=str(repo))

    assert not workspaces.wanted(job, repo, "shared", jobs.all_jobs)


def test_a_failed_copy_of_your_changes_leaves_no_worktree(fake_agent, repo, monkeypatch):
    real_git = workspaces.git

    def git_without_apply(folder, *args, **options):
        if args[0] == "apply":
            return subprocess.CompletedProcess(args, 1, "", "error: patch failed")
        return real_git(folder, *args, **options)

    monkeypatch.setattr(workspaces, "git", git_without_apply)
    job = jobs.add("Task.", model="haiku", use_memory=False, folder=str(repo))

    with pytest.raises(workspaces.WorkspaceError, match="uncommitted changes did not apply"):
        workspaces.create(job, repo, jobs.now())

    assert not (workspaces.WORKTREES_DIR / job.id).exists()
    assert "worktrees" not in git(repo, "worktree", "list")


def test_an_unreadable_untracked_file_leaves_no_worktree(fake_agent, repo):
    secret = repo / "locked.txt"
    secret.write_text("x\n")
    secret.chmod(0)
    job = jobs.add("Task.", model="haiku", use_memory=False, folder=str(repo))
    try:
        with pytest.raises(workspaces.WorkspaceError, match="could not be made"):
            workspaces.create(job, repo, jobs.now())
    finally:
        secret.chmod(0o600)

    assert not (workspaces.WORKTREES_DIR / job.id).exists()
    assert "worktrees" not in git(repo, "worktree", "list")


def test_a_job_with_conflicts_cannot_run_again_until_removed(fake_agent, repo):
    job = run_in(repo)
    (repo / "a.txt").write_text("one\nthe user's line\n")
    job, _ = jobs.workspace_action(job.id, "apply")
    worktree = workspaces.Path(job.workspace["worktree"])

    with pytest.raises(workspaces.WorkspaceError, match="conflicts"):
        jobs.follow_up(job.id, "Again.")
    with pytest.raises(workspaces.WorkspaceError, match="conflicts"):
        jobs.comment(job.id, "Change it.")
    assert worktree.exists() and jobs.load(job.id).workspace["conflicts"] == ["a.txt"]
    assert jobs.load(job.id).review is None  # the refused comment saved nothing

    assert web.api_workspace(job.id)["conflicts"] == ["a.txt"]  # the page lists them
    job, _ = jobs.workspace_action(job.id, "discard")  # "Remove workspace" on the page

    assert job.workspace["status"] == "applied" and not worktree.exists()
    assert jobs.follow_up(job.id, "Again.")[1]


def test_removing_a_workspace_keeps_your_own_missing_worktrees(fake_agent, repo, tmp_path):
    """`git worktree prune` would delete the record of your worktree on an unplugged disk."""
    usb = tmp_path / "usb-copy"
    git(repo, "worktree", "add", "--detach", "-q", str(usb), "HEAD")
    usb.rename(tmp_path / "unplugged")
    job = run_in(repo)
    worktree = workspaces.Path(job.workspace["worktree"])
    worktree.rename(tmp_path / "our-copy-gone")  # our folder is gone too: only its record is left

    workspaces.remove_worktree(repo, worktree)

    listed = git(repo, "worktree", "list", "--porcelain")
    assert str(usb) in listed
    assert str(worktree) not in listed


def test_a_run_that_changed_nothing_removes_its_workspace(fake_agent, repo, monkeypatch):
    monkeypatch.setattr(leads, "roster", lambda: {})
    monkeypatch.setattr(job_run, "ask", lambda prompt, model, **options:
                        {"result": "Nothing to do.", "total_cost_usd": 0.01, "session_id": "s1"})
    job = run_in(repo)

    assert job.status == "done"
    assert job.workspace["status"] == "discarded"
    assert not workspaces.Path(job.workspace["worktree"]).exists()
    assert not [item for item in today.needs_you(jobs.all_jobs()) if item["kind"] == "workspace"]


def test_an_interrupted_job_shows_its_workspace(fake_agent, repo, monkeypatch):
    monkeypatch.setattr(leads, "roster", lambda: {})
    job = run_in(repo)
    jobs.update(job.id, status="interrupted")

    kinds = [item["kind"] for item in today.needs_you(jobs.all_jobs()) if item.get("job") == job.id]

    assert sorted(kinds) == ["interrupted", "workspace"]


def test_a_tracked_file_that_becomes_ignored_is_not_deleted(fake_agent, repo):
    """Job 02eee4b0 added '.idea/' to .gitignore. The .idea files were in git, so they stay
    in the snapshot: the diff and Apply must not delete them (git rm --cached is the user's job)."""
    (repo / ".idea").mkdir()
    (repo / ".idea" / "misc.xml").write_text("<project/>\n")
    git(repo, "add", ".idea")
    git(repo, "commit", "-q", "-m", "ide files")
    job = run_in(repo)
    space = workspaces.Path(job.workspace["path"])
    (space / ".gitignore").write_text(".idea/\n")  # what the agent wrote

    files = workspaces.review(job)["files"]

    assert {"state": "D", "path": ".idea/misc.xml"} not in files
    assert {"state": "A", "path": ".gitignore"} in files


def test_an_ignored_venv_is_linked_and_the_workspace_is_made(fake_agent, repo):
    """Job f694068c: the project had an ignored .venv. The workspace links it, and git 2.25
    refused the ':(exclude).venv' pathspec for an ignored link, so no workspace was made."""
    (repo / ".gitignore").write_text(".venv\n")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "python").write_text("fake\n")

    job = run_in(repo)

    assert job.status == "done", job.error
    space = workspaces.Path(job.workspace["path"])
    assert (space / ".venv").is_symlink()
    paths = [entry["path"] for entry in workspaces.review(job)["files"]]
    assert paths == ["a.txt"]  # neither the link nor the .gitignore of the user is a change of the job
