"""
Place-name helpers: emirate names, the area alias table, and the text tweaks that
feed the geocoder (docs/DESIGN.md section 5). Offline and deterministic; every
project and developer name here is SYNTHETIC unless it is a place name.
"""

from __future__ import annotations

import json

import pytest

from src import areas
from src.areas import (
    ALIAS_FILE,
    alias_variants,
    canonical_area,
    developer_brand,
    known_areas,
    normalise_emirate,
    normalise_key,
    query_qualifier,
    strip_developer_suffix,
    strip_phase,
    tidy,
)
from src.config import EMIRATES


@pytest.fixture
def alias_file(tmp_path, monkeypatch):
    """Point the module at a throwaway alias file; the cached table is reset around it."""

    def install(payload) -> None:
        path = tmp_path / "area_aliases.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(areas, "ALIAS_FILE", path)
        areas._table.cache_clear()

    yield install
    monkeypatch.undo()
    areas._table.cache_clear()


# ---------------------------------------------------------------------------
# tidy / normalise_key / emirates
# ---------------------------------------------------------------------------
def test_tidy():
    assert tidy("  Business   Bay\n") == "Business Bay"
    assert tidy(None) == ""
    assert tidy(42) == "42"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Business Bay", "business bay"),
        ("  BUSINESS-BAY!! ", "business bay"),
        ("Ma'arid & Co.", "maarid and co"),
        ("Ma’arid", "maarid"),
        ("Café Del Mar", "cafe del mar"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalise_key(text, expected):
    assert normalise_key(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Dubai", "Dubai"),
        ("dubai", "Dubai"),
        ("  DUBAI EMIRATE ", "Dubai"),
        ("Emirate of Dubai", "Dubai"),
        ("Dubayy", "Dubai"),
        ("Sharjah", "Sharjah"),
        ("Sharjah Emirate", "Sharjah"),
        ("Ash Shariqah", "Sharjah"),
        ("Abu Dhabi", None),
        ("Ajman", None),
        ("UAE", None),
        ("", None),
        (None, None),
    ],
)
def test_normalise_emirate(text, expected):
    assert normalise_emirate(text) == expected


def test_query_qualifier():
    assert query_qualifier("Dubai") == ", Dubai, UAE"
    assert query_qualifier("sharjah emirate") == ", Sharjah, UAE"


@pytest.mark.parametrize("bad", ["Abu Dhabi", "", "UAE"])
def test_query_qualifier_refuses_an_unknown_emirate(bad):
    # An unqualified query is the dangerous kind: fail loudly instead.
    with pytest.raises(ValueError):
        query_qualifier(bad)


# ---------------------------------------------------------------------------
# canonical_area
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name, emirate, expected",
    [
        ("Jumeira Village Circle", "Dubai", "Jumeirah Village Circle"),
        ("jumeirah village circle", "Dubai", "Jumeirah Village Circle"),
        ("JVC", "Dubai", "Jumeirah Village Circle"),
        ("The Palm Jumeirah", "Dubai", "Palm Jumeirah"),
        ("Dubai Hills", "Dubai", "Dubai Hills Estate"),
        ("Dubai World Central", "Dubai", "Dubai South"),
        ("Mirdiff", "Dubai", "Mirdif"),
        ("MUWEILAH", "Sharjah", "Muwaileh"),
        ("Muwailih", "Sharjah", "Muwaileh"),
        ("El Mamsha", "Sharjah", "Al Mamsha"),
        ("Almamsha", "Sharjah", "Al Mamsha"),
        ("Tilal", "Sharjah", "Tilal City"),
        ("Al Jada", "Sharjah", "Aljada"),
        ("Waterfront City", "Sharjah", "Sharjah Waterfront City"),
        ("Business Bay", "Dubai", "Business Bay"),
        ("Business Bay", None, "Business Bay"),
    ],
)
def test_canonical_area(name, emirate, expected):
    assert canonical_area(name, emirate) == expected


def test_an_unknown_place_is_returned_as_given():
    assert canonical_area("  Some   Unlisted Place ", "Dubai") == "Some Unlisted Place"
    assert canonical_area("", "Dubai") == ""


def test_an_unknown_place_still_gets_the_word_level_respelling():
    assert canonical_area("Jumeira Something New", "Dubai") == "Jumeirah Something New"


def test_a_name_is_resolved_inside_the_declared_emirate_only():
    # Tilal City and Aljada exist in Sharjah only; Dubai must not borrow them.
    assert canonical_area("Tilal", "Dubai") == "Tilal"
    assert canonical_area("Al Jada", "Dubai") == "Al Jada"


def test_a_name_that_exists_in_both_emirates_stays_one_spelling():
    assert canonical_area("Al Nahdah", "Dubai") == "Al Nahda"
    assert canonical_area("Al Nahdah", "Sharjah") == "Al Nahda"


def test_capitalisation_style_of_a_swapped_word_follows_the_input():
    assert canonical_area("MUWEILAH", "Sharjah") == "Muwaileh"
    assert canonical_area("Jumeira Gardens West", "Dubai") == "Jumeirah Gardens West"
    assert canonical_area("JUMEIRA GARDENS", "Dubai") == "JUMEIRAH GARDENS"


# ---------------------------------------------------------------------------
# alias_variants / known_areas
# ---------------------------------------------------------------------------
def test_alias_variants_start_with_the_canonical_spelling():
    variants = alias_variants("Tilal", "Sharjah")
    assert variants[0] == "Tilal City"
    assert "Tilal" in variants


def test_alias_variants_include_the_spelling_swaps():
    variants = alias_variants("Muwaileh", "Sharjah")
    assert variants[0] == "Muwaileh"
    assert "Muweilah" in variants


def test_alias_variants_are_unique_ignoring_case_and_respect_the_limit():
    variants = alias_variants("Muwaileh", "Sharjah", limit=6)
    assert len({v.lower() for v in variants}) == len(variants)
    assert len(alias_variants("Muwaileh", "Sharjah", limit=2)) == 2
    assert len(alias_variants("Muwaileh", "Sharjah", limit=0)) == 1  # never empty for a real name


def test_alias_variants_for_an_unknown_place_is_just_the_name():
    assert alias_variants("  Some  Unlisted Place ", "Dubai") == ["Some Unlisted Place"]
    assert alias_variants("", "Dubai") == []


def test_alias_variants_use_the_declared_emirate_only():
    assert "Tilal City" not in alias_variants("Tilal", "Dubai")


def test_alias_variants_include_the_documented_aliases():
    variants = alias_variants("Jumeirah Village Circle", "Dubai")
    assert "JVC" in variants


def test_known_areas():
    dubai, sharjah = known_areas("Dubai"), known_areas("sharjah")
    assert "Business Bay" in dubai and "Business Bay" not in sharjah
    assert "Aljada" in sharjah and "Aljada" not in dubai
    assert dubai == sorted(dubai)
    with pytest.raises(ValueError):
        known_areas("Abu Dhabi")


# ---------------------------------------------------------------------------
# The alias file itself
# ---------------------------------------------------------------------------
def test_the_alias_file_is_valid_and_consistent():
    raw = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
    assert set(raw["areas"]) <= set(EMIRATES)
    for variant, canonical in raw["tokens"].items():
        assert variant == variant.lower() and canonical == canonical.lower()
    for emirate in EMIRATES:
        seen: dict[str, str] = {}
        for canonical, alt_names in raw["areas"][emirate].items():
            for spelling in (canonical, *alt_names):
                key = normalise_key(spelling)
                assert key, f"empty spelling under {canonical}"
                assert seen.setdefault(key, canonical) == canonical, (
                    f"{spelling!r} claimed by {seen[key]!r} and {canonical!r} in {emirate}"
                )
        assert known_areas(emirate), emirate


def test_every_canonical_name_resolves_to_itself():
    for emirate in EMIRATES:
        for name in known_areas(emirate):
            assert canonical_area(name, emirate) == name


def test_every_documented_alias_resolves_to_its_canonical_name():
    raw = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
    for emirate in EMIRATES:
        for canonical, alt_names in raw["areas"][emirate].items():
            for alias in alt_names:
                assert canonical_area(alias, emirate) == canonical, (emirate, alias)


def test_a_contradictory_alias_file_fails_loudly(alias_file):
    alias_file({"tokens": {}, "areas": {"Dubai": {"Place One": ["Shared"], "Place Two": ["shared"]}}})
    with pytest.raises(ValueError):
        canonical_area("Shared", "Dubai")


def test_an_alias_file_naming_another_emirate_fails_loudly(alias_file):
    alias_file({"tokens": {}, "areas": {"Abu Dhabi": {"Somewhere": []}}})
    with pytest.raises(ValueError):
        known_areas("Dubai")


def test_upper_case_tokens_fail_loudly(alias_file):
    alias_file({"tokens": {"Jumeira": "jumeirah"}, "areas": {}})
    with pytest.raises(ValueError):
        canonical_area("Jumeira", "Dubai")


# ---------------------------------------------------------------------------
# strip_phase
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name, expected",
    [
        ("Sample Gardens Phase 2", "Sample Gardens"),
        ("Sample Gardens phase 2", "Sample Gardens"),
        ("Sample Gardens - Phase 2", "Sample Gardens"),
        ("Sample Gardens, Phase 2", "Sample Gardens"),
        ("Sample Gardens (Phase 2)", "Sample Gardens"),
        ("Sample Gardens (Phase 2A)", "Sample Gardens"),
        ("Sample Gardens Phase 2B", "Sample Gardens"),
        ("Sample Gardens Stage III", "Sample Gardens"),
        ("Sample Gardens Phase-2", "Sample Gardens"),
        ("Sample Gardens Phase 1 Phase 2", "Sample Gardens"),
        # Not a trailing phase marker, or not a phase at all: untouched.
        ("Phase 2 Gardens", "Phase 2 Gardens"),
        ("Sample Tower B", "Sample Tower B"),
        ("Sample Block 4", "Sample Block 4"),
        ("Sample Gardens Phased", "Sample Gardens Phased"),
        ("Phase 2", "Phase 2"),  # nothing left would remain: unchanged
        ("", ""),
    ],
)
def test_strip_phase(name, expected):
    assert strip_phase(name) == expected


# ---------------------------------------------------------------------------
# developer_brand / strip_developer_suffix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "developer, expected",
    [
        ("EXAMPLE PROPERTIES DEVELOPMENT L.L.C", "Example"),
        ("EXAMPLE PROPERTIES CO. (L.L.C)", "Example"),
        ("Example Developer LLC", "Example"),
        ("Fictional Builders LLC", "Fictional Builders"),
        ("Example", "Example"),
        # Stripping must never leave a bare place word.
        ("Dubai Holding", "Dubai Holding"),
        ("Dubai Properties", "Dubai Properties"),
        ("Sharjah Real Estate", "Sharjah Real Estate"),
        ("Sharjah Real Estate LLC", "Sharjah Real Estate"),
        ("Dubai LLC", "Dubai LLC"),
        ("", ""),
        (None, ""),
    ],
)
def test_developer_brand(developer, expected):
    assert developer_brand(developer) == expected


@pytest.mark.parametrize(
    "name, developer, expected",
    [
        ("Sample Gardens by Example", "EXAMPLE PROPERTIES DEVELOPMENT L.L.C", "Sample Gardens"),
        ("Sample Gardens by Example Properties", "Example Properties LLC", "Sample Gardens"),
        ("Sample Gardens", "Example", "Sample Gardens"),
        # A "by" that is not the developer is part of the name.
        ("Residences by the Sea", "Example", "Residences by the Sea"),
        ("Sample Gardens by Someone Else", "Example", "Sample Gardens by Someone Else"),
        # With no developer to check against, only a short developer-like tag is removed.
        ("Sample Gardens by Example", "", "Sample Gardens"),
        ("Residences by the Sea", "", "Residences by the Sea"),
        ("Sample Gardens by a Very Long Description Of Something", "", "Sample Gardens by a Very Long Description Of Something"),
        ("  Sample   Gardens  ", "", "Sample Gardens"),
    ],
)
def test_strip_developer_suffix(name, developer, expected):
    assert strip_developer_suffix(name, developer) == expected
