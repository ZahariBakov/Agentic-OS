"""Critic: a second agent, with a fresh context, compares a result with a reference.

The critic does not give a score (7/10 means little, and it changes from run to
run). It answers one question: "which answer does the task better?". A choice
between two real texts is easier to judge and more stable.

The test is blind. Python shows the two texts as "Answer A" and "Answer B" in a
random order, so the critic does not know which one is the new result. Python
then maps A and B back (exact work, so no AI).

The reference is a real example of a good answer (written by you, or an old
result you liked). When the reference wins, the critic's "fix" says what the
result must change. Later (Phase 9, stage 2) a job sends it back to the builder.

Every comparison is logged to data/critic_log.jsonl.

Usage (from the project folder):
    python -m agentic_os.critic compare "the task" <result file> <reference file> [--model haiku]
"""

import argparse
import json
import random
import threading
from datetime import datetime
from pathlib import Path

from agentic_os.ask import ask
from agentic_os.router import EFFORTS, MODELS

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOG_FILE = DATA_DIR / "critic_log.jsonl"
LOG_LOCK = threading.Lock()
# The critic runs outside the project, so it does not read our CLAUDE.md.
CRITIC_DIR = Path.home() / ".cache" / "agentic-os" / "critic"

SYSTEM_PROMPT = (
    "You are a strict critic. You get a task and two answers to it, A and B. "
    "Decide which answer does the task better. Judge only by the task: is it correct, "
    "complete, clear, and does it follow every instruction? A longer answer is not "
    "better by itself. You must choose A or B. "
    'Reply only with JSON: {"better": "A or B", "why": "<one or two sentences>", '
    '"fix": "<what the worse answer must change to beat the better one>"}. '
    "Write the fix as direct instructions to the author of the worse answer "
    '(for example "Add a short code example."). The author never sees the other '
    "answer, so do not mention A, B or the other answer in the fix."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "better": {"type": "string", "enum": ["A", "B"]},
        "why": {"type": "string"},
        "fix": {"type": "string"},
    },
    "required": ["better", "why", "fix"],
}

PROMPT = """# Task
{task}

# Answer A
{a}

# Answer B
{b}

Which answer does the task better?"""


def log(verdict):
    """Add one verdict as one line to the log file."""
    DATA_DIR.mkdir(exist_ok=True)
    with LOG_LOCK, LOG_FILE.open("a") as file:
        file.write(json.dumps(verdict) + "\n")


class NoChoice(ValueError):
    """The critic answered, but chose neither A nor B. `cost` is what the call cost."""

    def __init__(self, message, cost):
        super().__init__(message)
        self.cost = cost


def compare(task, result, reference, model="sonnet", effort="low", rng=random, on_start=None):
    """Ask a fresh critic which text does the task better: the result or the reference.

    Return {"winner": "result" or "reference", "why", "fix", "result_was", "cost", ...}.
    rng gives the random order; tests pass a fake one. on_start gets the critic's pid.
    """
    result_first = rng.random() < 0.5
    a, b = (result, reference) if result_first else (reference, result)
    CRITIC_DIR.mkdir(parents=True, exist_ok=True)
    reply = ask(
        PROMPT.format(task=task.strip(), a=a.strip(), b=b.strip()),
        model=model,
        effort=effort,
        extra_args=[
            "--tools", "",
            "--system-prompt", SYSTEM_PROMPT,
            "--json-schema", json.dumps(SCHEMA),
        ],
        cwd=CRITIC_DIR,
        on_start=on_start,
    )
    choice = reply.get("structured_output") or {}
    if choice.get("better") not in ("A", "B"):
        raise NoChoice(f"the critic gave no clear choice: {choice}", reply.get("total_cost_usd") or 0.0)
    result_was = "A" if result_first else "B"
    verdict = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "task": task[:200],
        "winner": "result" if choice["better"] == result_was else "reference",
        "why": str(choice.get("why", "")),
        "fix": str(choice.get("fix", "")),
        "result_was": result_was,
        "model": model,
        "cost": reply["total_cost_usd"],
    }
    log(verdict)
    return verdict


def main():
    parser = argparse.ArgumentParser(description="Compare a result with a reference (blind).")
    commands = parser.add_subparsers(dest="command", required=True)
    compare_parser = commands.add_parser("compare", help="which one does the task better?")
    compare_parser.add_argument("task", help="the task that both texts answer")
    compare_parser.add_argument("result", help="a file with the new result")
    compare_parser.add_argument("reference", help="a file with a good example answer")
    compare_parser.add_argument("--model", choices=MODELS, default="sonnet")
    compare_parser.add_argument("--effort", choices=EFFORTS, default="low")
    args = parser.parse_args()

    if args.command == "compare":
        verdict = compare(
            args.task, Path(args.result).read_text(), Path(args.reference).read_text(),
            model=args.model, effort=args.effort,
        )
        print(f"Winner: {verdict['winner']} (the result was answer {verdict['result_was']})")
        print(f"Why:    {verdict['why']}")
        print(f"Fix:    {verdict['fix']}")
        print(f"Cost:   ${verdict['cost']:.4f}")


if __name__ == "__main__":
    main()
