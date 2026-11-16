"""Router: choose the model and the effort for a task.

Three routers, tried in this order:
    1. rules - keywords; free and instant, no AI
    2. jev   - a small router model on OpenRouter (needs an OpenRouter key)
    3. haiku - Claude Haiku through `claude -p`; the fallback

Every choice is logged to data/router_log.jsonl (one JSON object per line).

Usage (from the project folder):
    python -m agentic_os.router route "your task"
    python -m agentic_os.router compare
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from agentic_os.ask import ask

MODELS = ["haiku", "sonnet", "opus"]
EFFORTS = ["low", "medium", "high", "xhigh"]

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOG_FILE = DATA_DIR / "router_log.jsonl"
LOG_LOCK = threading.Lock()

# The Haiku router runs in this folder, outside the project,
# so it does not read our CLAUDE.md (a smaller context is cheaper).
ROUTER_DIR = Path.home() / ".cache" / "agentic-os" / "router"

JEV_URL = "https://openrouter.ai/api/v1/chat/completions"
JEV_MODEL = "typesafe/jev-router"
JEV_KEY_FILE = Path.home() / ".config" / "agentic-os" / "openrouter.key"

SYSTEM_PROMPT = (
    "You are a router for AI agents. Pick the smallest Claude model "
    "(haiku, sonnet, opus) and the lowest effort (low, medium, high, xhigh) "
    "that will do the task well. "
    "haiku: short answers, simple facts, lists, small edits. "
    "sonnet: normal code, long writing, reviews. "
    "opus: hard design, hard debugging, deep analysis. "
    'Reply only with JSON: {"model": "...", "effort": "...", "why": "<one short sentence>"}'
)

SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string", "enum": MODELS},
        "effort": {"type": "string", "enum": EFFORTS},
        "why": {"type": "string"},
    },
    "required": ["model", "effort", "why"],
}

# Rules: (keywords, model, effort). The first rule with a matching word wins.
RULES = [
    (["architecture", "design", "debug", "investigate"], "opus", "high"),
    (["story", "essay", "article", "refactor", "review", "implement"], "sonnet", "medium"),
    (["say", "translate", "capital", "list", "rename"], "haiku", "low"),
]

# A small test set for `compare`. "expected" is our own judgment of the best model.
TEST_TASKS = [
    ("Say the number 7 and nothing else.", "haiku"),
    ("What is the capital of Bulgaria?", "haiku"),
    ("Translate 'good morning' into German.", "haiku"),
    ("List the Python files in this folder.", "haiku"),
    ("Write a 1500-word story about a robot.", "sonnet"),
    ("Write a Python function that parses a CSV file, with tests.", "sonnet"),
    ("Review this pull request for bugs and style problems.", "sonnet"),
    ("Design the architecture for a system that runs 100 agents in parallel.", "opus"),
    ("Debug a race condition that appears once in 1000 runs.", "opus"),
    ("Find out why our web server gets slow after two days.", "opus"),
]


def check(choice):
    """Make sure a router's choice is valid. Return a clean dict."""
    if choice.get("model") not in MODELS:
        raise ValueError(f"unknown model: {choice.get('model')}")
    effort = choice.get("effort")
    if effort not in EFFORTS:
        effort = "medium"
    return {"model": choice["model"], "effort": effort, "why": str(choice.get("why", ""))[:160]}


def route_rules(task):
    """Choose by keywords. Return (choice, cost), or None if no rule matches."""
    words = set(re.findall(r"[a-z]+", task.lower()))
    for keywords, model, effort in RULES:
        found = [word for word in keywords if word in words]
        if found:
            return {"model": model, "effort": effort, "why": f"keyword: {found[0]}"}, 0.0
    return None


def openrouter_key():
    """Return the OpenRouter key from the environment or from a file, or None."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    if JEV_KEY_FILE.exists():
        return JEV_KEY_FILE.read_text().strip()
    return None


def route_jev(task):
    """Ask the Jev router model on OpenRouter. Return (choice, cost)."""
    key = openrouter_key()
    if key is None:
        return None  # Jev is optional: without a key it is skipped quietly (not a failure)
    body = {
        "model": JEV_MODEL,
        "temperature": 0,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
        "usage": {"include": True},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task[:4000]},
        ],
    }
    request = urllib.request.Request(
        JEV_URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        reply = json.load(response)
    choice = json.loads(reply["choices"][0]["message"]["content"])
    return check(choice), reply.get("usage", {}).get("cost", 0.0)


def route_haiku(task):
    """Ask Claude Haiku, with no tools and a JSON schema. Return (choice, cost)."""
    ROUTER_DIR.mkdir(parents=True, exist_ok=True)
    reply = ask(
        f"Task: {task}",
        model="haiku",
        effort="low",
        extra_args=[
            "--tools", "",
            "--system-prompt", SYSTEM_PROMPT,
            "--json-schema", json.dumps(SCHEMA),
        ],
        cwd=ROUTER_DIR,
    )
    return check(reply["structured_output"]), reply["total_cost_usd"]


ROUTERS = {"rules": route_rules, "jev": route_jev, "haiku": route_haiku}


def log(decision):
    """Add one decision as one line to the log file."""
    DATA_DIR.mkdir(exist_ok=True)
    with LOG_LOCK, LOG_FILE.open("a") as file:
        file.write(json.dumps(decision) + "\n")


def run_router(name, task):
    """Run one router, measure it and log the choice. Return the decision or None."""
    start = time.monotonic()
    result = ROUTERS[name](task)
    if result is None:
        return None
    choice, cost = result
    decision = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "router": name,
        "task": task,
        **choice,
        "cost": cost,
        "ms": round((time.monotonic() - start) * 1000),
    }
    try:
        log(decision)
    except OSError as error:  # the decision stands; only the report misses one line
        print(f"Could not log the router decision: {error}", file=sys.stderr)
    return decision


def route(task):
    """Try the routers in order: rules, then Jev, then Haiku."""
    for name in ROUTERS:
        try:
            decision = run_router(name, task)
        except Exception as error:
            print(f"Router {name} failed: {error}", file=sys.stderr)
            continue
        if decision is not None:
            return decision
    return {"router": "default", "model": "haiku", "effort": "low", "why": "all routers failed"}


def compare():
    """Run every router on the test set and print speed, cost and quality."""
    print(f"{'router':<7} {'answered':>8} {'correct':>8} {'avg ms':>7} {'cost':>8}")
    for name in ROUTERS:
        answered = correct = total_ms = 0
        total_cost = 0.0
        for task, expected in TEST_TASKS:
            try:
                decision = run_router(name, task)
            except Exception as error:
                print(f"{name:<7} skipped: {error}")
                break
            if decision is None:
                continue
            answered += 1
            correct += decision["model"] == expected
            total_ms += decision["ms"]
            total_cost += decision["cost"]
            if decision["model"] != expected:
                print(f"        {name} chose {decision['model']}, expected {expected}: {task}")
        else:
            avg_ms = total_ms // answered if answered else 0
            print(
                f"{name:<7} {answered:>5}/{len(TEST_TASKS)} {correct:>5}/{answered:<2}"
                f" {avg_ms:>7} ${total_cost:>7.4f}"
            )


def main():
    parser = argparse.ArgumentParser(description="Choose the model for a task.")
    commands = parser.add_subparsers(dest="command", required=True)
    route_parser = commands.add_parser("route", help="choose the model for one task")
    route_parser.add_argument("task")
    commands.add_parser("compare", help="compare the routers on a test set")
    args = parser.parse_args()

    if args.command == "route":
        print(json.dumps(route(args.task), indent=2))
    elif args.command == "compare":
        compare()


if __name__ == "__main__":
    main()
