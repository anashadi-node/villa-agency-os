"""Tests for ``geo`` — the gate between a villa's coordinate and the public map.

Two properties matter more than the drawing:

1. **Precision is lost, one way.** Anything the marketing site receives has
   already been rounded to :data:`geo.PUBLIC_ROUNDING_DP`, so a template edit
   cannot republish an exact doorstep. The distance lost is bounded with plain
   arithmetic (``111.195 km per degree``), not by trusting the same function
   that did the rounding.
2. **No claim is invented.** ``UNKNOWN``, a half-filled pair, ``NaN`` and an
   out-of-range value all produce no pin at all.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

import geo
from geo import GeoError

#: One degree of latitude on the mean sphere: ``6371.0088 km * pi / 180``.
KM_PER_DEGREE = 111.1950802335329


# --------------------------------------------------------------------------- #
# Ranges and types
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "latitude,longitude",
    [
        (95, 115.13),
        (-95, 115.13),
        (-8.66, 181),
        (-8.66, -181),
        ("south", 115.13),
        (None, 115.13),
        (True, 115.13),
        (float("nan"), 115.13),
        (-8.66, float("inf")),
    ],
)
def test_validate_coordinate_rejects_anything_not_on_earth(latitude, longitude):
    with pytest.raises(GeoError):
        geo.validate_coordinate(latitude, longitude)


def test_validate_coordinate_accepts_numeric_strings_and_casts_to_float():
    lat, lon = geo.validate_coordinate("-8.66", "115.13")
    assert (lat, lon) == (-8.66, 115.13)
    assert isinstance(lat, float) and isinstance(lon, float)


def test_validate_precision_is_a_closed_set():
    for allowed in geo.GEO_PRECISIONS:
        assert geo.validate_precision(allowed) == allowed
    for refused in ("VERIFIED ", "verified", "GUESSED", "", None, 3):
        with pytest.raises(GeoError):
            geo.validate_precision(refused)


def test_geo_precisions_match_the_sql_check_constraint():
    """The tuple and the CHECK constraint must not drift apart."""
    sql = Path(geo.__file__).resolve().parents[1] / "src" / "schema" / "017_property_geo.sql"
    text = sql.read_text(encoding="utf-8")
    for precision in geo.GEO_PRECISIONS:
        assert f"'{precision}'" in text


# --------------------------------------------------------------------------- #
# Distance
# --------------------------------------------------------------------------- #


def test_haversine_matches_degree_arithmetic():
    assert geo.haversine_km(0, 0, 0, 1) == pytest.approx(KM_PER_DEGREE, abs=1e-6)
    assert geo.haversine_km(0, 0, 1, 0) == pytest.approx(KM_PER_DEGREE, abs=1e-6)
    assert geo.haversine_km(0, 0, 0, 0) == 0.0
    # Symmetry: the map draws and labels the same number either way round.
    assert geo.haversine_km(-8.66, 115.13, -8.5069, 115.2627) == pytest.approx(
        geo.haversine_km(-8.5069, 115.2627, -8.66, 115.13), abs=1e-12
    )


def test_haversine_never_exceeds_half_the_circumference():
    """A floating-point clamp inside the formula, not just a nicety: without it
    antipodal points push ``sqrt(h)`` past 1 and ``asin`` raises."""
    half = math.pi * geo._EARTH_RADIUS_KM
    assert geo.haversine_km(0, 0, 0, 180) == pytest.approx(half, rel=1e-12)


# --------------------------------------------------------------------------- #
# What the public is allowed to see
# --------------------------------------------------------------------------- #


def test_public_point_rounds_and_the_loss_is_bounded():
    exact_lat, exact_lon = -8.661234, 115.136789
    lat, lon = geo.public_point(exact_lat, exact_lon)
    assert (lat, lon) == (-8.66, 115.14)

    # Bound the damage with independent arithmetic: half a hundredth of a
    # degree, north/south and east/west, and nothing more.
    half_step = 0.005
    assert abs(lat - exact_lat) <= half_step
    assert abs(lon - exact_lon) <= half_step
    north_south_km = abs(lat - exact_lat) * KM_PER_DEGREE
    east_west_km = abs(lon - exact_lon) * KM_PER_DEGREE * math.cos(math.radians(lat))
    assert north_south_km < 0.6
    assert east_west_km < 0.6
    assert math.hypot(north_south_km, east_west_km) < 0.85  # > 1 km away in total


def test_public_point_refuses_negative_rounding():
    with pytest.raises(GeoError):
        geo.public_point(-8.66, 115.13, dp=-1)


@pytest.mark.parametrize(
    "latitude,longitude,precision",
    [
        (None, None, "UNKNOWN"),
        (-8.66, 115.13, "UNKNOWN"),          # coordinates but no claim
        (-8.66, None, "VERIFIED"),           # half a pair
        (None, 115.13, "AREA_APPROX"),
    ],
)
def test_resolve_point_makes_no_claim_when_data_is_missing(latitude, longitude, precision):
    assert geo.resolve_point(latitude, longitude, precision) is None


def test_resolve_point_returns_the_rounded_pair_for_a_real_claim():
    resolved = geo.resolve_point(-8.661234, 115.136789, "VERIFIED")
    assert resolved is not None
    lat, lon, precision = resolved
    assert (lat, lon, precision) == (-8.66, 115.14, "VERIFIED")


def test_nearest_landmarks_are_sorted_and_capped():
    rows = geo.nearest_landmarks(-8.66, 115.1307, "Bali", limit=3)
    assert len(rows) == 3
    assert [row["km"] for row in rows] == sorted(row["km"] for row in rows)
    assert rows[0]["key"] == "canggu_beach"
    assert rows[0]["km"] == 0.0


def test_nearest_landmarks_for_an_unknown_province_is_empty_not_a_guess():
    assert geo.nearest_landmarks(-8.66, 115.13, "Atlantis") == []


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #


def test_area_map_is_empty_for_no_points():
    assert geo.area_map_svg([]) == ""


def test_area_map_escapes_labels_and_titles():
    svg = geo.area_map_svg(
        [{"label": "<script>alert(1)</script>", "latitude": -8.66, "longitude": 115.13,
          "detail": "\"&'"}],
        caption="a < b & c > d",
    )
    assert "<script" not in svg
    assert "&lt;script&gt;" in svg
    assert "a &lt; b &amp; c &gt; d" in svg


def test_area_map_embeds_nothing_remote():
    """The site's CSP allows ``default-src 'self'``; a remote asset would be
    blocked in production and would leak the guest's interest to a third party."""
    svg = geo.area_map_svg(
        [{"label": "A", "latitude": -8.66, "longitude": 115.13},
         {"label": "B", "latitude": -8.5069, "longitude": 115.2627}]
    )
    assert svg.count("http") == 1  # only the SVG namespace declaration
    assert "http://www.w3.org/2000/svg" in svg
    assert "<script" not in svg
    assert "class=\"area-map\"" in svg
    assert 'role="img"' in svg


def test_area_map_keeps_a_single_point_inside_the_frame():
    svg = geo.area_map_svg([{"label": "Only", "latitude": -8.66, "longitude": 115.13}], width=400, height=200)
    box = geo.BBox([(-8.66, 115.13)])
    x, y = box.project(-8.66, 115.13, 400, 200)
    assert 0 <= x <= 400 and 0 <= y <= 200
    assert f'cx="{x:.1f}"' in svg


def test_locator_map_never_prints_the_exact_coordinate():
    svg = geo.locator_map_svg(-8.661234, 115.136789, "Bali", label="Villa A")
    assert "-8.661234" not in svg
    assert "115.136789" not in svg
    assert 'aria-label="Map of the area around Villa A"' in svg
    assert "Approximate location only" in svg


def test_two_exact_points_in_one_rounding_cell_draw_identically():
    """The strongest form of the privacy gate: 20 m apart, same pixels. If a
    pin ever encodes more precision than the rounding cell, this diverges."""
    first = geo.locator_map_svg(-8.6601, 115.1361, "Bali", label="Villa A")
    second = geo.locator_map_svg(-8.6599, 115.1449, "Bali", label="Villa A")
    assert first == second


def test_locator_map_label_is_escaped_in_the_aria_attribute():
    svg = geo.locator_map_svg(-8.66, 115.13, "Bali", label='Villa "A" <b>')
    assert 'aria-label="Map of the area around Villa &quot;A&quot; &lt;b&gt;"' in svg


# --------------------------------------------------------------------------- #
# The one rule both writers call
#
# SQLite cannot add a CHECK constraint to an existing table, so migration 017
# could not express "VERIFIED implies coordinates" in the schema. It lives here
# instead, and every writer calls it: the bulk importer and the kernel. A test
# that only checked one of them would let the other drift.
# --------------------------------------------------------------------------- #
def test_a_blank_precision_means_unknown_and_carries_nothing():
    assert geo.validate_geo_fields() == ("UNKNOWN", None, None, None)
    assert geo.validate_geo_fields("", "  ", None, "none") == ("UNKNOWN", None, None, None)


def test_a_named_precision_keeps_the_coordinate_it_was_given():
    chosen, latitude, longitude, source = geo.validate_geo_fields(
        "VERIFIED", "-8.5069", "115.2627", "survei 2026-11-12"
    )
    assert chosen == "VERIFIED"
    assert latitude == pytest.approx(-8.5069)
    assert longitude == pytest.approx(115.2627)
    assert source == "survei 2026-11-12"
    # Exact in the column, rounded on the way to the map: the publication rule is unchanged.
    published = geo.public_point(latitude, longitude)
    assert published != (latitude, longitude)
    assert abs(published[0] - latitude) <= 0.0051
    assert abs(published[1] - longitude) <= 0.0051


def test_a_lowercase_precision_is_normalised_not_stored_as_typed():
    assert geo.validate_geo_fields(" verified ", "-8.5", "115.2", "survei")[0] == "VERIFIED"


@pytest.mark.parametrize("precision", ["VERIFIED", "AREA_APPROX"])
def test_a_named_precision_without_both_coordinates_is_refused(precision):
    with pytest.raises(GeoError) as caught:
        geo.validate_geo_fields(precision, "-8.5", None, "survei")
    assert caught.value.details["field"] == "longitude"

    with pytest.raises(GeoError) as caught:
        geo.validate_geo_fields(precision, None, "115.2", "survei")
    assert caught.value.details["field"] == "latitude"


@pytest.mark.parametrize("precision", ["VERIFIED", "AREA_APPROX"])
def test_a_named_precision_needs_the_sentence_that_says_who_confirmed_it(precision):
    with pytest.raises(GeoError) as caught:
        geo.validate_geo_fields(precision, "-8.5", "115.2", "   ")
    assert caught.value.details["field"] == "geo_source"


def test_a_source_too_short_to_identify_anybody_is_refused():
    with pytest.raises(GeoError) as caught:
        geo.validate_geo_fields("VERIFIED", "-8.5", "115.2", "x")
    assert caught.value.details["field"] == "geo_source"


def test_unknown_refuses_a_coordinate_rather_than_dropping_it():
    """Silently discarding it would tell the operator the coordinate landed."""
    with pytest.raises(GeoError) as caught:
        geo.validate_geo_fields("UNKNOWN", "-8.5", "115.2", None)
    assert caught.value.details["field"] == "geo_precision"


def test_unknown_refuses_a_source_it_could_not_have_used():
    with pytest.raises(GeoError) as caught:
        geo.validate_geo_fields("UNKNOWN", None, None, "survei 2026-11-12")
    assert caught.value.details["field"] == "geo_precision"


def test_a_precision_nobody_defined_is_refused_by_name():
    with pytest.raises(GeoError) as caught:
        geo.validate_geo_fields("APPROX", "-8.5", "115.2", "survei")
    assert caught.value.details["field"] == "geo_precision"
    assert "VERIFIED" in caught.value.details["value"] or "APPROX" in caught.value.details["value"]


def test_a_coordinate_that_is_not_a_number_is_refused():
    with pytest.raises(GeoError) as caught:
        geo.validate_geo_fields("VERIFIED", "south", "115.2", "survei")
    assert caught.value.details["field"] == "latitude"


def test_the_kernel_and_the_importer_share_one_rule():
    """Two writers, one function: importing a refused coordinate must fail the same way."""
    import inspect

    import reservation_kernel
    import villa_import

    for module in (reservation_kernel, villa_import):
        assert "geo.validate_geo_fields(" in inspect.getsource(module), module.__name__
