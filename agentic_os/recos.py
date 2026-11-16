"""Recos: find tasks you ask for again and again, and suggest them as routines.

A routine that suggests routines. It is exact work, so it is
Python only, no AI, and it costs nothing:

1. Collect your requests of the last days: the prompts you typed in Claude
   Code (~/.claude/history.jsonl) and the jobs of Agent Office. Jobs from
   routines, skills and ingest are skipped: they are already automatic.
2. Turn each request into a set of words (without small words like "the").
3. Put similar requests into groups. Two requests are similar when they share
   many words (Jaccard similarity: shared words / all words).
4. A group with MIN_REPEATS or more requests becomes a card: a suggested
   routine with a name, a schedule and a mode (active or passive).

Memory cards ("correct the same thing twice -> the memory missed something"): the review notes you gave a lead (Reject, Request changes)
are grouped the same way. Two similar corrections make a card. "Set up" adds a
lesson job for the lead; its lesson waits as a draft, like any other lesson.

The cards are saved in data/recos.json (`refresh`, also a weekly routine
action). A card you set up or dismiss is "handled": its id is remembered and it
does not come back. A memory card's id includes the number of corrections, so a
new correction on the same thing brings the card back.

Usage (from the project folder):
    python -m agentic_os.recos suggest [--days 7] [--min 3] [--json]   compute, save nothing
    python -m agentic_os.recos refresh                                  compute and save the cards
    python -m agentic_os.recos list                                     the saved cards
    python -m agentic_os.recos setup <card id> [<card id> ...]          a routine file or a lesson job
    python -m agentic_os.recos dismiss <card id>                        never show this card again
"""

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from agentic_os import job_review, job_store, jobs, leads, memory, routines
from agentic_os.routines import DAYS

HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"
DAYS_BACK = 7
MIN_REPEATS = 3  # how many similar requests make a card
SIMILAR = 0.4  # the Jaccard similarity that puts a request into a group
MIN_WORDS = 2  # a request with fewer real words cannot be compared
MIN_LENGTH = 12  # shorter texts ("ok", "next step") are not tasks
MAX_CARDS = 12
RECOS_FILE = job_store.DATA_DIR / "recos.json"
SIMILAR_CORRECTIONS = 0.3  # corrections are short, so they group at a lower similarity
MIN_CORRECTIONS = 2  # the same correction twice makes a memory card
MIN_CORRECTION_WORDS = 3
CORRECTIONS = ("rejected", "changes")  # review verdicts with the user's words about what was wrong

STOP_WORDS = frozenset("""
a an the and or but if then to of in on at for from with by is are was were be been it its
this that these those i you me my your we our us can could would should will just please now
also not no yes do does did make get let use using into out up so as about what which who how
when where why there here all any some more most very really want need like ok okay step next
""".split())
WORD = re.compile(r"[^\W\d_][\w-]{2,}")  # a word: starts with a letter, 3+ characters
PASTED = re.compile(r"\[(?:Pasted text|Image) #\d+[^\]]*\]", re.IGNORECASE)
NOISE = re.compile(r"https?://\S+|[~/][\w./-]+|\[(?:Pasted text|Image) #\d+[^\]]*\]", re.IGNORECASE)
QUESTION = re.compile(r"\?\s*$|^(is|are|can|could|would|should|do|does|did|what|why|how|which|where|when|who)\b",
                      re.IGNORECASE)
PASSIVE = re.compile(r"\b(check|watch|monitor|scan|look for|alert|any new|status)\b", re.IGNORECASE)


@dataclass
class Request:
    text: str
    at: datetime
    source: str  # "Claude Code" or "Agent Office"
    words: frozenset


def word_list(text):
    """Return the real words of a text in their order: lower case, no links, paths, numbers or small words."""
    return [word for word in WORD.findall(NOISE.sub(" ", text.lower())) if word not in STOP_WORDS]


def words(text):
    return frozenset(word_list(text))


def jaccard(a, b):
    """Return how similar two word sets are: 0.0 (nothing shared) to 1.0 (the same)."""
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def request(text, at, source):
    """Return a Request, or None if the text is not a task we can compare."""
    text = " ".join(text.split())
    if len(text) < MIN_LENGTH or text.startswith("/") or QUESTION.search(text):
        return None
    if PASTED.search(text):  # "[Pasted text #1 +40 lines]": a routine would not have that text
        return None
    found = words(text)
    return Request(text, at, source, found) if len(found) >= MIN_WORDS else None


def from_history(path, since):
    """Return the requests typed in Claude Code since the given time."""
    if not path.is_file():
        return []
    found = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
            at = datetime.fromtimestamp(entry["timestamp"] / 1000)
        except (ValueError, KeyError, TypeError):
            continue  # a broken line: skip it
        if at >= since and (item := request(str(entry.get("display", "")), at, "Claude Code")):
            found.append(item)
    return found


def from_jobs(since):
    """Return the jobs you started by hand since the given time (not from routines, skills, ingest or lessons)."""
    found = []
    for job in jobs.all_jobs():
        if job.routine or job.skill or job.source or job.lesson_for or job.parent or not job.created_at:
            continue
        at = datetime.fromisoformat(job.created_at)
        if at >= since and (item := request(job.task, at, "Agent Office")):
            found.append(item)
    return found


def make_groups(requests, similar=SIMILAR):
    """Put similar requests into groups. Return a list of (requests, word counts).

    Each request joins the most similar group, if that group is similar enough.
    A group is compared by its common words: the words in at least half of its requests.
    """
    groups = []  # [requests, word counts, common words]
    for item in requests:
        best = max(groups, key=lambda group: jaccard(item.words, group[2]), default=None)
        if best is not None and jaccard(item.words, best[2]) >= similar:
            best[0].append(item)
            best[1].update(item.words)
            best[2] = frozenset(word for word, count in best[1].items() if count * 2 >= len(best[0]))
        else:
            groups.append([[item], Counter(item.words), item.words])
    return [(items, counts) for items, counts, _ in groups]


def card(items, counts, days):
    """Turn a group of similar requests into a suggested routine (a dict)."""
    items = sorted(items, key=lambda item: item.at, reverse=True)  # newest first
    # The most common words. A set has no fixed order (it changes between runs), so ties
    # go by the order in the newest request, then A-Z. The title and id stay the same.
    order = {word: place for place, word in reversed(list(enumerate(word_list(items[0].text))))}
    top = sorted(counts, key=lambda word: (-counts[word], order.get(word, len(order)), word))[:4]
    day = Counter(item.at.weekday() for item in items).most_common(1)[0][0]
    hour = Counter(item.at.hour for item in items).most_common(1)[0][0]
    on_many_days = len({item.at.date() for item in items}) >= 4
    schedule = f"daily {hour:02}:00" if on_many_days else f"weekly {DAYS[day]} {hour:02}:00"
    passive = any(PASSIVE.search(item.text) for item in items)
    sources = sorted({item.source for item in items})
    return {
        "id": hashlib.sha1(" ".join(sorted(top)).encode()).hexdigest()[:10],
        "kind": "routine",
        "title": " | ".join(top),
        "count": len(items),
        "what": f"You asked for this {len(items)} times in {days} days ({', '.join(sources)}). "
                + ("As a passive routine it adds the job on time; the job waits until you start it."
                   if passive else "As an active routine it runs by itself and gives you the result."),
        "examples": [item.text[:220] for item in items[:3]],
        "keys": [key(item) for item in items],
        "routine": {"name": "-".join(top[:3]), "task": items[0].text, "schedule": schedule,
                    "mode": "passive" if passive else "active"},
    }


def key(item):
    """Return a request's key: where and when it was typed (it never changes, unlike a card's words)."""
    return f"{item.source}|{item.at.isoformat()}"


def suggest(days=DAYS_BACK, minimum=MIN_REPEATS, now=None, history=None):
    """Return the cards for the last `days` days, the biggest group first."""
    since = (now or datetime.now()) - timedelta(days=days)
    # HISTORY_FILE is read here, not as the default value: a default is fixed when the
    # function is defined, so a test could not move it.
    requests = sorted(from_history(history or HISTORY_FILE, since) + from_jobs(since), key=lambda item: item.at)
    groups = [(items, counts) for items, counts in make_groups(requests) if len(items) >= minimum]
    groups.sort(key=lambda group: len(group[0]), reverse=True)
    return [card(items, counts, days) for items, counts in groups[:MAX_CARDS]]


def corrections(lead):
    """Return the corrections the user gave a lead (review notes on its jobs) as Requests.

    Lesson jobs are skipped: a review of a lesson is not about the work.
    """
    found = []
    for job in jobs.all_jobs():
        review = job.review or {}
        if job.lead != lead or job.lesson_for or review.get("verdict") not in CORRECTIONS:
            continue
        text = " ".join(review.get("note", "").split())
        if len(word_list(text)) >= MIN_CORRECTION_WORDS:
            found.append(Request(text, datetime.fromisoformat(review["at"]), job.id, words(text)))
    return sorted(found, key=lambda item: item.at)


def memory_cards():
    """Return a card for each group of the same correction (MIN_CORRECTIONS+ times) of each lead.

    Every group, not only the biggest: after one card is handled, the next one shows.
    """
    cards = []
    for lead in sorted(leads.roster()):
        groups = [items for items, _ in make_groups(corrections(lead), SIMILAR_CORRECTIONS)
                  if len(items) >= MIN_CORRECTIONS]
        for group in sorted(groups, key=len, reverse=True):
            cards.append(memory_card(lead, sorted(group, key=lambda item: item.at, reverse=True)))  # newest first
    return cards


def memory_card(lead, items):
    """Turn one group of corrections of a lead into a memory card."""
    job_ids = [item.source for item in items]  # for a correction, `source` is the job id
    return {
        "id": "m-" + hashlib.sha1(f"{lead} {len(items)} {' '.join(sorted(job_ids))}".encode()).hexdigest()[:10],
        "kind": "memory",
        "lead": lead,
        "title": f"{lead}: the same correction {len(items)}x",
        "count": len(items),
        "what": f"You told {lead} the same thing {len(items)} times. Its memory missed it. "
                "Set up asks the lead for one lesson that covers all of them; it waits as a draft for you.",
        "examples": [item.text[:220] for item in items[:3]],
        "jobs": job_ids,
    }  # no "keys": a new correction after a handled card must bring it back (the lesson did not help)


# --- Saved cards: data/recos.json ---

def load_recos():
    """Return the saved cards: {"at", "days", "cards", "handled"} (empty before the first refresh)."""
    if not RECOS_FILE.exists():
        return {"at": None, "days": DAYS_BACK, "cards": [], "handled": []}
    return json.loads(RECOS_FILE.read_text())


def save_recos(recos):
    RECOS_FILE.parent.mkdir(parents=True, exist_ok=True)
    memory.write_atomic(RECOS_FILE, json.dumps(recos, indent=2))


def lock():
    """The web server and the CLI (or the weekly routine) may write the file at the same time."""
    return memory.file_lock(RECOS_FILE.with_name(".recos.lock"))


def refresh(now=None, days=DAYS_BACK):
    """Compute the cards again and save them, without the handled ones. Return (recos, new card ids)."""
    cards = suggest(days, now=now) + memory_cards()
    with lock():
        old = load_recos()
        handled = set(old["handled"])
        handled_keys = set(old.get("handled_keys", []))
        # A card with a request you already handled stays away, even when its words or its
        # group changed (then its id is new): Dismiss means "never again", Set up means "done".
        cards = [found for found in cards if found["id"] not in handled
                 and not handled_keys & set(found.get("keys", []))]
        known = {found["id"] for found in old["cards"]}
        recos = {"at": (now or datetime.now()).isoformat(timespec="seconds"), "days": days,
                 "cards": cards, "handled": old["handled"], "handled_keys": sorted(handled_keys)}
        save_recos(recos)
    return recos, [found["id"] for found in cards if found["id"] not in known]


def routine_action(limit):
    """The weekly `recos` routine action: refresh the cards. It adds no jobs (it costs nothing)."""
    _, new = refresh()
    print(f"Recos: {len(new)} new card(s).")
    return []


def free_name(name):
    """Return a routine name that matches routines.NAME_PATTERN and has no file yet."""
    base = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40].strip("-") or "routine"
    candidate, number = base, 2
    while (routines.ROUTINES_DIR / f"{candidate}.md").exists():
        candidate, number = f"{base}-{number}", number + 1
    return candidate


def write_routine(found):
    """Write routines/<name>.md for a routine card, with the default cost cap. Return the name.

    Python writes the file (routines/ is protected from agents). The new routine
    waits for its first scheduled time, like every new routine.
    """
    routine = found["routine"]
    name = free_name(routine["name"])
    path = routines.ROUTINES_DIR / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    memory.write_atomic(path, f"""---
description: {found['title']} (from Recos: you asked for this {found['count']} times)
schedule: {routine['schedule']}
mode: {routine['mode']}
model: haiku
memory: false
max_week_usd: {routines.DEFAULT_CAP:.2f}
---

{routine['task']}
""")
    try:
        routines.load(name)  # the same checks as for a routine that you write by hand
    except ValueError:
        path.unlink()
        raise
    return name


def ask_memory_lesson(found):
    """Add a lesson job for a memory card: the lead turns the repeated corrections into one lesson."""
    corrections_text = "\n".join(f"- {example}" for example in found["examples"])
    task = job_review.REPEATED_TASK.format(count=found["count"], corrections=corrections_text)
    lesson_job = jobs.add(task, lead=found["lead"], fields={"lesson_for": {"job": found["jobs"][0], "signal": "repeated"}})
    return lesson_job


def setup(card_ids):
    """Set up the chosen cards. Return (messages, lesson jobs to start).

    A routine card writes a routine file; a memory card adds a lesson job (the
    caller starts it: the web pool, or `jobs run`). Each card becomes handled.
    """
    messages, started = [], []
    with lock():
        recos = load_recos()
        chosen = [found for found in recos["cards"] if found["id"] in card_ids]
        missing = sorted(set(card_ids) - {found["id"] for found in chosen})
        if missing:
            raise ValueError(f"No such card: {', '.join(missing)}")
        for found in chosen:
            if found["kind"] == "routine":
                try:
                    name = write_routine(found)
                except ValueError as error:  # the card stays, so you can try again after a fix
                    messages.append(f"Not set up: {found['title']}: {error}")
                    continue
                messages.append(f"Routine '{name}' ({found['routine']['schedule']}, "
                                f"at most ${routines.DEFAULT_CAP:.2f} a week)")
            else:
                try:
                    lesson_job = ask_memory_lesson(found)
                except ValueError as error:  # for example, the lead was removed from roster.json
                    messages.append(f"Not set up: {found['title']}: {error}")
                    continue
                started.append(lesson_job)
                messages.append(f"Lesson job {lesson_job.id} for {found['lead']}")
            mark_handled(recos, found)
        recos["cards"] = [found for found in recos["cards"] if found["id"] not in recos["handled"]]
        save_recos(recos)
    return messages, started


def mark_handled(recos, found):
    """Remember a handled card: its id and its requests (see refresh)."""
    recos["handled"].append(found["id"])
    recos["handled_keys"] = sorted(set(recos.get("handled_keys", [])) | set(found.get("keys", [])))


def dismiss(card_id):
    """Never show this card again (you rejected it)."""
    with lock():
        recos = load_recos()
        if not any(found["id"] == card_id for found in recos["cards"]):
            raise ValueError(f"No such card: {card_id}")
        mark_handled(recos, next(found for found in recos["cards"] if found["id"] == card_id))
        recos["cards"] = [found for found in recos["cards"] if found["id"] != card_id]
        save_recos(recos)


def print_cards(cards):
    for found in cards:
        print(f"[{found['id']}] {found['kind']}: {found['title']} ({found['count']}x)")
        print(f"  {found['what']}")
        if found["kind"] == "routine":
            routine = found["routine"]
            print(f"  routine: {routine['name']} | {routine['schedule']} | {routine['mode']}")
        print(f"  e.g. {found['examples'][0]}\n")


def main():
    parser = argparse.ArgumentParser(description="Suggest routines for tasks you repeat by hand.")
    commands = parser.add_subparsers(dest="command", required=True)
    suggest_parser = commands.add_parser("suggest", help="show the suggested routines (saves nothing)")
    suggest_parser.add_argument("--days", type=int, default=DAYS_BACK, help="how many days back")
    suggest_parser.add_argument("--min", type=int, default=MIN_REPEATS, help="how many repeats make a card")
    suggest_parser.add_argument("--json", action="store_true", help="print JSON")
    commands.add_parser("refresh", help="compute the cards and save them in data/recos.json")
    commands.add_parser("list", help="show the saved cards")
    setup_parser = commands.add_parser("setup", help="set up cards: a routine file or a lesson job")
    setup_parser.add_argument("ids", nargs="+")
    dismiss_parser = commands.add_parser("dismiss", help="never show this card again")
    dismiss_parser.add_argument("id")
    args = parser.parse_args()

    try:
        if args.command == "suggest":
            cards = suggest(args.days, args.min) + memory_cards()
            if args.json:
                print(json.dumps(cards, indent=2))
                return
            if not cards:
                print(f"No task repeated {args.min}+ times in the last {args.days} days, and no repeated correction.")
            print_cards(cards)
        elif args.command == "refresh":
            recos, new = refresh()
            print(f"{len(recos['cards'])} card(s), {len(new)} new.")
        elif args.command == "list":
            cards = load_recos()["cards"]
            print_cards(cards) if cards else print("No saved cards. Run: python -m agentic_os.recos refresh")
        elif args.command == "setup":
            messages, started = setup(args.ids)
            print("\n".join(messages))
            if started:
                print("Start the lesson jobs with: python -m agentic_os.jobs run")
        else:
            dismiss(args.id)
            print(f"Dismissed {args.id}.")
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
