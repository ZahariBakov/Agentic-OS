"""Ask two questions in the same session and print the total cost.

This is the "done" test for Phase 1: the second answer shows that the agent
remembers the first question, because both calls use the same session.

Usage (from the project folder):
    python -m agentic_os.two_questions
"""

import argparse

from agentic_os.ask import ask


def main():
    argparse.ArgumentParser(description="Ask two questions in the same session (two cheap Haiku calls).").parse_args()
    first = ask("My name is Alex. Remember it. Answer in one short sentence.")
    print("Answer 1:", first["result"])

    second = ask("What is my name? Answer in one word.", session_id=first["session_id"])
    print("Answer 2:", second["result"])

    print()
    print(f"Session 1: {first['session_id']}")
    print(f"Session 2: {second['session_id']}")
    total = first["total_cost_usd"] + second["total_cost_usd"]
    print(f"Total cost: ${total:.4f}")


if __name__ == "__main__":
    main()
