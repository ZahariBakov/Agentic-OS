"""Job run: everything that runs a job with an agent.

It owns the agent calls (first, resume, follow-up), the judges after an answer (the
check, the wildcard check, the critic), `run_job` (one whole run, from claim to the
last save), the desks of a lead, and stop, recover and resume.

The top part of the jobs code: it imports job_store, job_talk and job_review.
"""

import os
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agentic_os import (
    artifacts,
    changes,
    commands,
    critic,
    edit_hook,
    feedback,
    job_review,
    job_store,
    job_talk,
    leads,
    limits,
    mail,
    memory,
    project_memory,
    safety,
    wildcards,
    workspaces,
)
from agentic_os.ask import AgentError, ask, kill_group


PROMPTS_DIR = job_store.DATA_DIR / "prompts"  # the memory prompt of each job
# Where a job without its own folder works. It is outside the project, because
# Claude Code reads CLAUDE.md in the work folder and in all its parent folders.
DEFAULT_WORK_DIR = Path.home() / ".cache" / "agentic-os" / "workspaces" / "jobs"
MAX_TRIES = 3  # agent calls per job when the check fails
CHECK_TIMEOUT = 600  # seconds
FIX_PROMPT = """The check `{check}` failed with exit code {code}. The end of its output:

{output}

Fix the problem. The check runs again after your answer.
Then write your whole answer again (what you did, and any blocks like <note>): it replaces your last answer."""
CRITIC_FIX_PROMPT = """A critic compared your answer with a good example answer, and the example was better.
Why: {why}
What to change: {fix}

Write your whole answer again, improved. The new answer replaces the old one."""

# For a lead whose session does not have the job any more (it started fresh since).
FOLLOWUP_CONTEXT = """[Context: you did this job in an earlier session; this session starts without it.]
The task was:
{task}

Your last answer:
{answer}

"""

RESUME_PROMPT = """This job was interrupted: {why}.
Your last turn may have stopped in the middle. The user pressed Resume.

The task was:
{task}

{done}

Before you do anything, check the current state: read the changed files, and run
the tests if the project has them. Do NOT repeat actions that already happened
(installs, deletions, files written, messages sent). Continue from where the work
stopped, then give your full answer to the task."""


def work_folder(job):
    """Return the folder where the job's agent runs (its sessions belong to this folder)."""
    if job.folder:
        return Path(job.folder)
    DEFAULT_WORK_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_WORK_DIR


def edit_folder(job):
    """Return the folder where the job's agent may edit and its check runs.

    The job's isolated workspace while it is active (workspaces.py), else work_folder.
    """
    return Path(job.workspace["path"]) if workspaces.active(job) else work_folder(job)


def agent_args(job, tools):
    """Return the permission options for the job's agent (safety.agent_args), with the office tools.

    The office MCP server (office_mcp.py) comes first: tests read the deny list up to the end.
    """
    from agentic_os import office_mcp  # here, not at the top: office_mcp imports jobs
    return [*office_mcp.config_args(job.id),
            *safety.agent_args(edit_folder(job), [*tools, *office_mcp.tool_names()],
                               job.workspace if workspaces.active(job) else None)]


def prepare_workspace(job):
    """Give the job an isolated workspace before its run, when it needs one. Save the job.

    Only for an agent that can edit, in a git project, by the isolate_jobs setting.
    A job that runs again after its changes were applied or discarded gets a new one.
    Raise workspaces.WorkspaceError when git cannot make it (the job then fails:
    with "always", a job must not quietly edit the live project).
    """
    if workspaces.active(job) or not can_edit(job):
        return
    mode = job_store.settings()["isolate_jobs"]
    if not workspaces.wanted(job, work_folder(job), mode, job_store.all_jobs):
        return
    job.workspace = workspaces.create(job, work_folder(job), job_store.now())
    # The new worktree has the old one's path: copies from an earlier run are not "before" it.
    changes.forget(job.id, job.workspace["worktree"])
    job_store.save(job)
    print(f"{' ' * 25}isolated workspace: {job.workspace['path']}")


def first_call(job, read_only=False):
    """Return (session id, extra `claude` options) for the job's first agent call.

    read_only: only the read tools (the planning call: nothing changes before the OK).
    """
    folder = work_folder(job)
    if job.lead:
        desk = job.desk or job.lead
        session_id, prompt_file, why = leads.prepare(desk)
        print(f"{' ' * 25}{desk}: {why}")
        return session_id, lead_args(job, prompt_file, read_only)
    # A one-time agent may edit files only when the job has its own folder.
    tools = safety.READ_TOOLS + (safety.EDIT_TOOLS if can_edit(job) and not read_only else [])
    extra_args = safety.no_tool_args() if no_tools(job) else agent_args(job, tools)
    if job.max_budget_usd:
        extra_args = ["--max-budget-usd", f"{job.max_budget_usd:.4f}", *extra_args]  # Claude Code stops there
    if job.use_memory:
        prompt_file = PROMPTS_DIR / f"{job.id}.md"
        # A resumed or followed-up job uses its old file again: the same session with
        # the same prompt, so the prompt cache hits.
        if not prompt_file.exists():
            memory.write_prompt(prompt_file)
        extra_args = ["--append-system-prompt-file", str(prompt_file), *extra_args]
    return None, extra_args


def no_tools(job):
    """Return True when the job's agent gets no tools: a skill with "tools: none" (mail)."""
    if not job.skill:
        return False
    from agentic_os import skills  # here, not at the top: skills imports jobs
    return skills.no_tools(job.skill)


def fill_inbox(job):
    """Fill the {inbox_facts} blank of a skill's task with the new mail, now. Save the job.

    At the start, not when the job is added: a job may wait in the queue, and the mail
    must be new. Only once (a follow-up or a resume keeps the mail of the first run).
    Raise mail.MailError when the mail cannot be read: the job fails before any agent call.
    """
    if not job.skill or job.session_id or job.messages or mail.BLANK not in job.task:
        return
    job.task = job.task.replace(mail.BLANK, mail.facts(), 1)
    job_store.save(job)


def lead_args(job, prompt_file, read_only=False):
    """Return the `claude` options for a lead's job: its prompt file and its tools."""
    tools = leads.roster()[job.lead]["tools"]
    # A twin edits only in the job's own isolated workspace (take_desk_for sends no other edits there).
    if read_only or (leads.is_twin(job.desk) and not workspaces.active(job)):
        tools = [tool for tool in tools if tool in safety.READ_TOOLS]
    return ["--append-system-prompt-file", str(prompt_file), *agent_args(job, tools)]


def resume_brief(job):
    """Return the prompt for an interrupted job: the task and what already happened."""
    done = [f"Agent calls that finished before the interruption: {job.tries}."]
    changed = [entry["path"] for entry in changes.changes(job.id, with_diff=False) if entry["state"] != "unchanged"]
    if changed:
        done.append("Files already changed on this job (they keep those changes):\n"
                    + "\n".join(f"- {path}" for path in changed))
    if job.check_output:
        done.append(f"The last output of the check `{job.check}`:\n{job.check_output}")
    return RESUME_PROMPT.format(why=job.interrupted_why or "unknown", task=job.task, done="\n\n".join(done))


def continue_call(job):
    """Return (session id, `claude` options) for a call that continues a job (resume, follow-up).

    A lead's job goes to the lead's current session (first_call -> leads.prepare); a
    one-time agent continues the job's own session.
    """
    if job.lead:
        return first_call(job)
    return job.session_id, first_call(job)[1]


def resume_call(job, on_start, messages):
    """The first agent call of a resumed job: the brief goes to the job's session.

    Return (the reply, the `claude` options for the next calls). A one-time agent
    continues the job's own session. A lead's job goes to the lead's current
    session (leads.prepare), like a follow-up: other jobs may have moved the lead
    to a newer session, and going back to the job's old one would lose that work.
    The brief has the task, so a fresh session can do it too.
    """
    session_id, extra_args = continue_call(job)
    print(f"{' ' * 25}resume session {session_id}")
    prompt = resume_brief(job)
    if messages:
        prompt += "\n\n" + job_talk.prompt_for(messages)
    job.resuming = False
    return call_agent(job, prompt, session_id, extra_args, on_start), extra_args


def follow_up_call(job, messages, on_start):
    """The first agent call of a job that the user wrote to after it ended.

    Return (the reply, the `claude` options for the next calls). A one-time agent
    continues the job's session. A lead's job goes to the lead's current session
    (leads.prepare); when that is not the job's session, the lead gets the task and
    its last answer first.
    """
    prompt = job_talk.prompt_for(messages)
    session_id, extra_args = continue_call(job)
    if job.lead and session_id != job.session_id:
        prompt = FOLLOWUP_CONTEXT.format(task=job.task, answer=job_talk.last_answer(job)[:4000]) + prompt
    job_talk.add_messages(job, job.result, messages)
    return call_agent(job, prompt, session_id, extra_args, on_start), extra_args


def can_edit(job):
    """Return True if the job's agent has the edit tools (in its folder)."""
    if job.parent:
        return False  # a sub-job only reads (specialists.py)
    if job.lead:
        if leads.is_twin(job.desk) and not workspaces.active(job):
            return False
        return any(tool in safety.EDIT_TOOLS for tool in leads.roster()[job.lead]["tools"])
    return job.folder is not None  # a one-time agent edits only in its own folder (first_call)


def tool_timeout_ms():
    """Claude Code's limit for one office tool call, in ms: the longest tool's wait, and a margin."""
    from agentic_os import specialists  # here, not at the top: specialists imports jobs
    return max(commands.TOOL_TIMEOUT_MS, (specialists.WAIT_SECONDS + 60) * 1000)


def call_agent(job, prompt, session_id, extra_args, on_start, read_only=False):
    """Run one agent call for the job. Return the reply.

    read_only: the planning call; the edit hook gets no folder, so it saves no copies.

    Every paid call goes through here, so this is where a stop is checked last
    (resume, follow-up, fix and critic rounds all come here).
    """
    if job_store.is_stopped(job):
        raise RuntimeError("the job was stopped before the next agent call")
    def on_session(new_session_id):
        # Saved at the start of the call, so an interrupted job can resume this session.
        job.session_id = new_session_id
        job_store.save(job)

    try:
        reply = ask(
            prompt + workspaces.note(job), job.model, session_id=session_id, on_start=on_start,
            on_session=on_session, effort=job.effort, extra_args=extra_args, cwd=work_folder(job),
            env={**edit_hook.environment(job.id, edit_folder(job) if can_edit(job) and not read_only else None),
                 # run_command waits for your answer, delegate for its sub-jobs: longer than the default
                 "MCP_TOOL_TIMEOUT": str(tool_timeout_ms())},
        )
    except AgentError as error:
        job.pid = None
        job.cost += error.cost  # a failed call can still cost money (for example, too many turns)
        job_store.save(job)
        raise
    job.pid = None
    job.tries += 1
    job.cost += reply["total_cost_usd"]
    job.session_id = reply["session_id"]
    if job.lead:
        reply["result"] = leads.finish(job.desk or job.lead, reply)
    job_store.save(job)
    return reply


def run_check(job):
    """Run the job's check in its folder. Return (exit code, the end of the output).

    In a workspace, the files that the check makes (a build folder) are remembered, so they
    are not a change of the job (workspaces.check_made).
    """
    before = workspaces.new_files(job) if workspaces.active(job) else None
    try:
        return check_output(job)
    finally:
        if before is not None:
            workspaces.check_made(job, before)


def check_output(job):
    """Run the check command in the job's edit folder. Return (exit code, the end of the output)."""
    # Its own process group: on a timeout we end the shell and everything it started
    # (`cd x && pytest`: the shell's children would go on running otherwise).
    # errors="replace": a check may print bytes that are not UTF-8 (a binary, another encoding).
    process = subprocess.Popen(job.check, shell=True, cwd=edit_folder(job), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
    try:
        output, _ = process.communicate(timeout=CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        kill_group(process.pid)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            # A child in its own session still holds the pipe open: stop waiting for it.
            process.stdout.close()
            process.wait()
        return -1, f"The check took more than {CHECK_TIMEOUT} seconds."
    return process.returncode, output[-3000:]


def run_critic(job, result, on_start=None):
    """Compare the result with the job's reference (critic.py). Save the verdict on the job.

    Return the verdict. The critic's cost counts as the job's cost, also when it fails
    (the error's `cost`). on_start gets the critic's pid, so Stop can end it.
    """
    if job_store.is_stopped(job):
        raise RuntimeError("the job was stopped before the critic")
    try:
        verdict = critic.compare(job.task, result or "", Path(job.reference).read_text(), on_start=on_start)
    except Exception as error:
        job.pid = None
        job.cost += getattr(error, "cost", 0.0)
        job_store.save(job)
        raise
    job.pid = None
    job.cost += verdict["cost"]
    job.verdicts.append({key: verdict[key] for key in ("winner", "why", "fix", "cost")})
    job_store.save(job)
    print(f"{' ' * 25}critic: the {verdict['winner']} is better, try {job.tries} of {MAX_TRIES}")
    return verdict


def work(job, on_start):
    """Run the agent, then send feedback back to the same session while there is some.

    Feedback comes from these judges, in this order:
      1. the check (exact, so it comes first): its error output
         (and for a skill with wildcards, Python's check of the numbered items)
      2. the critic, only for a result that passed the check: its fix, when the
         reference is better than the result. The critic gives advice, not an
         exact test, so when the reference still wins after MAX_TRIES calls,
         the job is still done; its verdicts show what the critic thought.

    Follow-up messages (the inbox) come first when the run starts, and after
    the judges are happy: the agent answers them in the same session.

    Return (the last reply, None if the job passed, or why it failed).
    """
    start_tries = job.tries  # MAX_TRIES counts the calls of this run, so a follow-up gets new tries
    planning = False  # True only while the first call's reply may hold a plan
    if job_store.is_stopped(job):  # before the inbox is taken: its messages wait for the next run
        return None, "stopped"
    messages = job_store.take_inbox(job.id)
    if job.resuming:
        job_talk.add_messages(job, None, messages)  # saved before the call, so a crash does not lose them
        reply, extra_args = resume_call(job, on_start, messages)
    elif messages and job.session_id:
        reply, extra_args = follow_up_call(job, messages, on_start)
    else:
        job_talk.add_messages(job, None, messages)  # written before the first answer: they go with the task
        planning = job_talk.needs_plan(job)
        session_id, extra_args = first_call(job, read_only=planning)
        if job_store.is_stopped(job):  # stopped before the agent started
            return None, "stopped"
        # What we know about the job's project (only the relevant part), in the task, not in
        # the system prompt: a lead's prompt file stays the same, so its prompt cache still hits.
        task = job.task + project_memory.brief(job.folder, job.task)
        if planning:
            task += job_talk.PLAN_RULES.format(reference=job_talk.cost_reference(job.model))
        else:
            task += job_talk.ASK_RULES if job_talk.can_ask(job) else ""
        if messages:
            task += "\n\n" + job_talk.prompt_for(messages)
        reply = call_agent(job, task, session_id, extra_args, on_start, read_only=planning)
    previous_output = None
    while True:
        if planning:
            planning = False
            if job_talk.extract_plan(reply["result"])[0]:
                return reply, "plan"  # the job waits for the user's OK; the work comes after it
            # No plan: the agent answered at once. The next calls may edit again.
            extra_args = first_call(job)[1] if not job.lead else lead_args(job, leads.lead_dir(job.desk or job.lead) / "prompt.md")
        # A question ends the run: the job waits for the user, and the checks run after the answer.
        if job_talk.can_ask(job) and job_talk.extract_question(reply["result"])[0]:
            return reply, "question"
        prompt = None  # the feedback for the next agent call; None: no more calls
        if job.check:
            code, job.check_output = run_check(job)
            if code == 0:
                print(f"{' ' * 25}check passed: {job.check}")
            else:
                tries = job.tries - start_tries
                print(f"{' ' * 25}check failed (exit code {code}), try {tries} of {MAX_TRIES}")
                if tries >= MAX_TRIES:
                    return reply, f"the check failed {tries} times: {job.check}"
                # The same output again means that the agent's changes did not help
                # (for example, the check itself is broken). One more try only costs money.
                if job.check_output == previous_output:
                    return reply, f"the check gave the same output twice: {job.check}"
                previous_output = job.check_output
                prompt = FIX_PROMPT.format(check=job.check, code=code, output=job.check_output)
        if prompt is None and job.wildcards:
            # Exact, like the check: the number of items and the WILDCARD marks.
            problems = wildcards.check(reply["result"], job.wildcards["count"], job.wildcards["places"])
            job.wildcards["problems"] = problems
            if problems:
                tries = job.tries - start_tries
                print(f"{' ' * 25}wildcard check: {len(problems)} problem(s), try {tries} of {MAX_TRIES}")
                if tries >= MAX_TRIES:
                    return reply, f"the items were still wrong after {tries} tries: {problems[0][2:]}"
                prompt = wildcards.FIX_PROMPT.format(problems="\n".join(problems))
        if prompt is None and job.reference:
            try:
                verdict = run_critic(job, reply["result"], on_start)
            except (RuntimeError, ValueError, OSError) as error:
                if job_store.is_stopped(job):
                    return reply, "stopped"
                # Advice, not a test: the check passed, so the job stays done. The round shows why.
                print(f"{' ' * 25}critic failed: {error}")
                job.verdicts.append({"winner": None, "why": f"The critic failed: {error}", "fix": "",
                                     "cost": getattr(error, "cost", 0.0)})
                job_store.save(job)
                verdict = {"winner": None}
            if verdict["winner"] == "reference" and job.tries - start_tries < MAX_TRIES:
                prompt = CRITIC_FIX_PROMPT.format(why=verdict["why"], fix=verdict["fix"])
        if prompt is None:
            # The user wrote while the agent worked: answer it in the same run.
            messages = job_store.take_inbox(job.id)
            if messages:
                job_talk.add_messages(job, reply["result"], messages)
                prompt = job_talk.prompt_for(messages)
        if prompt is None:
            return reply, None
        if job_store.is_stopped(job):  # stopped while the check or the critic ran: no more agent calls
            return reply, "stopped"
        reply = call_agent(job, prompt, job.session_id, extra_args, on_start)


def save_project_memory(job):
    """Save the answer's <project-note> blocks and one outcome line in the job's project memory.

    Never fails the job: a problem is written to job.error.
    """
    try:
        saved, refused = project_memory.save_agent_notes(job.folder, job.result, job.id, job.lead or "agent")
        job.project_notes = saved
        for line in saved:
            print(f"{' ' * 25}project note: {line}")
        for line in refused:
            print(f"{' ' * 25}project note refused: {line}")
        files = len(changes.read_index(changes.job_folder(job.id)))
        project_memory.record_outcome(job, files)
    except Exception as error:
        job.error = job.error or f"saving the project memory failed: {error}"


class Waits(Exception):
    """claim() found a reason to leave the job queued (the text says why)."""


def routine_wait_reason(job):
    """Return why a routine's job must wait now, or None. Call it under the claim lock.

    One job of a routine runs at a time: a job's cost is known only when it ends, so jobs
    that started together would all pass the weekly cap. The next job starts when this one
    ends (run_job), and checks the cap again, with the cost of the one before it.
    """
    if not job.routine:
        return None
    for other in job_store.all_jobs():
        if other.routine == job.routine and other.id != job.id and other.status == "running":
            return f"job {other.id} of the routine {job.routine} runs; this one starts after it"
    from agentic_os import routines  # here, not at the top: routines imports jobs
    return routines.job_cap_reason(job.routine)


def claim(job_id, guard=None):
    """Change a queued job to running. Return the job, or None if it is not queued any more.

    Reading the status and writing "running" happen under one file lock, so
    two workers (threads, or the web server and `jobs run`) never both get the
    same job. We read the file, not the caller's Job object: that copy can be old.
    guard(job) is checked under the same lock; a reason it returns raises Waits.
    """
    with job_store.claim_lock():
        job = job_store.load(job_id)
        if job.status != "queued":
            return None
        reason = guard(job) if guard else None
        if reason is not None:
            raise Waits(reason)
        job.status = "running"
        job.worker = os.getpid()
        job.held_for = None
        job.waits_for = None
        job_store.stop_file(job_id).unlink(missing_ok=True)  # a stop of an earlier run is over
        job_store.save(job)
        return job


# Set when the web server stops (web.stop_work): no new runs, and a run whose agent was
# ended for the stop keeps "running", so recover() marks it interrupted at the next start.
SHUTDOWN = threading.Event()


def run_job(job):
    """Run one job with an agent. Save the job before and after the run."""
    if SHUTDOWN.is_set():
        return  # the job stays queued for the next start
    reason = limits.pause_reason()
    if reason is not None:
        print(f"{job_store.now()}  waits    {job.id}  ({reason})")
        return  # The job stays queued, so the next `run` starts it.

    try:
        # Under the claim lock: two jobs of one routine never both pass the cap check.
        claimed = claim(job.id, guard=routine_wait_reason)
    except Waits as waits:
        print(f"{job_store.now()}  waits    {job.id}  ({waits})")
        return  # queued: the routine's running job starts it when it ends, or the next `run`
    if claimed is None:
        print(f"{job_store.now()}  skip     {job.id}  (already {job_store.load(job.id).status})")
        return
    job = claimed
    # For "run again?" at the end: a message that waited before this run and was not
    # taken (the run failed before work() took the inbox) would only fail the same way again.
    inbox_before = job_store.inbox_file(job.id).exists()
    messages_before = len(job.messages)

    def on_start(pid):
        job.pid = pid
        job_store.save(job)

    if job.routine and not job.session_id and not job.messages:
        # Its first run: fill the routine's blanks now (a passive job may have waited for days).
        from agentic_os import routines  # here, not at the top: routines imports jobs
        task = routines.fresh_task(job.routine)
        if task is not None:
            job.task = task
            job_store.save(job)
    print(f"{job_store.now()}  start    {job.id}  {job.task}")
    held_by = waits = None
    try:
        fill_inbox(job)  # in the try: a mail error fails the job, with no agent call
        prepare_workspace(job)  # before the agent: it must edit the copy, never the live project
        # A desk does one job at a time, because it has one session (leads.py: desks and twins).
        desk_lock = None
        if job.lead:
            desk_lock, held_by, waits = take_desk_for(job)
        try:
            if not job.lead or desk_lock is not None:
                reply, problem = work(job, on_start)
                if problem == "question":
                    # Saved under the desk's lock, so the desk's next job sees "waiting" and goes elsewhere.
                    job.question, job.result = job_talk.extract_question(reply["result"])
                    job.question["at"] = job_store.now()
                    job.status = "waiting"
                    job.pid = None
                    job_store.save(job)
                elif problem == "plan":
                    job.plan, job.result = job_talk.extract_plan(reply["result"])
                    job.plan.update(status="pending", at=job_store.now())
                    job.status = "waiting"
                    job.pid = None
                    reason = job_talk.auto_approve_reason(job.plan)
                    if reason:  # the inbox message sends the job back to the queue at the end (see below)
                        with job_store.claim_lock():  # every inbox write holds it
                            job_talk.decide(job, "approved", "", reason)
                    job_store.save(job)
        finally:
            leads.release_desk(desk_lock)  # here, not at the end: a run started below takes a desk again
    except Exception as error:
        if SHUTDOWN.is_set() and not job_store.is_stopped(job):
            # The server stops and ended the agent. Not a failure: the job stays "running"
            # with this (soon dead) worker, and recover() makes it "interrupted" (Resume).
            print(f"{job_store.now()}  cut      {job.id}  (the server stops; resume it after the next start)")
            return
        if job_store.is_stopped(job):  # the stop killed the agent: that is not a failure
            job.status = "stopped"
        else:
            job.status = "failed"
            job.error = str(error)
    else:
        if held_by is not None:
            # The lead's session has an open question (job held_by). Another job in that
            # session now would mix two conversations, so this one waits for the answer.
            with job_store.claim_lock():
                if job_store.is_stopped(job):  # stopped meanwhile: "queued" would undo the stop
                    job.status, job.finished_at = "stopped", job_store.now()
                else:
                    job.status, job.held_for = "queued", held_by
                job.worker = None
                job_store.save(job)
            print(f"{job_store.now()}  {'held' if job.status == 'queued' else 'stopped':<7}  {job.id}  "
                  f"(lead {job.lead} waits for an answer in job {held_by})")
            return
        if waits is not None:
            # Every desk of the lead is busy. The job goes back to the queue and frees its place
            # in the pool; the next job that frees a desk starts it (release_held).
            with job_store.claim_lock():
                if job_store.is_stopped(job):
                    job.status, job.finished_at = "stopped", job_store.now()
                else:
                    job.status, job.waits_for = "queued", waits
                job.worker = None
                job_store.save(job)
            print(f"{job_store.now()}  waits    {job.id}  (every desk of {waits} is busy)")
            if job.status == "queued" and desk_free(job):
                run_job(job)  # a desk was freed while we saved: nobody else would start it now
            return
        if problem in ("question", "plan"):
            pass  # saved above, under the lead's lock; notes are saved below
        elif problem == "stopped":
            job.status = "stopped"
            job.result = reply["result"] if reply else job.result  # stopped before a call: keep the old answer
        else:
            job.status = "done" if problem is None else "failed"
            job.error = problem
            job.result = reply["result"]
        job.style_problems = memory.style_problems(job.result or "")
        # Only memory jobs can save notes. If saving fails, we keep the result
        # and still save the job below, so it never stays "running".
        if job.use_memory and job.status != "stopped":
            try:
                notes = memory.extract_notes(feedback.without_skill_files(job.result))
                # A note that looks like a secret is refused alone: the other notes are saved.
                refused = [note for note in notes if memory.note_secret(note) is not None]
                paths = memory.save_notes([note for note in notes if note not in refused], source=f"job {job.id}")
                job.notes = [str(path.relative_to(memory.MEMORY_DIR)) for path in paths]
                if refused:
                    job.error = job.error or (f"{len(refused)} note(s) not saved: they look like they "
                                              "hold a secret (write where it is kept, never the value)")
            except Exception as error:
                job.error = job.error or f"saving notes failed: {error}"
        if job.status in ("done", "failed"):
            save_project_memory(job)
        if job.status == "done" and job.skill == "calibrate":  # only calibrate proposes skill files
            try:
                for name in feedback.save_proposals(job.result, since=job.created_at):
                    print(f"{' ' * 25}proposal saved for skill {name}: python -m agentic_os.skills diff {name}")
            except Exception as error:
                job.error = job.error or f"saving the proposal failed: {error}"
        if job.status == "done" and job.lesson_for:
            try:
                draft = job_review.save_lesson_draft(job)
                print(f"{' ' * 25}lesson draft for {job.lead}: " + (draft["text"] if draft else "none"))
            except Exception as error:  # never leave the job "running" because of a draft
                job.error = job.error or f"the lesson was not saved: {error}"
        if job.status == "done" and job.result:
            try:
                artifacts.save(job)
            except Exception as error:
                job.error = job.error or f"saving the artifact failed: {error}"
    if job.status != "stopped" and job_store.is_stopped(job):
        # Stopped while the run ended (a check, the critic, a question): the stop wins.
        job.status = "stopped"
    job.pid = None
    job.worker = None
    if job.status in job_store.FINISHED:
        job.finished_at = job_store.now()
        try:
            if workspaces.drop_if_unchanged(job, job_store.now()):
                print(f"{' ' * 25}the workspace had no changes: removed")
        except (OSError, ValueError) as error:  # never leave the job "running" because of it
            print(f"{' ' * 25}the workspace was not checked: {error}")
    # A message can come after the last agent call and before this save. Then the
    # job goes back to the queue (under the same lock as follow_up), and runs again.
    # Not when a plan waits for the OK: a message that came during the planning call stays in
    # the inbox and goes to the agent with the user's decision (running now would skip the OK).
    plan_pending = job.status == "waiting" and bool(job.plan) and job.plan["status"] == "pending"
    taken = not inbox_before or len(job.messages) > messages_before
    with job_store.claim_lock():
        again = (job.status in ("done", "failed", "waiting") and not plan_pending and taken
                 and job_store.inbox_file(job.id).exists())
        if again:
            job.status = "queued"
        job_store.save(job)
    print(f"{job_store.now()}  {job.status:<7}  {job.id}  ${job.cost:.4f}")
    if job.error:
        print(f"{' ' * 25}error: {job.error}")
    for note in job.notes:
        print(f"{' ' * 25}saved note: {note}")
    if job.style_problems:
        print(f"{' ' * 25}banned phrases: {job.style_problems}")
    if job.status == "waiting" and job.question:
        print(f"{' ' * 25}question: {job.question['text'][:80]}")
        print(f"{' ' * 25}answer it: python -m agentic_os.jobs answer {job.id} \"...\"")
    if job.status == "waiting" and job.plan and job.plan["status"] == "pending":
        estimate = job_store.usd(job.plan["estimate"]) if job.plan["estimate"] is not None else "none"
        print(f"{' ' * 25}plan (estimate {estimate}, a guess): python -m agentic_os.jobs plan {job.id} approve|change|reject")
    if job.plan and job.plan["status"] == "approved" and job.plan.get("auto") and job.status == "queued":
        print(f"{' ' * 25}plan {job.plan['auto']}")
    if job.status == "done" and job.plan and job.plan["estimate"] is not None:
        print(f"{' ' * 25}plan estimate ${job.plan['estimate']:.4f}, real cost ${job.cost:.4f}")
    if again:
        print(f"{' ' * 25}a follow-up came at the end: the job runs again")
        run_job(job)
    elif job.status != "waiting":
        for held in release_held(job.id):  # the lead's jobs that waited for this one
            run_job(held)
        if job.status != "running":  # not cut by a server stop
            for next_job in next_of_routine(job):  # the routine's job that waited for this one
                run_job(next_job)


def run_queued(workers):
    """Run all queued jobs, with at most `workers` jobs at the same time."""
    queued = [job for job in job_store.all_jobs() if job.status == "queued"]
    print(f"{len(queued)} queued jobs, {workers} at the same time")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # list() waits for all jobs, and shows an error if a job crashed.
        list(pool.map(run_job, queued))


def stop(job_id):
    """Stop a job: mark it as stopped, then end its agent process.

    A queued job is only cancelled: no agent runs yet, and claim (under the same lock)
    starts only queued jobs, so a pool thread that picks it later does nothing.
    """
    with job_store.claim_lock():
        job = job_store.load(job_id)
        if job.status == "queued":
            job.status = "stopped"
            job.finished_at = job_store.now()
            job_store.save(job)
            print(f"Cancelled job {job_id} (it had not started).")
            return
        if job.status not in ("waiting", "running"):
            print(f"Job {job_id} is not running (status: {job.status}).")
            return
        job_store.stop_file(job_id).touch()  # first: a worker's later save cannot undo this (the flag wins)
        since = job_store.saved_at(job_id)  # before the save below
        waiting = job.status == "waiting"
        job.status = "stopped"
        if waiting:
            job.finished_at = job_store.now()
        job_store.save(job)
    if waiting:  # nobody answers the question: the lead's held jobs can go on
        print(f"Stopped job {job_id} (it waited for an answer).")
        return
    if job.pid is None:
        # No agent runs now (for example, the check runs). The worker sees
        # "stopped" before the next agent call and ends the job.
        print(f"Stopped job {job_id}. A running check finishes first; no more agent calls.")
        return
    if not job_store.is_alive(job.pid, since):  # never signal a stranger that got the old pid
        print(f"Stopped job {job_id}. Its agent had already ended.")
        return
    kill_group(job.pid, signal.SIGTERM)  # the agent and its tools
    print(f"Stopped job {job_id}.")


def recover(why="the server stopped while the job was running"):
    """Mark running jobs whose worker process is dead as interrupted. Return those jobs.

    A job can run in the web server or in `jobs run`, so "running" alone does not
    mean "dead": we check the worker's process id. A job saved before we had the
    `worker` field counts as dead when its agent process is dead too.
    If the job's old agent process still runs, nobody reads its output any more:
    we stop it, so it cannot change files while the job waits for Resume.
    """
    interrupted = []
    with job_store.claim_lock():
        for job in job_store.all_jobs():
            since = job_store.saved_at(job.id)  # pids saved before a reboot belong to other processes now
            if job.status != "running":
                if job.worker is not None and not job_store.is_alive(job.worker, since):
                    job.worker, job.pid = None, None  # ended before its last save (a crash)
                    job_store.save(job)
                continue
            if job.worker is not None and job_store.is_alive(job.worker, since):
                continue
            if job.worker is None and job.pid is not None and job_store.is_alive(job.pid, since):
                continue
            if job.pid is not None and job_store.is_alive(job.pid, since):
                kill_group(job.pid, signal.SIGTERM)
            if job_store.stop_file(job.id).exists():  # the user stopped it before the worker died
                job.status = "stopped"
                job.finished_at = job_store.now()
                job.pid = None
                job.worker = None
                job_store.save(job)
                continue
            job.status = "interrupted"
            job.interrupted_why = why
            job.pid = None
            job.worker = None
            job_store.save(job)
            interrupted.append(job)
    return interrupted


def resume(job_id):
    """Put an interrupted job back in the queue. Return the job.

    The next run sends the resume brief to the job's session (see `work`), so
    the agent continues and does not start again from the beginning.
    """
    with job_store.claim_lock():
        job = job_store.load(job_id)
        if job.status != "interrupted":
            raise ValueError(f"Job {job_id} is not interrupted (status: {job.status}).")
        workspaces.check_rerun(job)
        job.status = "queued"
        job.resumes += 1
        # Cut during its planning call (no plan, no finished call): the run starts the
        # planning call again (read-only), instead of a resume that may edit at once.
        job.resuming = not job_talk.needs_plan(job)
        job_store.save(job)
        return job


def waiting_job(desk, other_than):
    """Return the id of the desk's job that waits for an answer (not other_than), or None.

    desk: "Pip" or a twin "Pip~2" (an old job without a desk ran on its lead's own desk).
    A queued job that still has its question was answered and waits for its run: its
    conversation comes first, or the answer would land in the middle of another job.
    """
    for job in job_store.all_jobs():
        answered = job.status == "queued" and job.question is not None
        if (job.desk or job.lead) == desk and (job.status == "waiting" or answered) and job.id != other_than:
            return job.id
    return None


def release_held(job_id):
    """Return the queued jobs that may run now that job_id's run ended (or was stopped):
    the jobs that waited for its answer, and the oldest job that waits for a free desk of its lead.
    """
    every = job_store.all_jobs()
    held = [job for job in every if job.held_for == job_id and job.status == "queued"]
    ended = next((job for job in every if job.id == job_id), None)
    if ended is not None and ended.lead:
        waiting = sorted((job for job in every if job.waits_for == ended.lead and job.status == "queued"
                          and job not in held), key=lambda job: job.created_at)
        held += waiting[:1]  # one: that job frees a desk again when it ends, and starts the next
    return held


def desk_candidates(job):
    """Return the desks that may run a lead's job, in order (leads.py: desks and twins).

    A job that already ran (a follow-up, an answer, a resume) continues on its own desk, where
    its conversation is. A lesson job asks the lead itself. A new job may go to a twin when the
    twin can do the same work: the lead only reads, or the job edits in its own workspace.
    """
    if job.desk or job.session_id or job.lesson_for:
        return [job.desk or job.lead]
    edits = any(tool in safety.EDIT_TOOLS for tool in leads.roster()[job.lead]["tools"])
    twins = job_store.settings()["lead_twins"] if not edits or workspaces.active(job) else 0
    return leads.desks(job.lead, twins)


def take_desk_for(job):
    """Take a free desk for a lead's job, without waiting. Set job.desk. Return (lock, held_by, waits).

    lock: the held desk (leads.release_desk frees it), or None. A desk whose session has an open
    question is skipped (its conversation must not mix with another job). When no desk is free:
    held_by is that question's job when every desk waits for an answer, else waits = the lead.
    """
    candidates, held_by = desk_candidates(job), None
    for candidate in candidates:
        desk, lock = leads.take_desk([candidate])
        if desk is None:
            continue  # busy
        waiting = waiting_job(desk, job.id)  # read under the desk's lock: exact
        if waiting is None:
            if job.desk != desk:
                job.desk = desk
                job_store.save(job)
            if desk != job.lead:
                print(f"{' ' * 25}{job.lead} is busy: the twin desk {desk} takes the job")
            return lock, None, None
        leads.release_desk(lock)
        held_by = held_by or waiting
    busy = [candidate for candidate in candidates if waiting_job(candidate, job.id) is None]
    if busy:
        return None, None, job.lead
    return None, held_by or waiting_job(candidates[0], job.id), None


def desk_free(job):
    """Return True if a desk that could run the queued job is free now (it takes and frees it)."""
    for candidate in desk_candidates(job):
        desk, lock = leads.take_desk([candidate])
        if desk is not None:
            leads.release_desk(lock)
            return True
    return False


def next_of_routine(job):
    """Return the oldest queued job of the same routine (in a list), or [].

    One at a time: that job starts the next one when it ends (routine_wait_reason).
    """
    if not job.routine:
        return []
    waiting = [other for other in job_store.all_jobs() if other.routine == job.routine and other.status == "queued"]
    return sorted(waiting, key=lambda other: other.created_at)[:1]
