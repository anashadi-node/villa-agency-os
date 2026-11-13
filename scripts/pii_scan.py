#!/usr/bin/env python3
"""PII scan for a public tree — fail closed.

The repository is published. A credential scanner says nothing about guest
data, so this gate answers a different question: does any text file that could
be published — tracked, or untracked and not ignored — contain a value shaped
like a real person's contact detail, identifier, or card number?

What it can prove: unmasked e-mail addresses outside the reserved documentation
domains, unmasked Indonesian mobile numbers, 16-digit identifier numbers, and
Luhn-valid card numbers are absent from text files.

What it cannot prove — stated so nobody reads a PASS as more than it is:
  * binaries are skipped, so text inside a committed PDF or image is NOT scanned;
  * names, addresses, passport numbers and dates of birth are not pattern-scanned
    — they are caught by review, not by this script;
  * a value that is real but also matches the placeholder allowlist below is
    invisible to it.

A placeholder allowlist is unavoidable: the test suite legitimately contains
synthetic values. Each entry is listed with its reason, so adding a real value
to it is a visible, reviewable act rather than a silent regex tweak.

Usage: scripts/pii_scan.py [root]     (exit 0 = clean, 1 = findings)
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# --- Placeholders that may appear in a public tree, and why --------------------
# Every entry is a documented synthetic value, not a masked real one.
PLACEHOLDER_EMAILS = {
    "a@b.com": "one-letter example in ADR 0027 redaction tests",
    "budi@villa.id": "canonical Indonesian placeholder name, used to prove redaction works",
}
PLACEHOLDER_PHONES = {
    "081234567890": "documentation number (ascending digits)",
    "0812345678901": "documentation number, one digit longer variant",
    "+62 812 3456 7890": "documentation number with country code",
    "6281234567890": "documentation number without separators",
    "628123456789": "documentation number, 12 digits",
    "+628111111111": "repeated-digit placeholder for a WhatsApp setting",
    "+628222222222": "repeated-digit placeholder, second tenant's WhatsApp setting",
}
PLACEHOLDER_IDS = {
    "4111111111111111": "canonical Visa test card (never a real account)",
    "4242424242424242": "canonical Stripe test card (never a real account)",
    "3273010101900001": "synthetic NIK used in ADR 0062/0027 examples",
    "3273010101990001": "synthetic NIK, phase-2 verification fixture",
    "3174091234567890": "synthetic NIK with an ascending body",
    "3171012345678901": "synthetic NIK (province 3171, ascending body) in redaction tests",
    "1234567890123456": "ascending-digit fixture, not an identifier",
}

SAFE_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "example.test",
    "invalid", "test", "localhost",
}
# A token boundary: the value must not be glued to other word characters, which
# is what keeps digit runs inside hex digests and minted ids out of the report.
BEFORE = r"(?<![0-9A-Za-z_.+-])"
AFTER = r"(?![0-9A-Za-z_+-])"

EMAIL = re.compile(BEFORE + r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})" + AFTER)
PHONE = re.compile(BEFORE + r"(?:\+62|62|0)[ -]?8(?:[ -]?\d){7,11}" + AFTER)
IDENT = re.compile(BEFORE + r"\d{16}" + AFTER)
CARD = re.compile(BEFORE + r"(?:\d[ -]?){12,18}\d" + AFTER)


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def tracked_files(root: Path) -> list[str]:
    """Tracked files unioned with untracked-but-publishable ones.

    `git ls-files` alone is a trap: a file that is written but not yet committed
    is absent from the list, so the gate reports PASS over a tree it has not read.
    That is exactly how this gate first produced a false PASS. Untracked files
    that git does not ignore are added, because they are one `git add` away from
    being published.
    """
    try:
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
        ).stdout
        untracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--others", "--exclude-standard"],
            capture_output=True,
            check=True,
        ).stdout
        files = sorted({p for p in (tracked + untracked).decode().split("\0") if p})
        if files:
            return files
        raise ValueError("git ls-files returned nothing; refusing to PASS an unread tree")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    skip = {".git", "__pycache__", ".venv", "node_modules"}
    return [
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and not (skip & set(p.parts))
    ]


def is_text(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return False
    if b"\0" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def scan(root: Path) -> tuple[dict[str, list[str]], int]:
    findings: dict[str, list[str]] = defaultdict(list)
    files = tracked_files(root)
    scanned = 0
    for name in files:
        path = root / name
        if not path.is_file() or not is_text(path):
            continue
        scanned += 1
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            for m in EMAIL.finditer(line):
                value = m.group(0)
                domain = m.group(1).lower()
                if value.lower() in PLACEHOLDER_EMAILS:
                    continue
                if domain in SAFE_EMAIL_DOMAINS or domain.endswith(
                    (".example", ".invalid", ".test", ".localhost")
                ):
                    continue
                findings["e-mail"].append(f"{name}:{lineno}: {value}")

            for m in PHONE.finditer(line):
                value = m.group(0).strip()
                digits = re.sub(r"\D", "", value)
                if len(digits) < 9:
                    continue
                if value in PLACEHOLDER_PHONES or digits in PLACEHOLDER_PHONES:
                    continue
                # A tail made of one repeated digit (0812-0000-0000, +628111111111) is a
                # placeholder shape; no real numbering plan hands these out in bulk.
                if len(set(digits[6:])) <= 1:
                    continue
                findings["phone"].append(f"{name}:{lineno}: {value}")

            for m in IDENT.finditer(line):
                value = m.group(0)
                if value in PLACEHOLDER_IDS:
                    continue
                findings["16-digit id"].append(f"{name}:{lineno}: {value}")

            for m in CARD.finditer(line):
                value = m.group(0).strip()
                digits = re.sub(r"\D", "", value)
                if not 13 <= len(digits) <= 19:
                    continue
                if digits in PLACEHOLDER_IDS or value in PLACEHOLDER_IDS:
                    continue
                # Grouping must look like a card (4-4-4-4, 4-6-5, 4-4-4-1). Arbitrary
                # triples are drawing coordinates: an SVG path such as
                # "M0 365 C160 290 270 390 430 310" is Luhn-valid by accident and has
                # produced a false "card number" in review before.
                groups = [len(g) for g in re.split(r"[ -]", value)]
                if len(groups) > 1:
                    expected = [4, 4, 4, 4] if len(groups) == 4 else (
                        [4, 6, 5] if len(groups) == 3 else None
                    )
                    if len(groups) == 4 and groups[:3] == [4, 4, 4] and 1 <= groups[3] <= 4:
                        pass
                    elif expected is not None and groups == expected:
                        pass
                    else:
                        continue
                if luhn_ok(digits):
                    findings["card number"].append(f"{name}:{lineno}: {value}")
    findings["_scanned"] = [str(scanned)]
    return findings, scanned


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    findings, scanned = scan(root)
    total = sum(len(v) for k, v in findings.items() if k != "_scanned")
    for kind in ("e-mail", "phone", "16-digit id", "card number"):
        rows = findings.get(kind, [])
        if rows:
            print(f"PII-SCAN: {kind}: {len(rows)} finding(s)")
            for row in rows[:20]:
                print(f"  {row}")
    if total:
        print(f"PII-SCAN: FAIL ({total} finding(s)) — replace with synthetic values.")
        return 1
    print(f"PII-SCAN: PASS (0 findings, {scanned} text files scanned)")
    print("PII-SCAN: scope — text files only; names/addresses/binaries are not covered.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
