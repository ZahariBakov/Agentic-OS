"""Tests for documents: PDF/Office/HTML in memory/raw/ -> Markdown -> ingest. No agent runs."""

import subprocess

import pytest
from pptx import Presentation

from agentic_os import briefing, documents, jobs, memory


def raw(relative, text=None):
    path = memory.RAW_DIR / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        path.write_text(text)
    return path


def broken(relative):
    """Make a damaged Office file: a ZIP header, then garbage bytes."""
    path = raw(relative)
    path.write_bytes(b"PK\x03\x04" + bytes(range(256)) * 20)
    return path


def make_pptx(path, title, body):
    """Make a real PowerPoint file with one slide."""
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])  # title and content
    slide.shapes.title.text = title
    slide.placeholders[1].text = body
    path.parent.mkdir(parents=True, exist_ok=True)
    deck.save(path)
    return path


def test_html_becomes_markdown_next_to_it(temp_memory):
    page = raw("article.html", "<html><body><h1>Rust ownership</h1><p>Each value has one owner.</p></body></html>")

    target = documents.convert(page)

    assert target == memory.RAW_DIR / "article.html.md"
    text = target.read_text()
    assert text.startswith("<!-- Converted from article.html by markitdown, ")
    assert "# Rust ownership" in text and "Each value has one owner." in text


def test_a_powerpoint_meeting_stays_in_meetings(temp_memory):
    deck = make_pptx(raw("meetings/2026-11-03-review.pptx"), "Review", "We decided to ship on Friday.")

    target = documents.convert(deck)

    assert target.parent == memory.RAW_DIR / "meetings"
    assert "We decided to ship on Friday." in target.read_text()


@pytest.mark.parametrize("name, content, message", [
    ("notes.txt", "plain text", "Not a document"),
    ("empty.html", "<html><body></body></html>", "No text found in empty.html"),
])
def test_files_that_cannot_be_converted(temp_memory, name, content, message):
    path = raw(name, content)

    with pytest.raises(ValueError, match=message):
        documents.convert(path)

    assert not documents.markdown_path(path).exists()


def test_a_damaged_file_cannot_be_converted(temp_memory):
    path = broken("damaged.pptx")

    with pytest.raises(ValueError, match="Could not convert damaged.pptx"):
        documents.convert(path)

    assert not documents.markdown_path(path).exists()


def test_only_files_in_raw(temp_memory, tmp_path):
    outside = tmp_path / "secret.html"
    outside.write_text("<p>secret</p>")

    with pytest.raises(ValueError, match="Not a file in"):
        documents.convert(outside)


def test_too_big_files_are_skipped(temp_memory, monkeypatch):
    monkeypatch.setattr(documents, "MAX_BYTES", 10)
    page = raw("big.html", "<p>more than ten bytes</p>")

    with pytest.raises(ValueError, match="Too big"):
        documents.convert(page)


def test_convert_new_skips_done_and_reports_bad_files(temp_memory):
    raw("done.html", "<p>old</p>")
    documents.convert(memory.RAW_DIR / "done.html")
    raw("new.html", "<p>new text</p>")
    broken("broken.docx")

    converted, problems = documents.convert_new()

    assert converted == [memory.RAW_DIR / "new.html.md"]
    assert len(problems) == 1 and problems[0].startswith("Could not convert broken.docx")
    assert documents.new_documents() == [memory.RAW_DIR / "broken.docx"]  # still waiting


def test_ingest_new_converts_first(temp_memory, temp_jobs, capsys):
    make_pptx(raw("meetings/2026-11-03-review.pptx"), "Review", "We decided to ship on Friday.")
    broken("broken.docx")

    [job] = jobs.ingest_new()

    # The .md of the meeting deck is ingested as a meeting; the document itself is not a source.
    assert job.source == str((memory.RAW_DIR / "meetings/2026-11-03-review.pptx.md").resolve())
    assert job.task.startswith("Read the meeting transcript ")
    assert "Skipped: Could not convert broken.docx" in capsys.readouterr().out


def test_briefing_lists_documents_without_markdown(temp_briefing, temp_memory):
    broken("broken.docx")

    lines = briefing.memory_part(briefing.datetime(2026, 11, 3, 8, 0))

    assert lines[:2] == [
        "Documents without Markdown: 1 (run: python -m agentic_os.documents convert-new)",
        "- broken.docx",
    ]


def test_an_office_file_that_unpacks_to_too_much_is_skipped(temp_memory, monkeypatch):
    """A zip bomb: small on disk, huge when unpacked. Its table of contents tells, nothing is unpacked."""
    monkeypatch.setattr(documents, "MAX_UNZIPPED_BYTES", 1000)
    deck = make_pptx(raw("meetings/big.pptx"), "Plan", "x" * 2000)

    with pytest.raises(ValueError, match="Too big when unpacked"):
        documents.convert(deck)
    assert not documents.markdown_path(deck).exists()


def test_a_conversion_that_takes_too_long_is_stopped(temp_memory, monkeypatch):
    monkeypatch.setattr(documents, "TIMEOUT_SECONDS", 0.01)  # markitdown needs about 1 second to start
    page = raw("slow.html", "<p>Hello</p>")

    with pytest.raises(ValueError, match="Took too long"):
        documents.convert(page)

    assert not documents.markdown_path(page).exists()


def test_the_converter_runs_in_a_child_process(temp_memory, monkeypatch):
    seen = {}

    def run(command, **options):
        seen.update(command=command, options=options)
        return subprocess.CompletedProcess(command, 0, stdout="# Hello\n", stderr="")

    monkeypatch.setattr(documents.subprocess, "run", run)
    page = raw("page.html", "<h1>Hello</h1>")

    documents.convert(page)

    assert seen["command"][1:4] == ["-m", "agentic_os.documents", "child"]
    assert seen["options"]["timeout"] == documents.TIMEOUT_SECONDS
    assert "# Hello" in documents.markdown_path(page).read_text()
