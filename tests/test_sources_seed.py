"""
Tests for src/sources/seed_csv.py and for the two header-only seed files that ship with
the repository. All CSV content is built here as SYNTHETIC text (invented names, example
domains); nothing real is committed. No network.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import pytest

from src import listing_data
from src.config import DATA_DIR
from src.sources import seed_csv
from src.sources.base import SourceError, check_row_count
from src.sources.seed_csv import SeedCsvAdapter
from test_sources_support import TODAY, TODAY_ISO, make_cfg

HEADER = "Emirate,Project Name (EN),Developer,Master Developer,Master Project,Area (EN),Zone,Status,Completion Date,Source URL"
GOOD_A = "Dubai,Test Seed Tower A,Example Developer LLC,Example Master Developer LLC,Test Master Community,Test Area One,Test Zone,Under construction,30-06-2027,https://seed.example.com/a"
GOOD_B = "Sharjah,Test Seed Court,Example Developer Two LLC,,,Test Sharjah Area,,Planned,2028-01-15,https://seed.example.org/court"

# The Actions log of a public repository is public. A hand-edited file can lose its header row, so its first row
# can be a DATA row, and a header cell is the file's text: neither is printed. SYNTHETIC marker, not secret-shaped.
MARKER = "ZZROWZZ"


def write(tmp_path: Path, *lines: str, header: str = HEADER, newline: str = "\n", bom: bool = False) -> Path:
    text = newline.join([header, *lines]) + newline
    path = tmp_path / "seed_registry.csv"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return path


def fetch(path: Path):
    return SeedCsvAdapter(path=path, today=TODAY).fetch()


def fetch_fails(path: Path) -> str:
    with pytest.raises(SourceError) as info:
        SeedCsvAdapter(path=path, today=TODAY).fetch()
    return str(info.value)


def row_for(emirate="Dubai", name="Test Seed Tower", developer="Example Developer LLC", url="https://seed.example.com/x", *, area="", status="", date=""):
    """A CSV line in HEADER order, with the cells the tests care about as keywords."""
    values = {
        "Emirate": emirate, "Project Name (EN)": name, "Developer": developer, "Master Developer": "",
        "Master Project": "", "Area (EN)": area, "Zone": "", "Status": status, "Completion Date": date, "Source URL": url,
    }
    return ",".join(csv_quote(values[h]) for h in HEADER.split(","))


def csv_quote(cell: str) -> str:
    return '"' + cell.replace('"', '""') + '"' if any(c in cell for c in ',"\n') else cell


# ---------------------------------------------------------------------------
# The files that ship with the repository
# ---------------------------------------------------------------------------
def test_the_shipped_registry_seed_is_header_only_and_reads_as_zero_rows():
    shipped = DATA_DIR / "seed_registry.csv"
    assert shipped.read_text(encoding="utf-8").splitlines() == [HEADER]
    result = SeedCsvAdapter(today=TODAY).fetch()
    assert result.rows == [] and result.candidates == [] and result.source == "seed_csv"
    assert result.notes == ["seed_registry.csv has no rows (header only)"]
    check_row_count("seed_csv", 0, None, expects_rows=False, allow_drop=False)


def test_the_default_path_is_the_shipped_file():
    assert seed_csv.SEED_REGISTRY_FILE == DATA_DIR / "seed_registry.csv"


def test_the_shipped_listing_seed_has_exactly_the_listing_headers_and_no_rows():
    shipped = DATA_DIR / "seed_projects.csv"
    assert listing_data.SEED_FILE == shipped
    with open(shipped, newline="", encoding="utf-8") as handle:
        assert list(csv.reader(handle)) == [list(listing_data.LISTING_HEADERS)]
    assert listing_data.read_seed(shipped) == []
    assert listing_data.read_seed() == []


# ---------------------------------------------------------------------------
# Reading good rows
# ---------------------------------------------------------------------------
def test_good_rows_become_seed_registry_rows(tmp_path):
    result = fetch(write(tmp_path, GOOD_A, GOOD_B))
    a, b = result.rows
    assert (a.emirate, a.registry, a.registry_project_no) == ("Dubai", "SEED", None)
    assert a.project_name_en == "Test Seed Tower A" and a.developer == "Example Developer LLC"
    assert (a.master_developer, a.master_project) == ("Example Master Developer LLC", "Test Master Community")
    assert (a.area_en, a.zone, a.status) == ("Test Area One", "Test Zone", "Under construction")
    assert a.completion_date == "2027-06-30"  # day-first
    assert a.source_dataset == "https://seed.example.com/a"
    assert a.project_key == "name:dubai:example-developer-llc:test-seed-tower-a" and a.registry_key() is None
    assert (b.emirate, b.completion_date, b.master_project) == ("Sharjah", "2028-01-15", "")


def test_rows_are_stamped_with_today_and_have_no_invented_refresh_date(tmp_path):
    result = fetch(write(tmp_path, GOOD_A))
    assert result.rows[0].first_seen == TODAY_ISO and result.rows[0].last_seen == TODAY_ISO
    assert result.rows[0].source_refreshed == "" and result.source_refreshed is None
    assert result.notes[0] == "read 1 data row(s) from seed_registry.csv"


def test_without_a_given_date_rows_are_stamped_with_the_utc_date(tmp_path):
    (row,) = SeedCsvAdapter(path=write(tmp_path, GOOD_A)).fetch().rows
    assert len(row.first_seen) == 10 and row.first_seen == row.last_seen


def test_rows_survive_the_sheet_round_trip(tmp_path):
    from src.models import RegistryRow

    (row,) = fetch(write(tmp_path, GOOD_A)).rows
    again = RegistryRow.from_sheet_dict(row.to_sheet_dict())
    assert again.project_key == row.project_key and again.source_dataset == row.source_dataset


def test_rows_come_back_in_file_order(tmp_path):
    lines = [row_for(name=f"Test Seed {n}") for n in range(30, 0, -1)]
    names = [r.project_name_en for r in fetch(write(tmp_path, *lines)).rows]
    assert names == [f"Test Seed {n}" for n in range(30, 0, -1)]


def test_a_byte_order_mark_and_windows_line_endings_are_fine(tmp_path):
    result = fetch(write(tmp_path, GOOD_A, GOOD_B, newline="\r\n", bom=True))
    assert [r.emirate for r in result.rows] == ["Dubai", "Sharjah"]


def test_columns_are_found_by_name_in_any_order_and_case(tmp_path):
    header = "source url, DEVELOPER ,Project Name (en),emirate"
    path = write(tmp_path, "https://seed.example.com/a,Example Developer LLC,Test Seed Tower A,Dubai", header=header)
    (row,) = fetch(path).rows
    assert (row.emirate, row.project_name_en, row.developer) == ("Dubai", "Test Seed Tower A", "Example Developer LLC")


def test_extra_columns_are_ignored_and_counted_but_their_text_is_not_reported(tmp_path):
    path = write(tmp_path, GOOD_A + ",kept?,x", header=HEADER + ",Notes,Latitude")
    result = fetch(path)
    assert len(result.rows) == 1 and result.rows[0].latitude is None
    assert "ignored 2 column(s) not in the seed layout" in result.notes
    assert not any("Notes" in n or "Latitude" in n for n in result.notes)


def test_the_text_of_an_ignored_header_cell_is_never_reported(tmp_path):
    # The Actions log of a public repository is public, and a header cell is the file's text. SYNTHETIC marker.
    path = write(tmp_path, GOOD_A + ",x", header=HEADER + f",{MARKER} Fake Notes")
    result = fetch(path)
    assert "ignored 1 column(s) not in the seed layout" in result.notes
    assert MARKER not in " ".join(result.notes) and "Fake Notes" not in " ".join(result.notes)


def test_a_misspelt_optional_header_is_noticed_by_the_layout_column_the_file_lacks(tmp_path):
    result = fetch(write(tmp_path, GOOD_A, header=HEADER.replace("Zone", "Zon")))
    (note,) = [n for n in result.notes if n.startswith("ignored")]
    assert note == "ignored 1 column(s) not in the seed layout; layout column(s) not found in the file: Zone"
    assert len(result.rows) == 1  # the optional column is simply absent


def test_a_cell_is_tidied_and_unicode_is_kept(tmp_path):
    line = row_for(name="  Café   Résidences ☆ ", developer="Zürich Developer LLC", area="مجمع الاختبار")
    (row,) = fetch(write(tmp_path, line)).rows
    assert row.project_name_en == "Café Résidences ☆" and row.area_en == "مجمع الاختبار"
    assert row.project_key == "name:dubai:zurich-developer-llc:cafe-residences"


def test_quoted_cells_may_contain_commas_and_newlines(tmp_path):
    line = row_for(name="Test Seed, Phase 2", status="line one\nline two")
    (row,) = fetch(write(tmp_path, line)).rows
    assert row.project_name_en == "Test Seed, Phase 2" and row.status == "line one line two"


def test_blank_lines_and_blank_rows_are_skipped_silently(tmp_path):
    path = write(tmp_path, "", GOOD_A, ",,,,,,,,,", "   ", GOOD_B)
    result = fetch(path)
    assert len(result.rows) == 2 and result.notes[0] == "read 2 data row(s) from seed_registry.csv"


def test_tower_letters_keep_projects_apart(tmp_path):
    result = fetch(write(tmp_path, row_for(name="Test Seed Tower A"), row_for(name="Test Seed Tower B")))
    assert len({r.project_key for r in result.rows}) == 2


def test_the_same_project_in_two_emirates_is_two_projects(tmp_path):
    result = fetch(write(tmp_path, row_for("Dubai"), row_for("Sharjah")))
    assert len({r.project_key for r in result.rows}) == 2


# ---------------------------------------------------------------------------
# Rows that are refused
# ---------------------------------------------------------------------------
def drop_count(result, reason):
    return next((n for n in result.notes if reason in n), "")


@pytest.mark.parametrize("emirate", ["dubai", "DUBAI", "Dubai Emirate", " Sharjah ", "sharjah"])
def test_emirate_spellings_are_normalised(tmp_path, emirate):
    (row,) = fetch(write(tmp_path, row_for(emirate))).rows
    assert row.emirate in ("Dubai", "Sharjah")


def test_rows_outside_dubai_and_sharjah_are_dropped_and_counted_in_the_notes(tmp_path):
    path = write(
        tmp_path, GOOD_A, GOOD_B, row_for(name="Test Seed Third"),
        row_for("Abu Dhabi", name="Test Seed Out"), row_for("Ajman", name="Test Seed Out2"), row_for("", name="No Emirate"),
    )
    result = fetch(path)
    assert [r.project_name_en for r in result.rows] == ["Test Seed Tower A", "Test Seed Court", "Test Seed Third"]
    assert f"{seed_csv.R_EMIRATE} (x3)" in " ".join(result.notes)
    assert "dropped 3 row(s)" in " ".join(result.notes)


def test_a_file_that_is_mostly_other_emirates_is_the_wrong_file_and_fails_loudly(tmp_path):
    path = write(tmp_path, GOOD_A, row_for("Abu Dhabi", name="Test Seed Out"), row_for("Ajman", name="Test Seed Out2"))
    assert seed_csv.R_EMIRATE in fetch_fails(path)


@pytest.mark.parametrize(
    "url",
    ["", "n/a", "ftp://seed.example.com/a", "javascript:alert(1)", "www.seed.example.com/a", "http://", "https://", "https://seed .example.com", "//seed.example.com/a", "file:///etc/hosts", "https://host:notaport/x"],
)
def test_a_row_needs_a_real_http_source_url(tmp_path, url):
    result = fetch(write(tmp_path, GOOD_A, row_for(name="Test Seed Bad", url=url)))
    assert len(result.rows) == 1
    assert f"{seed_csv.R_NO_URL} (x1)" in " ".join(result.notes)


@pytest.mark.parametrize(
    "url",
    ["https://user:SEEDPW@seed.example.com/a", "https://seed.example.com/a?api_key=SEEDPW", "https://seed.example.com/a?token=SEEDPW&x=1"],
)
def test_a_source_url_that_carries_credentials_is_refused_and_never_echoed(tmp_path, url, caplog):
    with caplog.at_level(logging.DEBUG):
        result = fetch(write(tmp_path, GOOD_A, row_for(name="Test Seed Leaky", url=url)))
    assert [r.project_name_en for r in result.rows] == ["Test Seed Tower A"]
    assert f"{seed_csv.R_URL_CREDENTIALS} (x1)" in " ".join(result.notes)
    for text in (caplog.text, " ".join(result.notes), repr(result.rows)):
        assert "SEEDPW" not in text


@pytest.mark.parametrize("developer", ["", "n/a", "-", "TBC"])
def test_a_row_without_a_developer_cannot_be_identified(tmp_path, developer):
    result = fetch(write(tmp_path, GOOD_A, row_for(name="Test Seed Orphan", developer=developer)))
    assert len(result.rows) == 1 and f"{seed_csv.R_NO_IDENTITY} (x1)" in " ".join(result.notes)


def test_an_arabic_only_name_has_no_latin_identity(tmp_path):
    result = fetch(write(tmp_path, GOOD_A, row_for(name="مشروع اختبار")))
    assert len(result.rows) == 1 and f"{seed_csv.R_NO_IDENTITY} (x1)" in " ".join(result.notes)


def test_the_no_identity_reason_is_a_static_text_that_names_every_refused_kind_of_name():
    reason = seed_csv.R_NO_IDENTITY
    assert reason.startswith("no usable identity (") and "Developer" in reason and "project name" in reason
    assert "Latin letter" in reason and "another script" in reason
    assert all(symbol in reason for symbol in ("+", "#", "@"))


@pytest.mark.parametrize(
    "name, developer",
    [
        ("Test Seed Tower A+", "Example Developer LLC"),
        ("Test Seed C#", "Example Developer LLC"),
        ("Test Seed @ Marina", "Example Developer LLC"),
        ("Test Seed T\u00f8wer", "Example Developer LLC"),  # o-stroke does not fold to an ASCII letter
        ("Test Seed Tower \u0928", "Example Developer LLC"),  # a Devanagari letter survives folding, so it could not key
        ("Test Seed Tower", "Example Developer+ LLC"),
        ("Test Seed Tower", "Example D\u00f8veloper LLC"),
    ],
)
def test_a_name_with_a_symbol_or_a_letter_it_cannot_key_is_dropped_with_the_static_reason(tmp_path, caplog, name, developer):
    with caplog.at_level(logging.DEBUG):
        result = fetch(write(tmp_path, GOOD_A, row_for(name=name, developer=developer)))
    assert [r.project_name_en for r in result.rows] == ["Test Seed Tower A"]
    notes = " ".join(result.notes)
    assert f"{seed_csv.R_NO_IDENTITY} (x1)" in notes
    for text in (notes, caplog.text):  # the reason is static: no cell value is echoed
        assert name not in text and developer not in text


@pytest.mark.parametrize("name", ["", "TBC", "n/a", "-", "   "])
def test_a_row_without_a_name_is_dropped(tmp_path, name):
    result = fetch(write(tmp_path, GOOD_A, row_for(name=name)))
    assert len(result.rows) == 1 and f"{seed_csv.R_NO_NAME} (x1)" in " ".join(result.notes)


def test_huge_values_drop_the_row(tmp_path):
    long_url = "https://seed.example.com/" + "a" * 2001
    path = write(tmp_path, GOOD_A, GOOD_B, GOOD_B.replace("Court", "Court 2"), row_for(name="x" * 501), row_for(name="Test Seed Y", area="z" * 10_000), row_for(name="Test Seed U", url=long_url))
    result = fetch(path)
    assert len(result.rows) == 3 and f"{seed_csv.R_OVERSIZE} (x3)" in " ".join(result.notes)


def test_a_value_at_the_length_limit_is_kept(tmp_path):
    (row,) = fetch(write(tmp_path, row_for(name="Test " + "x" * 495))).rows
    assert len(row.project_name_en) == 500


def test_a_row_with_extra_cells_is_dropped_but_trailing_empty_cells_are_fine(tmp_path):
    shifted = GOOD_A.replace("Test Seed Tower A", "Test Seed, Tower A")  # an unquoted comma shifts every column
    result = fetch(write(tmp_path, GOOD_B, shifted, GOOD_A + ",,,"))
    assert [r.emirate for r in result.rows] == ["Sharjah", "Dubai"]
    assert f"{seed_csv.R_TOO_MANY_CELLS} (x1)" in " ".join(result.notes)


def test_a_half_written_row_is_dropped_not_padded_into_a_project(tmp_path):
    cut = ",".join(GOOD_A.split(",")[:8])  # the file ends mid-row: no date, no source URL
    result = fetch(write(tmp_path, GOOD_B, GOOD_B.replace("Court", "Court 2"), cut))
    assert len(result.rows) == 2 and f"{seed_csv.R_NO_URL} (x1)" in " ".join(result.notes)


def test_a_short_row_missing_only_optional_columns_is_fine(tmp_path):
    header = "Emirate,Project Name (EN),Developer,Source URL,Status"
    path = write(tmp_path, "Dubai,Test Seed Tower A,Example Developer LLC,https://seed.example.com/a", header=header)
    (row,) = fetch(path).rows
    assert row.status == ""


@pytest.mark.parametrize("date_text", ["Q4 2027", "soon", "2027-02-30", "3000-01-01", "13-13-2027"])
def test_a_completion_date_that_is_not_a_date_is_blanked_and_counted(tmp_path, date_text):
    result = fetch(write(tmp_path, row_for(date=date_text)))
    assert result.rows[0].completion_date == "" and seed_csv.A_DATE in " ".join(result.notes)


@pytest.mark.parametrize("date_text,expected", [("", ""), ("n/a", ""), ("TBC", ""), ("2027-06-30", "2027-06-30"), ("30/06/2027", "2027-06-30"), ("30-06-2027", "2027-06-30")])
def test_blank_and_placeholder_dates_are_blank_without_a_note(tmp_path, date_text, expected):
    result = fetch(write(tmp_path, row_for(date=date_text)))
    assert result.rows[0].completion_date == expected
    if not expected:
        assert seed_csv.A_DATE not in " ".join(result.notes)


def test_the_later_row_wins_for_a_repeated_project_and_keeps_the_earlier_position(tmp_path):
    path = write(
        tmp_path,
        row_for(name="Test Seed One", status="old"),
        row_for(name="Test Seed Two"),
        row_for(name="TEST  seed one", status="corrected"),
    )
    result = fetch(path)
    assert [r.status for r in result.rows] == ["corrected", ""]
    assert [r.project_name_en for r in result.rows] == ["TEST seed one", "Test Seed Two"]
    assert f"{seed_csv.R_DUPLICATE} (x1)" in " ".join(result.notes)


# ---------------------------------------------------------------------------
# How much dropping is too much
# ---------------------------------------------------------------------------
def test_when_every_row_is_unusable_the_run_fails(tmp_path):
    message = fetch_fails(write(tmp_path, row_for(url=""), row_for(url="", name="Test Seed Two")))
    assert "none of the 2 row(s)" in message and seed_csv.R_NO_URL in message


def test_exactly_half_unusable_is_tolerated_but_more_is_not(tmp_path):
    bad = row_for(name="Test Seed Bad", url="")
    assert len(fetch(write(tmp_path, GOOD_A, GOOD_B, bad, bad)).rows) == 2
    message = fetch_fails(write(tmp_path, GOOD_A, GOOD_B, bad, bad, bad))
    assert "3 of 5 row(s)" in message


def test_corrections_do_not_count_as_unusable_rows(tmp_path):
    lines = [row_for(name="Test Seed One", status=str(n)) for n in range(6)]
    result = fetch(write(tmp_path, *lines))
    assert len(result.rows) == 1 and result.rows[0].status == "5"


# ---------------------------------------------------------------------------
# Files that cannot be used
# ---------------------------------------------------------------------------
def test_a_missing_file_is_an_error_not_an_empty_seed(tmp_path):
    assert "missing" in fetch_fails(tmp_path / "nope.csv")


def test_a_directory_is_not_a_seed_file(tmp_path):
    assert "missing" in fetch_fails(tmp_path)


@pytest.mark.parametrize("content", [b"", b"\n\n  \n", b"\xef\xbb\xbf", b",,,\n"])
def test_a_file_with_no_header_is_an_error(tmp_path, content):
    path = tmp_path / "seed_registry.csv"
    path.write_bytes(content)
    assert "empty" in fetch_fails(path)


def test_bytes_that_are_not_utf8_are_an_error(tmp_path):
    path = tmp_path / "seed_registry.csv"
    path.write_bytes(HEADER.encode() + b"\nDubai,Caf\xe9,Dev,,,,,,,https://seed.example.com/a\n")
    assert "not valid UTF-8" in fetch_fails(path)


@pytest.mark.parametrize("missing", ["Emirate", "Project Name (EN)", "Developer", "Source URL"])
def test_a_header_without_a_required_column_is_refused_naming_it(tmp_path, missing):
    header = ",".join(h for h in HEADER.split(",") if h != missing)
    message = fetch_fails(write(tmp_path, "Dubai,x,y", header=header))
    assert f"no column for: {missing}." in message
    assert "The first row has 9 column(s); its text is not shown" in message
    for other in HEADER.split(","):  # the other column names are not echoed back either
        assert other == missing or other not in message


def test_a_file_saved_without_its_header_row_does_not_print_its_first_row(tmp_path):
    first = f"Dubai,{MARKER} Fake Tower 7,{MARKER} Fake Developer LLC,,,{MARKER} Fake Area,,Planned,2028-01-15,https://seed.example.com/{MARKER}"
    path = write(tmp_path, row_for(name="Fake Tower 8"), header=first)
    message = fetch_fails(path)
    assert "no column for: Emirate, Project Name (EN), Developer, Source URL." in message
    assert "The first row has 10 column(s); its text is not shown" in message
    assert MARKER not in message and "Fake Tower" not in message and "seed.example.com" not in message


def test_a_headerless_file_with_an_odd_first_row_is_counted_by_cells_not_shown(tmp_path):
    path = tmp_path / "seed_registry.csv"
    path.write_text(f'{MARKER},"{MARKER}, with a comma",{MARKER}\n{HEADER}\n', encoding="utf-8")
    message = fetch_fails(path)
    assert "The first row has 3 column(s); its text is not shown" in message and MARKER not in message


def test_a_misspelt_required_header_is_not_guessed(tmp_path):
    message = fetch_fails(write(tmp_path, GOOD_A, header=HEADER.replace("Developer,", "Developper,", 1)))
    assert "no column for: Developer." in message
    assert "Developper" not in message  # the file's own text is not echoed: the missing name is enough to find the typo


def test_a_header_that_names_a_column_twice_is_refused(tmp_path):
    message = fetch_fails(write(tmp_path, GOOD_A, header=HEADER + ",developer"))
    assert "twice" in message


def test_a_data_row_in_place_of_a_header_is_refused(tmp_path):
    path = tmp_path / "seed_registry.csv"
    path.write_text(GOOD_A + "\n", encoding="utf-8")
    assert "header has no column" in fetch_fails(path)


def test_an_oversized_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(seed_csv, "MAX_SEED_BYTES", 100)
    assert "byte limit" in fetch_fails(write(tmp_path, GOOD_A, GOOD_B))


def test_a_runaway_field_is_a_csv_error_not_a_crash(tmp_path):
    path = write(tmp_path, row_for(name="x" * 200_000))
    assert "not valid CSV" in fetch_fails(path)


def test_a_nul_byte_never_crashes_the_adapter(tmp_path):
    path = tmp_path / "seed_registry.csv"
    path.write_bytes((HEADER + "\n" + GOOD_A.replace("Test Area One", "Test\x00Area") + "\n").encode("utf-8"))
    try:
        result = SeedCsvAdapter(path=path, today=TODAY).fetch()
    except SourceError:
        return
    assert len(result.rows) == 1


def test_the_error_names_the_file_but_never_its_content(tmp_path):
    path = tmp_path / "seed_registry.csv"
    path.write_bytes(b"\xff\xfePRIVATE-CONTENT")
    message = fetch_fails(path)
    assert "seed_registry.csv" in message and "PRIVATE-CONTENT" not in message and str(tmp_path) not in message


# ---------------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------------
def test_the_seed_is_always_on_and_quiet_zero_rows_are_normal():
    adapter = SeedCsvAdapter()
    assert adapter.name == "seed_csv" and adapter.expects_rows is False
    for cfg in (make_cfg(), make_cfg(ENABLE_SRERD="true", ENABLE_GEMINI_NEWS="true")):
        assert adapter.enabled(cfg) is True and adapter.skip_reason(cfg) == ""


def test_dropped_rows_are_logged_as_counts_not_content(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        fetch(write(tmp_path, GOOD_A, GOOD_B, row_for(name="PRIVATE-NAME", url="")))
    assert "PRIVATE-NAME" not in caplog.text and seed_csv.R_NO_URL in caplog.text
