"""
Tests for src/sources/srerd_projects.py. The real Sharjah endpoint has never been
called and its response shape is unverified, so these tests pin down what the adapter
does with the shapes it can recognise and, above all, that it refuses the ones it cannot.
SYNTHETIC fixtures only; no network.
"""

from __future__ import annotations

import pytest
import requests

from src.sources import srerd_projects as srerd
from src.sources.base import RowTally, SourceError
from src.sources.srerd_projects import SrerdProjectsAdapter, check_shape, parse_srerd_records
from test_sources_support import (
    TODAY,
    TODAY_ISO,
    FakeResponse,
    FakeSession,
    Sleeps,
    assert_no_secret,
    full_text,
    load_fixture,
    make_cfg,
)


def records():
    return load_fixture("srerd_projects.json")["data"]


def enabled_cfg():
    return make_cfg(ENABLE_SRERD="true")


def project(pro_id, name="Test Gardens", **extra):
    return {"proId": pro_id, "projectNameEn": name, **extra}


def make_adapter(session, *, sleeps=None, **kwargs):
    return SrerdProjectsAdapter(enabled_cfg(), session=session, today=TODAY, sleep=sleeps if sleeps is not None else Sleeps(), **kwargs)


def fetch_fails(payload=None, *, script=None, sleeps=None):
    """The SourceError from a fetch whose single answer is `payload`, or whose calls follow `script`."""
    if script is None:
        script = [FakeResponse(200, payload)]
    adapter = make_adapter(FakeSession(script), sleeps=sleeps)
    with pytest.raises(SourceError) as info:
        adapter.fetch()
    return info.value


# ---------------------------------------------------------------------------
# Mapping rows
# ---------------------------------------------------------------------------
def test_the_fixture_maps_to_sharjah_srerd_rows():
    tally = RowTally()
    rows = parse_srerd_records(records(), today=TODAY, tally=tally)
    assert [r.registry_project_no for r in rows] == ["5501", "5502", "SH-77", "5503", "5504"]
    assert {r.emirate for r in rows} == {"Sharjah"} and {r.registry for r in rows} == {"SRERD"}
    first = rows[0]
    assert (first.project_name_en, first.project_name_ar) == ("Test Gardens", "حدائق اختبار")
    assert (first.area_en, first.area_ar) == ("Test Sharjah Area", "منطقة اختبار")
    assert first.project_key == "srerd:sharjah:5501" and rows[2].project_key == "srerd:sharjah:SH-77"
    assert first.developer == "" and first.master_project == ""  # nothing is invented for columns the list lacks


def test_every_row_is_stamped_with_today_and_the_source():
    for row in parse_srerd_records(records(), today=TODAY):
        assert row.first_seen == TODAY_ISO and row.last_seen == TODAY_ISO
        assert row.source_dataset == srerd.SRERD_PROJECTS_URL and row.source_refreshed == ""


def test_a_row_with_no_id_and_no_developer_has_no_key_and_is_dropped():
    tally = RowTally()
    rows = parse_srerd_records(records(), today=TODAY, tally=tally)
    assert "Test Unkeyed" not in {r.project_name_en for r in rows}
    assert tally.dropped_count(srerd.R_NO_IDENTITY) == 1 and all(r.project_key for r in rows)


def test_an_arabic_only_project_is_kept_because_its_id_identifies_it():
    row = next(r for r in parse_srerd_records(records(), today=TODAY) if r.registry_project_no == "5503")
    assert row.project_name_en == "" and row.project_name_ar == "برج اختبار"


def test_an_area_that_is_only_a_number_is_a_size_not_a_place():
    row = next(r for r in parse_srerd_records(records(), today=TODAY) if r.registry_project_no == "5504")
    assert row.area_en == ""


def test_a_name_with_no_id_is_keyed_by_developer_and_name_when_the_list_gives_a_developer():
    rows = parse_srerd_records([{"projectNameEn": "Test Bay", "developerName": "Example Developer LLC"}], today=TODAY)
    assert rows[0].project_key == "name:sharjah:example-developer-llc:test-bay"


def test_the_no_identity_reason_is_static_text_that_states_the_usable_name_rule():
    reason = srerd.R_NO_IDENTITY
    assert reason.startswith("no usable identity (no id field, and ")  # the first clause is kept
    assert "developer" in reason and "project name" in reason
    assert "Latin letter" in reason and "another script" in reason
    assert all(symbol in reason for symbol in ("+", "#", "@"))


@pytest.mark.parametrize(
    "developer, name",
    [
        ("Example Developer+ LLC", "Test Bay"),
        ("Example Developer LLC", "Test C#"),
        ("Example Developer LLC", "Test @ Marina"),
        ("Example Døveloper LLC", "Test Bay"),  # o-stroke does not fold to an ASCII letter
        ("Example Developer LLC", "Test Bay न"),  # a Devanagari letter survives folding, so it could not key
    ],
)
def test_a_name_that_cannot_key_is_dropped_with_the_static_reason_and_never_echoed(developer, name):
    tally = RowTally()
    assert parse_srerd_records([project(None, name, developerName=developer)], today=TODAY, tally=tally) == []
    assert tally.dropped_count(srerd.R_NO_IDENTITY) == 1
    text = " ".join([*tally.notes(), tally.dropped_summary()])
    assert srerd.R_NO_IDENTITY in text
    assert name not in text and developer not in text  # the reason is static: no cell value in it
    # the same names are fine once a real id identifies the row
    assert len(parse_srerd_records([project(9, name, developerName=developer)], today=TODAY)) == 1


@pytest.mark.parametrize("bad_id", ["A B", "x" * 41, "id/with/slashes", "tab\there", "a;b"])
def test_an_id_with_forbidden_characters_is_blanked_never_used_as_a_key(bad_id):
    tally = RowTally()
    rows = parse_srerd_records([project(bad_id, developerName="Example Developer LLC")], today=TODAY, tally=tally)
    assert rows[0].registry_project_no is None and rows[0].project_key.startswith("name:sharjah:")
    assert tally.adjusted_count(srerd.A_BAD_ID) == 1


@pytest.mark.parametrize("placeholder", [None, "", "0", "n/a", "-", 0])
def test_a_placeholder_id_is_no_id(placeholder):
    tally = RowTally()
    assert parse_srerd_records([project(placeholder)], today=TODAY, tally=tally) == []
    assert tally.dropped_count(srerd.R_NO_IDENTITY) == 1


def test_the_first_row_wins_for_a_repeated_id():
    tally = RowTally()
    rows = parse_srerd_records([project(7, "First"), project(7, "Second")], today=TODAY, tally=tally)
    assert [r.project_name_en for r in rows] == ["First"] and tally.dropped_count(srerd.R_DUPLICATE) == 1


# -- the positional-id guard -----------------------------------------------------
def test_ids_that_are_exactly_one_to_n_in_order_are_treated_as_row_positions_and_ignored():
    tally = RowTally()
    rows = parse_srerd_records([project(n, f"Test {n}") for n in range(1, 7)], today=TODAY, tally=tally)
    assert rows == [] and tally.adjusted_count(srerd.A_POSITIONAL) == 1
    assert tally.dropped_count(srerd.R_NO_IDENTITY) == 6


def test_positional_ids_do_not_stop_rows_that_have_a_developer_from_being_keyed_by_name():
    data = [project(n, f"Test {n}", developerName="Example Developer LLC") for n in range(1, 7)]
    rows = parse_srerd_records(data, today=TODAY)
    assert len(rows) == 6 and all(r.registry_project_no is None for r in rows)
    assert rows[0].project_key == "name:sharjah:example-developer-llc:test-1"


@pytest.mark.parametrize(
    "ids,kept",
    [
        ([1, 2, 3], ["1", "2", "3"]),                     # too few rows for "1..N" to prove anything
        ([1, 2, 3, 4], ["1", "2", "3", "4"]),
        ([2, 1, 3, 4, 5, 6], ["2", "1", "3", "4", "5", "6"]),  # not in order
        ([1, 2, 3, 4, 5, 7], ["1", "2", "3", "4", "5", "7"]),  # a gap
        ([7, 1, 2, 3, 4, 5], ["7", "1", "2", "3", "4", "5"]),  # one id off its position
        ([101, 102, 103, 104, 105, 106], ["101", "102", "103", "104", "105", "106"]),
    ],
)
def test_short_shuffled_or_offset_id_runs_are_real_ids(ids, kept):
    rows = parse_srerd_records([project(i, f"Test {i}") for i in ids], today=TODAY)
    assert [r.registry_project_no for r in rows] == kept


def test_an_id_run_that_starts_at_one_but_has_a_gap_is_trusted():
    rows = parse_srerd_records([project(i, f"Test {i}") for i in (1, 2, 3, 4, 6)], today=TODAY)
    assert len(rows) == 5


# -- name and column handling ----------------------------------------------------
@pytest.mark.parametrize(
    "record",
    [
        {"ProId": 9, "ProjectNameEn": "Style Tower", "AreaNameEn": "Test Area"},
        {"PRO_ID": 9, "PROJECT_NAME_EN": "Style Tower", "AREA_NAME_EN": "Test Area"},
        {"project_id": 9, "project_name_en": "Style Tower", "area_en": "Test Area"},
        {"projectNo": 9, "name_en": "Style Tower", "area": "Test Area"},
    ],
)
def test_column_naming_styles_are_all_understood(record):
    (row,) = parse_srerd_records([record], today=TODAY)
    assert (row.registry_project_no, row.project_name_en, row.area_en) == ("9", "Style Tower", "Test Area")


def test_a_bare_id_column_is_never_trusted_as_a_project_id():
    tally = RowTally()
    assert parse_srerd_records([{"id": 9, "projectNameEn": "Style Tower"}], today=TODAY, tally=tally) == []
    assert tally.dropped_count(srerd.R_NO_IDENTITY) == 1


def test_a_generic_name_column_is_routed_by_script():
    rows = parse_srerd_records([{"proId": 1, "projectName": "مشروع"}, {"proId": 2, "projectName": "Latin Tower"}], today=TODAY)
    assert (rows[0].project_name_ar, rows[0].project_name_en) == ("مشروع", "")
    assert (rows[1].project_name_en, rows[1].project_name_ar) == ("Latin Tower", "")


def test_an_explicit_english_column_wins_over_the_generic_one():
    (row,) = parse_srerd_records([{"proId": 1, "projectNameEn": "English", "projectName": "Other"}], today=TODAY)
    assert row.project_name_en == "English"


def test_a_row_without_a_name_is_dropped():
    tally = RowTally()
    assert parse_srerd_records([{"proId": 1, "areaNameEn": "Test Area"}, {"proId": 2, "projectNameEn": "  "}], today=TODAY, tally=tally) == []
    assert tally.dropped_count(srerd.R_NO_NAME) == 2


@pytest.mark.parametrize("raw", [None, "text", 5, ["list"], 2.5])
def test_entries_that_are_not_objects_are_dropped_and_counted(raw):
    tally = RowTally()
    rows = parse_srerd_records([raw, project(1, "Real")], today=TODAY, tally=tally)
    assert [r.project_name_en for r in rows] == ["Real"] and tally.dropped_count(srerd.R_NOT_OBJECT) == 1


def test_a_huge_value_drops_the_row():
    tally = RowTally()
    rows = parse_srerd_records([project(1, "x" * 501), project(2, "ok", areaNameEn="y" * 10_000_000)], today=TODAY, tally=tally)
    assert rows == [] and tally.dropped_count(srerd.R_OVERSIZE) == 2


def test_unicode_survives_into_the_rows():
    (row,) = parse_srerd_records([project(1, "Café Résidences ☆", projectNameAr="مساكن", areaNameEn="Zürich Road")], today=TODAY)
    assert (row.project_name_en, row.project_name_ar, row.area_en) == ("Café Résidences ☆", "مساكن", "Zürich Road")


def test_half_written_rows_are_dropped_without_crashing():
    tally = RowTally()
    rows = parse_srerd_records([{}, {"proId": 3}, {"projectNameEn": "Test X"}, project(4)], today=TODAY, tally=tally)
    assert [r.registry_project_no for r in rows] == ["4"] and tally.dropped == 3


def test_empty_input_gives_no_rows():
    assert parse_srerd_records([], today=TODAY) == []


# ---------------------------------------------------------------------------
# check_shape
# ---------------------------------------------------------------------------
def test_rows_with_no_name_column_are_refused_and_the_error_counts_columns_never_names_or_values():
    with pytest.raises(SourceError) as info:
        check_shape([{"colA": "PRIVATE-VALUE", "colB": 2}])
    message = str(info.value)
    assert "Columns seen: 2 name(s); none known" in message
    assert "colA" not in message and "PRIVATE-VALUE" not in message


def test_marker_named_columns_are_counted_but_never_shown():
    with pytest.raises(SourceError) as info:
        check_shape([{f"{KEY_MARKER}-p": 1, f"{KEY_MARKER}-d": 2}, {f"{KEY_MARKER}-x": 3}])
    message = str(info.value)
    assert "no recognised project name column" in message and "Columns seen: 3 name(s); none known" in message
    assert KEY_MARKER.lower() not in message.lower()


def test_a_list_with_no_objects_is_refused():
    with pytest.raises(SourceError, match="none is an object"):
        check_shape(["a", 1])


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------
def test_fetch_makes_one_anonymous_post_and_returns_the_rows():
    session = FakeSession([FakeResponse(200, load_fixture("srerd_projects.json"))])
    result = make_adapter(session).fetch()
    assert result.source == "srerd_projects" and len(result.rows) == 5 and result.source_refreshed is None
    assert result.notes[0] == "fetched 6 raw row(s)" and any("dropped 1 row(s)" in n for n in result.notes)
    (call,) = session.calls
    assert (call.method, call.url) == ("POST", srerd.SRERD_PROJECTS_URL)
    assert call.kwargs["json"] == {}
    headers = call.kwargs["headers"]
    assert headers["User-Agent"].startswith("uae-real-estate-scraper/") and "Mozilla" not in headers["User-Agent"]
    assert "Authorization" not in headers and "Cookie" not in headers
    assert call.kwargs["timeout"][0] > 0


def test_the_request_body_constant_cannot_be_changed_by_a_call():
    session = FakeSession([FakeResponse(200, load_fixture("srerd_projects.json"))])
    make_adapter(session).fetch()
    session.calls[0].kwargs["json"]["injected"] = True
    assert srerd.SRERD_REQUEST_BODY == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [project(1, "Test A")]},
        [project(1, "Test A")],
        {"result": {"items": [project(1, "Test A")]}},
        {"Projects": [project(1, "Test A")]},
    ],
)
def test_the_list_may_sit_in_any_common_envelope(payload):
    result = make_adapter(FakeSession([FakeResponse(200, payload)])).fetch()
    assert [r.project_name_en for r in result.rows] == ["Test A"]


def test_a_session_we_make_is_closed(monkeypatch):
    from src.sources import base

    made = []

    class Own(FakeSession):
        def __init__(self):
            super().__init__([FakeResponse(200, [project(1, "Test A")])])
            made.append(self)

    monkeypatch.setattr(base.requests, "Session", Own)
    make_adapter(None).fetch()
    assert made[0].closed is True


# -- switches --------------------------------------------------------------------
def test_it_is_off_unless_enabled_and_says_why():
    adapter = SrerdProjectsAdapter(make_cfg())
    assert adapter.name == "srerd_projects" and adapter.expects_rows is True
    assert adapter.enabled(make_cfg()) is False and "ENABLE_SRERD" in adapter.skip_reason(make_cfg())
    assert adapter.enabled(enabled_cfg()) is True and adapter.skip_reason(enabled_cfg()) == ""


def test_fetching_while_disabled_is_an_error_and_makes_no_request():
    session = FakeSession([])
    adapter = SrerdProjectsAdapter(make_cfg(), session=session)
    with pytest.raises(SourceError, match="ENABLE_SRERD"):
        adapter.fetch()
    assert session.calls == []


# -- shapes we do not recognise -----------------------------------------------------
def test_an_answer_with_no_list_names_its_known_keys_never_its_values():
    message = str(fetch_fails({"message": "PRIVATE-VALUE", "code": 7}))
    assert "no list of projects (it contained: 2 name(s); known: message, code)" in message
    assert "PRIVATE-VALUE" not in message


# A name in the answer can be DATA (an answer keyed by project name), and the Actions log of a public repository
# is public: the error counts the names and never repeats one that is not a known envelope name.
KEY_MARKER = "ZZKEYZZ"  # SYNTHETIC


def test_an_answer_keyed_by_project_name_has_its_keys_counted_not_shown():
    keyed = {f"{KEY_MARKER}-Fake Tower {n}": project(n, f"Fake Tower {n}") for n in range(1, 6)}
    keyed["total"] = 5
    message = str(fetch_fails(keyed))
    assert "no list of projects (it contained: 6 name(s); known: total)" in message
    assert KEY_MARKER.lower() not in message.lower() and "fake tower" not in message.lower()


@pytest.mark.parametrize("payload", ["just text", 42, None, True])
def test_a_scalar_answer_is_an_error(payload):
    assert "no list of projects" in str(fetch_fails(payload))


@pytest.mark.parametrize("payload", [[], {"data": []}, {"result": {"items": []}}])
def test_zero_projects_is_an_error_not_an_empty_register(payload):
    assert "zero projects" in str(fetch_fails(payload))


def test_rows_with_no_recognised_name_column_are_an_error():
    message = str(fetch_fails([{"colA": "PRIVATE", "colB": 1}]))
    assert "no recognised project name column" in message and "2 name(s); none known" in message
    assert "colA" not in message and "PRIVATE" not in message


def test_a_list_of_non_objects_is_an_error():
    assert "none is an object" in str(fetch_fails(["a", "b"]))


def test_a_non_json_answer_is_an_error_that_does_not_quote_the_body():
    message = str(fetch_fails(script=[FakeResponse(200, text="<html>PRIVATE-PAGE</html>", headers={"Content-Type": "text/html"})]))
    assert "not valid JSON" in message and "PRIVATE-PAGE" not in message


def test_a_stated_total_larger_than_the_rows_received_is_refused_as_a_partial_list():
    message = str(fetch_fails({"data": [project(1, "Test A"), project(2, "Test B")], "total": 50}))
    assert "2 of the 50" in message and "partial" in message


@pytest.mark.parametrize("total", [2, 1, "2"])
def test_a_total_that_matches_or_is_smaller_is_fine(total):
    result = make_adapter(FakeSession([FakeResponse(200, {"data": [project(1, "Test A"), project(2, "Test B")], "total": total})])).fetch()
    assert len(result.rows) == 2


def test_when_no_row_can_be_keyed_the_run_fails_and_says_why():
    message = str(fetch_fails([{"projectNameEn": f"Test {n}"} for n in range(4)]))
    assert "none of the 4 row(s) could be used" in message and srerd.R_NO_IDENTITY in message


def test_positional_ids_alone_fail_the_run_rather_than_writing_unstable_keys():
    message = str(fetch_fails([project(n, f"Test {n}") for n in range(1, 8)]))
    assert "none of the 7 row(s) could be used" in message


def test_more_than_half_unusable_fails_but_exactly_half_is_tolerated():
    good = [project(1, "Test A"), project(2, "Test B")]
    bad = {"projectNameEn": "Test Orphan"}
    assert len(make_adapter(FakeSession([FakeResponse(200, good + [bad, dict(bad)])])).fetch().rows) == 2
    assert "3 of 5 row(s) were unusable" in str(fetch_fails(good + [bad, dict(bad), dict(bad)]))


# -- HTTP failures --------------------------------------------------------------------
def test_a_rate_limited_answer_is_retried():
    sleeps = Sleeps()
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "3"}), FakeResponse(200, [project(1, "Test A")])])
    assert len(make_adapter(session, sleeps=sleeps).fetch().rows) == 1 and sleeps == [3.0]


def test_a_persistent_server_error_fails_after_the_retries():
    sleeps = Sleeps()
    message = str(fetch_fails(script=[FakeResponse(502, text="bad gateway")] * 4, sleeps=sleeps))
    assert "HTTP 502" in message and "gave up after 4 attempts" in message and len(sleeps) == 3


def test_a_timeout_is_retried_then_reported():
    assert "timed out" in str(fetch_fails(script=[requests.exceptions.ReadTimeout("slow")] * 4))


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_refusal_is_not_retried(status):
    session = FakeSession([FakeResponse(status, text="no")])
    adapter = make_adapter(session)
    with pytest.raises(SourceError, match=f"HTTP {status}"):
        adapter.fetch()
    assert len(session.calls) == 1


def test_credential_shaped_text_in_an_error_body_is_scrubbed_anyway():
    body = '{"error": "x", "client_secret": "PRIVATE-SECRET-VALUE", "next": "https://h.example/?api_key=PRIVATE-KEY-VALUE"}'
    error = fetch_fails(script=[FakeResponse(500, text=body)] * 4)
    assert_no_secret(full_text(error), "PRIVATE-SECRET-VALUE", "PRIVATE-KEY-VALUE")


# Changed in review: the old case ([0, 1, 2, 3, 4, 5] kept as 1..5) assumed a 0 id was a lone placeholder.
# A run 0..N-1 is a zero-based row index, so it is now treated as positions (see test_sources_review_regressions.py).
def test_a_zero_placeholder_in_an_otherwise_real_run_is_still_a_real_id_run():
    rows = parse_srerd_records([project(i, f"Test {i}") for i in (0, 11, 12, 13, 14, 15)], today=TODAY)
    assert [r.registry_project_no for r in rows] == ["11", "12", "13", "14", "15"]
