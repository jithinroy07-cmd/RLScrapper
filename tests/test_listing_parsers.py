"""
Listing parsers, property types, Listing_Data records and the seed file
(docs/DESIGN.md sections 3 and 6).

The rule under test is the one from DESIGN section 0: blank beats invented. Every
"ambiguous" case here must come back as None, never as a best guess. All values
are SYNTHETIC.
"""

from __future__ import annotations

import logging

import pytest

from src.listing_data import (
    LISTING_HEADERS,
    PROPERTY_TYPES,
    SOURCE_LICENCES,
    SeedFileError,
    check_seed_rows,
    load_listing_records,
    normalise_type,
    parse_area_sqft,
    parse_bedrooms,
    parse_price_aed,
    parse_price_per_sqft_aed,
    parse_property_types,
    read_seed,
    rows_to_add,
    seed_row_problem,
    type_from_name,
)

KEY_A = "dld:dubai:1001"
KEY_B = "name:sharjah:imaginary-estates:sample-gardens"
URL = "https://example.com/listing/synthetic"


# ===========================================================================
# Total price
# ===========================================================================
@pytest.mark.parametrize(
    "text, expected",
    [
        ("AED 1.2M", 1_200_000),
        ("1.2 million", 1_200_000),
        ("1.2M", 1_200_000),
        ("AED1.2M", 1_200_000),
        ("aed 1.2m", 1_200_000),
        ("AED 2.5 Mn", 2_500_000),
        ("850K", 850_000),
        ("850 k", 850_000),
        ("850 thousand", 850_000),
        ("Dhs 1,250,000", 1_250_000),
        ("Dhs. 1,250,000", 1_250_000),
        ("AED 1,250,000", 1_250_000),
        ("1,250,000", 1_250_000),
        ("from AED 850,000", 850_000),
        ("Starting from AED 1.2 million", 1_200_000),
        ("AED 850,000 onwards", 850_000),
        ("1.23456M", 1_234_560),
        ("0.5M", 500_000),
        (".5M", 500_000),  # not 5,000,000: the leading dot is part of the number
        ("1250000", 1_250_000),
        # A bare number is taken exactly as typed, never scaled.
        ("850", 850),
        ("1.2", None),  # not a whole number of AED, and not scaled to make it one
        (1_200_000, 1_200_000),
        (1_200_000.0, 1_200_000),
    ],
)
def test_parse_price_aed(text, expected):
    assert parse_price_aed(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        0,
        -5,
        True,
        1.5,
        float("nan"),
        "on request",
        "TBA",
        "n/a",
        # The comma is a thousands separator and nothing else.
        "1,2 million",
        "1,2",
        "12,50,000",
        "1,25,000",
        "1,25",
        # Ranges and two numbers.
        "AED 850K - 1.2M",
        "850,000 - 1,200,000",
        "850K to 1.2M",
        "1.2M 1.5M",
        # Units we do not know are not guessed at.
        "1.2 bn",
        "$1.2M",
        "USD 300,000",
        "1.2 xyz",
        # A lone dot before three digits could be a European thousands separator: not read as a decimal.
        "750.000",
        "1.000",
        "AED 750.000",
        "12.000",
        # A leading minus sign is not stripped away, and a leading dot is kept.
        "-850",
        "AED -850K",
        # A per-area price is not a total.
        "AED 1,800 per sq ft",
        "AED 1,800/sqft",
        "1,800 psf",
        "AED 1,800 per sqm",
        "1,800/sq m",
    ],
)
def test_parse_price_aed_refuses_the_ambiguous(text):
    assert parse_price_aed(text) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1.250M", 1_250_000),  # a unit makes the dot a decimal point
        ("1.250 million", 1_250_000),
        ("750.00", 750),  # two decimals are not a grouping
        ("750.5K", 750_500),
        ("1,250.000", 1250),  # with an English thousands comma the dot can only be a decimal
        ("1.250 k", 1250),
        ("1.250 thousand", 1250),
        ("1250.000", 1250),  # four digits before the dot is not a thousands group
    ],
)
def test_a_dot_is_still_a_decimal_point_when_nothing_makes_it_ambiguous(text, expected):
    assert parse_price_aed(text) == expected


def test_digits_from_other_scripts_are_read_by_value_and_the_arabic_decimal_mark_is_refused():
    assert parse_area_sqft("١٢٠٠ sq ft") == 1200  # Arabic-Indic digits
    assert parse_area_sqft("１２００ sq ft") == 1200  # fullwidth digits
    assert parse_price_aed("٨٥٠") == 850
    assert parse_price_aed("１．２M") == 1_200_000
    assert parse_bedrooms("٣ BR") == (3, 3)
    assert parse_price_aed("AED ١٫٢M") is None  # Arabic decimal separator: not understood, so refused


@pytest.mark.parametrize(
    "text, expected",
    [
        ("AED.5M", 500_000),  # the dot belongs to the number, not to the abbreviation
        ("Dhs.5m", 500_000),
        ("from.5M", 500_000),
        ("AED. 1.2M", 1_200_000),  # a dot followed by a space is the abbreviation's
        ("Dhs. 850,000", 850_000),
        ("approx. AED 1.2M", 1_200_000),
    ],
)
def test_an_abbreviation_dot_is_eaten_only_when_no_digit_follows_it(text, expected):
    assert parse_price_aed(text) == expected


def test_the_dot_before_three_digits_rule_covers_the_per_sqft_price_too():
    assert parse_price_per_sqft_aed("1.800") is None
    assert parse_price_per_sqft_aed("AED 1.800/sq ft") is None
    assert parse_price_per_sqft_aed("1,800") == 1800


def test_a_total_price_is_never_a_per_area_price():
    # The two are different columns with different parsers; one is never read as the other.
    assert parse_price_aed("AED 1,800/sq ft") is None
    assert parse_price_per_sqft_aed("AED 1,800/sq ft") == 1800


# ===========================================================================
# Price per sq ft
# ===========================================================================
@pytest.mark.parametrize(
    "text, expected",
    [
        ("1,800", 1800),
        ("1800", 1800),
        ("AED 1,800", 1800),
        ("AED 1,800/sq ft", 1800),
        ("AED 1,800 / sq ft", 1800),
        ("AED 1,800 per sq ft", 1800),
        ("AED 1,800 per sqft", 1800),
        ("1,800 psf", 1800),
        ("Dhs 1,800 psf", 1800),
        (1800, 1800),
        (1800.0, 1800),
    ],
)
def test_parse_price_per_sqft_aed(text, expected):
    assert parse_price_per_sqft_aed(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        0,
        "n/a",
        "1,2",
        "12,50",
        "1,800 - 2,000",
        "1800 per sqm",  # a per-square-metre price is not converted: that would invent a number
        "AED 1,800/sq m",
        "1,800 per sq metre",
    ],
)
def test_parse_price_per_sqft_aed_refuses_the_ambiguous(text):
    assert parse_price_per_sqft_aed(text) is None


# ===========================================================================
# Bedrooms
# ===========================================================================
@pytest.mark.parametrize(
    "text, expected",
    [
        ("Studio", (0, 0)),
        ("studios", (0, 0)),
        ("1BR", (1, 1)),
        ("1 BR", (1, 1)),
        ("2 br", (2, 2)),
        ("3 Bedroom", (3, 3)),
        ("3 Bedrooms", (3, 3)),
        ("4 BHK", (4, 4)),
        ("2 B/R", (2, 2)),
        ("1-3 BR", (1, 3)),
        ("1 - 3 BR", (1, 3)),
        ("1–3 BR", (1, 3)),  # en dash
        ("1 to 2 bedrooms", (1, 2)),
        ("Studio to 3BR", (0, 3)),
        ("Studio - 2 BR", (0, 2)),
        ("1, 2 & 3 BR", (1, 3)),
        ("Studio, 1 & 2 BR", (0, 2)),
        ("1 BR and 3 BR", (1, 3)),  # two sizes: the spanning range, the only thing two columns can hold
        # Maid rooms are ignored, not counted.
        ("3 BR + Maid", (3, 3)),
        ("3 BR + maid's room", (3, 3)),
        ("3BR+Maid", (3, 3)),
        ("2 BR + 1 maid", (2, 2)),
        # An area phrase is ignored, not read as a bedroom count.
        ("2 BR 1,200 sq ft", (2, 2)),
        ("2 BR, 110 sqm", (2, 2)),
        # Bare numbers: that is how the Bedrooms Min / Max cells are typed.
        ("3", (3, 3)),
        ("1-3", (1, 3)),
        (3, (3, 3)),
        (0, (0, 0)),
        (2.0, (2, 2)),
    ],
)
def test_parse_bedrooms(text, expected):
    assert parse_bedrooms(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        True,
        2.5,
        float("nan"),
        -1,
        "2.5 BR",
        "3+ BR",  # open ended
        "up to 4 BR",  # a maximum only
        "from 2 BR",  # a minimum only
        "3 floors",
        "3 BR villa with 5 car garage",
        "-1",
        "-3 BR",
        "1-",
        "3 BR -",
        "1--3",
        "30",
        "100 BR",
        "1,200",
        "villa",
        "n/a",
        "TBC",
    ],
)
def test_parse_bedrooms_refuses_the_ambiguous(text):
    assert parse_bedrooms(text) is None


# ===========================================================================
# Area
# ===========================================================================
@pytest.mark.parametrize(
    "text, expected",
    [
        ("1,200 sq ft", 1200),
        ("1200 sq ft", 1200),
        ("1200sqft", 1200),
        ("1,200 sqft", 1200),
        ("1,200 sq.ft.", 1200),
        ("1,200 square feet", 1200),
        ("from 1,200 sq ft", 1200),
        ("1200", 1200),  # the column is sq ft, so a bare number is sq ft
        ("1,200.4 sq ft", 1200),
        (1200, 1200),
        (1200.4, 1200),
        # The dot-before-three-digits refusal is narrow: these are not thousands-or-decimal ambiguities.
        ("1200.000 sq ft", 1200),
        ("1,200.000 sq ft", 1200),
        ("1200.500 sq ft", 1201),
        ("1.2345 sq ft", 1),
        # Square metres convert with 10.7639 and round half up.
        ("110 sq m", 1184),
        ("110 sqm", 1184),
        ("110 m2", 1184),
        ("110 m²", 1184),
        ("110 square metres", 1184),
        ("100 sqm", 1076),
        ("50 sqm", 538),  # 538.195
    ],
)
def test_parse_area_sqft(text, expected):
    assert parse_area_sqft(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        0,
        True,
        float("nan"),
        "n/a",
        "1,200 - 1,500 sq ft",  # a range: enter From and To separately
        "1200 to 1500",
        "2 acres",
        "1,2 sq ft",
        # A lone dot before three digits could be a European thousands separator: not read as a decimal.
        "1.200 sq ft",
        "12.345 sqft",
        "750.000",
        "1.250 sqm",
        "1,200 sq ft or 110 sq m",
        "about a thousand sq ft",
        "sq ft",
    ],
)
def test_parse_area_sqft_refuses_the_ambiguous(text):
    assert parse_area_sqft(text) is None


# ===========================================================================
# Property types
# ===========================================================================
def test_the_type_list_is_the_one_in_the_design():
    assert PROPERTY_TYPES == (
        "Apartment", "Villa", "Townhouse", "Penthouse", "Duplex",
        "Hotel Apartment", "Plot", "Commercial", "Mixed Use", "Unknown",
    )
    assert SOURCE_LICENCES == ("licensed-api", "developer-permitted", "manual")


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Sample Villas", "Villa"),
        ("Sample Townhouses", "Townhouse"),
        ("Sample Town Houses", "Townhouse"),
        ("Sample Penthouses", "Penthouse"),
        ("Sample Duplex Collection", "Duplex"),
        ("Sample Apartments", "Apartment"),
        ("Sample Flats", "Apartment"),
        ("Sample Hotel Apartments", "Hotel Apartment"),  # not also an Apartment
        ("Sample Plots", "Plot"),
        ("Sample Offices", "Commercial"),
        ("Sample Retail Park", "Commercial"),
        ("Sample Mixed-Use Quarter", "Mixed Use"),
        ("Sample Office Apartments", "Mixed Use"),  # commercial and residential words together
        ("Sample Villas and Townhouses", "Unknown"),  # two residential types: no single answer
        # Unknown beats a wrong guess.
        ("Sample Tower", "Unknown"),  # a tower is not implied to be commercial, or anything
        ("Sample Towers", "Unknown"),
        ("Sample Residences", "Unknown"),
        ("Sample Heights", "Unknown"),
        ("Sample Mall", "Unknown"),
        ("Sample Gardens", "Unknown"),
        ("", "Unknown"),
        (None, "Unknown"),
    ],
)
def test_type_from_name(name, expected):
    assert type_from_name(name) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Apartment", "Apartment"),
        ("apartments", "Apartment"),
        ("Flats", "Apartment"),
        ("Villas", "Villa"),
        ("Town House", "Townhouse"),
        ("townhomes", "Townhouse"),
        ("Land", "Plot"),
        ("Hotel Apartments", "Hotel Apartment"),
        ("hotel_apartment", "Hotel Apartment"),
        ("Mixed-use", "Mixed Use"),
        ("Offices", "Commercial"),
        ("Unknown", "Unknown"),
        ("something else", "Unknown"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalise_type(text, expected):
    assert normalise_type(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Apartment", ["Apartment"]),
        ("Apartments & Penthouses", ["Apartment", "Penthouse"]),
        ("apartment, villa", ["Apartment", "Villa"]),
        ("Villa; Townhouse / Plot", ["Villa", "Townhouse", "Plot"]),
        ("Villas and Townhouses", ["Villa", "Townhouse"]),
        ("Apartment, apartments", ["Apartment"]),  # no repeats
        ("Apartment, gibberish", ["Apartment"]),  # Unknown dropped when something real is present
        ("gibberish", ["Unknown"]),
        ("", []),
        (None, []),
    ],
)
def test_parse_property_types(text, expected):
    assert parse_property_types(text) == expected


# ===========================================================================
# Listing_Data records
# ===========================================================================
def test_listing_headers_match_the_design():
    assert LISTING_HEADERS == (
        "Project Key", "Price From (AED)", "Price To (AED)", "Price per sq ft (AED)",
        "Area From (sq ft)", "Area To (sq ft)", "Bedrooms Min", "Bedrooms Max",
        "Property Types", "Handover", "Payment Plan", "Source URL", "Source Licence",
        "As Of", "Entered By",
    )
    assert len(set(LISTING_HEADERS)) == len(LISTING_HEADERS)


def record(**overrides) -> dict:
    # SYNTHETIC
    data = {
        "Project Key": KEY_A,
        "Price From (AED)": "AED 1.2M",
        "Price To (AED)": "2,400,000",
        "Price per sq ft (AED)": "1,800",
        "Area From (sq ft)": "650",
        "Area To (sq ft)": "110 sqm",
        "Bedrooms Min": "Studio",
        "Bedrooms Max": "3 BR",
        "Property Types": "Apartments & Penthouses",
        "Handover": "Q4 2027",
        "Payment Plan": "60/40",
        "Source URL": URL,
        "Source Licence": "manual",
        "As Of": "25-10-2026",
        "Entered By": "analyst",
    }
    data.update(overrides)
    return data


def test_load_listing_records_parses_every_column():
    out = load_listing_records([record()])
    assert out[KEY_A] == {
        "project_key": KEY_A,
        "price_from": 1_200_000,
        "price_to": 2_400_000,
        "price_per_sqft": 1800,
        "area_from": 650,
        "area_to": 1184,
        "bedrooms_min": 0,
        "bedrooms_max": 3,
        "types": ["Apartment", "Penthouse"],
        "handover": "Q4 2027",
        "payment_plan": "60/40",
        "source_url": URL,
        "licence": "manual",
        "as_of": "2026-10-25",
        "entered_by": "analyst",
    }


def test_unparseable_numbers_become_none_not_guesses():
    out = load_listing_records(
        [record(**{"Price From (AED)": "on request", "Price To (AED)": "1,2 million", "Bedrooms Min": "3+ BR",
                   "Area From (sq ft)": "2 acres", "Property Types": "", "As Of": "soon"})]
    )[KEY_A]
    assert out["price_from"] is None and out["price_to"] is None
    assert out["bedrooms_min"] is None
    assert out["area_from"] is None
    assert out["types"] == []
    assert out["as_of"] == ""


def test_refused_number_cells_are_counted_in_the_log_without_their_text(caplog):
    typed = "SECRETWORD-on-request"
    with caplog.at_level(logging.WARNING, logger="src.listing_data"):
        load_listing_records(
            [
                record(**{"Price From (AED)": typed, "Area From (sq ft)": "1.200 sq ft", "Bedrooms Min": "3+ BR"}),
                record(**{"Project Key": KEY_B, "Price To (AED)": "", "Area To (sq ft)": None, "Bedrooms Max": "  "}),
            ]
        )
    refusals = [m for m in caplog.messages if "refused" in m]
    assert len(refusals) == 1 and refusals[0].startswith("Listing_Data: 3 ")  # blank cells are not refusals
    assert typed not in caplog.text and "1.200" not in caplog.text


def test_clean_number_cells_log_no_refusal(caplog):
    with caplog.at_level(logging.WARNING, logger="src.listing_data"):
        load_listing_records([record()])
    assert not [m for m in caplog.messages if "refused" in m]


def test_numeric_cells_are_accepted():
    out = load_listing_records(
        [record(**{"Price From (AED)": 1_200_000, "Bedrooms Min": 1, "Bedrooms Max": 2, "Area From (sq ft)": 700})]
    )[KEY_A]
    assert (out["price_from"], out["bedrooms_min"], out["bedrooms_max"], out["area_from"]) == (1_200_000, 1, 2, 700)


def test_a_row_without_a_source_url_is_skipped(caplog):
    with caplog.at_level(logging.WARNING, logger="src.listing_data"):
        out = load_listing_records([record(**{"Source URL": ""}), record(**{"Source URL": "ftp://example.com/x"})])
    assert out == {}
    assert any("Source URL" in message for message in caplog.messages)


@pytest.mark.parametrize("key", ["", "1001", "dld:dubai:0", "dld:sharjah:1", "name:dubai::x", None])
def test_a_row_without_a_valid_key_is_skipped(key):
    assert load_listing_records([record(**{"Project Key": key})]) == {}


def test_the_latest_row_for_a_key_wins():
    first = record(**{"Price From (AED)": "1M"})
    second = record(**{"Price From (AED)": "1.1M"})
    assert load_listing_records([first, second])[KEY_A]["price_from"] == 1_100_000


def test_a_later_row_without_a_source_does_not_replace_a_good_one():
    good = record(**{"Price From (AED)": "1M"})
    bad = record(**{"Price From (AED)": "9M", "Source URL": ""})
    assert load_listing_records([good, bad])[KEY_A]["price_from"] == 1_000_000


def test_keys_and_text_are_trimmed():
    out = load_listing_records([record(**{"Project Key": f"  {KEY_A}  ", "Handover": "  Q4   2027 "})])
    assert out[KEY_A]["handover"] == "Q4 2027"


def test_empty_input():
    assert load_listing_records([]) == {}


# ===========================================================================
# Seed file
# ===========================================================================
def seed_line(key=KEY_A, url=URL, **extra) -> list[str]:
    cells = {h: "" for h in LISTING_HEADERS}
    cells.update({"Project Key": key, "Source URL": url, "Price From (AED)": "1M"})
    cells.update(extra)
    return [cells[h] for h in LISTING_HEADERS]


def test_read_seed_missing_file_is_empty(tmp_path):
    assert read_seed(tmp_path / "nothing-here.csv") == []


def test_read_seed_empty_file_is_empty(tmp_path):
    path = tmp_path / "seed.csv"
    path.write_text("", encoding="utf-8")
    assert read_seed(path) == []


def test_read_seed_matches_columns_by_name(tmp_path):
    path = tmp_path / "seed.csv"
    path.write_text(
        "Source URL,Notes,Project Key,Price From (AED)\n"
        f"{URL},ignore me,{KEY_A},1M\n"
        "\n"
        f"{URL},,{KEY_B},\n",
        encoding="utf-8",
    )
    rows = read_seed(path)
    assert len(rows) == 2
    assert all(len(r) == len(LISTING_HEADERS) for r in rows)
    assert rows[0][LISTING_HEADERS.index("Project Key")] == KEY_A
    assert rows[0][LISTING_HEADERS.index("Source URL")] == URL
    assert rows[0][LISTING_HEADERS.index("Price From (AED)")] == "1M"
    assert rows[1][LISTING_HEADERS.index("Price From (AED)")] == ""
    assert "ignore me" not in rows[0]


def test_read_seed_strips_a_byte_order_mark(tmp_path):
    path = tmp_path / "seed.csv"
    path.write_text(f"Project Key,Source URL\n{KEY_A},{URL}\n", encoding="utf-8-sig")
    assert read_seed(path)[0][0] == KEY_A


def test_read_seed_without_a_header_uses_the_canonical_order(tmp_path):
    path = tmp_path / "seed.csv"
    line = ",".join(seed_line())
    path.write_text(line + "\n", encoding="utf-8")
    rows = read_seed(path)
    assert len(rows) == 1 and rows[0][0] == KEY_A


def test_read_seed_with_a_header_but_no_key_column_fails_loudly(tmp_path):
    path = tmp_path / "seed.csv"
    path.write_text("Name,Developer\nSample,Imaginary\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Project Key"):  # SeedFileError is still a ValueError
        read_seed(path)


def test_read_seed_without_a_key_column_raises_the_project_own_error_with_a_fixed_phrase(tmp_path):
    # The header cell and the file name carry a marker: the message names the column it wants, never what it found.
    path = tmp_path / "seed-MARKER-LEAK-file.csv"
    path.write_text("MARKER-LEAK-Column,Developer\nMARKER-LEAK Sample,Imaginary\n", encoding="utf-8")
    with pytest.raises(SeedFileError) as info:
        read_seed(path)
    assert str(info.value) == "the Listing_Data seed file has no 'Project Key' column in its header row"
    assert "MARKER-LEAK" not in str(info.value)


def test_short_rows_are_padded(tmp_path):
    path = tmp_path / "seed.csv"
    path.write_text(f"Project Key,Source URL,Handover\n{KEY_A},{URL}\n", encoding="utf-8")
    rows = read_seed(path)
    assert rows[0][LISTING_HEADERS.index("Handover")] == ""
    assert len(rows[0]) == len(LISTING_HEADERS)


# -- validation -----------------------------------------------------------
def test_seed_row_problem():
    assert seed_row_problem(seed_line()) is None
    assert "Project Key" in seed_row_problem(seed_line(key="1001"))
    assert "Project Key" in seed_row_problem(seed_line(key=""))
    assert "Source URL" in seed_row_problem(seed_line(url=""))
    assert "Source URL" in seed_row_problem(seed_line(url="ftp://example.com"))
    assert seed_row_problem([]) is not None  # an empty row is a problem, not an error


def test_check_seed_rows_names_every_bad_row():
    problems = check_seed_rows([seed_line(), seed_line(key="1001"), seed_line(url=""), seed_line(key=KEY_B)])
    assert len(problems) == 2
    assert problems[0].startswith("seed data row 2")
    assert problems[1].startswith("seed data row 3")
    assert check_seed_rows([]) == []


# -- applying the seed ----------------------------------------------------
def test_rows_to_add_only_adds_new_keys():
    seed = [seed_line(KEY_A), seed_line(KEY_B)]
    added = rows_to_add([KEY_A], seed)
    assert [r[0] for r in added] == [KEY_B]


def test_rows_to_add_never_touches_an_existing_key():
    # An analyst's row always outranks the seed, however the seed differs.
    seed = [seed_line(KEY_A, **{"Price From (AED)": "999"})]
    assert rows_to_add([KEY_A], seed) == []
    assert rows_to_add([f"  {KEY_A}  "], seed) == []


def test_rows_to_add_latest_seed_row_wins():
    seed = [seed_line(KEY_A, **{"Price From (AED)": "1M"}), seed_line(KEY_B), seed_line(KEY_A, **{"Price From (AED)": "2M"})]
    added = rows_to_add([], seed)
    assert [r[0] for r in added] == [KEY_A, KEY_B]  # first-seen key order
    assert added[0][LISTING_HEADERS.index("Price From (AED)")] == "2M"


def test_rows_to_add_drops_invalid_rows_and_says_so(caplog):
    seed = [seed_line(KEY_A), seed_line(key="1001"), seed_line(KEY_B, url="")]
    with caplog.at_level(logging.WARNING, logger="src.listing_data"):
        added = rows_to_add([], seed)
    assert [r[0] for r in added] == [KEY_A]
    assert len(caplog.messages) == 2


def test_rows_to_add_pads_short_rows():
    added = rows_to_add([], [[KEY_A] + [""] * 10 + [URL]])
    assert len(added[0]) == len(LISTING_HEADERS)
    assert added[0][LISTING_HEADERS.index("Source URL")] == URL


def test_rows_to_add_is_idempotent():
    seed = [seed_line(KEY_A), seed_line(KEY_B)]
    first = rows_to_add([], seed)
    second = rows_to_add([r[0] for r in first], seed)
    assert second == []


def test_an_empty_sheet_and_an_empty_seed():
    assert rows_to_add([], []) == []
    assert rows_to_add([None, "", "  "], [seed_line()]) != []


# ===========================================================================
# Regressions found by the independent review (all values SYNTHETIC)
# ===========================================================================
@pytest.mark.parametrize(
    "text", ["-1200 sq ft", "- 1200", "\u22121200", "\u20131200 sqft", "-110 sqm", "area: -650"]
)
def test_a_negative_area_is_refused_not_turned_positive(text):
    assert parse_area_sqft(text) is None


def test_a_hyphen_inside_a_word_is_not_a_minus_sign():
    assert parse_area_sqft("Built-up 1,200 sq ft") == 1200


@pytest.mark.parametrize("value, expected", [(1200.5, 1201), (1201.5, 1202), (1200.49, 1200), (0.5, 1)])
def test_a_float_area_rounds_half_up_like_the_text_path(value, expected):
    assert parse_area_sqft(value) == expected
    assert parse_area_sqft(f"{value} sq ft") == expected


@pytest.mark.parametrize("text", ["1" + "0" * 40 + " sq ft", "1" + "0" * 29 + " sqm", 1e30, 1e300, 10**40])
def test_a_huge_area_never_raises(text):
    result = parse_area_sqft(text)
    assert result is None or isinstance(result, int)


@pytest.mark.parametrize("text", ["1,250,000-", "850K -", "AED 850K-", "AED 1.2M \u2013"])
def test_a_range_with_its_end_missing_is_not_a_total_price(text):
    assert parse_price_aed(text) is None


def test_a_range_with_its_end_missing_is_not_a_price_per_sqft():
    assert parse_price_per_sqft_aed("1,800-") is None
    assert parse_price_per_sqft_aed("1,800") == 1800


@pytest.mark.parametrize(
    "url",
    ["http://[::1", "http://[bad]/x", "http://host:abc/", "http://host:99999/", "http://:80/", "http://user@/x"],
)
def test_a_malformed_url_cell_skips_its_row_and_does_not_crash_the_load(url):
    out = load_listing_records([record(**{"Source URL": url}), record(**{"Project Key": KEY_B})])
    assert list(out) == [KEY_B]
    assert seed_row_problem(seed_line(url=url)) is not None


def test_a_typed_but_invalid_key_is_reported_and_a_blank_one_is_not(caplog):
    rows = [record(**{"Project Key": "1001"}), record(**{"Project Key": "dubai-77"}), record(**{"Project Key": ""})]
    with caplog.at_level(logging.WARNING, logger="src.listing_data"):
        out = load_listing_records(rows)
    assert out == {}
    assert any("2 row(s) skipped for an invalid Project Key" in message for message in caplog.messages)


def test_a_blank_key_alone_is_silent(caplog):
    with caplog.at_level(logging.WARNING, logger="src.listing_data"):
        assert load_listing_records([record(**{"Project Key": ""}), record(**{"Project Key": None})]) == {}
    assert caplog.messages == []


def test_a_none_cell_in_a_seed_row_is_blank_not_the_text_none():
    row = seed_line()
    row[LISTING_HEADERS.index("Payment Plan")] = None
    added = rows_to_add([], [row])
    assert added[0][LISTING_HEADERS.index("Payment Plan")] == ""
    assert "None" not in added[0]
    assert "None" not in (seed_row_problem([None]) or "")


# ===========================================================================
# Bounds and dangling symbols (all values SYNTHETIC)
#
# A comparison symbol, or a plus / dash / slash left hanging beside the number, says "less than",
# "at least" or "a range with its end missing". The parsers used to skip over the symbol and publish
# the bare number ('<3 BR' came back as exactly 3, the opposite of what was typed), so the cell is None.
# ===========================================================================
@pytest.mark.parametrize(
    "text",
    [
        "> 3 BR",
        "<3 BR",
        "\u2265 2 BR",
        "\u2264 2 BR",
        ">=3",
        "<=3 BR",
        "=3",
        "\u22603 BR",
        "\u00b11 BR",
        "\uff1c3 BR",  # a full-width '<' folds to '<' under NFKC
        "Studio to <3 BR",
        "3 BR >",
        "3 BR /",
        "/3 BR",
        "3 /",
        "3 BR \u2212",  # a minus sign or a figure dash hanging after the count is a range with its end missing
        "3 BR \u2012",
        "\u22123 BR",
        ".5 BR",  # the dot is part of the number: not 5
        ".5",
        # Already refused before the fix; they are the contrast the report was written against.
        "3+",
        "3 BR+",
        "at least 2 BR",
        "3 BR -",
    ],
)
def test_a_bound_or_a_dangling_symbol_is_not_a_bedroom_count(text):
    assert parse_bedrooms(text) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Studio to 3BR", (0, 3)),
        ("1-3 BR", (1, 3)),
        ("1 - 3 BR", (1, 3)),
        ("1, 2 & 3 BR", (1, 3)),
        ("1, 2, and 3 BR", (1, 3)),
        ("3 BR + Maid", (3, 3)),
        ("3 BR / Maid", (3, 3)),  # a slash before the maid room goes with it
        ("3BR/Maid", (3, 3)),
        ("2 BR 1,200 sq ft", (2, 2)),
        ("2 BR, 110 sqm", (2, 2)),
        ("1/2/3 BR", (1, 3)),  # a slash BETWEEN two sizes is a list, not a dangling symbol
        ("1\u20133 BR", (1, 3)),
        ("1\u20143 BR", (1, 3)),
        ("1\u22123 BR", (1, 3)),
        ("3 BR,", (3, 3)),
    ],
)
def test_the_bedroom_forms_next_to_the_refused_ones_still_parse(text, expected):
    assert parse_bedrooms(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "< 1200 sq ft",
        "> 5000 sq ft",
        "\u2264 1,500 sq ft",
        "\u2265 900",
        ">= 900 sq ft",
        "=1200",
        "\u22601200",
        "\u00b150 sq ft",
        "\uff1e 5000 sq ft",  # full-width '>'
        "1,200+ sq ft",
        "1,200 +",
        "1,200\uff0b sq ft",  # full-width '+'
        "1,200 -",
        "1,200 sq ft -",
        "1,200 \u2212",
        "1,200 sq ft \u2013",
        "1,200-sq ft",
        "1,200 /",
        "1,200 / sq ft",
        "1,200 \\ sq ft",
        "1,200 sq ft &",
        "1,200 sq ft |",
        "110 sqm +",
        # Already refused before the fix.
        "up to 1,500 sq ft",
        "1,200 - 1,500 sq ft",
        "-1200 sq ft",
    ],
)
def test_a_bound_or_a_dangling_symbol_is_not_an_area(text):
    assert parse_area_sqft(text) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("from 1,200 sq ft", 1200),
        ("1,200 sq.ft.", 1200),
        ("1,200 square feet", 1200),
        ("1200", 1200),
        ("1,200.4 sq ft", 1200),
        ("Built-up 1,200 sq ft", 1200),  # a hyphen inside a word is not a dash beside the number
        ("built-up area: 1,200 sq ft", 1200),
        ("BUA: 1,200 sq ft", 1200),
        ("~1,200 sq ft", 1200),
        ("1,200 sq ft*", 1200),
        ("approx. 1,200 sq ft", 1200),
        ("1,200 sq ft (approx)", 1200),
        ("110 sq m", 1184),
        ("110 sqm", 1184),
    ],
)
def test_the_area_forms_next_to_the_refused_ones_still_parse(text, expected):
    assert parse_area_sqft(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "< 1,250,000",
        "> 1,250,000",
        "\u2264 1,250,000",
        "\u2265 1.2M",
        "= 1.2M",
        "\u2260 1.2M",
        "\u00b1 1.2M",
        "1.2M \u00b1",
        "<= 850K",
        ">= AED 1.2M",
        "1.2M <",
    ],
)
def test_a_bound_is_not_a_total_price(text):
    assert parse_price_aed(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "<1,800",
        "> 1,800 psf",
        "\u2264 1,800",
        "\u2265 1,800/sq ft",
        "= 1,800",
        "1,800 psf <",
        "1,800 \u00b1 50",
        "1,800 per sq ft >=",
        "AED 1,800/sq ft \u2264",
    ],
)
def test_a_bound_is_not_a_price_per_sqft(text):
    assert parse_price_per_sqft_aed(text) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1,250,000+", 1_250_000),
        ("AED 1.2M+", 1_200_000),
        ("AED 850,000 onwards", 850_000),
        ("from AED 850,000", 850_000),
        ("~1.2M", 1_200_000),
        ("*1.2M", 1_200_000),
    ],
)
def test_an_open_ended_total_price_is_still_read_as_a_from_price(text, expected):
    # The column is Price From, so "or more" is exactly what it holds: unlike a bound, this is kept.
    assert parse_price_aed(text) == expected


def test_a_per_sqft_price_keeps_its_approximation_marker_and_unit():
    assert parse_price_per_sqft_aed("~1,800") == 1800
    assert parse_price_per_sqft_aed("AED 1,800/sq ft") == 1800


def test_bounds_in_a_listing_row_leave_blank_cells_and_a_count_only_log(caplog):
    row = record(
        **{
            "Bedrooms Min": "<3 BR",
            "Bedrooms Max": ">= 5 Hidden-Marker",
            "Area From (sq ft)": "< 1200 sq ft",
            "Area To (sq ft)": "1,500 sq ft +",
            "Price From (AED)": "< 1,250,000",
        }
    )
    with caplog.at_level(logging.WARNING, logger="src.listing_data"):
        out = load_listing_records([row])[KEY_A]
    assert (out["bedrooms_min"], out["bedrooms_max"], out["area_from"], out["area_to"], out["price_from"]) == (None,) * 5
    refusals = [m for m in caplog.messages if "refused" in m]
    assert len(refusals) == 1 and refusals[0].startswith("Listing_Data: 5 ")
    assert "Hidden-Marker" not in caplog.text and "1,250,000" not in caplog.text


# -- the seed log carries no cell text ------------------------------------
def test_a_rejected_seed_rows_problem_never_echoes_its_cells(caplog):
    # SYNTHETIC. A Project Key is a developer name plus a project name, and the Actions log is public.
    bad_key = "Imaginary Developer / Hidden Gardens"
    bad_url = "ftp://hidden.example/private-path"
    seed = [
        seed_line(key=bad_key),
        seed_line(KEY_B, url=bad_url),
        seed_line(key="", url=bad_url),
        seed_line(KEY_A),
    ]
    problems = check_seed_rows(seed)
    with caplog.at_level(logging.WARNING, logger="src.listing_data"):
        added = rows_to_add([], seed)
    assert [row[0] for row in added] == [KEY_A]

    assert problems == [
        "seed data row 1: Project Key is not a valid namespaced key",
        "seed data row 2: Source URL is required and must be an http(s) URL",
        "seed data row 3: Project Key is not a valid namespaced key",
    ]
    assert caplog.messages == [
        "seed data row 1 skipped: Project Key is not a valid namespaced key",
        "seed data row 2 skipped: Source URL is required and must be an http(s) URL",
        "seed data row 3 skipped: Project Key is not a valid namespaced key",
    ]
    published = problems + caplog.messages + [seed_row_problem(row) for row in seed]
    for text in published:
        for fragment in (bad_key, bad_url, "Imaginary", "Hidden", "hidden.example", "private-path", "ftp:"):
            assert fragment not in (text or "")
