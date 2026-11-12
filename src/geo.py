"""Where a villa is — and how much of that the public is allowed to know.

The guest-facing site needs a map. A map needs coordinates. But a villa's exact
doorstep is the single most abusable dataset a booking agency holds: publish it
and a day-tripper can drive to a private residence, and a competitor can reverse
the owner's identity from a pin. So this module exists to keep three things
apart, the way the rest of the repository keeps inventory, money and evidence
apart:

* **Truth (``VERIFIED``)** — the coordinate a human stood at, or that the owner
  supplied in writing. Stored in ``properties.latitude/longitude``.
* **Area (``AREA_APPROX``)** — the village or beach a villa sits in. Safe to
  publish, enough to plan a trip, useless for finding the gate.
* **Unknown (``UNKNOWN``)** — no claim at all. The map says so instead of
  guessing, for the same reason availability is never invented.

Everything public passes through :func:`public_point`, which rounds to
:data:`PUBLIC_ROUNDING_DP` decimals (≈1.1 km at the equator). The rounding is
deliberate and one-way: the marketing site is handed the rounded pair even when
the database holds a verified one, so a template change cannot leak the precise
value. ``tests/test_geo.py`` pins that distance down with arithmetic, not with a
comment.

The map itself is an SVG this module builds: no tile server, no JavaScript, no
third-party request from a guest's browser (the site's Content-Security-Policy
allows ``default-src 'self'`` and nothing else). A guest's interest in a villa is
not something to hand to a map vendor, and a page that renders offline is one
fewer thing that can break at 21:00 on a Saturday.

Landmark coordinates below were read from OpenStreetMap's Nominatim on
2026-11-12 and are reference data, not villa locations. They are public,
checkable facts: the same query reproduces the same answer.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional, Sequence

from primitives import DomainError

__all__ = [
    "GEO_PRECISIONS",
    "PUBLIC_ROUNDING_DP",
    "LANDMARKS",
    "BBox",
    "haversine_km",
    "public_point",
    "resolve_point",
    "nearest_landmarks",
    "area_map_svg",
    "locator_map_svg",
    "validate_coordinate",
    "validate_precision",
]

#: Mirror of the ``properties.geo_precision`` CHECK constraint, verbatim.
GEO_PRECISIONS: tuple[str, ...] = ("UNKNOWN", "AREA_APPROX", "VERIFIED")

#: Two decimals ≈ 1.11 km of latitude. Chosen because it is the coarsest value
#: that still separates neighbouring villages on a 400 px-wide map.
PUBLIC_ROUNDING_DP = 2

#: Earth's mean radius (IUGG). A sphere is within 0.5 % of the WGS-84 ellipsoid
#: at these latitudes — far below the accuracy of any landmark we ship.
_EARTH_RADIUS_KM = 6371.0088


class GeoError(DomainError):
    """A coordinate or precision that cannot be published or stored."""

    code = "GEO_INVALID"


#: Public reference points, grouped by province. Each entry is
#: ``(key, label, latitude, longitude)``. Keys are stable identifiers used in
#: tests and in the detail page's "getting here" block.
LANDMARKS: Mapping[str, tuple[tuple[str, str, float, float], ...]] = {
    "Bali": (
        ("airport", "Ngurah Rai International Airport", -8.7420, 115.1637),
        ("canggu_beach", "Canggu Beach", -8.6600, 115.1307),
        ("seminyak_beach", "Seminyak Beach", -8.6849, 115.1528),
        ("ubud_palace", "Ubud Palace", -8.5069, 115.2627),
        ("tanah_lot", "Tanah Lot Temple", -8.6212, 115.0869),
        ("uluwatu_temple", "Uluwatu Temple", -8.8294, 115.0843),
    ),
    "DI Yogyakarta": (
        ("airport", "Adisutjipto Airport", -7.7933, 110.4268),
        ("yia", "Yogyakarta International Airport (YIA)", -7.9005, 110.0520),
        ("malioboro", "Malioboro Street", -7.7932, 110.3658),
        ("prambanan", "Prambanan Temple", -7.7522, 110.4915),
        ("borobudur", "Borobudur Temple", -7.6064, 110.2045),
        ("kotagede", "Kotagede Old Town", -7.8183, 110.3979),
    ),
}

#: How many landmarks the detail page lists. Four fits one line of the note
#: block and covers "airport + the two things guests actually do".
LANDMARKS_ON_DETAIL = 4


def _as_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise GeoError(f"{field} must be a number, not a boolean", field=field, value=repr(value))
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            raise GeoError(f"{field} is not a number", field=field, value=value) from None
    else:
        raise GeoError(f"{field} must be a number", field=field, value=repr(value))
    if not math.isfinite(number):
        raise GeoError(f"{field} must be finite", field=field, value=value)
    return number


def validate_coordinate(latitude: Any, longitude: Any) -> tuple[float, float]:
    """Return ``(lat, lon)`` as floats, or raise :class:`GeoError`.

    Rejects NaN and infinity explicitly: ``NaN`` compares false against every
    bound, so a naive range check would let it through and place a marker at
    ``(nan, nan)`` — visible as an empty map rather than as an error.
    """
    lat = _as_float(latitude, "latitude")
    lon = _as_float(longitude, "longitude")
    if not -90.0 <= lat <= 90.0:
        raise GeoError("latitude must be between -90 and 90", field="latitude", value=lat)
    if not -180.0 <= lon <= 180.0:
        raise GeoError("longitude must be between -180 and 180", field="longitude", value=lon)
    return lat, lon


def validate_precision(precision: Any) -> str:
    if not isinstance(precision, str) or precision not in GEO_PRECISIONS:
        raise GeoError(
            "geo_precision must be one of " + ", ".join(GEO_PRECISIONS),
            field="geo_precision",
            value=repr(precision),
        )
    return precision


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres.

    Straight-line distance, not driving distance: a road can be three times
    longer on Bali's coastal lanes, so the detail page says "as the crow flies"
    rather than implying a travel time we have not measured.
    """
    a_lat, a_lon = validate_coordinate(lat1, lon1)
    b_lat, b_lon = validate_coordinate(lat2, lon2)
    phi1, phi2 = math.radians(a_lat), math.radians(b_lat)
    d_phi = phi2 - phi1
    d_lambda = math.radians(b_lon - a_lon)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def public_point(latitude: Any, longitude: Any, *, dp: int = PUBLIC_ROUNDING_DP) -> tuple[float, float]:
    """The coordinate pair the public site is allowed to see: rounded, never exact."""
    lat, lon = validate_coordinate(latitude, longitude)
    if dp < 0:
        raise GeoError("dp must not be negative", field="dp", value=dp)
    return round(lat, dp), round(lon, dp)


def resolve_point(
    latitude: Any = None,
    longitude: Any = None,
    precision: Any = "UNKNOWN",
) -> Optional[tuple[float, float, str]]:
    """Decide what a row may publish: ``(lat, lon, precision)`` or ``None``.

    ``None`` means "we make no claim", which every caller must render as words,
    not as a pin dropped in the sea. A half-filled pair is treated as ``UNKNOWN``
    rather than repaired: inventing the missing half is the same class of error
    as inventing availability.
    """
    chosen = validate_precision(precision)
    if latitude is None or longitude is None or chosen == "UNKNOWN":
        return None
    lat, lon = public_point(latitude, longitude)
    return lat, lon, chosen


def nearest_landmarks(
    latitude: Any,
    longitude: Any,
    province: str,
    *,
    limit: int = LANDMARKS_ON_DETAIL,
) -> list[dict[str, Any]]:
    """The closest public landmarks, nearest first, with straight-line distance."""
    lat, lon = validate_coordinate(latitude, longitude)
    points = LANDMARKS.get(province, ())
    rows = [
        {
            "key": key,
            "label": label,
            "latitude": p_lat,
            "longitude": p_lon,
            "km": round(haversine_km(lat, lon, p_lat, p_lon), 1),
        }
        for key, label, p_lat, p_lon in points
    ]
    rows.sort(key=lambda row: (row["km"], row["label"]))
    return rows[: max(0, limit)]


class BBox:
    """A padded latitude/longitude window, widened so tiny sets stay legible."""

    __slots__ = ("min_lat", "max_lat", "min_lon", "max_lon")

    def __init__(self, points: Sequence[tuple[float, float]], *, min_span_deg: float = 0.05) -> None:
        if not points:
            raise GeoError("a map needs at least one point", field="points")
        lats = [lat for lat, _lon in points]
        lons = [lon for _lat, lon in points]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        # One villa must not fill the frame edge-to-edge, and two villas in the
        # same village must not collapse onto one pixel.
        for _ in range(2):
            if max_lat - min_lat < min_span_deg:
                mid = (max_lat + min_lat) / 2
                min_lat, max_lat = mid - min_span_deg / 2, mid + min_span_deg / 2
            if max_lon - min_lon < min_span_deg:
                mid = (max_lon + min_lon) / 2
                min_lon, max_lon = mid - min_span_deg / 2, mid + min_span_deg / 2
        pad_lat = (max_lat - min_lat) * 0.12
        pad_lon = (max_lon - min_lon) * 0.12
        self.min_lat, self.max_lat = min_lat - pad_lat, max_lat + pad_lat
        self.min_lon, self.max_lon = min_lon - pad_lon, max_lon + pad_lon

    def project(self, latitude: float, longitude: float, width: int, height: int) -> tuple[float, float]:
        """Equirectangular projection with a cos(lat) correction on longitude."""
        mid_lat = math.radians((self.min_lat + self.max_lat) / 2)
        scale_x = math.cos(mid_lat) or 1e-9
        span_x = (self.max_lon - self.min_lon) * scale_x or 1e-9
        span_y = (self.max_lat - self.min_lat) or 1e-9
        x = (longitude - self.min_lon) * scale_x / span_x * width
        y = (self.max_lat - latitude) / span_y * height
        return x, y


def _svg_text(value: Any) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _scale_bar(bbox: BBox, width: int, height: int) -> tuple[str, str]:
    """A bar whose length is a round number of kilometres, and its label."""
    span_km = haversine_km(bbox.min_lat, bbox.min_lon, bbox.min_lat, bbox.max_lon)
    target = span_km / 4 or 1.0
    nice = 1.0
    for candidate in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500):
        if candidate <= target:
            nice = float(candidate)
    bar_px = nice / span_km * width if span_km else 0.0
    bar_px = max(18.0, min(bar_px, width * 0.5))
    x0, y0 = 14.0, height - 16.0
    label = f"{nice:g} km"
    svg = (
        f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x0 + bar_px:.1f}" y2="{y0:.1f}" '
        f'stroke="currentColor" stroke-width="1.6"/>'
        f'<line x1="{x0:.1f}" y1="{y0 - 4:.1f}" x2="{x0:.1f}" y2="{y0 + 4:.1f}" stroke="currentColor" stroke-width="1.6"/>'
        f'<line x1="{x0 + bar_px:.1f}" y1="{y0 - 4:.1f}" x2="{x0 + bar_px:.1f}" y2="{y0 + 4:.1f}" stroke="currentColor" stroke-width="1.6"/>'
        f'<text x="{x0 + bar_px + 6:.1f}" y="{y0 + 4:.1f}" font-size="10">{_svg_text(label)}</text>'
    )
    return svg, label


def _graticule(bbox: BBox, width: int, height: int) -> str:
    """Three faint lat/lon lines with degree labels — orientation without a basemap."""
    parts: list[str] = []
    for fraction in (0.25, 0.5, 0.75):
        x = width * fraction
        lon = bbox.min_lon + (bbox.max_lon - bbox.min_lon) * fraction
        y = height * fraction
        lat = bbox.max_lat - (bbox.max_lat - bbox.min_lat) * fraction
        parts.append(
            f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{height}" stroke="currentColor" '
            f'stroke-width="0.5" stroke-dasharray="3 5" opacity="0.35"/>'
            f'<line x1="0" y1="{y:.1f}" x2="{width}" y2="{y:.1f}" stroke="currentColor" '
            f'stroke-width="0.5" stroke-dasharray="3 5" opacity="0.35"/>'
            f'<text x="{x:.1f}" y="{height - 46:.1f}" font-size="9" opacity="0.6">{lon:.2f}°E</text>'
            f'<text x="6" y="{y - 4:.1f}" font-size="9" opacity="0.6">{lat:.2f}°S</text>'
        )
    return "".join(parts)


def _markers(
    bbox: BBox,
    points: Sequence[Mapping[str, Any]],
    width: int,
    height: int,
) -> str:
    parts: list[str] = []
    for point in points:
        lat, lon = float(point["latitude"]), float(point["longitude"])
        x, y = bbox.project(lat, lon, width, height)
        title = str(point.get("title") or point.get("label") or "")
        label = str(point.get("label") or "")
        detail = str(point.get("detail") or "")
        parts.append(
            f'<g class="pin"><title>{_svg_text(title or label)}'
            f'{" — " + _svg_text(detail) if detail else ""}</title>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="#cf9a44" stroke="#123a2d" stroke-width="2"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="#123a2d"/></g>'
        )
    return "".join(parts)


def _label_lines(
    bbox: BBox,
    points: Sequence[Mapping[str, Any]],
    width: int,
    height: int,
) -> str:
    """Labels placed to the right of each pin, flipped left near the frame edge."""
    parts: list[str] = []
    for point in points:
        lat, lon = float(point["latitude"]), float(point["longitude"])
        x, y = bbox.project(lat, lon, width, height)
        label = str(point.get("label") or "")
        if not label:
            continue
        anchor = "start"
        text_x = x + 12
        if x > width - 120:
            anchor = "end"
            text_x = x - 12
        parts.append(
            f'<text x="{text_x:.1f}" y="{y + 3.5:.1f}" font-size="11" text-anchor="{anchor}" '
            f'font-weight="600">{_svg_text(label)}</text>'
        )
    return "".join(parts)


def area_map_svg(
    points: Iterable[Mapping[str, Any]],
    *,
    width: int = 880,
    height: int = 380,
    caption: str = "Approximate area — exact addresses are shared once a booking is confirmed.",
    aria_label: str = "Map of villa areas in Bali and Yogyakarta",
) -> str:
    """A self-contained SVG map of villa areas.

    Each point is ``{label, latitude, longitude, detail?}`` where the pair has
    already been through :func:`public_point`. The result embeds no remote
    resource, so it works under the marketing site's strict CSP and reveals
    nothing to a third party.
    """
    rows = [dict(point) for point in points]
    if not rows:
        return ""
    pairs = []
    for row in rows:
        lat, lon = validate_coordinate(row["latitude"], row["longitude"])
        row["latitude"], row["longitude"] = lat, lon
        pairs.append((lat, lon))
    bbox = BBox(pairs)
    scale_svg, _label = _scale_bar(bbox, width, height)
    body = (
        _graticule(bbox, width, height)
        + _label_lines(bbox, rows, width, height)
        + _markers(bbox, rows, width, height)
        + scale_svg
    )
    return (
        f'<svg class="area-map" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{_svg_text(aria_label)}" '
        f'xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#eef6f1"/>'
        f'<g color="#123a2d">{body}</g>'
        f"</svg>"
        f'<p class="note map-note">{_svg_text(caption)}</p>'
    )


def locator_map_svg(
    latitude: Any,
    longitude: Any,
    province: str,
    *,
    label: str = "This villa (approx.)",
    width: int = 520,
    height: int = 280,
) -> str:
    """A zoomed map with the villa's public point and the nearest landmarks."""
    lat, lon = public_point(latitude, longitude)
    nearby = nearest_landmarks(lat, lon, province, limit=3)
    points = [{"label": label, "latitude": lat, "longitude": lon, "detail": "approximately"}]
    points += [
        {"label": row["label"], "latitude": row["latitude"], "longitude": row["longitude"],
         "detail": f"{row['km']:g} km away"}
        for row in nearby
    ]
    return area_map_svg(
        points,
        width=width,
        height=height,
        caption=(
            "Approximate location only: the pin marks the area, not the gate. "
            "Distances are straight-line, not driving distance."
        ),
        aria_label=f"Map of the area around {label}",
    )
