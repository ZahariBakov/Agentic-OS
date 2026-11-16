"""Tests for the suggested answers (suggest.py) and their use in jobs.extract_question."""

import pytest

from agentic_os import jobs, suggest


@pytest.mark.parametrize("text, answers", [
    ("Which way?\n1. Keep the old tests\n2. Remove them - they are slow", ["Keep the old tests", "Remove them"]),
    ("Which one do you want?\na) **Fast**\nb) Safe: slower", ["Fast", "Safe"]),
    ("Which database: Postgres, MySQL or SQLite?", ["Postgres", "MySQL", "SQLite"]),
    ("I looked at the code. Should I delete the old tests?", ["Yes", "No"]),
    ("Do you want a summary at the end?", ["Yes", "No"]),
    ("Can you send me the log?", []),  # a request, not a yes/no question
    ("Should I keep X or Y?", []),  # a choice in disguise: Yes would not answer it
    ("What name should the file have?", []),
    ("No question here.", []),
    ("Which one?\n1. Only one item", []),  # a list needs 2 items or more
    # review 2, step 12
    ("I did this:\n1. Read the file\n2. Changed the function\nShould I also add tests?", ["Yes", "No"]),  # 41
    ("Options:\n1. Fast\n2. Safe\n\nWhich one do you want?", ["Fast", "Safe"]),
    ("What time: 10:00 or 11:00?", ["10:00", "11:00"]),  # 73
    ("Which database: Postgres (fast, reliable) or SQLite?", ["Postgres (fast, reliable)", "SQLite"]),  # 74
    ("Which function: `run_job` or `work`?", ["`run_job`", "`work`"]),  # 75: the names stay as they are
])
def test_suggestions(text, answers):
    assert suggest.suggest(text) == answers


def test_a_question_without_options_gets_suggested_answers():
    question, rest = jobs.extract_question("I read it.\n<question>\nShould I fix the typo too?\n</question>")
    assert question == {"text": "Should I fix the typo too?", "options": ["Yes", "No"], "suggested": True}
    assert rest == "I read it."


def test_the_agent_options_win():
    question, _ = jobs.extract_question("<question>\nShould I fix it?\n- Fix it now\n- Later\n</question>")
    assert question == {"text": "Should I fix it?", "options": ["Fix it now", "Later"]}
