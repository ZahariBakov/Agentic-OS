"""Run one Claude Code agent and print what it does, live.

The call goes through ask.ask (rule: every `claude` call goes there), so it has
the timeout, saves the usage limits and ends the agent on an error.

Usage (from the project folder):
    python -m agentic_os.stream "your task"
    python -m agentic_os.stream --model sonnet "your task"
"""

import argparse
import json

from agentic_os.ask import ask
from agentic_os.router import MODELS


def show(event):
    """Print one event in a short form: text, tool calls and the final result."""
    if event["type"] == "assistant":
        for part in event["message"]["content"]:
            if part["type"] == "text":
                print(part["text"])
            elif part["type"] == "tool_use":
                print(f"[tool] {part['name']} {json.dumps(part['input'])}")
    elif event["type"] == "result":
        print()
        print(f"Cost:  ${event['total_cost_usd']:.4f}")
        print(f"Time:  {event['duration_ms'] / 1000:.1f} s")
        print(f"Turns: {event['num_turns']}")


def main():
    parser = argparse.ArgumentParser(description="Run one agent and show it live.")
    parser.add_argument("task", help="the task for the agent")
    parser.add_argument("--model", choices=MODELS, default="haiku", help="the model to use (default: haiku)")
    args = parser.parse_args()

    ask(args.task, args.model, on_event=show)


if __name__ == "__main__":
    main()
