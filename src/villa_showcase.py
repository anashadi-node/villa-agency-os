"""Per-villa showcase content for the public marketing surface.

The public villa page needs three things the kernel does not own — a photo binding, an
area note, and a factual highlight list — and each one carries a different honesty bar:

* **Photo binding.** ``assets/photos/`` holds CC BY-SA / CC0 stock imagery, matched to a
  villa by a keyword in its name (credits: ``docs/PHOTO_CREDITS_2026-10-06.md``). It is
  atmosphere, not a photo of the listed property, and the page says so.
* **Area note.** Curated U.S. English prose about the *region*, never about a specific
  property. A region the module has no note for falls back to a neutral sentence built
  from the province and region the kernel already stores.
* **Highlights.** Built only from values the kernel stores (unit count, bedrooms,
  capacity), so every number on the page is one the agency can defend.

What the module refuses to do is invent a property feature. Naming a private pool for a
villa nobody has documented would misrepresent the thing being sold, so unverified
detail is always answered with ``DISCLOSURE`` and "confirmed by our team" instead.

Replacing the placeholders with real per-villa photos and copy is deliberately NOT a
change to this module: a real photo is a dated property document, and
``evidence_objects`` already refuses storage without a clean scan and a classification,
so it belongs in that pipeline. This module is the placeholder regime until then.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "AREA_NOTES",
    "DISCLOSURE",
    "STYLE_NOTES",
    "VillaShowcase",
    "display_place",
    "photo_stem_for",
    "showcase_for",
    "style_note",
]

#: Keyword found in a villa name -> ``assets/photos/`` stem. Ordered: the first match
#: wins, so a name mention is enough and no villa has to carry a database column yet.
PHOTO_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("canggu", "canggu"),
    ("jatiluwih", "jatiluwih"),
    ("uluwatu", "uluwatu"),
    ("joglo", "joglo"),
    ("merapi", "joglo"),
)

#: Photo stem -> en-US note about the AREA the stem is named for. These describe a
#: region a traveller can look up, never a feature of the property being sold.
AREA_NOTES: Mapping[str, str] = {
    "canggu": "Canggu is a coastal strip on Bali's south-west shore, known for its surf breaks and cafes.",
    "jatiluwih": "Jatiluwih is highland rice-terrace country on the slopes of Mount Batukaru, in Tabanan, Bali.",
    "uluwatu": "Uluwatu is the limestone cliff peninsula at Bali's southern tip, facing the Indian Ocean.",
    "joglo": "The highlands north of Yogyakarta are cool upland country on the slopes of Mount Merapi.",
}

#: Property type -> en-US sentence. The vocabulary is the ``properties.property_type``
#: CHECK list in ``src/schema/001_initial.sql``; anything outside it gets the fallback.
STYLE_NOTES: Mapping[str, str] = {
    "villa": "A private villa, let as a whole or by unit; the exact layout is confirmed by our team.",
    "resort": "A resort property, with managed units and shared facilities.",
    "guesthouse": "A guesthouse, with rooms let individually.",
    "apartment": "A serviced apartment building, with units let individually.",
    "homestay": "A homestay, where guests stay alongside the host household.",
}

#: Printed on every villa page. It states the scope of the area note and the promise
#: the page does NOT make, so a guest is never left inferring a verified feature.
DISCLOSURE = (
    "Area notes describe the surrounding region, not verified features of this villa. "
    "Our team confirms layout, facilities and current photos before you book."
)


@dataclass(frozen=True)
class VillaShowcase:
    """Everything the public villa page shows about a villa, as plain text.

    Values here are unescaped. The caller escapes them at render time
    (``dashboard.escape_value``), the same as every other dynamic value.
    """

    photo_stem: Optional[str]
    area_note: str
    style_note: str
    highlights: tuple[str, ...]
    disclosure: str = DISCLOSURE


def photo_stem_for(villa_name: str) -> Optional[str]:
    """``assets/photos/`` stem for a villa by name keyword, or ``None`` for no match.

    Matching is case-insensitive and substring-based: an imported villa list written
    as "Villa Canggu Garden Retreat" and one written as "Canggu Retreat" both bind to
    the same placeholder photo.
    """
    lowered = villa_name.lower()
    for keyword, stem in PHOTO_KEYWORDS:
        if keyword in lowered:
            return stem
    return None


def style_note(property_type: str) -> str:
    """en-US sentence for a stored ``property_type``, with a neutral fallback."""
    note = STYLE_NOTES.get(property_type)
    if note is not None:
        return note
    readable = property_type.replace("_", " ") or "property"
    return f"A {readable}, with the details confirmed by our team."


def display_place(value: str, *others: str) -> str:
    """Normalise an operator-typed place name for prose, or drop it as redundant.

    Region and province are free text the agency types in, so "yogyakarta" and
    "DI Yogyakarta" arrive for the same place. In prose the lowercase one reads as a
    typo and the pair reads as a stutter, so: a name wholly contained in a sibling
    name is dropped, and an all-lowercase name is title-cased. Anything already
    carrying capitalisation ("DI Yogyakarta", "Nusa Penida") is passed through.
    """
    text = value.strip()
    if not text:
        return ""
    for other in others:
        sibling = other.strip()
        if sibling and sibling != text and text.lower() in sibling.lower():
            return ""
    return text.title() if text.islower() else text


def _area_note(stem: Optional[str], province: str, region: str) -> str:
    if stem is not None:
        note = AREA_NOTES.get(stem)
        if note is not None:
            return note
    places = [part for part in (display_place(region, province), display_place(province, region)) if part]
    if places:
        return f"This villa is in {', '.join(places)}."
    return "The area around this villa is described by our team on request."


def _highlights(units: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Facts the kernel can defend: how many units, how many bedrooms, how many guests."""
    lines: list[str] = []
    if units:
        lines.append(f"{len(units)} unit{'' if len(units) == 1 else 's'} in this villa")
        bedrooms = sorted({int(unit["bedrooms"]) for unit in units if int(unit["bedrooms"]) > 0})
        if bedrooms:
            span = f"{bedrooms[0]}" if len(bedrooms) == 1 else f"{bedrooms[0]}\u2013{bedrooms[-1]}"
            lines.append(f"{span} bedroom{'' if bedrooms == [1] else 's'} per unit")
        sleeps = max(int(unit["max_guests"]) for unit in units)
        if sleeps > 0:
            lines.append(f"Sleeps up to {sleeps}")
    if not lines:
        lines.append("Unit details are confirmed by our team on request")
    return tuple(lines)


def showcase_for(
    villa: Mapping[str, Any], units: Sequence[Mapping[str, Any]] = ()
) -> VillaShowcase:
    """Assemble the showcase block for one villa from stored values only."""
    name = str(villa["name"])
    stem = photo_stem_for(name)
    return VillaShowcase(
        photo_stem=stem,
        area_note=_area_note(stem, str(villa["province"]), str(villa["region"])),
        style_note=style_note(str(villa["property_type"])),
        highlights=_highlights(units),
    )
