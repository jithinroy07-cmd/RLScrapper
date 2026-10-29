"""
Tests for src/sources/dld_projects.py. The live Data.Dubai API has never been called;
everything here runs against SYNTHETIC fixtures and a scripted fake session. No network,
no credentials (the key and secret are obviously fake strings).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
import requests

from src.sources import base
from src.sources import dld_projects as dld
from src.sources.base import RowTally, SourceError
from src.sources.dld_projects import DldProjectsAdapter, check_schema, parse_dld_records
from test_sources_support import (
    ALL_FAKE_SECRETS,
    FAKE_KEY,
    FAKE_SECRET,
    FAKE_TOKEN,
    TODAY,
    TODAY_ISO,
    FakeResponse,
    FakeSession,
    Sleeps,
    assert_no_secret,
    dld_cfg,
    full_text,
    load_fixture,
    make_cfg,
    page,
    token_ok,
)


def fixture_records():
    return load_fixture("dld_page1.json")["data"] + load_fixture("dld_page2.json")["data"]


def dld_row(number, **extra):
    return {"PROJECT_NUMBER": number, "PROJECT_NAME": f"Test Tower {number}", "DEVELOPER_NAME": "Example Developer LLC", **extra}


def make_adapter(session, *, cfg=None, sleeps=None, **kwargs):
    kwargs.setdefault("today", TODAY)
    return DldProjectsAdapter(cfg or dld_cfg(), session=session, sleep=sleeps if sleeps is not None else Sleeps(), **kwargs)


def fetch_fails(session, *, cfg=None, **kwargs):
    adapter = make_adapter(session, cfg=cfg, **kwargs)
    with pytest.raises(SourceError) as info:
        adapter.fetch()
    return info.value


# ---------------------------------------------------------------------------
# Mapping the (synthetic) register rows
# ---------------------------------------------------------------------------
def test_the_fixture_rows_map_to_dubai_dld_rows():
    tally = RowTally()
    rows = parse_dld_records(fixture_records(), today=TODAY, source_refreshed="2026-10-20", tally=tally)
    assert [r.registry_project_no for r in rows] == ["1001", "1002", "1003", None, "1006"]
    assert {r.emirate for r in rows} == {"Dubai"} and {r.registry for r in rows} == {"DLD"}
    first = rows[0]
    assert first.project_name_en == "Test Residences A" and first.developer == "Example Developer LLC"
    assert first.master_developer == "Example Master Developer LLC" and first.master_project == "Test Master Community"
    assert (first.area_en, first.area_ar, first.zone) == ("Test Area One", "منطقة اختبار", "Test Zone")
    assert first.status == "ACTIVE" and first.percent_completed == 45.5
    assert first.escrow_agent == "Example Escrow Bank"
    assert (first.units, first.buildings, first.villas) == (320, 2, 0)  # zero villas is a real value, not blank
    assert first.project_key == "dld:dubai:1001"


def test_stamps_and_source_fields_are_set_on_every_row():
    rows = parse_dld_records(fixture_records(), today=TODAY, source_refreshed="2026-10-20")
    for row in rows:
        assert row.first_seen == TODAY_ISO and row.last_seen == TODAY_ISO
        assert row.source_dataset == dld.DLD_PROJECTS_URL
        assert row.source_refreshed == "2026-10-20"


def test_dates_are_read_day_first_and_the_actual_completion_wins_over_the_planned_end():
    rows = {r.registry_project_no: r for r in parse_dld_records(fixture_records(), today=TODAY)}
    assert rows["1001"].start_date == "2024-03-01"  # 01-03-2024 is 1 March
    assert rows["1001"].completion_date == "2027-06-30"  # planned end date, no actual completion
    assert rows["1006"].completion_date == "2026-09-15"  # actual completion beats the end date 2028-01-15


def test_no_date_sentinels_become_blank_not_a_fake_date():
    tally = RowTally()
    rows = parse_dld_records(fixture_records(), today=TODAY, tally=tally)
    villas = next(r for r in rows if r.project_name_en == "Test Villas C")
    assert villas.completion_date == "" and villas.cancellation_date == ""
    assert tally.adjusted_count("epoch-zero date read as blank") == 1


def test_numeric_strings_with_thousands_commas_are_counts_and_a_percent_string_is_a_percent():
    row = next(r for r in parse_dld_records(fixture_records(), today=TODAY) if r.registry_project_no == "1002")
    assert row.units == 1250 and row.percent_completed == 12.0


def test_tower_letters_stay_part_of_the_identity():
    rows = {r.project_name_en: r for r in parse_dld_records(fixture_records(), today=TODAY)}
    assert rows["Test Residences A"].project_key != rows["Test Residences B"].project_key


def test_an_arabic_only_name_goes_to_the_arabic_column_and_the_row_is_keyed_by_its_number():
    row = next(r for r in parse_dld_records(fixture_records(), today=TODAY) if r.registry_project_no == "1003")
    assert row.project_name_ar == "مشروع اختبار" and row.project_name_en == ""
    assert row.project_key == "dld:dubai:1003"


def test_a_row_with_no_valid_number_is_keyed_by_developer_and_name():
    tally = RowTally()
    rows = parse_dld_records(fixture_records(), today=TODAY, tally=tally)
    row = next(r for r in rows if r.project_name_en == "Test Villas C")
    assert row.registry_project_no is None
    assert row.project_key == "name:dubai:example-developer-two-llc:test-villas-c"
    assert tally.adjusted_count(dld.A_NAME_KEY) == 1


def test_a_row_that_cannot_be_identified_is_dropped_and_counted():
    tally = RowTally()
    rows = parse_dld_records(fixture_records(), today=TODAY, tally=tally)
    assert len(rows) == 5 and "Test Orphan Tower" not in {r.project_name_en for r in rows}
    assert tally.dropped_count(dld.R_NO_IDENTITY) == 1


def test_the_no_identity_reason_is_static_text_that_states_the_usable_name_rule():
    reason = dld.R_NO_IDENTITY
    assert reason.startswith("no usable identity (no valid project number, and ")  # the first clause is kept
    assert "developer" in reason and "project name" in reason
    assert "Latin letter" in reason and "another script" in reason
    assert all(symbol in reason for symbol in ("+", "#", "@"))


@pytest.mark.parametrize(
    "developer, name",
    [
        ("Example Developer+ LLC", "Test Tower"),
        ("Example Developer LLC", "Test C#"),
        ("Example Developer LLC", "Test @ Marina"),
        ("Example Døveloper LLC", "Test Tower"),  # o-stroke does not fold to an ASCII letter
        ("Example Developer LLC", "Test Tower न"),  # a Devanagari letter survives folding, so it could not key
        ("", "Test Tower"),
    ],
)
def test_a_name_that_cannot_key_is_dropped_with_the_static_reason_and_never_echoed(developer, name):
    tally = RowTally()
    assert parse_dld_records([dld_row(None, DEVELOPER_NAME=developer, PROJECT_NAME=name)], today=TODAY, tally=tally) == []
    assert tally.dropped_count(dld.R_NO_IDENTITY) == 1
    text = " ".join([*tally.notes(), tally.dropped_summary()])
    assert dld.R_NO_IDENTITY in text
    assert name not in text and (not developer or developer not in text)  # the reason is static: no cell value in it
    # the same names are fine once a valid project number identifies the row
    assert len(parse_dld_records([dld_row("1234", DEVELOPER_NAME=developer, PROJECT_NAME=name)], today=TODAY)) == 1


@pytest.mark.parametrize("raw", [None, "text", 5, ["list"], 3.5, True])
def test_entries_that_are_not_objects_are_dropped_and_counted(raw):
    tally = RowTally()
    assert parse_dld_records([raw, dld_row(1)], today=TODAY, tally=tally)[0].registry_project_no == "1"
    assert tally.dropped_count(dld.R_NOT_OBJECT) == 1


@pytest.mark.parametrize("number", ["ABC123", "12345678901", "12٣", "12 34", "1.5", "-4"])
def test_a_number_that_is_not_one_to_nine_ascii_digits_is_never_a_key(number):
    tally = RowTally()
    (row,) = parse_dld_records([dld_row(number)], today=TODAY, tally=tally)
    assert row.registry_project_no is None and row.project_key.startswith("name:dubai:")
    assert tally.adjusted_count(dld.A_BAD_NUMBER) == 1


@pytest.mark.parametrize("number", [None, "", "  ", "0", 0, "n/a", "TBC", float("nan"), True, "١٢٣"])
def test_placeholder_numbers_are_blank_not_keys(number):
    (row,) = parse_dld_records([dld_row(number)], today=TODAY)
    assert row.registry_project_no is None


def test_a_float_number_from_a_spreadsheet_export_loses_its_point_zero():
    (row,) = parse_dld_records([dld_row(1006.0)], today=TODAY)
    assert row.registry_project_no == "1006"


def test_the_first_row_wins_for_a_repeated_number():
    tally = RowTally()
    rows = parse_dld_records(
        [dld_row(7, PROJECT_NAME="First Tower"), dld_row(7, PROJECT_NAME="Second Tower")], today=TODAY, tally=tally
    )
    assert [r.project_name_en for r in rows] == ["First Tower"]
    assert tally.dropped_count(dld.R_DUPLICATE) == 1


@pytest.mark.parametrize(
    "record",
    [
        {"project_number": 7, "project_name": "Snake Tower", "developer_name": "Example Developer LLC"},
        {"Project Number": 7, "Project Name": "Snake Tower", "Developer Name": "Example Developer LLC"},
        {"projectNumber": 7, "projectName": "Snake Tower", "developerName": "Example Developer LLC"},
        {"PROJECT_NO": 7, "PROJECT_EN": "Snake Tower", "DEVELOPER_EN": "Example Developer LLC"},
    ],
)
def test_column_naming_styles_are_all_understood(record):
    (row,) = parse_dld_records([record], today=TODAY)
    assert row.registry_project_no == "7" and row.project_name_en == "Snake Tower"
    assert row.developer == "Example Developer LLC"


def test_an_english_column_wins_over_the_generic_name_column():
    (row,) = parse_dld_records([dld_row(1, PROJECT_NAME_EN="English Name", PROJECT_NAME="Other Name")], today=TODAY)
    assert row.project_name_en == "English Name"


def test_a_huge_value_is_a_dropped_row_not_a_giant_cell():
    tally = RowTally()
    rows = parse_dld_records([dld_row(1, PROJECT_NAME="x" * 501), dld_row(2, STATUS_NOTE="y" * 10_000_000)], today=TODAY, tally=tally)
    assert [r.registry_project_no for r in rows] == ["2"]
    assert tally.dropped_count(dld.R_OVERSIZE) == 1


@pytest.mark.parametrize(
    "extra,attr,expected",
    [
        ({"PERCENT_COMPLETED": 120}, "percent_completed", None),
        ({"PERCENT_COMPLETED": -3}, "percent_completed", None),
        ({"PERCENT_COMPLETED": 0}, "percent_completed", 0.0),  # zero percent is a real value
        ({"PERCENT_COMPLETED": "100%"}, "percent_completed", 100.0),
        ({"NO_OF_UNITS": -5}, "units", None),
        ({"NO_OF_UNITS": 3.5}, "units", None),
        ({"NO_OF_UNITS": "many"}, "units", None),
        ({"NO_OF_UNITS": [1]}, "units", None),
        ({"PROJECT_STATUS": {"nested": "object"}}, "status", ""),
        ({"PROJECT_STATUS": True}, "status", ""),
        ({"PROJECT_START_DATE": "3000-01-01"}, "start_date", ""),
        ({"PROJECT_START_DATE": "soon"}, "start_date", ""),
        ({"AREA_NAME_EN": None}, "area_en", ""),
    ],
)
def test_a_bad_optional_value_blanks_that_value_and_keeps_the_row(extra, attr, expected):
    (row,) = parse_dld_records([dld_row(5, **extra)], today=TODAY)
    assert row.registry_project_no == "5"
    assert getattr(row, attr) == expected


def test_an_unreadable_date_is_counted_so_a_format_change_is_noticed():
    tally = RowTally()
    parse_dld_records([dld_row(5, PROJECT_START_DATE="soon")], today=TODAY, tally=tally)
    assert tally.adjusted_count("date not readable") == 1


def test_a_half_written_row_is_dropped_without_crashing():
    tally = RowTally()
    rows = parse_dld_records([{"PROJECT_NUMBER": 9}, {}, {"PROJECT_NAME": None}, dld_row(10)], today=TODAY, tally=tally)
    assert [r.registry_project_no for r in rows] == ["10"]
    assert tally.dropped == 3


def test_empty_input_gives_no_rows():
    assert parse_dld_records([], today=TODAY) == []


def test_a_large_register_is_mapped_in_order():
    records = [dld_row(n) for n in range(1, 5001)]
    rows = parse_dld_records(records, today=TODAY)
    assert len(rows) == 5000 and rows[0].registry_project_no == "1" and rows[-1].registry_project_no == "5000"


# ---------------------------------------------------------------------------
# check_schema
# ---------------------------------------------------------------------------
def test_a_payload_without_a_number_column_is_refused_and_the_error_counts_columns_never_names_or_values():
    with pytest.raises(SourceError) as info:
        check_schema([{"NAME": "PRIVATE-VALUE", "OTHER": 1}])
    message = str(info.value)
    assert "project_number" in message and "project_name" in message
    assert "Columns seen: 2 name(s); none known" in message  # a column name of the answer is the server's text
    assert "NAME" not in message and "OTHER" not in message
    assert "PRIVATE-VALUE" not in message


def test_marker_named_columns_are_counted_but_never_shown():
    marked = {f"{KEY_MARKER}-column-{n}": n for n in range(1, 4)}
    with pytest.raises(SourceError) as info:
        check_schema([marked, {**marked, f"{KEY_MARKER}-extra": 9}])
    message = str(info.value)
    assert "project_number" in message and "Columns seen: 4 name(s); none known" in message
    assert KEY_MARKER.lower() not in message.lower()


def test_a_payload_with_only_a_name_column_names_just_the_number():
    with pytest.raises(SourceError) as info:
        check_schema([{"PROJECT_NAME": "x"}])
    assert "project_number" in str(info.value) and "no recognised column for: project_number." in str(info.value)


def test_a_payload_with_no_objects_is_refused():
    with pytest.raises(SourceError, match="none is an object"):
        check_schema(["a", 1, None])


def test_one_good_row_among_odd_ones_is_enough_for_the_schema_check():
    check_schema(["junk", {"PROJECT_NUMBER": 1, "PROJECT_NAME": "x"}])


# ---------------------------------------------------------------------------
# fetch(): the happy path
# ---------------------------------------------------------------------------
def happy_session():
    # The register ends at an empty page, so the script ends with one (see test_sources_review_regressions.py).
    return FakeSession([token_ok(), FakeResponse(200, load_fixture("dld_page1.json")), FakeResponse(200, load_fixture("dld_page2.json")), page([])])


def test_fetch_pages_through_the_register_and_returns_the_rows():
    session = happy_session()
    result = make_adapter(session, page_size=3).fetch()
    assert result.source == "dld_projects" and len(result.rows) == 5 and result.candidates == []
    assert result.source_refreshed == "2026-10-20"
    assert datetime.strptime(result.fetched_at, "%Y-%m-%dT%H:%M:%SZ")
    assert result.notes[0] == "fetched 6 raw row(s) in 2 page(s) of up to 3"
    assert any("dropped 1 row(s)" in n for n in result.notes)
    assert all(r.first_seen == TODAY_ISO for r in result.rows)
    assert session.script == []  # every scripted call was made, none left over


def test_fetch_asks_for_the_right_pages_with_a_bearer_token():
    session = happy_session()
    make_adapter(session, page_size=3).fetch()
    token_call, first, second, probe = session.calls  # probe: the confirming request for the end of the register
    assert (token_call.method, token_call.url) == ("POST", dld.DATA_DUBAI_TOKEN_URL)
    assert token_call.kwargs["data"] == {"grant_type": "client_credentials", "client_id": FAKE_KEY, "client_secret": FAKE_SECRET}
    assert FAKE_KEY not in token_call.url and "params" not in token_call.kwargs  # credentials travel in the body only
    assert (first.method, first.url) == ("GET", dld.DLD_PROJECTS_URL)
    assert first.kwargs["params"] == {"limit": 3, "offset": 0}
    assert second.kwargs["params"] == {"limit": 3, "offset": 3}
    assert probe.kwargs["params"] == {"limit": 3, "offset": 6}
    assert first.kwargs["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert FAKE_KEY not in json.dumps(first.kwargs, default=str)


def test_every_call_identifies_itself_and_has_a_timeout():
    session = happy_session()
    make_adapter(session, page_size=3).fetch()
    for call in session.calls:
        agent = call.kwargs["headers"]["User-Agent"]
        assert agent.startswith("uae-real-estate-scraper/") and "Mozilla" not in agent
        connect, read = call.kwargs["timeout"]
        assert 0 < connect <= read


def test_a_session_we_make_is_closed_and_one_we_are_given_is_not(monkeypatch):
    made = []

    class Own(FakeSession):
        def __init__(self):
            super().__init__([token_ok(), FakeResponse(200, load_fixture("dld_page1.json")), FakeResponse(200, load_fixture("dld_page2.json")), page([])])
            made.append(self)

    monkeypatch.setattr(base.requests, "Session", Own)
    make_adapter(None, page_size=3).fetch()
    assert made[0].closed is True
    given = happy_session()
    make_adapter(given, page_size=3).fetch()
    assert given.closed is False


def test_the_source_refreshed_date_is_left_empty_when_the_register_states_none():
    rows = load_fixture("dld_page1.json")["data"]
    session = FakeSession([token_ok(), page(rows)])
    result = make_adapter(session, page_size=10).fetch()
    assert result.source_refreshed is None and all(r.source_refreshed == "" for r in result.rows)


# ---------------------------------------------------------------------------
# fetch(): paging
# ---------------------------------------------------------------------------
def offsets(session):
    return [c.kwargs["params"]["offset"] for c in session.calls if c.method == "GET"]


def test_paging_advances_by_the_rows_received_not_the_rows_asked_for():
    # The server quietly serves 3 rows per page although 5 were asked for.
    session = FakeSession([token_ok(), page([dld_row(n) for n in (1, 2, 3)], total=6), page([dld_row(n) for n in (4, 5, 6)], total=6), page([], total=6)])
    result = make_adapter(session, page_size=5).fetch()
    assert offsets(session) == [0, 3, 6] and len(result.rows) == 6


def test_without_a_stated_total_a_short_page_is_followed_by_one_confirming_request():
    # Changed from "a short page ends the register": a server that clamps the page size serves short pages
    # all the way through, so the end is confirmed by an empty page.
    session = FakeSession([token_ok(), page([dld_row(1), dld_row(2)]), page([dld_row(3), dld_row(4)]), page([dld_row(5)]), page([])])
    result = make_adapter(session, page_size=2).fetch()
    assert len(result.rows) == 5 and offsets(session) == [0, 2, 4, 5]


def test_without_a_stated_total_a_full_last_page_is_followed_by_an_empty_one():
    session = FakeSession([token_ok(), page([dld_row(1), dld_row(2)]), page([dld_row(3), dld_row(4)]), page([])])
    result = make_adapter(session, page_size=2).fetch()
    assert len(result.rows) == 4 and result.notes[0].startswith("fetched 4 raw row(s) in 2 page(s)")


def test_a_stated_total_is_followed_by_one_confirming_request():
    # Changed from "ends paging without an extra request": a total that is only the size of one page (or any
    # number below the register) used to cut the register short with no error.
    session = FakeSession([token_ok(), page([dld_row(1), dld_row(2)], total=2), page([], total=2)])
    assert len(make_adapter(session, page_size=2).fetch().rows) == 2
    assert len(session.calls) == 3


def test_an_empty_page_before_the_stated_total_is_an_error_not_a_smaller_register():
    session = FakeSession([token_ok(), page([dld_row(1), dld_row(2)], total=10), page([], total=10)])
    error = fetch_fails(session, page_size=2)
    assert "stopped at 2 row(s) of the 10" in str(error)


def test_a_server_that_ignores_the_offset_is_caught():
    same = [dld_row(1), dld_row(2)]
    session = FakeSession([token_ok(), page(same, total=10), page(list(same), total=10)])
    assert "same page twice" in str(fetch_fails(session, page_size=2))


def test_paging_that_never_finishes_is_an_error_not_a_partial_register():
    session = FakeSession(
        [token_ok(), page([dld_row(1), dld_row(2)], total=100), page([dld_row(3), dld_row(4)], total=100)]
    )
    error = fetch_fails(session, page_size=2, max_pages=2)
    assert "did not finish within 2 page(s)" in str(error) and "refusing to return a partial register" in str(error)


def test_rows_repeated_across_pages_are_kept_once_and_the_unstable_order_is_reported():
    session = FakeSession(
        [token_ok(), page([dld_row(1), dld_row(2)], total=4), page([dld_row(2), dld_row(3)], total=4), page([], total=4)]
    )
    result = make_adapter(session, page_size=2).fetch()
    assert [r.registry_project_no for r in result.rows] == ["1", "2", "3"]
    assert any("paging order may not be stable" in n for n in result.notes)


def test_an_empty_first_page_is_an_error():
    session = FakeSession([token_ok(), page([])])
    assert "no rows at all" in str(fetch_fails(session))


# ---------------------------------------------------------------------------
# fetch(): an answer that is not what we expect
# ---------------------------------------------------------------------------
def test_an_answer_with_no_row_list_names_the_known_keys_it_had_never_the_values():
    session = FakeSession([token_ok(), FakeResponse(200, {"error": "PRIVATE-VALUE", "status": 1})])
    message = str(fetch_fails(session))
    assert "no list of rows" in message and "2 name(s); known: error, status" in message
    assert "PRIVATE-VALUE" not in message


# A name in the answer can be DATA (an answer keyed by project name), and the Actions log of a public repository
# is public: the errors below count the names and never repeat one that is not a known envelope name.
KEY_MARKER = "ZZKEYZZ"  # SYNTHETIC


def test_an_answer_keyed_by_project_name_has_its_keys_counted_not_shown():
    keyed = {f"{KEY_MARKER}-Fake Tower {n}": [{"PROJECT_NUMBER": n}] for n in range(1, 6)}
    keyed["total"] = 5
    message = str(fetch_fails(FakeSession([token_ok(), FakeResponse(200, keyed)])))
    assert "page 1: the answer has no list of rows (it contained: 6 name(s); known: total)" in message
    assert KEY_MARKER.lower() not in message.lower() and "fake tower" not in message.lower()


def test_a_token_answer_with_marker_keys_has_them_counted_not_shown():
    answer = FakeResponse(200, {f"{KEY_MARKER}-a": 1, f"{KEY_MARKER}-b": 2, "expires_in": 3600})
    message = str(fetch_fails(FakeSession([answer])))
    assert "no access token (it contained: 3 name(s); known: expiresin)" in message
    assert KEY_MARKER.lower() not in message.lower()


@pytest.mark.parametrize("payload", ["just text", 42, None, True])
def test_a_scalar_answer_is_an_error(payload):
    assert "no list of rows" in str(fetch_fails(FakeSession([token_ok(), FakeResponse(200, payload)])))


def test_rows_without_the_expected_columns_fail_loudly_with_a_column_count():
    session = FakeSession([token_ok(), page([{"Foo": "PRIVATE", "Bar": 2}])])
    message = str(fetch_fails(session))
    assert "project_number" in message and "2 name(s); none known" in message
    assert "Foo" not in message and "PRIVATE" not in message


def test_a_non_json_page_is_an_error_that_does_not_quote_the_body():
    session = FakeSession([token_ok(), FakeResponse(200, text="<html>PRIVATE-PAGE</html>", headers={"Content-Type": "text/html"})])
    message = str(fetch_fails(session))
    assert "not valid JSON" in message and "PRIVATE-PAGE" not in message


def test_when_every_row_is_unusable_the_run_fails():
    rows = [{"PROJECT_NUMBER": "n/a", "PROJECT_NAME": f"Test {n}"} for n in range(4)]
    session = FakeSession([token_ok(), page(rows)])
    assert "none of the 4 row(s) could be used" in str(fetch_fails(session))


def test_more_than_half_unusable_fails_but_exactly_half_is_tolerated():
    bad = {"PROJECT_NUMBER": "n/a", "PROJECT_NAME": "Test Orphan"}
    half = [dld_row(1), dld_row(2), bad, dict(bad)]
    assert len(make_adapter(FakeSession([token_ok(), page(half)])).fetch().rows) == 2
    most = [dld_row(1), dld_row(2), bad, dict(bad), dict(bad)]
    assert "3 of 5 row(s) were unusable" in str(fetch_fails(FakeSession([token_ok(), page(most)])))


# ---------------------------------------------------------------------------
# fetch(): HTTP failures
# ---------------------------------------------------------------------------
def test_a_rate_limited_page_is_retried_honouring_retry_after():
    sleeps = Sleeps()
    session = FakeSession([token_ok(), FakeResponse(429, headers={"Retry-After": "5"}), page([dld_row(1)])])
    result = make_adapter(session, sleeps=sleeps).fetch()
    assert len(result.rows) == 1 and sleeps == [5.0]


def test_a_server_error_that_persists_fails_after_the_retries():
    sleeps = Sleeps()
    session = FakeSession([token_ok()] + [FakeResponse(503, text="down")] * 4)
    message = str(fetch_fails(session, sleeps=sleeps))
    assert "page 1" in message and "HTTP 503" in message and "gave up after 4 attempts" in message
    assert len(sleeps) == 3


def test_a_timeout_on_a_page_is_retried_then_reported():
    session = FakeSession([token_ok()] + [requests.exceptions.ReadTimeout("slow")] * 4)
    assert "timed out" in str(fetch_fails(session))


def test_a_rejected_key_is_not_retried():
    session = FakeSession([token_ok(), FakeResponse(401, text="unauthorised")])
    message = str(fetch_fails(session))
    assert "HTTP 401" in message and "credentials rejected" in message
    assert len(session.calls) == 2  # the token call and one page call


def test_a_rejected_token_request_is_not_retried_and_stops_everything():
    session = FakeSession([FakeResponse(401, text="bad client")])
    message = str(fetch_fails(session))
    assert "token request" in message and "HTTP 401" in message
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "token_answer,expected",
    [
        (FakeResponse(200, {"expires_in": 3600}), "no access token (it contained: 1 name(s); known: expiresin)"),
        (FakeResponse(200, ["a"]), "a list"),
        (FakeResponse(200, {"access_token": ""}), "no access token"),
        (FakeResponse(200, {"access_token": None}), "no access token"),
        (FakeResponse(200, {"access_token": {"nested": 1}}), "no access token"),
        (FakeResponse(200, text="<html>"), "not valid JSON"),
    ],
)
def test_a_token_answer_without_a_token_is_an_error(token_answer, expected):
    assert expected in str(fetch_fails(FakeSession([token_answer])))


def test_the_token_field_may_be_named_token():
    session = FakeSession([FakeResponse(200, {"token": FAKE_TOKEN}), page([dld_row(1)])])
    make_adapter(session).fetch()
    assert session.calls[1].kwargs["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"


# ---------------------------------------------------------------------------
# Secrets never reach an error or a log
# ---------------------------------------------------------------------------
def leaky_body():
    return json.dumps(
        {
            "error": "invalid_client",
            "client_id": FAKE_KEY,
            "client_secret": FAKE_SECRET,
            "echo": f"Authorization: Bearer {FAKE_TOKEN}",
            "url": f"https://x.example/t?client_secret={FAKE_SECRET}&access_token={FAKE_TOKEN}",
        }
    )


@pytest.mark.parametrize(
    "script",
    [
        [FakeResponse(401, text=leaky_body())],
        [FakeResponse(500, text=leaky_body())] * 4,
        [requests.exceptions.ConnectionError(f"failed for ?client_secret={FAKE_SECRET} key {FAKE_KEY}")] * 4,
        [ValueError(f"unexpected {FAKE_KEY} {FAKE_SECRET}")],
        [token_ok(), FakeResponse(401, text=leaky_body())],
        [token_ok(), FakeResponse(500, text=leaky_body())] + [FakeResponse(500, text=leaky_body())] * 3,
        [token_ok(), requests.exceptions.ConnectionError(f"Authorization: Bearer {FAKE_TOKEN}")] * 2,
        [token_ok(), RuntimeError(f"token {FAKE_TOKEN} secret {FAKE_SECRET}")],
    ],
)
def test_no_secret_ever_reaches_an_error_or_a_log(script, caplog):
    with caplog.at_level("DEBUG"):
        error = fetch_fails(FakeSession(script))
    assert_no_secret(str(error), *ALL_FAKE_SECRETS)
    assert_no_secret(full_text(error), *ALL_FAKE_SECRETS)
    assert_no_secret(caplog.text, *ALL_FAKE_SECRETS)


def test_a_successful_run_does_not_log_secrets_either(caplog):
    with caplog.at_level("DEBUG"):
        make_adapter(happy_session(), page_size=3).fetch()
    assert_no_secret(caplog.text, *ALL_FAKE_SECRETS)


def test_a_malformed_token_answer_that_repeats_the_secret_is_not_echoed():
    answer = FakeResponse(200, {"access_token": "", "debug": FAKE_SECRET})
    error = fetch_fails(FakeSession([answer]))
    assert_no_secret(str(error), FAKE_SECRET)


# ---------------------------------------------------------------------------
# Switches and construction
# ---------------------------------------------------------------------------
def test_the_adapter_needs_both_halves_of_the_credentials():
    adapter = DldProjectsAdapter(make_cfg())
    assert adapter.name == "dld_projects" and adapter.expects_rows is True
    assert adapter.enabled(dld_cfg()) is True and adapter.skip_reason(dld_cfg()) == ""
    only_key = make_cfg(DATA_DUBAI_API_KEY=FAKE_KEY)
    only_secret = make_cfg(DATA_DUBAI_API_SECRET=FAKE_SECRET)
    assert not adapter.enabled(only_key) and not adapter.enabled(only_secret) and not adapter.enabled(make_cfg())


def test_the_skip_reason_names_missing_settings_and_never_their_values():
    adapter = DldProjectsAdapter(make_cfg())
    both = adapter.skip_reason(make_cfg())
    assert "DATA_DUBAI_API_KEY" in both and "DATA_DUBAI_API_SECRET" in both
    one = adapter.skip_reason(make_cfg(DATA_DUBAI_API_KEY=FAKE_KEY))
    assert "DATA_DUBAI_API_SECRET" in one and "DATA_DUBAI_API_KEY" not in one
    assert_no_secret(one, *ALL_FAKE_SECRETS)


def test_fetching_while_not_configured_is_an_error_and_makes_no_request():
    session = FakeSession([])
    error = fetch_fails(session, cfg=make_cfg())
    assert "not configured" in str(error) and session.calls == []


@pytest.mark.parametrize("kwargs", [{"page_size": 0}, {"max_pages": 0}, {"page_size": -1}])
def test_nonsense_paging_settings_are_refused(kwargs):
    with pytest.raises(ValueError):
        DldProjectsAdapter(dld_cfg(), **kwargs)


def test_the_public_constants_are_marked_unverified_where_they_are_guesses():
    source = Path(dld.__file__).read_text(encoding="utf-8")
    for name in ("DATA_DUBAI_TOKEN_URL", "TOKEN_GRANT_TYPE", "DLD_PAGE_SIZE"):
        assert name in source
    assert source.count("UNVERIFIED") >= 3


# ---------------------------------------------------------------------------
# The local download (DATA_DUBAI_PROJECTS_FILE): the register from a file a signed-in
# person downloaded from the dataset's portal page, with no key and no request
# ---------------------------------------------------------------------------
def write_download(tmp_path, payload, name="projects_download.json", text=None):
    path = tmp_path / name
    if text is None:
        text = json.dumps(payload, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return str(path)


def portal_rows():
    """Rows shaped like the portal's download: snake_case keys, a bare project_name, a load_timestamp."""
    return [
        {
            "project_number": 2001, "project_name": "Test Gate Residences", "developer_name": "Example Developer LLC",
            "area_name_en": "Test Area One", "project_status": "ACTIVE", "percent_completed": "67.98000",
            "no_of_units": 152, "load_timestamp": "2026-10-28 19:08:11",
        },
        {
            "project_number": 2002, "project_name": "برج اختبار", "developer_name": "مطور اختبار",
            "area_name_en": "Test Area Two", "project_status": "FINISHED", "percent_completed": "100",
            "no_of_units": 10, "load_timestamp": "2026-10-28 19:08:11",
        },
    ]


def file_cfg(path, **extra):
    return make_cfg(DATA_DUBAI_PROJECTS_FILE=path, **extra)


def test_a_download_path_enables_the_source_without_credentials(tmp_path):
    cfg = file_cfg(write_download(tmp_path, portal_rows()))
    adapter = DldProjectsAdapter(cfg, session=FakeSession([]), today=TODAY)
    assert adapter.enabled(cfg) and adapter.skip_reason(cfg) == ""
    assert not DldProjectsAdapter(make_cfg(), session=FakeSession([]), today=TODAY).enabled(make_cfg())


def test_the_download_is_read_with_no_request_and_the_rows_say_where_they_came_from(tmp_path):
    session = FakeSession([])
    result = DldProjectsAdapter(file_cfg(write_download(tmp_path, portal_rows())), session=session, today=TODAY).fetch()
    assert session.calls == []
    assert [r.project_key for r in result.rows] == ["dld:dubai:2001", "dld:dubai:2002"]
    assert result.rows[0].project_name_en == "Test Gate Residences" and result.rows[1].project_name_ar == "برج اختبار"
    assert {r.source_dataset for r in result.rows} == {dld.DLD_DOWNLOAD_PAGE}
    assert result.source_refreshed == "2026-10-28" and {r.source_refreshed for r in result.rows} == {"2026-10-28"}
    assert result.notes[0].startswith(dld.FILE_NOTE) and "2 raw row(s)" in result.notes[0]
    assert dld.FILE_WINS_NOTE not in result.notes


def test_an_enveloped_download_is_read_too(tmp_path):
    payload = {"data": portal_rows(), "refreshed_at": "2026-10-20"}
    result = DldProjectsAdapter(file_cfg(write_download(tmp_path, payload)), session=FakeSession([]), today=TODAY).fetch()
    assert len(result.rows) == 2 and result.source_refreshed == "2026-10-20"


def test_a_utf8_bom_in_the_download_is_tolerated(tmp_path):
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(portal_rows()).encode("utf-8"))
    result = DldProjectsAdapter(file_cfg(str(path)), session=FakeSession([]), today=TODAY).fetch()
    assert len(result.rows) == 2


def test_the_download_wins_over_credentials_and_says_so(tmp_path, caplog):
    cfg = file_cfg(write_download(tmp_path, portal_rows()), DATA_DUBAI_API_KEY=FAKE_KEY, DATA_DUBAI_API_SECRET=FAKE_SECRET)
    session = FakeSession([])
    with caplog.at_level("WARNING", logger="src.sources.dld_projects"):
        result = DldProjectsAdapter(cfg, session=session, today=TODAY).fetch()
    assert session.calls == [] and dld.FILE_WINS_NOTE in result.notes
    assert any(dld.FILE_WINS_NOTE in rec.getMessage() for rec in caplog.records)
    assert_no_secret(caplog.text)


def test_a_missing_download_is_an_error_that_names_the_class_not_the_path(tmp_path):
    path = str(tmp_path / "secret-folder-name" / "nope.json")
    session = FakeSession([])
    with pytest.raises(SourceError) as info:
        DldProjectsAdapter(file_cfg(path), session=session, today=TODAY).fetch()
    text = full_text(info.value)
    assert "FileNotFoundError" in str(info.value) and "cannot be read" in str(info.value)
    assert "secret-folder-name" not in text and "nope.json" not in text and session.calls == []


@pytest.mark.parametrize(
    "text, expected",
    [
        ("{not json", "not UTF-8 JSON"),
        ("42", "no list of rows"),
        ('{"meta": {"x": 1}}', "no list of rows"),
        ("[]", "no rows at all"),
        ('[{"PROJECT_NUMBER": 1}]', "no recognised column"),
    ],
)
def test_a_download_of_the_wrong_shape_fails_loudly(tmp_path, text, expected):
    with pytest.raises(SourceError) as info:
        DldProjectsAdapter(file_cfg(write_download(tmp_path, None, text=text)), session=FakeSession([]), today=TODAY).fetch()
    assert expected in str(info.value)


def test_a_download_that_is_not_utf8_is_an_error_without_the_bytes(tmp_path):
    path = tmp_path / "latin1.json"
    path.write_bytes(b'[{"project_number": 1, "project_name": "Caf\xe9 Tower"}]')
    with pytest.raises(SourceError) as info:
        DldProjectsAdapter(file_cfg(str(path)), session=FakeSession([]), today=TODAY).fetch()
    assert "not UTF-8 JSON" in str(info.value) and "Caf" not in full_text(info.value)


def test_a_download_in_which_most_rows_are_unusable_fails(tmp_path):
    rows = portal_rows() + [{"project_number": None, "project_name": "", "developer_name": ""} for _ in range(3)]
    with pytest.raises(SourceError) as info:
        DldProjectsAdapter(file_cfg(write_download(tmp_path, rows)), session=FakeSession([]), today=TODAY).fetch()
    assert "unusable" in str(info.value)


def test_the_download_setting_is_local_only_and_never_described():
    cfg = file_cfg("some/where.json")
    assert cfg.data_dubai_projects_file == "some/where.json"
    assert "DATA_DUBAI_PROJECTS_FILE" not in cfg.describe() and "some/where" not in str(cfg.describe())
