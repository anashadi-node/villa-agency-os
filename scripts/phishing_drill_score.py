"""Nilai hasil latihan phishing internal — gagal tertutup (fail closed).

Masukan: CSV ekspor satu baris per penerima. Skrip ini hanya menghitung dan
memutuskan; ia tidak mengirim apa pun dan tidak menyimpan data pribadi.

Ambang yang dipakai (lihat docs/security/PHISHING_DRILL_KIT.md):
  * kredensial terkirim  = 0            (satu pun menggagalkan latihan)
  * klik                <= 10% dari terkirim
  * lapor               >= 50% dari terkirim
  * kohort terkirim     >= 5           (di bawah itu: INSUFFICIENT, bukan PASS)

Exit: 0 = ambang terpenuhi · 2 = ambang dilanggar · 3 = data tidak cukup
      · 1 = berkas tidak sah (ditolak, bukan diabaikan).
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field

FLAGS = {"0", "1"}
MIN_COHORT = 5
CLICK_CEILING_PCT = 10.0
REPORT_FLOOR_PCT = 50.0
REQUIRED_COLUMNS = (
    "recipient_id",
    "template",
    "delivered",
    "clicked",
    "submitted_credentials",
    "reported",
)


@dataclass(frozen=True)
class Row:
    recipient_id: str
    template: str
    delivered: bool
    clicked: bool
    submitted: bool
    reported: bool


@dataclass
class Stats:
    template: str
    delivered: int = 0
    clicked: int = 0
    submitted: int = 0
    reported: int = 0

    def rate(self, count: int) -> float:
        return 100.0 * count / self.delivered if self.delivered else 0.0


@dataclass
class Summary:
    by_template: dict[str, Stats] = field(default_factory=dict)
    rows: int = 0

    @property
    def delivered(self) -> int:
        return sum(item.delivered for item in self.by_template.values())

    @property
    def clicked(self) -> int:
        return sum(item.clicked for item in self.by_template.values())

    @property
    def submitted(self) -> int:
        return sum(item.submitted for item in self.by_template.values())

    @property
    def reported(self) -> int:
        return sum(item.reported for item in self.by_template.values())


def parse_flag(value: str | None, line: int, column: str) -> bool:
    text = (value or "").strip()
    if text not in FLAGS:
        raise ValueError(f"baris {line}: {column}={value!r} harus 0 atau 1")
    return text == "1"


def load(path: str) -> Summary:
    summary = Summary()
    seen: set[str] = set()
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in REQUIRED_COLUMNS if name not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"kolom wajib tidak ada: {', '.join(missing)}")
        for line, raw in enumerate(reader, start=2):
            recipient = (raw.get("recipient_id") or "").strip()
            if not recipient:
                raise ValueError(f"baris {line}: recipient_id kosong")
            if recipient in seen:
                raise ValueError(f"baris {line}: recipient_id duplikat ({recipient})")
            seen.add(recipient)

            row = Row(
                recipient_id=recipient,
                template=(raw.get("template") or "").strip() or "unknown",
                delivered=parse_flag(raw.get("delivered"), line, "delivered"),
                clicked=parse_flag(raw.get("clicked"), line, "clicked"),
                submitted=parse_flag(raw.get("submitted_credentials"), line, "submitted_credentials"),
                reported=parse_flag(raw.get("reported"), line, "reported"),
            )
            if row.clicked and not row.delivered:
                raise ValueError(f"baris {line}: clicked=1 pada pesan yang tidak terkirim")
            if row.submitted and not row.clicked:
                raise ValueError(f"baris {line}: submitted=1 tanpa clicked=1")
            if row.reported and not row.delivered:
                raise ValueError(f"baris {line}: reported=1 pada pesan yang tidak terkirim")

            stats = summary.by_template.setdefault(row.template, Stats(template=row.template))
            stats.delivered += int(row.delivered)
            stats.clicked += int(row.clicked)
            stats.submitted += int(row.submitted)
            stats.reported += int(row.reported)
            summary.rows += 1
    return summary


def verdict(summary: Summary) -> tuple[int, list[str]]:
    """Return (exit_code, problems). Never returns 0 on missing evidence."""
    if summary.delivered < MIN_COHORT:
        return 3, [
            f"data tidak cukup: {summary.delivered} terkirim, minimum {MIN_COHORT}. "
            "Jangan simpulkan apa pun dari angka ini."
        ]

    problems: list[str] = []
    for stats in sorted(summary.by_template.values(), key=lambda item: item.template):
        if stats.submitted:
            problems.append(f"{stats.template}: {stats.submitted} penerima mengirim kredensial (ambang 0)")
        click_rate = stats.rate(stats.clicked)
        if click_rate > CLICK_CEILING_PCT:
            problems.append(
                f"{stats.template}: klik {click_rate:.1f}% melebihi batas {CLICK_CEILING_PCT:.0f}%"
            )
        report_rate = stats.rate(stats.reported)
        if report_rate < REPORT_FLOOR_PCT:
            problems.append(
                f"{stats.template}: lapor {report_rate:.1f}% di bawah lantai {REPORT_FLOOR_PCT:.0f}%"
            )
    return (2 if problems else 0), problems


def render(summary: Summary) -> str:
    header = f"{'template':<24}{'terkirim':>9}{'klik':>7}{'klik%':>8}{'kredensial':>12}{'lapor%':>8}"
    lines = [header, "-" * len(header)]
    for stats in sorted(summary.by_template.values(), key=lambda item: item.template):
        lines.append(
            f"{stats.template:<24}{stats.delivered:>9}{stats.clicked:>7}"
            f"{stats.rate(stats.clicked):>7.1f}%{stats.submitted:>12}"
            f"{stats.rate(stats.reported):>7.1f}%"
        )
    lines.append("-" * len(header))
    lines.append(
        f"{'TOTAL':<24}{summary.delivered:>9}{summary.clicked:>7}"
        f"{(100.0 * summary.clicked / summary.delivered if summary.delivered else 0):>7.1f}%"
        f"{summary.submitted:>12}"
        f"{(100.0 * summary.reported / summary.delivered if summary.delivered else 0):>7.1f}%"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nilai CSV latihan phishing internal.")
    parser.add_argument("csv_path", help="ekspor CSV satu baris per penerima")
    args = parser.parse_args(argv)
    try:
        summary = load(args.csv_path)
    except (OSError, ValueError) as exc:
        print(f"DITOLAK: {exc}", file=sys.stderr)
        return 1

    print(render(summary))
    code, problems = verdict(summary)
    for problem in problems:
        print(f"- {problem}", file=sys.stderr)
    if code == 0:
        print("PUTUSAN: ambang terpenuhi")
    elif code == 3:
        print("PUTUSAN: DATA TIDAK CUKUP", file=sys.stderr)
    else:
        print("PUTUSAN: AMBANG DILANGGAR", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
