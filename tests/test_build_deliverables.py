"""The deliverable builder: the sources it names must exist, and it must not write when asked not to.

Low value to test *rendering* (markdown does that); high value to test **the contract between the
repository and the owner-facing files**. That contract has broken once already: the builder lived in
``/tmp``, was lost, and the PDFs quietly went stale against the docs.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import build_deliverables as BD  # noqa: E402


def test_every_deliverable_points_at_a_document_that_exists():
    missing = [
        item["source"] for item in BD.DELIVERABLES if not (REPO / str(item["source"])).is_file()
    ]
    assert missing == [], f"deliverables cite documents that are not in the repository: {missing}"


def test_each_source_document_has_a_heading_and_room_to_read():
    """A renamed or emptied document should stop the build, not produce a blank PDF."""
    for item in BD.DELIVERABLES:
        text = (REPO / str(item["source"])).read_text(encoding="utf-8")
        assert text.strip(), f"{item['source']} is empty"
        assert "\n# " in "\n" + text, f"{item['source']} has no top-level heading"
        assert len(text) > 1000, f"{item['source']} is too thin to be a deliverable"


def test_the_check_mode_renders_everything_and_writes_nothing(tmp_path, capsys):
    target = tmp_path / "out"
    assert BD.main(["--check", "--out", str(target)]) == 0
    printed = capsys.readouterr().out
    assert printed.count("OK  ") == len(BD.DELIVERABLES)
    assert not target.exists(), "check mode wrote files"


def test_rendering_produces_a_page_with_the_title_and_a_table_of_contents():
    item = BD.DELIVERABLES[0]
    page, title, body = BD.render_html(item)
    assert title == item["title"]
    assert title in page
    assert 'id="toc"' in page and "<li>" in page
    assert "<table>" in body or "<table" in page, "the reading edition should render tables"


def test_a_missing_renderer_is_reported_with_the_fix_not_an_import_error():
    """The venv that has ``markdown`` is named in the message; the wrong python is a common slip."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, "markdown", None)
        with pytest.raises(SystemExit) as raised:
            BD.markdown_renderer()
    assert "venv proyek" in str(raised.value)
