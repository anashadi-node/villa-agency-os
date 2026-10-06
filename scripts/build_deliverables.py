#!/usr/bin/env python3
"""Build the owner-facing PDF/HTML deliverables from the Markdown in ``docs/``.

The Markdown is the source of truth; these files are a **rendering** for reading, and nothing in
the repository is edited by this script. That order matters: a PDF that disagrees with the Markdown
is a bug in the rendering, not a second opinion.

    python3 scripts/build_deliverables.py            # write HTML + PDF to ~/Downloads
    python3 scripts/build_deliverables.py --check     # parse and report, write nothing

Why a committed script instead of a one-off: the deliverables were first produced by a scratch
script that lived in ``/tmp`` and was lost, which left the PDFs stale against the docs. A recurring
owner-facing artifact belongs in the repository.

Requirements: ``markdown`` (in the project venv) and ``google-chrome`` for the PDF step. If Chrome
is missing, the HTML is still written and the run says so — it does not silently skip.
"""

from __future__ import annotations

import argparse
import html
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

DEFAULT_OUT = Path.home() / "Downloads"
EDITION = date.today().isoformat()

#: (source Markdown, title, one-line subtitle, badges). Order is the reading order.
DELIVERABLES: tuple[dict[str, object], ...] = (
    {
        "source": "docs/ECOSYSTEM_EXPLAINED.md",
        "title": "Villa Agency OS — Dijelaskan",
        "subtitle": "Apa yang sudah dibangun, apa yang belum, dan apa artinya bagi agency Anda.",
        "badges": ("Sumber: repository", f"Edisi {EDITION}", "Untuk pemilik"),
        "slug": "Villa_Agency_Dijelaskan",
    },
    {
        "source": "docs/STATUS_AND_GAP_MAP.md",
        "title": "Villa Agency OS — Peta Status & Kesenjangan",
        "subtitle": "Perbandingan jujur antara dokumen acuan dan apa yang benar-benar ada di kode.",
        "badges": ("Sumber: repository", f"Edisi {EDITION}", "Verifikasi baris-per-baris"),
        "slug": "Villa_Agency_Status_Map",
    },
    {
        "source": "docs/TENANT_SETUP.md",
        "title": "Panduan Setelan Tenant — Villa Agency OS",
        "subtitle": "Apa yang diisi agency sendiri sebelum dipakai dengan tamu dan uang nyata.",
        "badges": ("Sumber: repository", f"Edisi {EDITION}", "Self-service per agency"),
        "slug": "Villa_Agency_Panduan_Setelan_Tenant",
    },
    {
        "source": "docs/RESEARCH_AI_WORKFORCE_SKILLS_2026-10-06.md",
        "title": "Riset Workforce AI × Skills Pendukung",
        "subtitle": "Cakupan fondasi tiap agen AI, gap substantif, dan skills Hermes yang menunjang.",
        "badges": ("Sumber: repository", f"Edisi {EDITION}", "Riset 2026-10-06"),
        "slug": "Villa_Agency_Riset_AI_Workforce_Skills",
    },
    {
        "source": "docs/MESSAGE_TEMPLATES.md",
        "title": "Pustaka Template Pesan — Villa Agency OS",
        "subtitle": "Kalimat siap pakai untuk tamu, dengan batas keamanan yang sudah dipasang.",
        "badges": ("Sumber: repository", f"Edisi {EDITION}", "Bisa disunting agency"),
        "slug": "Villa_Agency_Pustaka_Template_Pesan",
    },
)

CSS = """:root{--ink:#20252b;--muted:#66707a;--line:#dfe4e8;--accent:#6f553f;--dark:#252a2f}
*{box-sizing:border-box}
body{margin:0;background:#eef0f1;color:var(--ink);
     font-family:Inter,"Segoe UI",Arial,sans-serif;line-height:1.6}
.page{max-width:1180px;margin:auto;background:#fff;min-height:100vh;box-shadow:0 1px 24px #0000001a}
header{padding:48px 60px 38px;background:var(--dark);color:#fff}
.eyebrow{text-transform:uppercase;letter-spacing:1.8px;font-size:12px;opacity:.72}
header h1{margin:10px 0 12px;font-size:34px;line-height:1.15}
header .sub{color:#dfe3e6;max-width:900px;font-size:14px}
.badges{display:flex;gap:8px;flex-wrap:wrap;margin-top:20px}
.badge{border:1px solid #ffffff33;border-radius:999px;padding:5px 11px;font-size:12px}
main{padding:36px 60px 60px}
h2{font-size:23px;margin:44px 0 14px;padding-bottom:8px;border-bottom:2px solid var(--dark)}
h3{font-size:18px;margin:28px 0 10px}
h4{font-size:15px;margin:20px 0 8px}
table{width:100%;border-collapse:collapse;margin:14px 0 26px;font-size:13.5px}
th,td{border:1px solid var(--line);padding:9px 11px;vertical-align:top;text-align:left}
th{background:#f0f2f3}
tbody tr:nth-child(even){background:#fafbfb}
code{background:#f3f4f5;padding:2px 5px;border-radius:4px;font-size:12.5px}
pre{background:#fafafa;border:1px solid var(--line);padding:14px;border-radius:9px;
    overflow-x:auto;font-size:12.5px}
ul,ol{padding-left:24px}
li{margin:4px 0}
hr{border:0;border-top:1px solid var(--line);margin:34px 0}
strong{color:#111}
blockquote{margin:16px 0;padding:12px 16px;background:#f7f2ed;border-left:4px solid var(--accent)}
#toc{background:#f7f8f9;border:1px solid var(--line);border-radius:10px;padding:18px 22px}
#toc::before{content:"Daftar Isi";display:block;font-weight:600;margin-bottom:8px}
#toc ul{list-style:none;padding-left:0;margin:0;columns:2;column-gap:32px}
#toc ul ul{padding-left:16px;columns:1;font-size:13px;color:var(--muted)}
#toc a{text-decoration:none;color:var(--ink)}
#toc a:hover{color:var(--accent);text-decoration:underline}
footer{padding:26px 60px;background:var(--dark);color:#d9dcde;font-size:12px}
@media(max-width:820px){header,main,footer{padding-left:22px;padding-right:22px}#toc ul{columns:1}}
@media print{body{background:#fff}.page{box-shadow:none;max-width:none}
  table,pre,#toc{break-inside:avoid}h2,h3{break-after:avoid}}"""


def markdown_renderer():
    try:
        import markdown
    except ModuleNotFoundError as exc:  # pragma: no cover - environment guard
        raise SystemExit(
            "python-markdown tidak ada. Jalankan dengan venv proyek: "
            "~/.hermes/hermes-agent/venv/bin/python scripts/build_deliverables.py"
        ) from exc
    return markdown.Markdown(extensions=["tables", "toc", "fenced_code", "attr_list", "sane_lists"])


def render_html(item: dict[str, object]) -> tuple[str, str, str]:
    """Return (html, title, body_text) — body_text is what we verify, not the HTML."""
    source = REPO / str(item["source"])
    text = source.read_text(encoding="utf-8")
    renderer = markdown_renderer()
    body = renderer.convert(text)
    toc = getattr(renderer, "toc", "")
    badges = "".join(
        f'<span class="badge">{html.escape(str(badge))}</span>' for badge in tuple(item["badges"])
    )
    page = f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(str(item["title"]))}</title>
<style>
{CSS}
</style>
</head>
<body><div class="page">
<header>
<div class="eyebrow">Sumber: {html.escape(str(item["source"]))} &middot; edisi {EDITION}</div>
<h1>{html.escape(str(item["title"]))}</h1>
<div class="sub">{html.escape(str(item["subtitle"]))}</div>
<div class="badges">{badges}</div>
</header>
<main>
<div id="toc">{toc}</div>
{body}
</main>
<footer>Dihasilkan dari Markdown repository oleh scripts/build_deliverables.py &middot; edisi {EDITION}
&middot; angka dan klaim di dokumen ini dapat diverifikasi langsung di kode.</footer>
</div></body></html>"""
    return page, str(item["title"]), text


def write_pdf(html_path: Path, pdf_path: Path) -> str:
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if chrome is None:
        return "PDF dilewati: google-chrome tidak ada di PATH (HTML tetap ditulis)"
    subprocess.run(
        [
            chrome, "--headless", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri(),
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )
    return f"PDF {pdf_path.name}: {pdf_path.stat().st_size // 1024} KiB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the owner-facing deliverables.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--check", action="store_true", help="render in memory and report only")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if not args.check:
        out.mkdir(parents=True, exist_ok=True)

    failures = 0
    for item in DELIVERABLES:
        page, title, body = render_html(item)
        slug = f"{item['slug']}_{EDITION}"
        if args.check:
            print(f"OK  {slug}: {len(body):,} char teks, {len(page):,} char HTML")
            continue
        html_path = out / f"{slug}.html"
        html_path.write_text(page, encoding="utf-8")
        print(f"OK  {html_path}: {len(body):,} char teks, {len(page):,} char HTML")
        try:
            print("    " + write_pdf(html_path, out / f"{slug}.pdf"))
        except subprocess.CalledProcessError as exc:
            failures += 1
            print(f"    PDF GAGAL untuk {slug}: {exc.stderr.decode('utf-8', 'replace')[:200]}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
