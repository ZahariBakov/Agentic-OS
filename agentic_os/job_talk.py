"""Job talk: the written conversation between the user and a job's agent.

It owns the agent's questions (`<question>`), its plans with a cost (`<plan>`), the
user's decisions about a plan, and follow-up messages. It builds the prompts and
changes the job's record; it never starts an agent (that is job_run.py).

Imports job_store only (of the job_* modules).
"""

import re
import time
from datetime import datetime

from agentic_os import job_store, leads, memory, suggest, workspaces


PLAN_MODELS = ("opus",)  # a job on these (expensive) models proposes a plan first

# Added to the task of a job that may ask (not routines, not ingest).
ASK_RULES = """

If you cannot do the task well without a decision or a fact that only the user has,
end your answer with one question block, and stop there:
<question>
The question, with the context the user needs.
- a short option
- another short option
</question>
Give 2 to 5 options when the answer is a choice (the user can still write their own).
Ask only when the answer changes what you will do. Do not ask for things you can
find out yourself, and ask everything you need in one question."""

QUESTION_PATTERN = re.compile(r"<question>(.*?)</question>", re.DOTALL)
MAX_OPTIONS = 5

PLAN_RULES = """

This job needs a plan first. You can only read now: do not try to change anything.
Look at what you need, then end your answer with one plan block, and stop there:
<plan>
1. A short step.
2. Another step.
Estimate: $0.40
</plan>
Plan only steps that your tools can do: you cannot run shell commands (if the job
has a check, Python runs it after your work). The estimate is your guess of the total
cost of the work in US dollars. {reference}
The user approves, changes or rejects the plan. Then you do the work."""

PLAN_PATTERN = re.compile(r"<plan>(.*?)</plan>", re.DOTALL)
ESTIMATE_PATTERN = re.compile(r"^\s*estimate:(.*)$", re.IGNORECASE | re.MULTILINE)  # the whole line
AMOUNT_PATTERN = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")  # "1,200.50"
PLAN_DECISION = {
    "approved": "The user approved your plan. Do the work now, then give your full answer.",
    "changed": "The user approved your plan with these changes:\n\n{note}\n\nChange the plan like this, do the work, then give your full answer.",
}

ANSWER_PROMPT = """The user answered your question:

{messages}

Continue the task with this answer."""

FOLLOWUP_PROMPT = """The user wrote a follow-up message about this job:

{messages}

Answer it. Your answer replaces your last answer, so make it complete."""


def message_text(messages):
    """Return the user's messages as one text, one paragraph per message."""
    return "\n\n".join(message["text"] for message in messages)


def can_ask(job):
    """Return True if the job's agent may ask the user. Routines and ingest run alone: nobody is there."""
    return not job.routine and not job.source and not job.lesson_for and not job.parent


def needs_plan(job):
    """Return True if the job's first call must propose a plan (Python decides, not the agent)."""
    return (can_ask(job) and job.plan is None and job.tries == 0
            and (job.plan_first or job.model in PLAN_MODELS))


def extract_plan(text):
    """Find the last <plan> block. Return (plan or None, the answer without it).

    plan is {"text", "estimate"}: the "Estimate: $0.40" line is taken out of the
    text; estimate is None when the agent gave none.
    """
    blocks, rest = memory.blocks_outside_code(PLAN_PATTERN, text)  # a block inside code is an example
    if not blocks:
        return None, text
    found = ESTIMATE_PATTERN.search(blocks[-1].group(1))
    plan_text = ESTIMATE_PATTERN.sub("", blocks[-1].group(1)).strip()
    if not plan_text.strip():
        return None, text
    return {"text": plan_text.strip(), "estimate": estimate_of(found[1]) if found else None}, rest.strip()


def estimate_of(line):
    """Return the estimate in a line like " $0.30 - $0.90" or "$1,200": the biggest amount, or None.

    The biggest, because auto-approve compares it with a limit: a range must not pass by its low end.
    """
    amounts = [float(amount.replace(",", "")) for amount in AMOUNT_PATTERN.findall(line)]
    return max(amounts) if amounts else None


def cost_reference(model):
    """Return a sentence with what earlier done jobs on this model really cost, for the estimate."""
    costs = sorted(job.cost for job in job_store.all_jobs() if job.model == model and job.status == "done" and job.cost > 0)
    if len(costs) < 3:
        return f"There is no cost history for your model ({model}) yet."
    median = costs[len(costs) // 2]
    return (f"For reference, earlier jobs on your model ({model}) really cost: median {job_store.usd(median)}, "
            f"the most {job_store.usd(costs[-1])} ({len(costs)} jobs).")


def auto_approve_reason(plan):
    """Return why a plan needs no OK, or None. A plan without an estimate always needs the OK."""
    limit = job_store.settings()["auto_approve_plans_usd"]
    if limit > 0 and plan["estimate"] is not None and plan["estimate"] <= limit:
        return f"approved automatically: the estimate is not more than ${limit:.2f}"
    return None


def plan_to_conversation(job):
    """Move the agent's words and its plan into the conversation (the decision follows them)."""
    if job.result:
        job.messages.append({"from": "agent", "text": job.result, "at": job.plan["at"]})
        job.result = None
    estimate = job_store.usd(job.plan["estimate"]) if job.plan["estimate"] is not None else "none"
    job.messages.append({"from": "agent", "text": f"Plan (estimate {estimate}, a guess):\n{job.plan['text']}",
                         "at": job.plan["at"]})


def extract_question(text):
    """Find the last <question> block in an answer. Return (question or None, the answer without it).

    question is {"text", "options"}: "- " lines are options (at most MAX_OPTIONS),
    the other lines are the question. Without options, Python suggests some from
    the question's words (suggest.py), and the question gets "suggested": True.
    """
    blocks, rest = memory.blocks_outside_code(QUESTION_PATTERN, text)  # a block inside code is an example
    if not blocks:
        return None, text
    lines = [line.strip() for line in blocks[-1].group(1).strip().splitlines()]
    options = [line[2:].strip() for line in lines if line.startswith("- ") and line[2:].strip()]
    question = "\n".join(line for line in lines if not line.startswith("- ")).strip()
    if not question:
        return None, text
    rest = rest.strip()
    if not options:
        options = suggest.suggest(question)
        if options:
            return {"text": question, "options": options[:MAX_OPTIONS], "suggested": True}, rest
    return {"text": question, "options": options[:MAX_OPTIONS]}, rest


def prompt_for(messages):
    """Return the prompt for the user's messages: an answer to a question, or a follow-up."""
    if all(message.get("plan") for message in messages):  # a plan decision is a ready prompt
        return message_text(messages)
    if any(message.get("answer") for message in messages):
        return ANSWER_PROMPT.format(messages=message_text(messages))
    return FOLLOWUP_PROMPT.format(messages=message_text(messages))


def add_messages(job, answer, messages):
    """Move the answer so far into the conversation, then the user's new messages."""
    if not messages:
        return
    if answer:
        job.messages.append({"from": "agent", "text": answer, "at": job_store.now()})
    if job.question:  # the user's messages answer it
        job.messages.append({"from": "agent", "text": job.question["text"], "at": job.question["at"],
                             "options": job.question["options"]})
        job.question = None
    job.messages.extend(messages)
    job_store.save(job)


def last_answer(job):
    """Return the agent's last words on the job, for a session that does not have them.

    job.result, else the agent's last messages (a plan moves the answer and the plan into
    the conversation), plus a question that waits for the answer the user now sends.
    """
    answer = job.result
    if not answer:
        last = []
        for message in reversed(job.messages):
            if message.get("from") != "agent":
                break
            last.insert(0, message["text"])
        answer = "\n\n".join(last)
    if job.question:
        answer = f"{answer}\n\nYour question:\n{job.question['text']}".strip()
    return answer or "(none)"


def cold_warning(job):
    """Return a warning when a follow-up would resume a cold session, else None.

    A session that was idle longer than the prompt cache lives (leads.CACHE_TTL)
    is reloaded at full price. A lead's job is left out: it goes to the lead's
    current session, and leads.prepare decides about that one.
    """
    if not job.session_id or job.lead or job.status in ("queued", "running"):
        return None
    idle = time.time() - last_activity(job)
    if idle <= leads.CACHE_TTL:
        return None
    return (f"The session was idle for {idle / 3600:.1f} hours (the cache is cold): "
            "the next call reloads the whole context at full price.")


def last_activity(job):
    """Return when the job's session was last used (Unix time).

    The end of its last run, or when it asked its question or proposed its plan. Not the
    job file's time: a review, an undo or an apply saves the file without using the session.
    """
    times = [job.finished_at, (job.question or {}).get("at"), (job.plan or {}).get("at")]
    found = [datetime.fromisoformat(value).timestamp() for value in times if value]
    if found:
        return max(found)
    return (job_store.JOBS_DIR / f"{job.id}.json").stat().st_mtime  # an old job without these times


def decide(job, status, note, auto=None):
    """Record a decision about the pending plan on `job` (the caller saves it).

    approved/changed: the decision goes to the inbox as a ready prompt, so the
    job runs again in the same session. rejected: the job stops, with no agent call.
    """
    plan_to_conversation(job)
    job.plan.update(status=status, note=note.strip(), auto=auto, decided_at=job_store.now())
    if status == "rejected":
        job.status = "stopped"
        job.finished_at = job_store.now()
        job.error = "the plan was rejected" + (f": {note.strip()}" if note.strip() else "")
        return
    text = PLAN_DECISION[status].format(note=note.strip())
    # "text" is the prompt for the agent; "note" is what the conversation shows: the user's words.
    shown = note.strip() or (auto.capitalize() if auto else "Approved the plan.")
    job_store.write_inbox(job.id, {"from": "user", "text": text, "note": shown, "at": job_store.now(), "plan": True})


def decide_plan(job_id, decision, note=""):
    """The user's decision about a job's plan: approve, change (with a note) or reject.

    Return (job, start): start is True when the job went back to the queue.
    """
    status = {"approve": "approved", "change": "changed", "reject": "rejected"}.get(decision)
    if status is None:
        raise ValueError("The decision is approve, change or reject.")
    if status == "changed" and not note.strip():
        raise ValueError("Write the changes to the plan.")
    with job_store.claim_lock():
        job = job_store.load(job_id)
        if job.status != "waiting" or not job.plan or job.plan["status"] != "pending":
            raise ValueError(f"No plan waits for your decision on job {job_id}.")
        if job_store.worker_busy(job):
            # "waiting" is saved before the run has really ended; the worker's last
            # save would undo this decision. It takes a moment.
            raise job_store.busy_error(job_id)
        decide(job, status, note)
        if status != "rejected":
            job.status = "queued"
        job_store.save(job)
        return job, status != "rejected"


def follow_up(job_id, text, shown=None):
    """Write a message to a job, during or after its run. Return (job, start, warning).

    shown: what the conversation shows, when `text` is a longer prompt made from the
    user's words (a review); None shows `text`.

    start is True when the job went back to the queue and the caller must start it.
    While the job is queued or running, the message waits in the inbox, and the
    agent reads it after its current call (in the same run). After the run, the
    job goes back to the queue and continues the same session. warning is a text
    when the session is cold (resuming it reloads the context at full price).
    """
    if not text.strip():
        raise ValueError("The message is empty.")
    job = job_store.load(job_id)
    if job.status == "waiting" and job.plan and job.plan["status"] == "pending":
        job, start = decide_plan(job_id, "change", text)  # a message to a waiting plan changes it
        return job, start, None
    with job_store.claim_lock():
        job = job_store.load(job_id)
        if job.status == "interrupted":
            raise ValueError(f"Job {job_id} is interrupted: resume it first.")
        workspaces.check_rerun(job)
        if job.status == "stopped" and job_store.worker_busy(job):
            raise job_store.busy_error(job_id)  # the end of a stopped run never takes the inbox
        message = {"from": "user", "text": text, "at": job_store.now()}
        if shown is not None:
            message["note"] = shown
        if job.status == "waiting":
            message["answer"] = True  # the agent gets ANSWER_PROMPT
        job_store.write_inbox(job_id, message)
        if job.status in ("queued", "running") or job_store.worker_busy(job):
            # The run has not ended yet ("waiting" is saved a moment before the end):
            # the worker finds the message at its end and runs the job again.
            return job, False, None
        warning = cold_warning(job)  # before the save below: it reads the file's time
        job.status = "queued"
        job_store.save(job)
        return job, True, warning
