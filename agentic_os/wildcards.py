"""Wildcards: a few items of a skill's answer break the pattern of its references on purpose.

A skill with references (good examples and principles) makes the agent copy them.
After a while every answer looks the same ("overfitting"). So when a skill asks
for many items, most follow the references and a few are wildcards: they break
one pattern on purpose. A wildcard you like goes into the references, so the
references grow with new ideas, not only with more of the same.

8 of 10 items follow the references, 2 are wildcards, marked WILDCARD. The exact
parts are Python:

1. `pick` chooses WHICH items are wildcards (random places, not always the last ones).
2. `rules` tells the agent the count and the places (added to the task).
3. `check` reads the answer: the right number of items, WILDCARD on the right ones.
   A problem goes back to the agent (jobs.work), like a failed check.
4. `keep` adds a wildcard you liked to the references file.

A skill turns this on in its front matter (`items: 10`, `wildcards: 2`); its
references are in skills/references/<name>.md (`{references}` in the prompt).
This module imports no other module of the project, so jobs and skills can both use it.
"""

import random
import re
from pathlib import Path

ITEM = re.compile(r"^\s*(\d{1,3})[.)]\s+(.+?)\s*$")  # "3. text" or "3) text"
# "WILDCARD:", "**WILDCARD:**", "WILDCARD idea"; the word must end there ("Wildcards in poker" is no mark)
MARK = re.compile(r"^[*_]*WILDCARD[*_]*(?:\s*:|\s)[*_]*\s*", re.IGNORECASE)
KEPT_HEADING = "## Wildcards that worked"

RULES = """

# Items and wildcards (Python checks your answer)
Write exactly {count} numbered items, one per line: "1. ...", "2. ...", up to "{count}. ...".
Write nothing else between the items.
{which}. Start {each} with "WILDCARD: " after the number,
for example "{example}. WILDCARD: ...". A wildcard breaks one pattern of the
references on purpose (a different topic, form, length or tone), but it still
follows the task. All other items follow the references and have no mark."""

FIX_PROMPT = """Python checked the numbered items in your answer and found these problems:
{problems}

Write your whole answer again with these fixed. The new answer replaces the old one."""


def pick(count, wild, rng=random):
    """Return the places (1-based, sorted) of `wild` wildcards among `count` items."""
    if not 0 < wild < count:
        raise ValueError(f"wildcards must be between 1 and {count - 1}, not {wild}")
    return sorted(rng.sample(range(1, count + 1), wild))


def say_places(places):
    """[3] -> "3"; [3, 8] -> "3 and 8"; [1, 4, 9] -> "1, 4 and 9"."""
    words = [str(place) for place in places]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def rules(count, places):
    """The text that goes after the skill's task."""
    which = (f"Item {places[0]} is a WILDCARD" if len(places) == 1
             else f"Items {say_places(places)} are WILDCARDS")
    return RULES.format(count=count, which=which, each="it" if len(places) == 1 else "each of them",
                        example=places[0])


def items(text):
    """Return the numbered items of an answer: [{"number", "text", "wildcard"}], in order.

    The text of a wildcard is without its WILDCARD mark.
    """
    found = []
    for line in (text or "").splitlines():
        match = ITEM.match(line)
        if match:
            body = match.group(2)
            mark = MARK.match(body)
            found.append({"number": int(match.group(1)), "text": body[mark.end():] if mark else body,
                          "wildcard": mark is not None})
    return found


def check(text, count, places):
    """Return the problems of an answer as a list of lines (empty: the answer is right)."""
    found = items(text)
    numbers = [item["number"] for item in found]
    if numbers != list(range(1, count + 1)):
        return [f"- I need exactly {count} numbered items, 1. to {count}., in order. "
                f"I found {len(found)}: {', '.join(map(str, numbers)) or 'none'}."]
    problems = []
    for item in found:
        if item["number"] in places and not item["wildcard"]:
            problems.append(f"- Item {item['number']} must be a wildcard: start it with \"WILDCARD: \".")
        if item["number"] not in places and item["wildcard"]:
            problems.append(f"- Item {item['number']} must follow the references: remove its WILDCARD mark "
                            f"(only {say_places(places)} {'is a wildcard' if len(places) == 1 else 'are wildcards'}).")
    return problems


def keep(path, text):
    """Add a wildcard to the "Wildcards that worked" part of a references file.

    That part is the last one in the file, so the new line goes at the end.
    Return False if the same line is there already. The caller holds a lock.
    """
    path = Path(path)
    content = path.read_text() if path.exists() else ""
    line = f"- {' '.join(text.split())}"
    if line in content.splitlines():
        return False
    if KEPT_HEADING not in content:
        content = content.rstrip() + f"\n\n{KEPT_HEADING}\n"
    path.write_text(content.rstrip() + "\n" + line + "\n")
    return True
