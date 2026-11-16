"""Tests for the brain view (brain.py): the graph of notes, areas, leads and projects. No agent runs."""

from agentic_os import brain, jobs, memory, project_memory, web


def edges_of(graph, kind):
    return sorted((edge["source"], edge["target"]) for edge in graph["edges"] if edge["kind"] == kind)


def test_notes_are_in_their_areas_and_links_are_edges(temp_memory, temp_jobs):
    memory.save_note("Git branches", "One branch per feature. See [[Pull requests]].", area="tech")
    memory.save_note("Pull requests", "Small ones. Also [the branches](git-branches.md) and [web](https://x.y).",
                     area="tech")
    memory.save_note("Coffee", "Two cups a day.", area="personal")

    graph = brain.graph()

    ids = {node["id"]: node for node in graph["nodes"]}
    assert ids["note:wiki/tech/git-branches.md"]["label"] == "Git branches"
    assert ids["area:tech"]["kind"] == "area"
    assert ("area:personal", "note:wiki/personal/coffee.md") in edges_of(graph, "in")
    assert edges_of(graph, "link") == [("note:wiki/tech/git-branches.md", "note:wiki/tech/pull-requests.md"),
                                       ("note:wiki/tech/pull-requests.md", "note:wiki/tech/git-branches.md")]
    # Linked notes get no "similar" edge on top.
    assert not [pair for pair in edges_of(graph, "similar") if "coffee" not in str(pair)
                and set(pair) == {"note:wiki/tech/git-branches.md", "note:wiki/tech/pull-requests.md"}]


def test_notes_with_shared_rare_words_are_similar(temp_memory, temp_jobs):
    memory.save_note("Rust ownership", "Each value has one owner; borrowing lends it.", area="tech")
    memory.save_note("Rust borrowing", "Borrowing lends a value without moving the owner.", area="tech")
    memory.save_note("Coffee", "Two cups a day, no sugar.", area="personal")

    graph = brain.graph()

    assert edges_of(graph, "similar") == [("note:wiki/tech/rust-borrowing.md", "note:wiki/tech/rust-ownership.md")]


def test_leads_wrote_notes_and_worked_on_projects(temp_memory, temp_jobs, tmp_path):
    path = memory.save_note("Coffee", "Two cups a day.", area="personal")
    folder = tmp_path / "shop"
    folder.mkdir()
    project_memory.add(folder, "instruction", "Use pytest.")
    for _ in range(2):
        job = jobs.add("Fix the cart.", "haiku", use_memory=False, folder=str(folder))
        job.notes = [str(path.relative_to(memory.MEMORY_DIR))]
        jobs.save(job)

    graph = brain.graph()

    wrote = [edge for edge in graph["edges"] if edge["kind"] == "wrote"]
    worked = [edge for edge in graph["edges"] if edge["kind"] == "worked"]
    assert [(edge["source"], edge["weight"]) for edge in wrote] == [("lead:one-time agents", 2)]
    assert [(edge["target"], edge["weight"]) for edge in worked] == [(f"project:{folder}", 2)]
    assert graph["counts"]["project"] == 1


def test_the_web_api_returns_the_graph(temp_memory, temp_jobs):
    memory.save_note("Coffee", "Two cups a day.", area="personal")

    graph = web.api_brain()

    assert graph["counts"]["note"] == 1 and {"nodes", "edges", "counts"} <= set(graph)
