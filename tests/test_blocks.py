"""Tests for tagged blocks outside Markdown code (memory.blocks_outside_code) and its six users.

A block whose tag starts inside code is an example: it is skipped and stays in the answer.
Code inside a real block stays as it is.
"""

from agentic_os import job_talk, jobs, leads, memory, project_memory

FENCE = "```"


def test_code_inside_a_real_note_stays():
    text = '<note title="Tests">Run `pytest -q` before you commit.</note>'

    [note] = memory.extract_notes(text)

    assert note["body"] == "Run `pytest -q` before you commit."


def test_notes_inside_code_are_examples():
    text = (f'An example:\n{FENCE}\n<note title="Fenced">x</note>\n{FENCE}\n'
            'and `<note title="Inline">y</note>` too.\n<note title="Real">z</note>')

    assert [note["title"] for note in memory.extract_notes(text)] == ["Real"]


def test_a_project_note_keeps_its_code():
    text = '<project-note kind="failure">`make test` fails without the `--fast` flag.</project-note>'

    assert project_memory.extract_notes(text) == [("failure", "`make test` fails without the `--fast` flag.")]


def test_a_question_in_inline_code_is_not_a_question():
    question, rest = jobs.extract_question("Write `<question>Which one?</question>` to ask the user.")

    assert question is None and "<question>" in rest


def test_a_fenced_example_stays_in_the_answer():
    example = f"{FENCE}\n<question>Example?</question>\n{FENCE}"
    question, rest = jobs.extract_question(f"The format:\n{example}\n<question>Shall I go on?\n- Yes\n- No</question>")

    assert question["text"] == "Shall I go on?" and question["options"] == ["Yes", "No"]
    assert example in rest and "Shall I go on?" not in rest


def test_a_fenced_plan_example_stays_and_the_real_plan_is_found():
    example = "~~~\n<plan>\n1. Example.\n</plan>\n~~~"
    plan, rest = jobs.extract_plan(f"{example}\n<plan>\n1. Real step.\nEstimate: $0.20\n</plan>")

    assert plan == {"text": "1. Real step.", "estimate": 0.20}
    assert example in rest


def test_a_handoff_example_in_code_is_not_the_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(leads, "LEADS_DIR", tmp_path)
    monkeypatch.setattr(leads, "folder", lambda name: tmp_path)
    monkeypatch.setattr(leads, "context_tokens", lambda reply: 0)
    example = f"{FENCE}\n<handoff>Example handoff.</handoff>\n{FENCE}"
    reply = {"session_id": "s1", "total_cost_usd": 0.0, "usage": {},
             "result": f"The format is:\n{example}\n<handoff>Next: fix the parser.</handoff>"}

    answer = leads.finish("Ada", reply)

    assert leads.handoff("Ada").strip() == "Next: fix the parser."
    assert example in answer and "Next: fix" not in answer


def test_a_lesson_example_in_code_is_skipped():
    text = f"{FENCE}\n<lesson>\nExample lesson.\n</lesson>\n{FENCE}"

    assert leads.extract_lesson(text) is None


def test_an_unclosed_fence_is_not_code():
    found, rest = memory.blocks_outside_code(job_talk.PLAN_PATTERN, f"{FENCE}python\n<plan>\n1. Step.\n</plan>")

    assert len(found) == 1 and "<plan>" not in rest
