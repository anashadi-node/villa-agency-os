"""Human-readable timestamps for the internal dashboard.

The database stores instants as ISO-8601 strings in UTC (``2026-11-07T00:00:00+00:00``).
Printing that raw in a table is unreadable for the owner, but silently dropping it
would lose precision, so every timestamp cell is rendered twice:

    <time datetime="2026-11-07T00:00:00+00:00">07 Nov 2026, 07:00 WIB</time>

The machine value stays in the attribute (tests, copy-paste, screen readers), the
human value is what the eye reads. Only *complete* ISO instants are rewritten:
a bare date (``2026-11-10``) or arbitrary text is left untouched, so this cannot
mangle an invoice number or a property name that happens to start with digits.

No third-party dependency: ``zoneinfo`` ships with Python, and if the platform has
no tz database we fall back to a fixed +07:00 offset rather than guessing or failing.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

ZONE = "Asia/Jakarta"
LABEL = "WIB"
FALLBACK = timezone(timedelta(hours=7), LABEL)

# A whole cell that is nothing but an ISO instant. Seconds and fractional seconds
# are optional because the database writes both shapes.
_INSTANT = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})$"
)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
           "Jul", "Agu", "Sep", "Okt", "Nov", "Des")


def _zone():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(ZONE)
    except Exception:  # no tz database, or too old a Python: fixed offset instead
        return FALLBACK


def parse(text: str) -> Optional[datetime]:
    """Return the instant ``text`` names, or ``None`` if it is not one."""
    match = _INSTANT.match(text.strip())
    if not match:
        return None
    stamp = match.group(0).replace("Z", "+00:00")
    if stamp[10] == " ":
        stamp = stamp[:10] + "T" + stamp[11:]
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None  # a naive instant has no defined meaning; leave it visible as written
    return moment


def label(moment: datetime) -> str:
    """``07 Nov 2026, 07:00 WIB`` for an aware instant."""
    local = moment.astimezone(_zone())
    return (
        f"{local.day:02d} {_MONTHS[local.month - 1]} {local.year}, "
        f"{local.hour:02d}:{local.minute:02d} {LABEL}"
    )


def humanize(value: object) -> Optional[str]:
    """HTML for a timestamp cell, or ``None`` when ``value`` is not one.

    ``None`` is the signal for the caller to fall back to plain escaping, so this
    function never changes text it does not fully understand.
    """
    if not isinstance(value, str):
        return None
    moment = parse(value)
    if moment is None:
        return None
    raw = html.escape(value.strip(), quote=True)
    return f'<time datetime="{raw}" title="{raw}">{label(moment)}</time>'
