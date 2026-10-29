"""
Identity, merging, clustering, sheet round trips, the geocode query tiers and the
configuration loader (docs/DESIGN.md sections 1, 2, 3, 5, 9).

Every project, developer and id below is SYNTHETIC. Nothing here touches the
network, a credential, or the real environment.
"""

from __future__ import annotations

import dataclasses
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from src import config as cfgmod
from src.config import (
    DEFAULT_GEOCODE_USER_AGENT,
    DEFAULT_SHEET_NAME,
    Config,
    load_config,
)
from src.models import (
    CANDIDATE_HEADERS,
    OVERRIDE_HEADERS,
    REGISTRY_HEADERS,
    DLD_ID_RE,
    GeocodeOverride,
    LaunchCandidate,
    RegistryRow,
    clean_registry_id,
    cluster_rows,
    dedupe_rows,
    format_dubai_time,
    is_http_url,
    is_placeholder,
    is_valid_project_key,
    key_authority,
    make_name_key,
    merge_rows,
    parse_source_date,
    same_project,
    identity_slug,
    slugify,
    utc_now_iso,
)


# ---------------------------------------------------------------------------
# Factories (SYNTHETIC)
# ---------------------------------------------------------------------------
def dld_row(**overrides) -> RegistryRow:
    # SYNTHETIC
    data = dict(
        emirate="Dubai",
        registry="DLD",
        registry_project_no="1001",
        project_name_en="Example Heights",
        developer="Fictional Builders LLC",
        area_en="Business Bay",
        status="Active",
    )
    data.update(overrides)
    return RegistryRow(**data)


def seed_row(**overrides) -> RegistryRow:
    # SYNTHETIC
    data = dict(
        emirate="Dubai",
        registry="SEED",
        project_name_en="Example Heights",
        developer="Fictional Builders LLC",
    )
    data.update(overrides)
    return RegistryRow(**data)


def srerd_row(**overrides) -> RegistryRow:
    # SYNTHETIC
    data = dict(
        emirate="Sharjah",
        registry="SRERD",
        registry_project_no="SH-2001",
        project_name_en="Sample Gardens",
        developer="Imaginary Estates",
    )
    data.update(overrides)
    return RegistryRow(**data)


# ===========================================================================
# Placeholders, ids, slugs, keys
# ===========================================================================
@pytest.mark.parametrize(
    "value",
    [None, "", "  ", "-", "--", "?", "0", "n/a", "N/A", "NA", "nil", "None", "null", "NaN",
     "unknown", "TBC", "tbd", "Not Specified", "...", float("nan"), "  n/a  "],
)
def test_placeholders_are_recognised(value):
    assert is_placeholder(value)


@pytest.mark.parametrize("value", ["Pending", "Active", "Masaar", "1234", "A", "Phase 1"])
def test_real_values_are_not_placeholders(value):
    # "Pending" is a genuine DLD project status and must survive.
    assert not is_placeholder(value)


@pytest.mark.parametrize(
    "value, expected",
    [
        (1234, "1234"),
        (1234.0, "1234"),
        ("1234.0", "1234"),
        ("1234.00", "1234"),
        (" 55 ", "55"),
        ("AB-12", "AB-12"),
        (12.5, None),
        (True, None),
        (None, None),
        ("0", None),
        ("000", None),
        ("0.0", None),
        ("n/a", None),
        ("TBC", None),
        ("-", None),
        ("", None),
        (float("nan"), None),
    ],
)
def test_clean_registry_id(value, expected):
    assert clean_registry_id(value) == expected


@pytest.mark.parametrize(
    "number",
    ["١٢٣", "۱۲۳", "１２３", "1234567890", "12 3"],
)
def test_dld_id_pattern_is_ascii_digits_only(number):
    # Python's \d matches Arabic-Indic and full-width digits; a DLD id never contains them.
    assert not DLD_ID_RE.match(number)
    assert not is_valid_project_key(f"dld:dubai:{number}")
    row = dld_row(registry_project_no=number)
    assert row.registry_key() is None


def test_dld_id_pattern_does_not_accept_a_trailing_newline():
    # "$" would match before a trailing newline; the pattern must not.
    assert not DLD_ID_RE.match("123\n")
    assert not is_valid_project_key("dld:dubai:123\n")


def test_dld_id_pattern_accepts_plain_ascii_ids():
    assert DLD_ID_RE.match("1")
    assert DLD_ID_RE.match("123456789")
    assert dld_row(registry_project_no="1001").registry_key() == "dld:dubai:1001"


def test_slugify():
    assert slugify("Example Heights") == "example-heights"
    assert slugify("  Tower  B ") == "tower-b"
    assert slugify("Cafe & Residences") == "cafe-and-residences"
    assert slugify("Élan Résidence") == "elan-residence"
    assert slugify(None) == ""
    assert slugify("مشروع") == ""  # no Latin letter or digit: no slug


def test_slugify_folds_other_scripts_digits_so_numbered_towers_stay_apart():
    arabic_indic_one, arabic_indic_two = "١", "٢"
    assert slugify(f"Example Tower {arabic_indic_one}") == "example-tower-1"
    assert slugify(f"Example Tower {arabic_indic_two}") == "example-tower-2"
    assert slugify("Example Tower १२") == "example-tower-12"  # Devanagari
    assert slugify("Example Tower ۱۲") == "example-tower-12"  # Extended Arabic-Indic
    assert slugify("Example Tower １２") == "example-tower-12"  # full-width
    assert slugify("Example Tower 12") == slugify("Example Tower ١٢")
    one = make_name_key("Dubai", "Dev", f"Example Tower {arabic_indic_one}")
    two = make_name_key("Dubai", "Dev", f"Example Tower {arabic_indic_two}")
    assert one and two and one != two
    # Digits that are not decimal digits are left to the usual rule (no fraction or superscript folding surprises).
    assert slugify("Example ½ Tower") == "example-1-2-tower"


@pytest.mark.parametrize("other_script_name", ["برج ١", "مشروع ١", "برج ٢٠٢٥", "१२", "１２"])
def test_a_name_in_another_script_that_holds_digits_has_no_identity_slug_so_projects_cannot_merge(other_script_name):
    # folding the digits keeps "Tower ١" and "Tower ٢" apart, but an Arabic-only name must not
    # fall back to its digits: "برج ١" and "مشروع ١" would both become key part "1" and merge.
    assert slugify(other_script_name).isdigit()  # what the fold produces ...
    assert identity_slug(other_script_name) == ""  # ... is not allowed to be a key part
    assert make_name_key("Dubai", "Dev", other_script_name) is None
    assert make_name_key("Dubai", other_script_name, "Example Tower") is None
    with pytest.raises(ValidationError):
        candidate(project_name=other_script_name)
    assert candidate(developer=other_script_name).candidate_key.split(":")[2] == "unknown"


def test_identity_slug_keeps_a_slug_that_has_a_latin_letter():
    assert identity_slug("Example Tower ١") == "example-tower-1"
    assert identity_slug(None) == "" and identity_slug("   ") == ""


# slugify throws away letters of other scripts and symbols such as "+". A name that lost one
# of them must not become a key part, or two projects that differ only by it would share a key
# and dedupe_rows would merge them without a trace.
_NAMES_THAT_LOSE_A_LETTER_OR_SYMBOL = [
    "Tower أ",  # Arabic letter
    "Tower 7 مشروع",  # a Latin name with an Arabic word: used to slug to "tower-7"
    "Tower क",  # Devanagari
    "Tower 五",  # CJK
    "Tower α",  # Greek
    "Tower ł",  # Latin letter with no decomposition (l with stroke)
    "Søren Tower",  # o with stroke
    "Straße Tower",  # sharp s
    "Tower A+",
    "C#",
    "Tower @ A",
    "A+ Builders",
    "Tower A＋",  # full-width plus: NFKD turns it into "+"
    "Tower A⁺",  # superscript plus
    "C＃",  # full-width number sign
]


@pytest.mark.parametrize("name", _NAMES_THAT_LOSE_A_LETTER_OR_SYMBOL)
def test_a_name_that_loses_a_letter_or_symbol_has_no_identity_slug_and_makes_no_name_key(name):
    assert slugify(name)  # the lossy slug exists ...
    assert identity_slug(name) == ""  # ... but is not allowed to be a key part
    assert make_name_key("Dubai", "Dev", name) is None
    assert make_name_key("Dubai", name, "Example Tower") is None  # the developer side too
    row = seed_row(project_name_en=name)
    assert row.name_key() is None and row.identity_keys() == [] and row.project_key == "" and not row.has_identity


@pytest.mark.parametrize(
    "name, slug",
    [
        ("Dubai Hills", "dubai-hills"),
        ("Palm Jumeirah - Phase 2", "palm-jumeirah-phase-2"),
        ("O'Brien & Sons, (Phase 1) Ltd.", "o-brien-and-sons-phase-1-ltd"),
        ("O’Brien Tower", "o-brien-tower"),  # typographic apostrophe
        ("Café Résidence", "cafe-residence"),  # diacritics fold
        ("Block 1/2", "block-1-2"),
        ("Al ʻAin Tower", "al-ain-tower"),  # a modifier letter used as an apostrophe is punctuation
        ("Tower ١", "tower-1"),  # a folded digit is not a lost letter
        ("Tower Ⅳ", "tower-iv"),  # Roman numeral four decomposes to Latin letters
        ("Example ½ Tower", "example-1-2-tower"),
    ],
)
def test_ordinary_punctuation_and_folding_diacritics_still_make_a_slug(name, slug):
    assert identity_slug(name) == slug
    assert make_name_key("Dubai", "Dev", name) == f"name:dubai:dev:{slug}"


def test_name_key_is_namespaced_and_needs_both_halves():
    assert make_name_key("Dubai", "Fictional Builders LLC", "Example Heights") == (
        "name:dubai:fictional-builders-llc:example-heights"
    )
    assert make_name_key("sharjah emirate", "Imaginary Estates", "Sample Gardens") == (
        "name:sharjah:imaginary-estates:sample-gardens"
    )
    assert make_name_key("Abu Dhabi", "Dev", "Proj") is None
    assert make_name_key("", "Dev", "Proj") is None
    assert make_name_key("Dubai", "", "Proj") is None
    assert make_name_key("Dubai", "n/a", "Proj") is None
    assert make_name_key("Dubai", "Dev", "TBC") is None
    assert make_name_key("Dubai", "Dev", "مشروع") is None


def test_tower_letters_are_part_of_identity():
    a = make_name_key("Dubai", "Dev", "Example Tower A")
    b = make_name_key("Dubai", "Dev", "Example Tower B")
    assert a != b


def test_same_name_in_two_emirates_never_shares_a_key():
    assert make_name_key("Dubai", "Dev", "Proj") != make_name_key("Sharjah", "Dev", "Proj")


@pytest.mark.parametrize(
    "key",
    [
        "dld:dubai:1",
        "dld:dubai:123456789",
        "srerd:sharjah:SH-001",
        "srerd:sharjah:a.b_c-1",
        "name:dubai:fictional-builders:example-heights",
        "name:sharjah:dev:proj-2",
    ],
)
def test_valid_project_keys(key):
    assert is_valid_project_key(key)


@pytest.mark.parametrize(
    "key",
    [
        None,
        "",
        "12345",  # a bare id has no namespace
        "dld:dubai:0",
        "dld:dubai:000",
        "dld:dubai:1234567890",  # ten digits
        "dld:dubai:12a",
        "dld:sharjah:1",  # wrong emirate for the authority
        "srerd:dubai:1",
        "srerd:sharjah:tbc",  # a placeholder id
        "srerd:sharjah:n/a",
        "srerd:sharjah:0",
        "name:abu-dhabi:dev:proj",
        "name:dubai::proj",
        "name:dubai:dev:",
        "NAME:dubai:dev:proj",
        "DLD:dubai:1",
        " dld:dubai:1",
        "dld:dubai:1 ",
        "other:dubai:1",
        42,
    ],
)
def test_invalid_project_keys(key):
    assert not is_valid_project_key(key)


def test_key_authority():
    assert key_authority("dld:dubai:1") == "dld"
    assert key_authority("srerd:sharjah:X1") == "srerd"
    assert key_authority("name:dubai:a:b") == "name"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://example.com/page", True),
        ("http://example.com", True),
        ("ftp://example.com", False),
        ("javascript:alert(1)", False),
        ("//example.com", False),
        ("https://", False),
        ("example.com", False),
        ("https://example.com/a b", False),
        (" https://example.com", False),
        ("", False),
        (None, False),
    ],
)
def test_is_http_url(value, expected):
    assert is_http_url(value) is expected


@pytest.mark.parametrize(
    "value",
    [
        "http://[::1",  # urlparse raises ValueError: must be refused, not crash the caller
        "http://[bad]/x",
        "http://host:abc/",  # the port is parsed lazily and raises ValueError
        "http://host:99999/",  # out of range
        "http://:80/",  # a port is not a host
        "http://user@/x",  # credentials are not a host
        "https:///path-only",
    ],
)
def test_is_http_url_refuses_malformed_hosts_without_raising(value):
    assert is_http_url(value) is False


@pytest.mark.parametrize("value", ["http://example.com:8080/x", "https://[::1]:8443/x", "http://user@example.com/x"])
def test_is_http_url_still_accepts_real_hosts(value):
    assert is_http_url(value) is True


def test_a_malformed_candidate_url_is_a_validation_error_not_a_crash():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LaunchCandidate(emirate="Sharjah", project_name="Sample Gardens", source_url="http://host:abc/")


# ===========================================================================
# Dates and time
# ===========================================================================
@pytest.mark.parametrize(
    "value, expected",
    [
        ("25-10-2026", "2026-10-25"),
        ("25/10/2026", "2026-10-25"),
        ("25.10.2026", "2026-10-25"),
        ("01-02-2026", "2026-02-01"),  # day first, always
        ("2026-10-25", "2026-10-25"),
        ("2026/10/25", "2026-10-25"),
        ("2026-10-25T03:00:00Z", "2026-10-25"),
        ("2026-10-25 03:00:00", "2026-10-25"),
        ("25-10-2026 07:30", "2026-10-25"),
        (date(2026, 10, 25), "2026-10-25"),
        (datetime(2026, 10, 25, 23, 59), "2026-10-25"),
        ("0001-01-01", None),  # the "no date" sentinel
        ("01-01-0001", None),
        ("1899-12-31", None),
        ("2101-01-01", None),
        ("31-02-2026", None),
        ("2026-13-01", None),
        ("", None),
        (None, None),
        ("n/a", None),
        ("soon", None),
        ("Q4 2026", None),
    ],
)
def test_parse_source_date(value, expected):
    assert parse_source_date(value) == expected


def test_utc_now_iso():
    moment = datetime(2026, 10, 25, 7, 0, 0, tzinfo=timezone(timedelta(hours=4)))
    assert utc_now_iso(moment) == "2026-10-25T03:00:00Z"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", utc_now_iso())
    with pytest.raises(ValueError):
        utc_now_iso(datetime(2026, 10, 25, 7, 0, 0))  # naive: refuse to guess a zone


def test_format_dubai_time():
    assert format_dubai_time("2026-10-25T03:00:00Z") == "2026-10-25 07:00 GST"
    assert format_dubai_time("2026-10-25T23:30:00+00:00") == "2026-10-26 03:30 GST"
    assert format_dubai_time("2026-10-25T07:00:00+04:00") == "2026-10-25 07:00 GST"
    assert format_dubai_time("") == ""
    assert format_dubai_time(None) == ""
    # Not a timezone-aware timestamp: shown as written, never given an invented zone.
    assert format_dubai_time("2026-10-25T03:00:00") == "2026-10-25T03:00:00"
    assert format_dubai_time("last Tuesday") == "last Tuesday"


# ===========================================================================
# RegistryRow: construction and identity
# ===========================================================================
def test_registry_row_requires_a_supported_emirate_and_registry():
    with pytest.raises(ValidationError):
        dld_row(emirate="Abu Dhabi")
    with pytest.raises(ValidationError):
        dld_row(registry="OTHER")
    with pytest.raises(ValidationError):
        RegistryRow(registry="DLD")  # emirate missing
    with pytest.raises(ValidationError):
        dld_row(emirate="Sharjah")  # a DLD row is Dubai, not whatever the caller says
    with pytest.raises(ValidationError):
        srerd_row(emirate="Dubai")


def test_registry_row_rejects_unknown_columns():
    with pytest.raises(ValidationError):
        dld_row(project_nmae_en="typo")


def test_emirate_and_registry_are_normalised():
    row = RegistryRow(emirate="dubai emirate", registry="dld", registry_project_no="7")
    assert (row.emirate, row.registry) == ("Dubai", "DLD")


def test_dld_registry_key():
    row = dld_row(registry_project_no="1001")
    assert row.registry_key() == "dld:dubai:1001"
    assert row.project_key == "dld:dubai:1001"
    assert row.identity_keys() == [
        "dld:dubai:1001",
        "name:dubai:fictional-builders-llc:example-heights",
    ]


def test_float_ids_from_sheets_are_cleaned():
    assert dld_row(registry_project_no=1001.0).registry_project_no == "1001"
    assert dld_row(registry_project_no="1001.0").registry_key() == "dld:dubai:1001"


def test_srerd_registry_key():
    row = srerd_row()
    assert row.registry_key() == "srerd:sharjah:SH-2001"
    assert row.project_key == "srerd:sharjah:SH-2001"


def test_placeholder_id_falls_back_to_the_name_key():
    row = dld_row(registry_project_no="n/a")
    assert row.registry_project_no is None
    assert row.registry_key() is None
    assert row.project_key == "name:dubai:fictional-builders-llc:example-heights"


def test_malformed_dld_id_is_not_a_key():
    row = dld_row(registry_project_no="AB-12")
    assert row.registry_key() is None  # DLD ids are digits
    assert row.project_key.startswith("name:dubai:")


def test_seed_rows_never_claim_a_registry_key():
    row = seed_row(registry_project_no="1001")
    assert row.registry_key() is None
    assert row.project_key == "name:dubai:fictional-builders-llc:example-heights"


def test_permit_number_is_never_a_key():
    row = RegistryRow(emirate="Dubai", registry="DLD", permit_no="PERMIT-998877")
    assert row.identity_keys() == []
    assert row.project_key == ""
    assert not row.has_identity


def test_arabic_only_name_has_no_name_key():
    row = seed_row(project_name_en="", project_name_ar="مشروع تجريبي")
    assert row.name_key() is None
    assert not row.has_identity


def test_placeholders_in_descriptive_columns_become_blank():
    row = dld_row(developer="N/A", master_project="-", zone="TBC", escrow_agent="null", status="Pending")
    assert row.developer == ""
    assert row.master_project == ""
    assert row.zone == ""
    assert row.escrow_agent == ""
    assert row.status == "Pending"


def test_whitespace_is_collapsed():
    assert dld_row(project_name_en="  Example   Heights ").project_name_en == "Example Heights"


@pytest.mark.parametrize(
    "value, expected",
    [("45.5", 45.5), ("67.5%", 67.5), (100, 100.0), (0, 0.0), ("0", 0.0), ("0%", 0.0), ("150", None), ("-1", None), ("n/a", None), ("", None)],
)
def test_percent_completed(value, expected):
    assert dld_row(percent_completed=value).percent_completed == expected


@pytest.mark.parametrize(
    "value, expected",
    [("320", 320), ("1,234", 1234), (12.0, 12), ("0", 0), (0, 0), ("12.5", None), ("-3", None), ("n/a", None), ("", None), (None, None)],
)
def test_counts(value, expected):
    assert dld_row(units=value).units == expected


def test_a_number_too_large_for_a_float_is_blank_not_an_exception():
    # int -> float raises OverflowError, which pydantic would not wrap as a ValidationError.
    assert dld_row(units=10**400).units is None
    assert dld_row(percent_completed=10**400).percent_completed is None
    assert dld_row(latitude=10**400, longitude=10**400).latitude is None


@pytest.mark.parametrize("value", ["1_000", "3_2", "1_0.5"])
def test_python_digit_separators_are_not_numbers(value):
    # float("1_000") is 1000.0 in Python; a sheet cell like that is not a number.
    assert dld_row(units=value).units is None
    assert dld_row(percent_completed=value).percent_completed is None


def test_dates_are_normalised_and_sentinels_blanked():
    row = dld_row(start_date="01-02-2024", completion_date="0001-01-01", cancellation_date="2026-10-25T03:00:00Z")
    assert row.start_date == "2024-02-01"
    assert row.completion_date == ""
    assert row.cancellation_date == "2026-10-25"


def test_coordinates_are_validated():
    assert dld_row(latitude="25.2048", longitude="55.2708").has_coordinates()
    assert dld_row(latitude=95, longitude=55.2).latitude is None
    assert dld_row(latitude=25.2, longitude=181).longitude is None
    # A decimal comma is not guessed at: '25,2048' is rejected, not read as 252048.
    assert dld_row(latitude="25,2048", longitude="55,2708").latitude is None
    assert dld_row(latitude="n/a", longitude="n/a").latitude is None
    assert not dld_row(latitude=25.2).has_coordinates()


def test_invalid_precision_becomes_blank():
    assert dld_row(location_precision="Exact").location_precision == "exact"
    assert dld_row(location_precision="centroid").location_precision == ""


# ===========================================================================
# RegistryRow: sheet round trip
# ===========================================================================
def test_registry_headers_are_unique_and_complete():
    assert len(REGISTRY_HEADERS) == len(set(REGISTRY_HEADERS)) == 32
    assert REGISTRY_HEADERS[0] == "Project Key"
    assert len(CANDIDATE_HEADERS) == len(set(CANDIDATE_HEADERS))
    assert len(OVERRIDE_HEADERS) == len(set(OVERRIDE_HEADERS))
    row = dld_row()
    assert list(row.to_sheet_dict()) == list(REGISTRY_HEADERS)
    assert len(row.to_sheet_row()) == len(REGISTRY_HEADERS)


def test_sheet_values_are_all_strings():
    row = dld_row(units=320, percent_completed=45.5, latitude=25.2048, longitude=55.2708)
    assert all(isinstance(v, str) for v in row.to_sheet_dict().values())
    assert all(isinstance(v, str) for v in row.to_sheet_row())


def test_sheet_round_trip_is_lossless():
    row = dld_row(
        project_name_ar="مثال",
        master_developer="Fictional Master Developer",
        master_project="Fictional Lagoons",
        area_ar="الخليج التجاري",
        zone="Zone 3",
        percent_completed=45.5,
        start_date="01-02-2024",
        completion_date="2027-06-30",
        escrow_agent="Synthetic Bank",
        units=320,
        buildings=2,
        villas=0,
        permit_no="PERMIT-1",
        source_dataset="synthetic-fixture",
        source_refreshed="2026-10-25",
        first_seen="2026-10-01T03:00:00Z",
        last_seen="2026-10-25T03:00:00Z",
        latitude=25.2048,
        longitude=55.2708,
        location_precision="exact",
        geocode_method="nominatim",
        geocode_status="resolved via nominatim exact 2026-10-25",
        map_pin_link="https://www.google.com/maps/search/?api=1&query=25.2048,55.2708",
    )
    again = RegistryRow.from_sheet_dict(row.to_sheet_dict())
    assert again == row
    assert again.to_sheet_dict() == row.to_sheet_dict()


def test_from_sheet_dict_recomputes_the_key_and_ignores_extra_columns():
    data = dld_row().to_sheet_dict()
    data["Project Key"] = "dld:dubai:999"  # stale or hand-edited: ignored
    data["Some Analyst Note"] = "keep out"
    row = RegistryRow.from_sheet_dict(data)
    assert row.project_key == "dld:dubai:1001"


def test_from_sheet_dict_accepts_sheet_strings():
    row = RegistryRow.from_sheet_dict(
        {
            "Emirate": "Dubai",
            "Registry": "DLD",
            "Registry Project No.": "1001.0",
            "Units": "1,234",
            "Percent Completed": "67.5",
            "Latitude": "25.2048",
            "Longitude": "55.2708",
            "Location Precision": "",
        }
    )
    assert row.registry_project_no == "1001"
    assert row.units == 1234
    assert row.percent_completed == 67.5
    assert row.has_coordinates()
    assert row.location_precision == ""  # unknown precision stays unknown, never "exact"


@pytest.mark.parametrize("missing", ["Emirate", "Registry"])
def test_from_sheet_dict_fails_loudly_without_emirate_or_registry(missing):
    data = dld_row().to_sheet_dict()
    data.pop(missing)
    with pytest.raises(ValueError):
        RegistryRow.from_sheet_dict(data)


def test_half_a_coordinate_pair_is_never_written():
    row = dld_row(latitude=25.2048, location_precision="exact")
    out = row.to_sheet_dict()
    assert out["Latitude"] == out["Longitude"] == out["Location Precision"] == ""


def test_coordinates_are_written_without_float_noise():
    out = dld_row(latitude=25.2, longitude=55.30000000001, location_precision="exact").to_sheet_dict()
    assert out["Latitude"] == "25.2"
    assert out["Longitude"] == "55.3"


# ===========================================================================
# RegistryRow: geocode tiers
# ===========================================================================
def test_geocode_tiers_in_order_with_qualifier():
    row = seed_row(
        emirate="Sharjah",
        project_name_en="Sample Residences Phase 2 by Imaginary Estates",
        developer="Imaginary Estates LLC",
        master_project="Sample Gardens",
        area_en="Muwaileh",
    )
    tiers = dict(row.geocode_tiers())
    assert [t for t, _ in row.geocode_tiers()] == ["exact", "community", "area"]
    assert tiers["exact"] == [
        "Sample Residences Phase 2 by Imaginary Estates, Sharjah, UAE",
        "Sample Residences Phase 2, Sharjah, UAE",
        "Sample Residences, Sharjah, UAE",
    ]
    assert tiers["community"] == ["Sample Gardens, Sharjah, UAE"]
    assert tiers["area"] == ["Muwaileh, Sharjah, UAE", "Muweilah, Sharjah, UAE"]


def test_every_query_is_qualified_and_bounded():
    row = dld_row(master_project="Fictional Lagoons", area_en="Jumeirah Village Circle")
    for tier, queries in row.geocode_tiers():
        assert queries
        assert all(q.endswith(", Dubai, UAE") for q in queries)
        assert len(queries) <= (3 if tier == "exact" else 2)


def test_developer_is_appended_once_when_the_name_has_no_suffix():
    row = dld_row(project_name_en="Example Heights", developer="Fictional Builders LLC", area_en="Business Bay")
    exact = dict(row.geocode_tiers())["exact"]
    assert exact == [
        "Example Heights, Dubai, UAE",
        "Example Heights by Fictional Builders, Dubai, UAE",
    ]


def test_developer_is_not_appended_when_the_brand_is_already_in_the_name():
    row = dld_row(project_name_en="Fictional Builders Heights", developer="Fictional Builders LLC", area_en="")
    assert dict(row.geocode_tiers())["exact"] == ["Fictional Builders Heights, Dubai, UAE"]


def test_tower_letters_survive_in_queries():
    row = dld_row(project_name_en="Example Tower B", developer="", area_en="")
    assert dict(row.geocode_tiers())["exact"] == ["Example Tower B, Dubai, UAE"]


def test_a_community_name_is_not_queried_as_an_exact_pin():
    # The project is the community: a centroid must not be filed as 'exact'.
    row = seed_row(
        emirate="Sharjah", project_name_en="Masaar", developer="",
        master_project="Masaar", area_en="Muwaileh",
    )
    tiers = dict(row.geocode_tiers())
    assert "exact" not in tiers
    assert tiers["community"] == ["Masaar, Sharjah, UAE"]


def test_only_the_developer_qualified_form_of_a_community_name_stays_exact():
    # "<name> by <developer>" is how OSM labels a project, so it is a genuine
    # project query; the bare community name is still left to its own tier.
    row = seed_row(
        emirate="Sharjah", project_name_en="Masaar", developer="Imaginary Estates",
        master_project="Masaar", area_en="Muwaileh",
    )
    tiers = dict(row.geocode_tiers())
    assert tiers["exact"] == ["Masaar by Imaginary Estates, Sharjah, UAE"]
    assert tiers["community"] == ["Masaar, Sharjah, UAE"]


def test_an_area_alias_name_is_not_queried_as_an_exact_pin():
    row = dld_row(project_name_en="JVC", developer="", area_en="Jumeirah Village Circle", master_project="")
    tiers = dict(row.geocode_tiers())
    assert "exact" not in tiers
    assert "area" in tiers


def test_a_repeated_query_is_issued_once():
    row = dld_row(project_name_en="", master_project="Business Bay", area_en="Business Bay")
    tiers = row.geocode_tiers()
    assert [t for t, _ in tiers] == ["community"]


def test_no_master_project_means_no_community_tier():
    row = dld_row(master_project="", area_en="Business Bay")
    assert [t for t, _ in row.geocode_tiers()] == ["exact", "area"]


def test_nothing_to_search_gives_no_tiers():
    assert RegistryRow(emirate="Dubai", registry="DLD", registry_project_no="5").geocode_tiers() == []


def test_project_names_are_not_respelled_but_places_are():
    row = seed_row(
        emirate="Sharjah", project_name_en="Mariam Residences", developer="", master_project="",
        area_en="Muweilah",
    )
    tiers = dict(row.geocode_tiers())
    assert tiers["exact"] == ["Mariam Residences, Sharjah, UAE"]  # a brand name: queried as given
    assert tiers["area"][0] == "Muwaileh, Sharjah, UAE"  # a place name: canonical spelling first


# ===========================================================================
# RegistryRow: map link
# ===========================================================================
def test_map_pin_link_uses_coordinates_when_present():
    row = dld_row(latitude=25.2048, longitude=55.2708)
    assert row.build_map_pin_link() == "https://www.google.com/maps/search/?api=1&query=25.2048,55.2708"


def test_map_pin_link_without_coordinates_is_a_name_search():
    link = dld_row().build_map_pin_link()
    assert link.startswith("https://www.google.com/maps/search/?api=1&query=Example+Heights")
    assert "Dubai%2C+UAE" in link


def test_map_pin_link_blank_when_nothing_to_search():
    assert RegistryRow(emirate="Dubai", registry="DLD", registry_project_no="5").build_map_pin_link() == ""


# ===========================================================================
# same_project / merge_rows
# ===========================================================================
def test_same_registry_id_is_the_same_project():
    assert same_project(dld_row(), dld_row(project_name_en="Renamed Heights"))


def test_different_registry_ids_are_different_projects_even_with_the_same_name():
    assert not same_project(dld_row(registry_project_no="1001"), dld_row(registry_project_no="1002"))


def test_a_seed_row_finds_its_registry_row_by_name():
    assert same_project(dld_row(), seed_row())


def test_a_seed_row_with_a_different_name_is_a_different_project():
    assert not same_project(dld_row(), seed_row(project_name_en="Other Heights"))


def test_rows_in_different_emirates_are_never_the_same_project():
    dubai = seed_row(emirate="Dubai")
    sharjah = seed_row(emirate="Sharjah")
    assert not same_project(dubai, sharjah)


def test_merge_prefers_registry_authority_over_a_seed():
    seed = seed_row(registry_project_no="777", master_project="Seed Lagoons", status="Announced")
    registry = dld_row(registry_project_no="1001", status="Active", master_project="")
    merged = merge_rows(seed, registry)  # the seed is passed first and is still outranked
    assert merged.registry == "DLD"
    assert merged.registry_project_no == "1001"
    assert merged.status == "Active"
    assert merged.master_project == "Seed Lagoons"  # an empty value is filled, never invented
    assert merged.project_key == "dld:dubai:1001"


def test_merge_keeps_the_first_row_for_conflicts_between_equals():
    fresh = dld_row(status="Active")
    old = dld_row(status="Pending")
    assert merge_rows(fresh, old).status == "Active"
    assert merge_rows(old, fresh).status == "Pending"


def test_merge_never_invents_a_value():
    merged = merge_rows(dld_row(), seed_row())
    assert merged.escrow_agent == ""
    assert merged.units is None
    assert merged.latitude is None and merged.longitude is None


def test_registry_and_id_travel_together():
    id_less_dld = dld_row(registry_project_no=None)
    with_id = dld_row(registry_project_no="1001")
    merged = merge_rows(id_less_dld, with_id)
    assert (merged.registry, merged.registry_project_no) == ("DLD", "1001")


def test_the_geo_bundle_travels_together():
    pinned = dld_row(
        latitude=25.2048, longitude=55.2708, location_precision="exact",
        geocode_method="nominatim", geocode_status="resolved via nominatim exact 2026-10-25",
        map_pin_link="https://www.google.com/maps/search/?api=1&query=25.2048,55.2708",
    )
    rough = dld_row(latitude=25.0, longitude=55.0, location_precision="area", geocode_method="nominatim")
    for first, second in ((pinned, rough), (rough, pinned)):
        merged = merge_rows(first, second)
        assert (merged.latitude, merged.longitude) == (25.2048, 55.2708)
        assert merged.location_precision == "exact"
        assert merged.geocode_status.startswith("resolved via nominatim exact")


def test_a_lone_latitude_never_pairs_with_another_rows_longitude():
    half = dld_row(latitude=25.9)  # no longitude: not a pin
    other = dld_row(latitude=25.1, longitude=55.1, location_precision="community")
    merged = merge_rows(half, other)
    assert (merged.latitude, merged.longitude) == (25.1, 55.1)


def test_higher_precision_wins_between_two_pins():
    community = dld_row(latitude=25.0, longitude=55.0, location_precision="community")
    exact = dld_row(latitude=25.2, longitude=55.2, location_precision="exact")
    assert merge_rows(community, exact).location_precision == "exact"
    assert merge_rows(exact, community).location_precision == "exact"


def test_first_seen_is_the_earliest_and_last_seen_the_latest():
    a = dld_row(first_seen="2026-10-10T03:00:00Z", last_seen="2026-10-20T03:00:00Z")
    b = dld_row(first_seen="2026-10-01T03:00:00Z", last_seen="2026-10-25T03:00:00Z")
    merged = merge_rows(a, b)
    assert merged.first_seen == "2026-10-01T03:00:00Z"
    assert merged.last_seen == "2026-10-25T03:00:00Z"
    assert merge_rows(dld_row(), dld_row()).first_seen == ""


def test_merge_refuses_different_emirates():
    with pytest.raises(ValueError):
        merge_rows(seed_row(emirate="Dubai"), seed_row(emirate="Sharjah"))


def test_merge_refuses_different_registry_ids():
    with pytest.raises(ValueError):
        merge_rows(dld_row(registry_project_no="1001"), dld_row(registry_project_no="1002"))


def test_the_merge_refusal_never_repeats_the_registry_ids_it_refused():
    # Regression: the message carried both ids, which are sheet cells, into whatever logged it.
    with pytest.raises(ValueError) as caught:
        merge_rows(dld_row(registry_project_no="7770001"), dld_row(registry_project_no="7770002"))
    assert "registry ids" in str(caught.value)
    assert "777000" not in str(caught.value) and "dld:" not in str(caught.value)


# ===========================================================================
# cluster_rows / dedupe_rows
# ===========================================================================
def test_clustering_groups_a_seed_row_with_its_registry_row():
    rows = [dld_row(), seed_row(), seed_row(project_name_en="Other Heights")]
    clusters = cluster_rows(rows)
    assert [len(c) for c in clusters] == [2, 1]
    assert clusters[0] == [rows[0], rows[1]]


def test_clustering_groups_equal_registry_ids():
    rows = [dld_row(project_name_en="Old Name"), dld_row(project_name_en="New Name")]
    assert len(cluster_rows(rows)) == 1


def test_clusters_never_hold_two_registry_ids_of_one_authority():
    # Two phases of one name, plus a seed row that cannot say which phase it means.
    rows = [
        dld_row(registry_project_no="1001"),
        dld_row(registry_project_no="1002"),
        seed_row(),
    ]
    clusters = cluster_rows(rows)
    assert len(clusters) == 3
    for cluster in clusters:
        ids = {r.registry_key() for r in cluster if r.registry_key()}
        assert len(ids) <= 1


def test_id_less_rows_of_an_ambiguous_name_are_grouped_among_themselves():
    rows = [
        dld_row(registry_project_no="1001"),
        dld_row(registry_project_no="1002"),
        seed_row(),
        seed_row(),
    ]
    sizes = sorted(len(c) for c in cluster_rows(rows))
    assert sizes == [1, 1, 2]


def test_clustering_never_crosses_emirates():
    rows = [seed_row(emirate="Dubai"), seed_row(emirate="Sharjah")]
    assert len(cluster_rows(rows)) == 2


def test_clustering_is_deterministic_and_keeps_input_order():
    rows = [
        seed_row(project_name_en="Alpha Heights"),
        seed_row(project_name_en="Beta Heights"),
        seed_row(project_name_en="Alpha Heights"),
    ]
    clusters = cluster_rows(rows)
    assert clusters[0] == [rows[0], rows[2]]
    assert clusters[1] == [rows[1]]
    assert cluster_rows(rows) == clusters


def test_clustering_empty_input():
    assert cluster_rows([]) == []
    assert dedupe_rows([]) == []


def test_rows_without_identity_stay_alone():
    blank_a = RegistryRow(emirate="Dubai", registry="SEED")
    blank_b = RegistryRow(emirate="Dubai", registry="SEED")
    assert len(cluster_rows([blank_a, blank_b])) == 2


@pytest.mark.parametrize(
    "name_a, name_b",
    [
        ("Tower أ", "Tower ب"),  # two Arabic letters
        ("Tower A+", "Tower A"),
        ("C#", "C"),
        ("Tower @ A", "Tower A"),
    ],
)
def test_id_less_projects_that_differ_only_by_a_dropped_letter_or_symbol_stay_two_rows(name_a, name_b):
    # They used to share one name key, so dedupe_rows gave back one row and the other project vanished.
    rows = [
        seed_row(developer="Test Dev", project_name_en=name_a, units=100),
        seed_row(developer="Test Dev", project_name_en=name_b, units=200),
    ]
    assert len(cluster_rows(rows)) == 2
    merged = dedupe_rows(rows)
    assert len(merged) == len(rows)
    assert [(r.project_name_en, r.units) for r in merged] == [(name_a, 100), (name_b, 200)]
    assert [r.units for r in dedupe_rows(rows[::-1])] == [200, 100]  # either order


def test_developers_that_differ_only_by_a_dropped_letter_or_symbol_stay_two_rows():
    rows = [
        seed_row(developer="Test Dev أ", project_name_en="Example Tower", units=100),
        seed_row(developer="Test Dev ب", project_name_en="Example Tower", units=200),
        seed_row(developer="Test Dev A+", project_name_en="Example Tower", units=300),
        seed_row(developer="Test Dev A", project_name_en="Example Tower", units=400),
    ]
    assert [r.units for r in dedupe_rows(rows)] == [100, 200, 300, 400]


def test_a_row_without_a_usable_name_is_kept_beside_rows_that_still_merge():
    twin_a = seed_row(project_name_en="Palm Jumeirah - Phase 2", units=100)
    twin_b = seed_row(project_name_en="Palm Jumeirah Phase 2", units=100)
    plain = seed_row(project_name_en="Example Tower A+", units=7)
    rows = [twin_a, plain, twin_b]
    assert [len(c) for c in cluster_rows(rows)] == [2, 1]  # ordinary punctuation does not keep projects apart
    merged = dedupe_rows(rows)
    assert [r.units for r in merged] == [100, 7]
    assert merged[1] == plain  # passed through untouched
    assert len(dedupe_rows([plain, plain.model_copy()])) == 2  # no key, so not even an exact repeat is recognised


def test_a_registry_row_with_an_unusable_name_is_still_keyed_and_merged_by_its_id():
    first = dld_row(project_name_en="Tower A+", units=10)
    second = dld_row(project_name_en="Tower A+ (renamed)", units=20)
    other = dld_row(project_name_en="Tower A+", registry_project_no="1002")
    assert first.name_key() is None and first.identity_keys() == ["dld:dubai:1001"]
    assert [len(c) for c in cluster_rows([first, second, other])] == [2, 1]
    assert [r.project_key for r in dedupe_rows([first, second, other])] == ["dld:dubai:1001", "dld:dubai:1002"]


def test_dedupe_merges_fresh_rows_into_existing_ones():
    existing = dld_row(
        latitude=25.2048, longitude=55.2708, location_precision="exact",
        first_seen="2026-10-01T03:00:00Z", last_seen="2026-10-20T03:00:00Z",
    )
    fresh = dld_row(status="Completed", units=300, first_seen="2026-10-25T03:00:00Z", last_seen="2026-10-25T03:00:00Z")
    other = dld_row(registry_project_no="2002", project_name_en="Another Tower")
    merged = dedupe_rows([fresh, existing, other])
    assert len(merged) == 2
    first = merged[0]
    assert first.status == "Completed" and first.units == 300  # fresh values preferred
    assert (first.latitude, first.longitude) == (25.2048, 55.2708)  # the pin is kept
    assert first.first_seen == "2026-10-01T03:00:00Z"
    assert first.last_seen == "2026-10-25T03:00:00Z"
    assert merged[1].project_key == "dld:dubai:2002"


# ===========================================================================
# LaunchCandidate
# ===========================================================================
def candidate(**overrides) -> LaunchCandidate:
    # SYNTHETIC
    data = dict(
        emirate="Dubai",
        project_name="Example Heights",
        developer="Fictional Builders LLC",
        area="Business Bay",
        claim="Launch announced for Q1 (synthetic)",
        source_url="https://example.com/news/example-heights",
    )
    data.update(overrides)
    return LaunchCandidate(**data)


def test_candidate_key():
    assert candidate().candidate_key == "cand:dubai:fictional-builders-llc:example-heights"
    assert candidate(developer="").candidate_key == "cand:dubai:unknown:example-heights"
    assert candidate(developer="n/a").candidate_key == "cand:dubai:unknown:example-heights"


def test_a_candidate_key_is_never_a_registry_key():
    assert not is_valid_project_key(candidate().candidate_key)


@pytest.mark.parametrize("url", ["", "not a url", "ftp://example.com/x", "javascript:alert(1)", "//example.com"])
def test_candidate_needs_an_http_source(url):
    with pytest.raises(ValidationError):
        candidate(source_url=url)


def test_candidate_needs_a_checkable_source_field():
    with pytest.raises(ValidationError):
        LaunchCandidate(emirate="Dubai", project_name="Example Heights")


def test_candidate_validation():
    with pytest.raises(ValidationError):
        candidate(emirate="Abu Dhabi")
    with pytest.raises(ValidationError):
        candidate(status="approved")
    with pytest.raises(ValidationError):
        candidate(project_name="مشروع")  # no Latin letter
    with pytest.raises(ValidationError):
        candidate(project_name="n/a")
    assert candidate().status == "new"
    assert candidate(status="Confirmed").status == "confirmed"


@pytest.mark.parametrize("name", ["Tower A+", "C#", "Tower @ A", "Tower أ", "Tower 7 مشروع"])
def test_a_candidate_name_that_loses_a_letter_or_symbol_is_refused_not_given_a_shared_key(name):
    # "Tower A+" and "Tower A" would otherwise share cand:dubai:...:tower-a and one lead would be dropped as a duplicate.
    with pytest.raises(ValidationError):
        candidate(project_name=name)


def test_a_candidate_developer_that_loses_a_letter_or_symbol_is_unknown():
    for developer in ("Test Dev أ", "Test Dev A+"):
        assert candidate(developer=developer).candidate_key.split(":")[2] == "unknown"


def test_candidate_found_at_defaults_to_utc():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", candidate().found_at)


def test_candidate_sheet_round_trip():
    original = candidate(found_at="2026-10-25T03:00:00Z")
    out = original.to_sheet_dict()
    assert list(out) == list(CANDIDATE_HEADERS)
    assert LaunchCandidate.from_sheet_dict(out) == original
    out["Candidate Key"] = "cand:dubai:stale:key"  # recomputed, never trusted
    assert LaunchCandidate.from_sheet_dict(out).candidate_key == original.candidate_key


# ===========================================================================
# GeocodeOverride
# ===========================================================================
def override(**overrides) -> GeocodeOverride:
    # SYNTHETIC
    data = dict(project_key="dld:dubai:1001", latitude="25.2048", longitude="55.2708", precision="exact", note="checked")
    data.update(overrides)
    return GeocodeOverride(**data)


def test_override_round_trip():
    original = override()
    out = original.to_sheet_dict()
    assert list(out) == list(OVERRIDE_HEADERS)
    assert out["Latitude"] == "25.2048"
    assert GeocodeOverride.from_sheet_dict(out) == original


@pytest.mark.parametrize("key", ["1001", "dld:dubai:0", "dld:sharjah:1", "", "name:dubai::x"])
def test_override_needs_a_valid_project_key(key):
    with pytest.raises(ValidationError):
        override(project_key=key)


def test_override_accepts_name_keys():
    assert override(project_key="name:dubai:fictional-builders-llc:example-heights")


@pytest.mark.parametrize("lat, lon", [("", "55.2"), ("25.2", ""), (91, 55.2), (25.2, 181), ("25,2048", "55,2708"), ("abc", "def")])
def test_override_needs_real_coordinates(lat, lon):
    with pytest.raises(ValidationError):
        override(latitude=lat, longitude=lon)


def test_override_precision_is_what_the_human_states_or_blank():
    assert override(precision="Community").precision == "community"
    assert override(precision="centroid").precision == ""
    assert override(precision="").precision == ""  # blank is kept blank, never assumed exact


# ===========================================================================
# Configuration (docs/DESIGN.md section 9)
# ===========================================================================
SECRET_NAMES = [
    "GEMINI_API_KEY", "GCP_SERVICE_ACCOUNT_KEY", "SPREADSHEET_ID", "DATA_DUBAI_API_KEY",
    "DATA_DUBAI_API_SECRET", "LOCATIONIQ_API_KEY", "GEOAPIFY_API_KEY", "GOOGLE_MAPS_API_KEY",
    "SHARE_WITH_EMAILS",
]
VARIABLE_NAMES = [
    "GOOGLE_SHEET_NAME", "GEMINI_MODEL", "ENABLE_SRERD", "ENABLE_GEMINI_NEWS", "REQUIRE_SOURCES",
    "ALLOW_ROW_DROP", "GEOCODE_ENABLED", "GEOCODE_BACKEND", "GEOCODE_MAX_PER_RUN",
    "GEOCODE_USER_AGENT", "GEOCODE_CONTACT", "GEOCODE_RETRY_COOLDOWN_DAYS",
    "GEOCODE_TIME_BUDGET_SECONDS", "GEOCODE_FLUSH_EVERY", "SHARE_ROLE", "SHARE_NOTIFY", "DEPLOY_MAP",
]
# Documented and parsed by Config, but not printed by describe(): only the workflow's deploy job reads it
# (the scrape step is never given it, so a printed value would say nothing about the deployment).
WORKFLOW_ONLY_NAMES = {"DEPLOY_MAP"}
FAKE = "synthetic-not-a-real-value-0000"


def test_defaults():
    cfg = load_config({})
    assert cfg.google_sheet_name == DEFAULT_SHEET_NAME == "UAE Real Estate Projects Tracker"
    assert cfg.geocode_user_agent == DEFAULT_GEOCODE_USER_AGENT
    assert cfg.geocode_enabled is True
    assert cfg.geocode_backend == ""
    assert cfg.geocode_max_per_run == 0
    assert cfg.geocode_retry_cooldown_days == 30
    assert cfg.geocode_time_budget_seconds == 3300
    assert cfg.geocode_flush_every == 250
    assert cfg.enable_srerd is False
    assert cfg.enable_gemini_news is False
    assert cfg.allow_row_drop is False
    assert cfg.deploy_map is False
    assert cfg.share_role == "writer"
    assert cfg.share_notify is False
    assert cfg.gemini_model == ""  # no unverified model name is ever assumed
    assert cfg.require_sources == ()
    assert cfg.gcp_service_account_file == "service_account.json"
    assert cfg.warnings == ()


def test_config_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        load_config({}).share_role = "reader"


def test_secrets_are_read_and_stripped():
    env = {name: f"  {FAKE}-{i}  " for i, name in enumerate(SECRET_NAMES) if name != "SHARE_WITH_EMAILS"}
    cfg = load_config(env)
    assert cfg.gemini_api_key == f"{FAKE}-0"
    assert cfg.spreadsheet_id == f"{FAKE}-2"
    assert cfg.data_dubai_api_secret == f"{FAKE}-4"


def test_secrets_never_appear_in_repr_or_describe():
    env = {name: FAKE for name in SECRET_NAMES if name != "SHARE_WITH_EMAILS"}
    env["SHARE_WITH_EMAILS"] = "person@example.com"
    cfg = load_config(env)
    assert FAKE not in repr(cfg)
    assert FAKE not in str(cfg)
    assert "person@example.com" not in repr(cfg)
    described = cfg.describe()
    assert FAKE not in " ".join(described.values())
    assert described["GEMINI_API_KEY"] == "set"
    assert described["SPREADSHEET_ID"] == "set"
    assert described["SHARE_WITH_EMAILS"] == "1 address(es)"
    assert load_config({}).describe()["GEMINI_API_KEY"] == "not set"


def test_describe_covers_every_documented_name():
    described = set(load_config({}).describe())
    # the names describe() prints, plus the explicit workflow-only ones, are exactly the documented names
    assert described | WORKFLOW_ONLY_NAMES == set(SECRET_NAMES) | set(VARIABLE_NAMES)
    # and the workflow-only ones are deliberately not printed (a name moved out of this set must be printed)
    assert not described & WORKFLOW_ONLY_NAMES


def test_data_dubai_credentials_need_both_halves():
    assert not load_config({"DATA_DUBAI_API_KEY": FAKE}).has_data_dubai_credentials
    assert not load_config({"DATA_DUBAI_API_SECRET": FAKE}).has_data_dubai_credentials
    both = {"DATA_DUBAI_API_KEY": FAKE, "DATA_DUBAI_API_SECRET": FAKE}
    assert load_config(both).has_data_dubai_credentials


def test_inline_gcp_credentials_flag():
    assert not load_config({}).has_gcp_credentials_inline
    assert load_config({"GCP_SERVICE_ACCOUNT_KEY": "{}"}).has_gcp_credentials_inline


@pytest.mark.parametrize("raw", ["true", "True", "TRUE", "1", "yes", "y", "on"])
def test_true_spellings(raw):
    cfg = load_config({"ENABLE_SRERD": raw, "GEOCODE_ENABLED": raw})
    assert cfg.enable_srerd is True and cfg.geocode_enabled is True
    assert cfg.warnings == ()


@pytest.mark.parametrize("raw", ["false", "False", "0", "no", "n", "off"])
def test_false_spellings(raw):
    cfg = load_config({"ENABLE_SRERD": raw, "GEOCODE_ENABLED": raw})
    assert cfg.enable_srerd is False and cfg.geocode_enabled is False
    assert cfg.warnings == ()


@pytest.mark.parametrize("raw", ["", "   "])
def test_an_empty_variable_means_the_default(raw):
    # GitHub Actions renders an unset repository variable as an empty string.
    cfg = load_config({name: raw for name in VARIABLE_NAMES})
    assert cfg == load_config({})


def test_a_bad_boolean_warns_and_keeps_the_default():
    cfg = load_config({"GEOCODE_ENABLED": "maybe", "DEPLOY_MAP": "tru"})
    assert cfg.geocode_enabled is True  # that setting's default
    assert cfg.deploy_map is False
    assert len(cfg.warnings) == 2
    assert any("GEOCODE_ENABLED" in w for w in cfg.warnings)
    assert any("DEPLOY_MAP" in w for w in cfg.warnings)


def test_integers():
    cfg = load_config(
        {"GEOCODE_MAX_PER_RUN": "400", "GEOCODE_RETRY_COOLDOWN_DAYS": "7",
         "GEOCODE_TIME_BUDGET_SECONDS": "600", "GEOCODE_FLUSH_EVERY": "50"}
    )
    assert (cfg.geocode_max_per_run, cfg.geocode_retry_cooldown_days) == (400, 7)
    assert (cfg.geocode_time_budget_seconds, cfg.geocode_flush_every) == (600, 50)
    assert cfg.warnings == ()


@pytest.mark.parametrize(
    "name, raw, default_attr",
    [
        ("GEOCODE_MAX_PER_RUN", "lots", "geocode_max_per_run"),
        ("GEOCODE_MAX_PER_RUN", "-5", "geocode_max_per_run"),
        ("GEOCODE_RETRY_COOLDOWN_DAYS", "1.5", "geocode_retry_cooldown_days"),
        ("GEOCODE_TIME_BUDGET_SECONDS", "0", "geocode_time_budget_seconds"),  # minimum is 1
        ("GEOCODE_FLUSH_EVERY", "0", "geocode_flush_every"),
    ],
)
def test_a_bad_integer_warns_and_keeps_the_default(name, raw, default_attr):
    cfg = load_config({name: raw})
    assert getattr(cfg, default_attr) == getattr(load_config({}), default_attr)
    assert len(cfg.warnings) == 1 and name in cfg.warnings[0]


def test_lists_are_split_deduplicated_and_ordered():
    cfg = load_config({"SHARE_WITH_EMAILS": "a@example.com, b@example.com;a@example.com  c@example.com"})
    assert cfg.share_with_emails == ("a@example.com", "b@example.com", "c@example.com")
    assert load_config({"REQUIRE_SOURCES": "DLD, Seed, dld"}).require_sources == ("dld", "seed")
    assert load_config({"SHARE_WITH_EMAILS": ""}).share_with_emails == ()


def test_geocode_backend_choice():
    assert load_config({"GEOCODE_BACKEND": "Google"}).geocode_backend == "google"  # opt-in, explicit only
    assert load_config({"GEOCODE_BACKEND": "locationiq"}).geocode_backend == "locationiq"
    assert load_config({}).geocode_backend == ""  # never auto-selects google
    bad = load_config({"GEOCODE_BACKEND": "bing"})
    assert bad.geocode_backend == ""
    assert any("GEOCODE_BACKEND" in w for w in bad.warnings)


def test_share_role_choice():
    assert load_config({"SHARE_ROLE": "Reader"}).share_role == "reader"
    bad = load_config({"SHARE_ROLE": "owner"})
    assert bad.share_role == "writer"
    assert any("SHARE_ROLE" in w for w in bad.warnings)


def test_user_agent_carries_the_contact_once():
    cfg = load_config({"GEOCODE_CONTACT": "ops@example.com"})
    assert cfg.geocode_user_agent == f"{DEFAULT_GEOCODE_USER_AGENT} (ops@example.com)"
    already = load_config({"GEOCODE_USER_AGENT": "my-app/2.0 (ops@example.com)", "GEOCODE_CONTACT": "ops@example.com"})
    assert already.geocode_user_agent == "my-app/2.0 (ops@example.com)"
    custom = load_config({"GEOCODE_USER_AGENT": "my-app/2.0"})
    assert custom.geocode_user_agent == "my-app/2.0"
    assert load_config({"GEOCODE_CONTACT": "ops@example.com"}).describe()["GEOCODE_CONTACT"] == "set"


def test_an_explicit_mapping_never_reads_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE)
    monkeypatch.setenv("DEPLOY_MAP", "true")
    cfg = load_config({})
    assert cfg.gemini_api_key == "" and cfg.deploy_map is False


def test_the_environment_is_read_when_no_mapping_is_given(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE)
    cfg = load_config(env_file=tmp_path / "does-not-exist.env")
    assert cfg.gemini_api_key == FAKE


def test_env_file_is_read_without_touching_the_process_environment(monkeypatch, tmp_path):
    for name in ("SPREADSHEET_ID", "GEOCODE_BACKEND", "SHARE_ROLE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEOCODE_BACKEND", "geoapify")
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"SPREADSHEET_ID={FAKE}\nGEOCODE_BACKEND=locationiq\nSHARE_ROLE=reader\n", encoding="utf-8"
    )
    cfg = load_config(env_file=env_file)
    assert cfg.spreadsheet_id == FAKE  # from the file
    assert cfg.geocode_backend == "geoapify"  # the real environment wins
    assert cfg.share_role == "reader"
    assert "SPREADSHEET_ID" not in os.environ  # the file was parsed, not exported


def test_an_empty_exported_value_does_not_shadow_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("SHARE_ROLE", "")
    env_file = tmp_path / ".env"
    env_file.write_text("SHARE_ROLE=commenter\n", encoding="utf-8")
    assert load_config(env_file=env_file).share_role == "commenter"


def test_importing_the_config_module_does_not_load_the_environment():
    # The module keeps no secrets at import time: only constants and the loader.
    public = {n for n in dir(cfgmod) if not n.startswith("_")}
    assert "load_config" in public
    assert not any(n.endswith("_API_KEY") for n in public)


def test_tab_names_match_the_design():
    assert cfgmod.ALL_TABS == (
        "Registry_Projects", "Listing_Data", "Launch_Candidates", "Map_Pins",
        "Geocode_Overrides", "Scrape_Run_Logs",
    )
    assert cfgmod.EMIRATES == ("Dubai", "Sharjah")
    assert cfgmod.REGISTRIES == ("DLD", "SRERD", "SEED")


def test_the_config_class_is_exported():
    assert dataclasses.is_dataclass(Config)


# ===========================================================================
# .env.example stays in step with the configuration
# ===========================================================================
ENV_EXAMPLE = Path(cfgmod.ROOT_DIR) / ".env.example"


def _env_example_assignments() -> dict[str, str]:
    found: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#?\s*([A-Z][A-Z0-9_]+)=(.*)$", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def test_env_example_names_every_setting():
    names = set(_env_example_assignments())
    expected = set(SECRET_NAMES) | set(VARIABLE_NAMES) | {"GCP_SERVICE_ACCOUNT_FILE", "DATA_DUBAI_PROJECTS_FILE"}
    assert expected <= names, sorted(expected - names)
    assert names <= expected, sorted(names - expected)


def test_env_example_holds_no_secret_values():
    assignments = _env_example_assignments()
    for name in SECRET_NAMES:
        assert assignments[name] == "", f"{name} must be blank in .env.example"
