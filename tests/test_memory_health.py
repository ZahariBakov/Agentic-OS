"""Tests for memory.health(): exact checks of the memory, no AI."""

from agentic_os import memory


def write_note(folder, relative, title="A note", description="About it", body="Some text."):
    """Write a note file. title or description None leaves that field out."""
    path = folder / "wiki" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    head = "".join(f"{key}: {value}\n" for key, value in
                   [("title", title), ("description", description)] if value is not None)
    path.write_text(f"---\n{head}---\n\n{body}\n")
    return path


def problems(folder):
    """Return health() as (path relative to the memory folder, problem) pairs."""
    return [(str(path.relative_to(folder)), problem) for path, problem in memory.health()]


def test_healthy_memory_has_no_problems(temp_memory):
    write_note(temp_memory, "tools/editor.md", "Editor", body="See [the map](MEMORY.md) and [[Shell]].")
    write_note(temp_memory, "tools/shell.md", "Shell", body="Bash. [Web](https://example.com)")
    memory.rebuild_index()

    assert problems(temp_memory) == []
    assert memory.health_report() == "No problems found by the exact checks."


def test_map_out_of_date(temp_memory):
    memory.rebuild_index()
    write_note(temp_memory, "tools/editor.md")  # added after the map was built

    assert problems(temp_memory) == [("MEMORY.md", "the map is out of date (run: python -m agentic_os.memory index)")]


def test_missing_fields_and_empty_note(temp_memory):
    write_note(temp_memory, "a/no-title.md", title=None)
    write_note(temp_memory, "a/no-description.md", "Two", description=None)
    write_note(temp_memory, "a/empty.md", "Three", body="")
    memory.rebuild_index()

    assert problems(temp_memory) == [
        ("wiki/a/empty.md", "the note is empty"),
        ("wiki/a/no-description.md", "no description in the front matter"),
        ("wiki/a/no-title.md", "no title in the front matter"),
    ]


def test_same_title_twice(temp_memory):
    write_note(temp_memory, "a/one.md", "Same")
    write_note(temp_memory, "b/two.md", "Same")
    memory.rebuild_index()

    assert problems(temp_memory) == [
        ("wiki/a/one.md", "the same title as wiki/b/two.md"),
        ("wiki/b/two.md", "the same title as wiki/a/one.md"),
    ]


def test_note_outside_an_area_folder(temp_memory):
    (temp_memory / "wiki").mkdir()
    (temp_memory / "wiki" / "loose.md").write_text("---\ntitle: Loose\n---\n\nText.\n")
    memory.rebuild_index()

    assert problems(temp_memory) == [("wiki/loose.md", "not in an area folder, so the map does not show it")]


def test_broken_links(temp_memory):
    write_note(temp_memory, "a/one.md", "One", body=(
        "[gone](wiki/a/missing.md) [ok](two.md) [[Nobody]] [[Two|the second note]]\n"
        "`[in code](nowhere.md)` is an example, not a link."))
    write_note(temp_memory, "a/two.md", "Two")
    memory.rebuild_index()

    assert problems(temp_memory) == [
        ("wiki/a/one.md", "broken link: wiki/a/missing.md"),
        ("wiki/a/one.md", "broken link: [[Nobody]]"),
    ]


def test_banned_phrases(temp_memory):
    (temp_memory / "identity").mkdir()
    (temp_memory / "identity" / "anti-ai-style.md").write_text("# Banned\n\n- delve\n")
    write_note(temp_memory, "a/one.md", "One", body="Let us Delve into it.")
    memory.rebuild_index()

    assert problems(temp_memory) == [("wiki/a/one.md", "banned phrases: delve")]


def test_banned_character_by_its_code(temp_memory):
    (temp_memory / "identity").mkdir()
    (temp_memory / "identity" / "anti-ai-style.md").write_text("# Banned\n\n- U+2014\n")

    assert memory.banned_phrases() == [chr(0x2014)]
    assert memory.style_problems("one " + chr(0x2014) + " two") == [chr(0x2014)]
    assert memory.style_problems("one - two") == []


# --- review 2, step 11 ---

def test_an_update_without_a_description_keeps_the_old_one(temp_memory):
    """Item 69: the title must not replace the stored description."""
    memory.save_note("Editor", "I use Vim.", description="The user's editor")
    path = memory.save_note("Editor", "I use Neovim now.")

    meta, body = memory.read_note(path)
    assert meta["description"] == "The user's editor" and "Neovim" in body


def test_write_atomic_keeps_a_private_file_private(tmp_path, open_umask):
    path = tmp_path / "secret.json"
    memory.write_atomic(path, "{}")
    assert path.stat().st_mode & 0o777 == 0o644  # a new file: the usual rights
    path.chmod(0o600)

    memory.write_atomic(path, '{"a": 1}')

    assert path.stat().st_mode & 0o777 == 0o600 and path.read_text() == '{"a": 1}'
