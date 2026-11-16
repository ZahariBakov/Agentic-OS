"""Suggested answers for a question that an agent asks without options.

The agent
may end with a <question> block without "- " option lines. Then Python makes
one-click answers from the question's own words; it is exact work, so there is
no model and no cost:

  - a short list next to the question ("1. Keep it", "a) Remove it", "* Move it") -> those items
    (right after the question; right before it only when the question asks to choose)
  - "Which one: Postgres, MySQL or SQLite?"                                  -> Postgres, MySQL, SQLite
  - a yes/no question ("Should I ...?", "Do you want ...?", "Is it OK ...?") -> Yes, No

When none of these fit, nothing is suggested: the user writes the answer.

Usage (from the project folder):
    python -m agentic_os.suggest "Should I delete the old tests?"
"""

import argparse
import re

MAX = 5  # at most this many answers (like job_talk.MAX_OPTIONS)
MAX_LEN = 60  # characters; a longer item is not a short answer

LIST_ITEM = re.compile(r"^\s*(?:\d{1,2}[.)]|[a-e][.)]|[-*\u2022])\s+(.+)$", re.IGNORECASE)
# "Keep it - the old way" or "Keep it: the old way": the answer is the part before.
EXPLANATION = re.compile(r"\s+[\u2014\u2013-]\s+.*$|:\s.*$")
LAST_QUESTION = re.compile(r"[^.?!]*\?(?=[^?]*$)")  # the last sentence that ends with "?"
# "...: A, B or C?". The colon needs a space after it: "10:00 or 11:00" has colons inside the options.
CHOICE = re.compile(r"(?::\s|[\u2014\u2013]\s*)((?:[^:\u2014\u2013?]|:(?!\s))+?\sor\s[^?]+)\?$")
CHOICE_SPLIT = re.compile(r",\s*(?:or\s+)?|\s+or\s+")
CHOOSE = re.compile(r"\b(?:which|what|choose|pick|prefer|options?)\b", re.IGNORECASE)
YES_NO = re.compile(r"^(?:so,?\s+|ok,?\s+|and\s+)?(?:should|shall|do|does|did|can|could|would|will|is|are|may"
                    r"|want me to|ok to|okay to)\b", re.IGNORECASE)
# "Can you send the log?" asks for something; it is not a yes/no question.
REQUEST = re.compile(r"^(?:so,?\s+|ok,?\s+|and\s+)?(?:can|could|will|would) you\b"
                     r"(?!\s+(?:like|prefer|want|be ok))", re.IGNORECASE)


def tidy(text):
    """Remove bold marks and extra spaces, and a final . ; or ,

    Backticks and the case stay: the answer goes back to the agent, and a name in
    code (`run_job`) must reach it as it was.
    """
    text = re.sub(r"\*\*|__", "", text)
    return re.sub(r"[.;,]$", "", " ".join(text.split()))


def list_block(lines):
    """Return the list items at the start of these lines (blank lines between items are fine)."""
    items = []
    for line in lines:
        match = LIST_ITEM.match(line)
        if match:
            items.append(match.group(1))
        elif line.strip():
            break
    return items


def from_list(text):
    """Return the items of a short list next to the question (2 to MAX short items), else [].

    A list right after the question line is its options. A list right before it is
    options only when the question asks to choose ("Which one?"): otherwise it is
    the agent's report ("1. Read the file 2. Changed it. Should I add tests?").
    """
    lines = text.splitlines()
    asked = [number for number, line in enumerate(lines) if "?" in line and not LIST_ITEM.match(line)]
    if not asked:
        return []
    where = asked[-1]
    found = list_block(lines[where + 1:])
    if not found and CHOOSE.search(lines[where]):
        found = list_block(reversed(lines[:where]))[::-1]
    items = [tidy(EXPLANATION.sub("", item)) for item in found]
    if not 2 <= len(items) <= MAX or any(not item or len(item) > MAX_LEN for item in items):
        return []
    return items


def from_choice(question):
    """Return the parts of "Which one: A, B or C?", else []."""
    match = CHOICE.search(question)
    if not match:
        return []
    parts = [tidy(part) for part in split_outside_brackets(match.group(1)) if tidy(part)]
    if not 2 <= len(parts) <= MAX or any(len(part) > MAX_LEN or len(part.split()) > 6 for part in parts):
        return []
    return parts


def split_outside_brackets(text):
    """Split "A, B or C" at the commas and "or"s that are not inside ( ): "Postgres (fast, safe) or X"."""
    parts, start = [], 0
    for match in CHOICE_SPLIT.finditer(text):
        before = text[:match.start()]
        if before.count("(") > before.count(")"):
            continue  # inside brackets: part of one option
        parts.append(text[start:match.start()])
        start = match.end()
    return parts + [text[start:]]


def suggest(text):
    """Return suggested answers for a question text (a list, maybe empty)."""
    text = text or ""
    if "?" not in text:
        return []
    items = from_list(text)
    if items:
        return items
    # The last line that asks: list lines before it ("2. Changed it") are not part of the question.
    last = [line for line in text.splitlines() if "?" in line and not LIST_ITEM.match(line)]
    match = LAST_QUESTION.search(" ".join(last[-1].split())) if last else None
    question = match.group(0).strip() if match else ""
    choice = from_choice(question)
    if choice:
        return choice
    # "Should I keep X or Y?" without a colon is a choice in disguise: "Yes" would not answer it.
    if YES_NO.match(question) and not REQUEST.match(question) and not re.search(r"\sor\s", question, re.IGNORECASE):
        return ["Yes", "No"]
    return []


def main():
    parser = argparse.ArgumentParser(description="Suggest one-click answers for a question.")
    parser.add_argument("question")
    args = parser.parse_args()
    print("\n".join(suggest(args.question)) or "(nothing to suggest)")


if __name__ == "__main__":
    main()
