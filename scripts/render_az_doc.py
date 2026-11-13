#!/usr/bin/env python3
"""Render ``docs/PROJECT_VILLA_AGENCY_A-Z.md`` into ``docs/rendered/`` as HTML + PDF (A4).

The Markdown is the source of truth; the rendered files are a **reading copy**. A PDF that
disagrees with the Markdown is a bug in this script, not a second opinion — so the script is
committed here instead of living in ``/tmp`` where it was first written and lost.

    python3 scripts/render_az_doc.py

Requirements: the ``markdown`` package (project venv) and ``google-chrome`` for the PDF step.
No remote assets: CSS is inline and fonts are system families, so the render does not depend
on the network. Chrome is invoked with ``--no-pdf-header-footer`` so the page furniture comes
from our own CSS, not from the browser's default header.

Reproducibility: the **HTML is byte-identical** across runs. The **PDF is not** — Chrome
stamps a creation date — so never treat a PDF hash as an integrity check. Compare extracted
text instead: ``pdftotext out.pdf - | md5sum`` is stable, ``md5sum out.pdf`` is not.

Why a cover page: the deliverable is read by the Owner, not by a build system. The first page
carries the title, the division, and the source path so a printed copy can never be mistaken
for a different edition.
"""

from __future__ import annotations

import html
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "docs" / "PROJECT_VILLA_AGENCY_A-Z.md"
OUT = REPO / "docs" / "rendered"
STEM = "PROJECT_VILLA_AGENCY_A-Z"

CSS = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }
html { font-size: 10.5pt; }
body { font-family: "DejaVu Serif", Georgia, serif; color: #1a1a1a; line-height: 1.5; }
.cover { min-height: 250mm; border-left: 6pt solid #0f3d2e; padding: 26mm 0 0 16mm;
         page-break-after: always; }
.cover h1 { font-family: "DejaVu Sans", Helvetica, sans-serif; font-size: 26pt; line-height: 1.2;
            margin: 0 0 6mm 0; color: #0f3d2e; page-break-before: avoid; }
.cover .sub { font-size: 12pt; color: #444; margin: 0 0 3mm 0; }
.cover .meta { font-family: "DejaVu Sans", sans-serif; font-size: 9pt; color: #666; margin-top: 10mm; }
h1, h2, h3, h4 { font-family: "DejaVu Sans", Helvetica, sans-serif; color: #0f3d2e;
                 page-break-after: avoid; }
h1 { font-size: 18pt; margin: 0 0 4mm 0; border-bottom: 1.5pt solid #0f3d2e; padding-bottom: 2mm;
     page-break-before: always; }
h2 { font-size: 13pt; margin: 7mm 0 2.5mm 0; }
h3 { font-size: 11pt; margin: 5mm 0 2mm 0; }
p, li { orphans: 3; widows: 3; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 8.8pt; background: #f3f4f2;
       padding: 0.4mm 1mm; border-radius: 1mm; }
pre { background: #f6f7f5; border-left: 2.5pt solid #b9c6bd; padding: 2.5mm 3mm; overflow-x: auto;
      page-break-inside: avoid; }
pre code { background: none; font-size: 8.3pt; }
table { border-collapse: collapse; width: 100%; margin: 3mm 0 4mm 0; font-size: 8.6pt;
        page-break-inside: auto; }
thead { display: table-header-group; }
th, td { border: 0.5pt solid #c9cfcb; padding: 1.2mm 1.8mm; text-align: left; vertical-align: top; }
th { background: #eef1ee; font-family: "DejaVu Sans", sans-serif; font-size: 8.4pt; }
tr { page-break-inside: avoid; }
blockquote { margin: 3mm 0; padding: 2mm 4mm; border-left: 2.5pt solid #d8b26a; color: #333;
             background: #fcf8ef; }
a { color: #14523d; text-decoration: none; }
hr { border: none; border-top: 0.5pt solid #c9cfcb; margin: 6mm 0; }
.toc { page-break-after: always; }
.toc ul { list-style: none; padding-left: 0; }
.toc ul ul { padding-left: 6mm; font-size: 9pt; color: #444; }
.toc a::after { content: ""; }
"""


def build_body(markdown_text: str) -> tuple[str, str]:
    """Split the leading ``# Title`` off (it becomes the cover) and render the rest."""
    import markdown

    lines = markdown_text.splitlines()
    title = "Document"
    for index, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            del lines[index]
            break
    body = markdown.markdown(
        "\n".join(lines),
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
        extension_configs={"toc": {"toc_depth": "2-3", "title": "Daftar Isi"}},
    )
    return title, body


def main() -> int:
    if not SRC.is_file():
        print(f"missing source: {SRC}")
        return 2
    markdown_text = SRC.read_text(encoding="utf-8")
    title, body = build_body(markdown_text)
    OUT.mkdir(parents=True, exist_ok=True)
    page = f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>{CSS}</style></head>
<body>
<section class="cover">
  <div class="sub">Divisi Villa Management &amp; Agency — Bali &amp; Yogyakarta</div>
  <h1>{html.escape(title)}</h1>
  <div class="sub">Reservation &amp; Operations Control Plane</div>
  <div class="meta">Dokumen kerja internal · dirender dari
  <code>docs/{SRC.name}</code> · {len(markdown_text):,} karakter</div>
</section>
{body}
</body></html>
"""
    html_path = OUT / f"{STEM}.html"
    html_path.write_text(page, encoding="utf-8")

    pdf_path = OUT / f"{STEM}.pdf"
    chrome = subprocess.run(
        [
            "google-chrome", "--headless", "--disable-gpu", "--no-sandbox",
            "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}",
            # quote() because the repo path may contain characters Chrome's parser dislikes.
            "file://" + quote(str(html_path)),
        ],
        capture_output=True,
        text=True,
    )
    if chrome.returncode != 0 or not pdf_path.is_file():
        print(f"chrome failed rc={chrome.returncode}: {chrome.stderr[-800:]}")
        return 1
    print(f"html={html_path.stat().st_size} pdf={pdf_path.stat().st_size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
