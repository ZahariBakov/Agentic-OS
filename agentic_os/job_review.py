"""Job review: what the user does with a finished job.

Approve, reject, comment (ask for changes), undo the file changes, apply or discard
the job's workspace. A bad result (rejected, rework, undone) is a signal: the lead
gets a lesson job, and its `<lesson>` block becomes a draft for the user.

Imports job_store and job_talk (of the job_* modules).
"""

from pathlib import Path

from agentic_os import changes, job_store, job_talk, leads, workspaces


REVIEW_PROMPT = """The user reviewed your work on this job and asks for changes.

{text}{files}

Make the changes, then give your full answer again."""
SIGNALS = ("rejected", "rework", "undone")  # bad results: the lead is asked for a lesson
SIGNAL_WHY = {
    "rejected": "the user rejected the result",
    "rework": "the user had to ask for changes",
    "undone": "the user undid your file changes",
    "repeated": "the user gave you the same correction several times",  # a memory card in Recos
}

LESSON_TASK = """Job {job_id} went wrong: {why}{note}.

The task of that job was:
{task}

Think about what you would do differently next time. Then write ONE lesson that
you can apply to similar jobs, and why:

<lesson>
One line, under 200 characters: what to do (or not to do) next time.
Why: what you did on this job that led to the problem, and how the lesson would have prevented it.
</lesson>

The user reads it and decides to keep it or not. Write no block if nothing in
your approach was at fault."""

# Recos found a correction that the user gave this lead again and again (recos.memory_cards).
REPEATED_TASK = """The user corrected your work on the same thing {count} times:
{corrections}

Your memory missed it. Find the one principle behind these corrections, so the
user never has to say it again. Write it as ONE lesson, and why:

<lesson>
One line, under 200 characters: what to do (or not to do) from now on.
Why: what these corrections have in common.
</lesson>

The user reads it and decides to keep it or not."""


def workspace_action(job_id, action):
    """Apply or discard a job's workspace (workspaces.py). Return (job, message).

    Under the claim lock, so the job cannot start again meanwhile. Raises
    workspaces.WorkspaceError (a ValueError) when it cannot be done.
    """
    if action not in ("apply", "discard"):
        raise ValueError("The action is apply or discard.")
    with job_store.claim_lock():
        job = job_store.load(job_id)
        if job_store.worker_busy(job):
            raise job_store.busy_error(job_id)
        message = (workspaces.apply if action == "apply" else workspaces.discard)(job, job_store.now())
        job_store.save(job)
        changes.forget(job_id, job.workspace["worktree"])  # copies of a worktree that is done with
        return job, message


def add_signal(job, signal):
    """Remember a bad result on the job, once. Return True if it is new."""
    if signal in job.signals:
        return False
    job.signals.append(signal)
    return True


def ask_lesson(job, signal, note=""):
    """After a bad result on a lead's job, add a lesson job for that lead. Return it, or None.

    One-time agents have no next session, so they are not asked. A lesson job
    itself is not asked about (no lessons about lessons).
    """
    if not job.lead or job.lesson_for:
        return None
    if job.lead not in leads.roster():  # the lead was removed from roster.json: nobody to learn
        return None
    task = LESSON_TASK.format(job_id=job.id, why=SIGNAL_WHY[signal],
                              note=f': "{note.strip()[:400]}"' if note.strip() else "", task=job.task[:1500])
    return job_store.add(task, lead=job.lead, fields={"lesson_for": {"job": job.id, "signal": signal}})


def queued_lessons(job_id):
    """Return the queued lesson jobs about job_id (the web page starts them)."""
    return [job for job in job_store.all_jobs() if job.lesson_for and job.lesson_for["job"] == job_id
            and job.status == "queued"]


def save_lesson_draft(job):
    """A finished lesson job: save its <lesson> block as a draft for the user. Return the draft or None."""
    found = leads.extract_lesson(job.result)  # ValueError for a bad line
    if found is None:
        return None
    line, reason = found
    return leads.add_draft(job.lead, line, reason, job.lesson_for["job"], SIGNAL_WHY[job.lesson_for["signal"]])


def approve(job_id, note=""):
    """The user says the result is good. Return the job."""
    with job_store.claim_lock():
        job = job_store.finished_job(job_id)
        job.review = {"verdict": "approved", "note": note.strip(), "at": job_store.now()}
        job_store.save(job)
        return job


def reject(job_id, note):
    """The user says the result is bad, and does not want a new try. Return the job."""
    if not note.strip():
        raise ValueError("Say why: the reason helps the agent to learn.")
    with job_store.claim_lock():
        job = job_store.finished_job(job_id)
        job.review = {"verdict": "rejected", "note": note.strip(), "at": job_store.now()}
        if add_signal(job, "rejected"):
            ask_lesson(job, "rejected", note)
        job_store.save(job)
        return job


def comment(job_id, text, files=None):
    """The user asks for changes: comments on the answer and on changed files.

    files maps a changed file's path to a comment. The comments go to the agent
    as a follow-up (the same session), so the job runs again: "rework", a bad
    result. Return (job, start, warning) like follow_up.
    """
    files = files or {}
    if not text.strip() and not any(note.strip() for note in files.values()):
        raise ValueError("The review has no comments.")
    workspaces.check_rerun(job_store.finished_job(job_id))  # before the comments are made; follow_up checks again under the lock
    changed = {entry["path"] for entry in changes.changes(job_id, with_diff=False)}
    resolved = {str(Path(path).resolve()): note.strip() for path, note in files.items()}
    unknown = sorted(path for path in resolved if path not in changed)
    if unknown:
        raise ValueError(f"The job did not change these files: {', '.join(unknown)}")
    file_lines = "".join(f"\n- {path}: {note}" for path, note in sorted(resolved.items()) if note)
    prompt = REVIEW_PROMPT.format(
        text=text.strip() or "See the comments on the files.",
        files=f"\n\nComments on the changed files:{file_lines}" if file_lines else "",
    )
    # Your words (not the prompt): the conversation shows them, and Recos groups them.
    shown = "\n".join([text.strip()] + [f"{Path(path).name}: {note}" for path, note in sorted(resolved.items()) if note]).strip()
    with job_store.claim_lock():
        job = job_store.finished_job(job_id)
        job.review = {"verdict": "changes", "note": shown, "at": job_store.now()}
        if add_signal(job, "rework"):
            ask_lesson(job, "rework", text)
        job_store.save(job)
    return job_talk.follow_up(job_id, prompt, shown=shown)


def undo(job_id, path=None):
    """Undo the job's file changes (changes.undo). An undo is a bad result. Return the undone paths."""
    with job_store.claim_lock():
        job = job_store.load(job_id)
        # While the agent works, it can still edit the files, and the undo would be mixed up.
        if job.status in ("queued", "running"):
            raise ValueError(f"Job {job_id} is still working; stop it or wait.")
        if job.workspace and not workspaces.active(job):
            raise ValueError(f"The changes of job {job_id} were {job.workspace['status']} "
                             "(see its workspace); undo them in the project with git.")
        undone = changes.undo(job_id, path)
        if undone and add_signal(job, "undone"):
            ask_lesson(job, "undone")
            job_store.save(job)
        return undone
