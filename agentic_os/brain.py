"""Brain view: a map of what Agent Office knows - notes, areas, leads and projects - as a graph.

It is built from what the office already records. Python only, no AI, nothing new is saved: the web page /brain draws it.

Nodes (each has an id "<kind>:<name>"):
    area      a folder of memory/wiki (second-brain, personal, ...)
    note      a note in memory/wiki
    lead      a lead from roster.json, or "one-time agents"
    project   a project with a memory (project_memory.py)

Edges:
    in        area -> note (the note's folder)
    link      note -> note: a [text](path) or [[Title]] link in the note
    similar   note -- note: they share rare words (each note to its SIMILAR_PER_NOTE best
              neighbours above MIN_SIMILAR); only when there is no link between them
    wrote     lead -> note: a job of the lead saved the note (job.notes)
    worked    lead -> project: jobs of the lead on the project (weight = how many)

Usage (from the project folder):
    python -m agentic_os.brain          the counts, as JSON
"""

import json
import math
import re
from pathlib import Path

from agentic_os import jobs, leads, memory, project_memory

SIMILAR_PER_NOTE = 2
MIN_SIMILAR = 3.0  # the sum of log(1 + N / df) over shared words (about two rare words)
ONE_TIME = "one-time agents"


def note_id(path):
    return "note:" + str(path.relative_to(memory.MEMORY_DIR))


def read_notes():
    """Return [(path, title, description, body)] of every note in memory/wiki, sorted by path."""
    notes = []
    for path in sorted(memory.WIKI_DIR.glob("*/*.md")):
        meta, body = memory.read_note(path)
        notes.append((path, meta.get("title", path.stem), meta.get("description", ""), body))
    return notes


def links(path, body, by_title):
    """Return the paths of the notes that a note links to (only ones that exist)."""
    text = memory.CODE_PATTERN.sub("", body)
    found = []
    for target in memory.LINK_PATTERN.findall(text):
        if target.startswith("#") or memory.URL_SCHEME.match(target):
            continue
        file = target.split("#")[0]
        for candidate in (path.parent / file, memory.MEMORY_DIR / file):
            if candidate.suffix == ".md" and candidate.is_file() and candidate.resolve().is_relative_to(memory.WIKI_DIR.resolve()):
                found.append(candidate.resolve())
                break
    for title in memory.WIKI_LINK_PATTERN.findall(text):
        target = by_title.get(title.split("|")[0].split("#")[0].strip())
        if target is not None:
            found.append(target)
    return [target for target in dict.fromkeys(found) if target != path.resolve()]


def similar_pairs(notes):
    """Return {(i, j): score} for notes that share rare words, each note to its best neighbours."""
    sets = [set(project_memory.words(f"{title} {description} {body}")) for _, title, description, body in notes]
    count = {}
    for found in sets:
        for word in found:
            count[word] = count.get(word, 0) + 1
    pairs = {}
    for i, mine in enumerate(sets):
        scored = []
        for j, theirs in enumerate(sets):
            if i == j:
                continue
            score = sum(math.log(1 + len(notes) / count[word]) for word in mine & theirs if count[word] < len(notes))
            if score >= MIN_SIMILAR:
                scored.append((score, j))
        for score, j in sorted(scored, key=lambda item: (-item[0], item[1]))[:SIMILAR_PER_NOTE]:
            pairs[tuple(sorted((i, j)))] = round(score, 2)
    return pairs


def graph(all_jobs=None):
    """Return {"nodes": [...], "edges": [...], "counts": {...}} for the web page."""
    all_jobs = jobs.all_jobs() if all_jobs is None else all_jobs
    nodes, edges = {}, []

    def node(node_id, kind, label, **info):
        nodes.setdefault(node_id, {"id": node_id, "kind": kind, "label": label, **info})

    notes = read_notes()
    by_title = {title: path.resolve() for path, title, _, _ in notes}
    by_path = {path.resolve(): note_id(path) for path, *_ in notes}
    for path, title, description, body in notes:
        area = path.parent.name
        node(f"area:{area}", "area", area)
        node(note_id(path), "note", title, description=description, path=str(path.relative_to(memory.MEMORY_DIR)),
             area=area)
        edges.append({"source": f"area:{area}", "target": note_id(path), "kind": "in"})
    linked = set()
    for path, _, _, body in notes:
        for target in links(path, body, by_title):
            edges.append({"source": note_id(path), "target": by_path[target], "kind": "link"})
            linked.add(frozenset((note_id(path), by_path[target])))
    for (i, j), score in sorted(similar_pairs(notes).items()):
        pair = (note_id(notes[i][0]), note_id(notes[j][0]))
        if frozenset(pair) not in linked:
            edges.append({"source": pair[0], "target": pair[1], "kind": "similar", "weight": score})

    roster = leads.roster()
    wrote, worked = {}, {}
    projects = {row["project"]: row for row in project_memory.all_projects()}
    for job in all_jobs:
        who = job.lead or ONE_TIME
        for note in job.notes:
            target = memory.MEMORY_DIR / note
            if target.resolve() in by_path:
                wrote.setdefault((who, by_path[target.resolve()]), 0)
                wrote[(who, by_path[target.resolve()])] += 1
        project = project_memory.project_of(job.folder) if job.folder else None
        if project is not None and str(project) in projects:
            worked[(who, str(project))] = worked.get((who, str(project)), 0) + 1
    for name in sorted(roster):
        node(f"lead:{name}", "lead", name, role=roster[name].get("role", ""))
    for row in projects.values():
        node(f"project:{row['project']}", "project", row["name"], path=row["project"], counts=row["counts"])
    for (who, target), count in sorted(wrote.items()):
        node(f"lead:{who}", "lead", who)
        edges.append({"source": f"lead:{who}", "target": target, "kind": "wrote", "weight": count})
    for (who, project), count in sorted(worked.items()):
        node(f"lead:{who}", "lead", who)
        edges.append({"source": f"lead:{who}", "target": f"project:{project}", "kind": "worked", "weight": count})

    counts = {}
    for item in nodes.values():
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    for edge in edges:
        counts["edge:" + edge["kind"]] = counts.get("edge:" + edge["kind"], 0) + 1
    return {"nodes": list(nodes.values()), "edges": edges, "counts": counts}


def main():
    print(json.dumps(graph()["counts"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
