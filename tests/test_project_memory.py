"""Tests for project memory (project_memory.py): entries per project, given to a job when relevant.

No real agent runs: job_run.ask is replaced with a fake. temp_jobs moves
project_memory.PROJECTS_DIR into tmp_path.
"""

import subprocess

import pytest
from fastapi import HTTPException

from agentic_os import artifacts, job_run, job_store, jobs, limits, project_memory, web


@pytest.fixture
def project(tmp_path, temp_jobs):
    """A plain project folder (not in git)."""
    folder = tmp_path / "shop"
    folder.mkdir()
    return folder


@pytest.fixture
def fake_agent(tmp_path, monkeypatch, temp_jobs, temp_memory):
    """Replace the agent call. Return (the prompts it got, the replies it will give, in order)."""
    monkeypatch.setattr(job_run, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(job_run, "DEFAULT_WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(job_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(limits, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "INDEX_FILE", tmp_path / "artifacts" / "INDEX.md")
    prompts, replies = [], []

    def ask(prompt, model, **options):
        prompts.append(prompt)
        return {"result": replies.pop(0) if replies else "ok", "total_cost_usd": 0.01, "session_id": "s1"}

    monkeypatch.setattr(job_run, "ask", ask)
    return prompts, replies


def test_the_project_is_the_git_repository_of_the_folder(tmp_path, project, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    agents = tmp_path / "agents"
    (agents / "Pip").mkdir(parents=True)
    monkeypatch.setattr(project_memory, "AGENT_FOLDERS", agents)

    assert project_memory.project_of(repo / "src") == repo.resolve()
    assert project_memory.project_of(project) == project.resolve()  # not in git: the folder itself
    assert project_memory.project_of(agents / "Pip") is None  # an agent's own folder
    assert project_memory.project_of(None) is None
    assert project_memory.project_of(tmp_path / "missing") is None
    assert "does not exist" in project_memory.folder_problem(tmp_path / "missing")
    assert "agent's own folder" in project_memory.folder_problem(agents / "Pip")
    assert project_memory.folder_problem(project) is None


def test_add_checks_the_text_and_replaces_the_same_entry(project):
    project_memory.add(project, "instruction", "Use pytest for every test.")
    project_memory.add(project, "instruction", "use  PYTEST for every test")  # the same words

    entries = project_memory.load(project)["entries"]
    assert [entry["text"] for entry in entries] == ["use PYTEST for every test"]
    with pytest.raises(ValueError, match="short"):
        project_memory.add(project, "instruction", "short")
    with pytest.raises(ValueError, match="Too long"):
        project_memory.add(project, "decision", "x" * 700)
    with pytest.raises(ValueError, match="secret"):
        project_memory.add(project, "failure", "The key is " + "ghp_" + "a" * 36)
    with pytest.raises(ValueError, match="kind"):
        project_memory.add(project, "wish", "Something nice to have.")


def test_an_outcome_of_the_same_job_is_replaced_and_old_outcomes_go(project, monkeypatch):
    project_memory.add(project, "outcome", '"Fix it" ended failed, $0.01.', by="python", job="aaa")
    project_memory.add(project, "outcome", '"Fix it" ended done, $0.02.', by="python", job="aaa")
    assert [entry["text"] for entry in project_memory.load(project)["entries"]] == ['"Fix it" ended done, $0.02.']

    monkeypatch.setattr(project_memory, "MAX_OUTCOMES", 2)
    project_memory.add(project, "decision", "Keep the prices in cents.")
    for number in range(3):
        project_memory.add(project, "outcome", f'"Job number {number}" ended done.', job=f"j{number}")
    texts = [entry["text"] for entry in project_memory.load(project)["entries"]]
    assert texts == ["Keep the prices in cents.", '"Job number 1" ended done.', '"Job number 2" ended done.']


def test_rank_prefers_rare_shared_words(project):
    project_memory.add(project, "decision", "Prices are stored in cents, never floats.", now="2026-11-01T10:00:00")
    project_memory.add(project, "failure", "The checkout test fails without the .env file.", now="2026-11-01T10:01:00")
    project_memory.add(project, "outcome", '"Add the login page" ended done.', now="2026-11-01T10:02:00")

    instructions, related = project_memory.relevant(project, "Show the prices in euros, not cents")

    assert instructions == []
    assert [entry["kind"] for entry in related] == ["decision"]
    assert project_memory.relevant(project, "Write a poem")[1] == []


def test_the_brief_has_every_instruction_and_only_relevant_entries(project):
    project_memory.add(project, "instruction", "Answer in simple English.")
    project_memory.add(project, "failure", "The checkout test fails without the .env file.")
    project_memory.add(project, "decision", "The logo is blue.")

    text = project_memory.brief(project, "Fix the checkout test")

    assert "(the user's instruction) Answer in simple English." in text
    assert "(known failure) The checkout test fails" in text
    assert "logo" not in text
    assert '<project-note kind="decision">' in text  # the rules: how to add a note
    assert project_memory.brief(None, "Fix the checkout test") == ""


def test_agent_notes_are_checked_one_by_one(project):
    answer = """Done.
<project-note kind="decision">Totals are rounded at the end, not per line.</project-note>
<project-note kind="instruction">Always use tabs.</project-note>
<project-note kind="failure">tiny</project-note>
```
<project-note kind="decision">An example inside code, not a note.</project-note>
```"""

    saved, refused = project_memory.save_agent_notes(project, answer, "job1", "Ada")

    assert saved == ["Totals are rounded at the end, not per line."]
    assert len(refused) == 2  # an agent cannot add instructions; "tiny" is too short
    entry = project_memory.load(project)["entries"][0]
    assert (entry["kind"], entry["by"], entry["job"]) == ("decision", "Ada", "job1")


def test_a_job_gets_the_brief_and_saves_notes_and_an_outcome(fake_agent, project):
    prompts, replies = fake_agent
    project_memory.add(project, "instruction", "Answer in simple English.")
    replies.append('Done.\n<project-note kind="failure">The cart total test needs the TZ variable.</project-note>')

    first = jobs.add("Fix the cart total.", model="haiku", use_memory=False, folder=str(project))
    jobs.run_job(first)
    first = jobs.load(first.id)

    assert "Answer in simple English." in prompts[0]
    assert first.task == "Fix the cart total."  # the brief goes to the agent, not into the job
    assert first.project_notes == ["The cart total test needs the TZ variable."]
    outcomes = [entry for entry in project_memory.load(project)["entries"] if entry["kind"] == "outcome"]
    assert len(outcomes) == 1 and outcomes[0]["job"] == first.id
    assert outcomes[0]["text"].startswith('"Fix the cart total." ended done')

    second = jobs.add("The cart total is wrong again.", model="haiku", use_memory=False, folder=str(project))
    jobs.run_job(second)

    assert "(known failure, job " + first.id + ") The cart total test needs the TZ variable." in prompts[1]


def test_a_job_without_a_folder_has_no_project_memory(fake_agent):
    prompts, _ = fake_agent
    jobs.run_job(jobs.add("Say hi.", model="haiku", use_memory=False))

    assert "Project memory" not in prompts[0]
    assert project_memory.all_projects() == []


def test_the_web_page_adds_lists_and_removes_entries(project):
    entry = web.api_add_project_entry(web.ProjectEntry(folder=str(project), text="Use pytest, not unittest."))

    rows = web.api_projects()
    assert [(row["name"], row["counts"]["instruction"]) for row in rows] == [("shop", 1)]
    assert rows[0]["entries"][0]["text"] == "Use pytest, not unittest."

    web.api_remove_project_entry(rows[0]["file"], entry["id"])
    assert web.api_projects()[0]["entries"] == []
    with pytest.raises(HTTPException) as error:
        web.api_remove_project_entry(rows[0]["file"], entry["id"])
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        web.api_add_project_entry(web.ProjectEntry(folder=str(project), kind="outcome", text="Python writes these."))
    assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        web.api_add_project_entry(web.ProjectEntry(folder=str(project / "missing"), text="Use pytest, not unittest."))
    assert error.value.status_code == 400
    assert "does not exist" in error.value.detail


# --- review 2, step 11 ---

def test_a_refused_note_is_reported_without_its_text(project):
    """Item 14: the refused line goes to the server log, so it must not hold the secret."""
    token = "ghp_" + "a1B2" * 9
    answer = f'<project-note kind="failure">The deploy needs the token {token} to work.</project-note>'

    saved, refused = project_memory.save_agent_notes(project, answer, "job1", "Ada")

    assert saved == [] and len(refused) == 1
    assert token not in refused[0] and "a1B2" not in refused[0]
    assert refused[0].startswith("note 1 (failure): ")


def test_entries_that_differ_only_in_a_number_are_two_entries(project):
    """Item 68: short words and numbers make entries different."""
    project_memory.add(project, "failure", "Test 1 fails on the CI server.")
    project_memory.add(project, "failure", "Test 2 fails on the CI server.")

    assert len(project_memory.load(project)["entries"]) == 2


def test_agent_entries_never_push_out_the_users_instructions(project, monkeypatch):
    """Item 67: when there are too many entries, the agents' old ones go first."""
    monkeypatch.setattr(project_memory, "MAX_ENTRIES", 3)
    project_memory.add(project, "instruction", "Always answer in simple English.")
    for number in range(4):
        project_memory.add(project, "decision", f"Agent decision number {number} about the code.", by="Ada")

    entries = project_memory.load(project)["entries"]
    assert len(entries) == 3
    assert entries[0]["text"] == "Always answer in simple English."
    assert [entry["text"] for entry in entries[1:]] == ["Agent decision number 2 about the code.",
                                                       "Agent decision number 3 about the code."]


def test_the_newest_past_job_says_where_a_project_stands(project):
    project_memory.add(project, "instruction", "Use pytest.", now="2026-11-10T09:00:00")
    project_memory.add(project, "outcome", '"Add the cart" ended done.', by="python", job="a1",
                       now="2026-11-10T10:00:00")
    project_memory.add(project, "outcome", '"Fix the total" ended failed.', by="python", job="b2",
                       now="2026-11-10T11:00:00")

    row = project_memory.all_projects()[0]

    assert row["latest_outcome"]["text"] == '"Fix the total" ended failed.'
    assert row["latest_outcome"]["job"] == "b2"


def test_a_project_without_past_jobs_has_no_latest_outcome(project):
    project_memory.add(project, "instruction", "Use pytest.")

    assert project_memory.all_projects()[0]["latest_outcome"] is None


# --- Continue fills the Check field; an empty "Next:" is refused (2026-11-13) ---

def test_last_checks_gives_the_check_of_the_newest_done_job(project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    def job(job_id, status, check, finished, folder=project):
        return jobs.Job(id=job_id, task="t", status=status, check=check, folder=str(folder),
                        finished_at=finished)

    found = project_memory.last_checks([
        job("a", "done", "pytest -q", "2026-11-13T10:00:00"),
        job("b", "done", " make test ", "2026-11-13T12:00:00"),   # the newest done one wins
        job("c", "failed", "broken", "2026-11-13T13:00:00"),       # its check did not pass
        job("d", "done", None, "2026-11-13T14:00:00"),             # no check: nothing to give
        job("e", "done", "npm test", "2026-11-13T09:00:00", folder=other),
    ])

    assert found == {str(project.resolve()): "make test", str(other.resolve()): "npm test"}


def test_the_projects_api_gives_the_last_check(project, monkeypatch):
    project_memory.add(project, "instruction", "Use pytest for every test.")
    jobs.save(jobs.Job(id="a1", task="t", status="done", check="pytest -q", folder=str(project),
                       finished_at="2026-11-13T10:00:00"))

    rows = web.api_projects()

    assert rows[0]["last_check"] == "pytest -q"


def test_an_empty_next_from_continue_is_refused(temp_jobs, monkeypatch):
    monkeypatch.setattr(web, "run_in_pool", lambda job: pytest.fail("a job started"))
    task = 'Continue the work on shop. The last job: "Add a cart" ended done.\n\nNext: '

    with pytest.raises(HTTPException) as error:
        web.api_add_job(web.NewJob(task=task, model="sonnet", memory=False))

    assert error.value.status_code == 400 and "Next:" in error.value.detail
    assert jobs.all_jobs() == []


def test_the_outcome_of_a_continue_job_says_what_it_did():
    """The words after "Next:" are the job's own work; the text before only quotes the last job."""
    task = ('Continue the work on shop. The last job: "Continue the work on shop. The last job: ..." '
            'ended done.\n\nNext: \nAdd a cart page with a total.')
    job = jobs.Job(id="c1", task=task, status="done", check="pytest -q", cost=0.25)

    line = project_memory.outcome_line(job, 3)

    assert line == '"Add a cart page with a total." ended done, the check passed, 3 file(s) changed, $0.25.'
    # A normal task and a Continue text with nothing after "Next:" keep their words.
    assert project_memory.task_words("Fix the login. Next: the logout.") == "Fix the login. Next: the logout."
    assert project_memory.task_words("Continue the work on shop.\n\nNext: ") == "Continue the work on shop.\n\nNext: "
