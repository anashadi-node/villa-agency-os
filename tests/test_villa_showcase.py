"""Tests for the per-villa showcase content (photo binding, area note, highlights).

The module is the honesty boundary of the public villa page, so the tests are written
against what it must REFUSE as much as what it must produce: no invented property
feature, no dangling photo binding, no copy that is not U.S. English.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import villa_showcase as S

REPO_ROOT = Path(__file__).resolve().parents[1]
PHOTOS = REPO_ROOT / "assets" / "photos"


def _villa(**overrides):
    villa = {
        "property_id": "prop-x",
        "name": "Some Villa",
        "region": "Ubud",
        "province": "Bali",
        "property_type": "villa",
    }
    villa.update(overrides)
    return villa


def _unit(bedrooms=2, max_guests=4):
    return {
        "name": "Unit",
        "unit_type": "entire_villa",
        "bedrooms": bedrooms,
        "bathrooms": 1,
        "base_capacity": 2,
        "max_guests": max_guests,
    }


class TestPhotoBinding:
    def test_keyword_match_is_case_insensitive(self):
        assert S.photo_stem_for("Villa ULUWATU Cliffside") == "uluwatu"
        assert S.photo_stem_for("canggu garden retreat") == "canggu"

    def test_merapi_binds_to_the_joglo_stem(self):
        assert S.photo_stem_for("Joglo Volcano Merapi Residence") == "joglo"

    def test_unmatched_villa_has_no_stem(self):
        assert S.photo_stem_for("Some Unlisted Homestay") is None

    def test_every_stem_has_an_area_note(self):
        """A stem without a note would render a villa page with a photo and no words."""
        stems = {stem for _keyword, stem in S.PHOTO_KEYWORDS}
        assert stems, "the keyword table moved or emptied"
        assert stems <= set(S.AREA_NOTES), sorted(stems - set(S.AREA_NOTES))

    def test_every_stem_has_both_card_and_detail_files(self):
        """A dangling binding degrades to a placeholder silently — catch it here."""
        missing = []
        for _keyword, stem in S.PHOTO_KEYWORDS:
            for size in ("card", "detail"):
                if not (PHOTOS / size / f"{stem}.jpg").is_file():
                    missing.append(f"{size}/{stem}.jpg")
        assert missing == [], f"photo files missing for bound stems: {missing}"


class TestAreaNote:
    def test_matched_villa_uses_the_curated_area_note(self):
        showcase = S.showcase_for(_villa(name="Villa Canggu Garden Retreat", region="Canggu"))
        assert showcase.area_note == S.AREA_NOTES["canggu"]
        assert showcase.photo_stem == "canggu"

    def test_unmatched_villa_falls_back_to_region_and_province(self):
        showcase = S.showcase_for(_villa(name="Some Unlisted Homestay"))
        assert showcase.area_note == "This villa is in Ubud, Bali."
        assert showcase.photo_stem is None

    def test_fallback_makes_no_claim_about_the_property(self):
        """The fallback may place the villa; it may not describe what is inside it."""
        note = S.showcase_for(_villa(name="Some Unlisted Homestay")).area_note.lower()
        for claim in ("pool", "wifi", "staff", "kitchen", "view"):
            assert claim not in note, claim

    def test_missing_region_and_province_still_returns_a_sentence(self):
        showcase = S.showcase_for(_villa(name="Bare Villa", region="", province=""))
        assert showcase.area_note == (
            "The area around this villa is described by our team on request."
        )


class TestPlaceDisplay:
    """Region and province are operator-typed free text, so prose must normalise them."""

    def test_lowercase_place_is_title_cased(self):
        assert S.display_place("yogyakarta") == "Yogyakarta"
        assert S.display_place("denpasar barat") == "Denpasar Barat"

    def test_already_capitalised_place_is_passed_through(self):
        assert S.display_place("DI Yogyakarta") == "DI Yogyakarta"
        assert S.display_place("Nusa Penida") == "Nusa Penida"

    def test_place_named_inside_its_sibling_is_dropped(self):
        assert S.display_place("yogyakarta", "DI Yogyakarta") == ""
        assert S.display_place("DI Yogyakarta", "yogyakarta") == "DI Yogyakarta"

    def test_distinct_places_are_both_kept(self):
        assert S.display_place("Ubud", "Bali") == "Ubud"
        assert S.display_place("Kaliurang", "DI Yogyakarta") == "Kaliurang"

    def test_blank_place_is_dropped(self):
        assert S.display_place("   ", "Bali") == ""

    def test_fallback_sentence_never_starts_lowercase_or_stutters(self):
        showcase = S.showcase_for(_villa(name="Unlisted", region="yogyakarta", province="DI Yogyakarta"))
        assert showcase.area_note == "This villa is in DI Yogyakarta."

    def test_fallback_sentence_keeps_distinct_places_both(self):
        showcase = S.showcase_for(_villa(name="Unlisted", region="kaliurang", province="DI Yogyakarta"))
        assert showcase.area_note == "This villa is in Kaliurang, DI Yogyakarta."


class TestStyleNote:
    @pytest.mark.parametrize("property_type", sorted(S.STYLE_NOTES))
    def test_known_type_returns_its_note(self, property_type):
        assert S.style_note(property_type) == S.STYLE_NOTES[property_type]

    def test_unknown_type_is_readable_and_neutral(self):
        assert S.style_note("bungalow") == "A bungalow, with the details confirmed by our team."

    def test_empty_type_does_not_produce_a_broken_sentence(self):
        assert S.style_note("") == "A property, with the details confirmed by our team."

    def test_no_style_note_names_a_facility(self):
        """Style notes describe the letting shape; naming a facility would overclaim."""
        for note in list(S.STYLE_NOTES.values()) + [S.style_note("bungalow")]:
            lowered = note.lower()
            for facility in ("pool", "gym", "spa", "wifi", "ocean view", "beachfront"):
                assert facility not in lowered, (facility, note)


class TestHighlights:
    def test_two_units_report_a_bedroom_span_and_a_capacity(self):
        showcase = S.showcase_for(
            _villa(), [_unit(bedrooms=1, max_guests=2), _unit(bedrooms=3, max_guests=8)]
        )
        assert showcase.highlights == (
            "2 units in this villa",
            "1\u20133 bedrooms per unit",
            "Sleeps up to 8",
        )

    def test_single_unit_is_singular(self):
        showcase = S.showcase_for(_villa(), [_unit(bedrooms=1, max_guests=2)])
        assert showcase.highlights == (
            "1 unit in this villa",
            "1 bedroom per unit",
            "Sleeps up to 2",
        )

    def test_no_units_yields_a_neutral_line_not_an_empty_list(self):
        showcase = S.showcase_for(_villa(), [])
        assert showcase.highlights == ("Unit details are confirmed by our team on request",)

    def test_zero_bedrooms_are_not_reported(self):
        showcase = S.showcase_for(_villa(), [_unit(bedrooms=0, max_guests=2)])
        assert "0 bedrooms per unit" not in showcase.highlights


class TestShowcaseShape:
    def test_every_showcase_carries_the_disclosure(self):
        for name in ("Villa Canggu Garden Retreat", "Some Unlisted Homestay"):
            assert S.showcase_for(_villa(name=name)).disclosure == S.DISCLOSURE

    def test_the_disclosure_states_the_limit_of_the_area_note(self):
        assert "not verified features" in S.DISCLOSURE
        assert "before you book" in S.DISCLOSURE

    def test_composition_is_deterministic(self):
        villa, units = _villa(name="Villa Uluwatu Cliffside"), [_unit()]
        assert S.showcase_for(villa, units) == S.showcase_for(villa, units)

    def test_showcase_is_read_only(self):
        """The value object must be frozen: nothing should be able to mutate it later."""
        showcase = S.showcase_for(_villa())
        with pytest.raises(Exception):
            showcase.area_note = "mutated"  # type: ignore[misc]
