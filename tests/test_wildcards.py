"""Tests for wildcards: Python picks which items break the references' pattern,
checks the answer, and keeps the good wildcards in the references.

No real agent runs: job_run.ask is replaced with a fake whose answers we choose.
"""

import random

import pytest
from fastapi import HTTPException

from agentic_os import artifacts, changes, job_run, job_store, jobs, limits, skills, web, wildcards

SKILL = """---
name: practice
description: Practice sentences
model: haiku
input: A topic
items: 4
wildcards: 1
---

Write sentences about {input}.

{references}
"""
REFERENCES = """# References

## Examples

- Tell me what the next step is.

## Wildcards that worked
"""


def answer(wild=(3,), count=4):
    """A numbered answer with WILDCARD on the given items."""
    return "\n".join(f"{n}. {'WILDCARD: ' if n in wild else ''}Sentence {n}." for n in range(1, count + 1))


@pytest.fixture
def agent(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """A temporary skill "practice" (4 items, 1 wildcard, at place 3) and a fake agent.

    Return (calls, answers): put the next answers in `answers`.
    """
    folder = tmp_path / "skills"
    (folder / "references").mkdir(parents=True)
    (folder / "practice.md").write_text(SKILL)
    (folder / "references" / "practice.md").write_text(REFERENCES)
    monkeypatch.setattr(skills, "SKILLS_DIR", folder)
    monkeypatch.setattr(skills, "REFERENCES_DIR", folder / "references")
    monkeypatch.setattr(wildcards, "pick", lambda count, wild, rng=None: [3])
    monkeypatch.setattr(job_run, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(job_store, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(job_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(job_run, "DEFAULT_WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "artifacts" / "INDEX.md")
    monkeypatch.setattr(changes, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    calls, answers = [], []

    def ask(prompt, model, session_id=None, extra_args=(), **options):
        calls.append(prompt)
        return {"result": answers.pop(0) if answers else answer(), "total_cost_usd": 0.01, "session_id": "s1"}

    monkeypatch.setattr(job_run, "ask", ask)
    return calls, answers


def run_practice():
    job = skills.add("practice", "git")
    jobs.run_job(job)
    return jobs.load(job.id)


# --- The pure functions ---

def test_pick_chooses_sorted_places_inside_the_items():
    places = wildcards.pick(10, 2, random.Random(7))

    assert len(places) == 2 and places == sorted(places) and all(1 <= place <= 10 for place in places)
    assert {tuple(wildcards.pick(10, 2, random.Random(seed))) for seed in range(20)} != {(9, 10)}  # not always last
    with pytest.raises(ValueError):
        wildcards.pick(4, 4)


def test_the_rules_name_the_places():
    text = wildcards.rules(10, [3, 8])

    assert "exactly 10 numbered items" in text and "Items 3 and 8 are WILDCARDS" in text


def test_items_read_the_numbers_and_the_marks():
    found = wildcards.items("Here they are:\n1. One.\n2) **WILDCARD:** Two.\n3. wildcard: Three.\nThe end.")

    assert found == [{"number": 1, "text": "One.", "wildcard": False},
                     {"number": 2, "text": "Two.", "wildcard": True},
                     {"number": 3, "text": "Three.", "wildcard": True}]


@pytest.mark.parametrize("text, problem", [
    (answer(), None),
    (answer(count=3), "exactly 4 numbered items"),
    (answer(wild=()), "Item 3 must be a wildcard"),
    (answer(wild=(1, 3)), "Item 1 must follow the references"),
])
def test_check(text, problem):
    problems = wildcards.check(text, 4, [3])

    assert problems == [] if problem is None else problem in problems[0]


def test_keep_adds_a_line_once(tmp_path):
    path = tmp_path / "references.md"
    path.write_text(REFERENCES)

    assert wildcards.keep(path, "A  new\nidea.")
    assert not wildcards.keep(path, "A new idea.")

    assert path.read_text().endswith("## Wildcards that worked\n- A new idea.\n")


def test_keep_adds_the_heading_when_it_is_missing(tmp_path):
    path = tmp_path / "references.md"
    path.write_text("# References\n")

    wildcards.keep(path, "An idea.")

    assert path.read_text() == "# References\n\n## Wildcards that worked\n- An idea.\n"


# --- Skills and jobs ---

def test_a_skill_job_gets_the_references_and_the_rules(agent):
    job = skills.add("practice", "git")

    assert "Tell me what the next step is." in job.task  # the references, filled in by Python
    assert "Item 3 is a WILDCARD. Start it with" in job.task and "exactly 4 numbered items" in job.task
    assert jobs.load(job.id).wildcards == {"count": 4, "places": [3], "kept": []}


def test_a_right_answer_is_done_with_one_call(agent):
    calls, _ = agent

    job = run_practice()

    assert job.status == "done" and len(calls) == 1 and job.wildcards["problems"] == []


def test_a_wrong_answer_goes_back_to_the_agent(agent):
    calls, answers = agent
    answers.extend([answer(wild=()), answer()])

    job = run_practice()

    assert job.status == "done" and len(calls) == 2
    assert "Item 3 must be a wildcard" in calls[1] and "replaces the old one" in calls[1]


def test_the_job_fails_when_the_items_stay_wrong(agent):
    calls, answers = agent
    answers.extend([answer(count=2)] * 3)

    job = run_practice()

    assert job.status == "failed" and len(calls) == 3
    assert "still wrong after 3 tries" in job.error


def test_keep_a_wildcard(agent):
    job = run_practice()

    assert skills.keep_wildcard(job.id, 3) == "Sentence 3."

    assert "- Sentence 3." in skills.references_file("practice").read_text()
    assert jobs.load(job.id).wildcards["kept"] == [3]


@pytest.mark.parametrize("number, error", [(2, "not a wildcard"), (9, "not a wildcard")])
def test_only_a_wildcard_can_be_kept(agent, number, error):
    job = run_practice()

    with pytest.raises(ValueError, match=error):
        skills.keep_wildcard(job.id, number)


def test_a_job_that_is_not_done_cannot_keep(agent):
    job = skills.add("practice", "git")  # queued

    with pytest.raises(ValueError, match="not a done job"):
        skills.keep_wildcard(job.id, 3)


def test_a_skill_with_wrong_numbers_is_skipped(agent):
    (skills.SKILLS_DIR / "broken.md").write_text("---\nitems: 2\nwildcards: 2\n---\nDo it.\n")

    assert [skill["name"] for skill in skills.all_skills()] == ["practice"]


def test_the_real_english_practice_skill_is_valid():
    skill = skills.load("english-practice")

    assert skill["items"] == 10 and skill["wildcards"] == 2 and skill["references"]
    assert "{references}" in skill["prompt"]


# --- Web ---

def test_the_web_view_and_keep(agent):
    job = run_practice()

    view = web.api_job(job.id)
    assert [item["wildcard"] for item in view["wildcard_items"]] == [False, False, True, False]

    assert web.api_keep_wildcard(job.id, 3)["wildcards"]["kept"] == [3]
    with pytest.raises(HTTPException) as error:
        web.api_keep_wildcard(job.id, 1)
    assert error.value.status_code == 400


@pytest.mark.parametrize("line, text, wildcard", [
    ("1. Wildcards in poker", "Wildcards in poker", False),  # a topic, not a mark
    ("1. WILDCARD-free words", "WILDCARD-free words", False),
    ("1. WILDCARD: x", "x", True),
    ("1. **WILDCARD:** x", "x", True),
    ("1. **WILDCARD**: x", "x", True),
    ("1. WILDCARD x", "x", True),
])
def test_the_mark_must_be_a_whole_word(line, text, wildcard):
    assert wildcards.items(line) == [{"number": 1, "text": text, "wildcard": wildcard}]
