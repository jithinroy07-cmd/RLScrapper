"""
Tests for src/sources/base.py: the contract, the row-count guard, scrubbing, retries
and the tolerant record helpers. All data is SYNTHETIC; no network, no credentials.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
import requests

from src.config import load_config
from src.sources import base
from src.sources.base import (
    REDACTED,
    RetryPolicy,
    RowTally,
    SourceError,
    SourceResult,
    as_cell,
    as_date,
    as_text,
    check_row_count,
    describe_keys,
    describe_types,
    extract_records,
    extract_refreshed,
    extract_total,
    field_key,
    first_date,
    first_match,
    is_arabic_only,
    normalise_record,
    parse_json_response,
    resolve_today,
    scrub_text,
    scrub_url,
    send_with_retries,
    session_scope,
    url_has_credentials,
    user_agent,
)
from test_sources_support import (
    FAKE_KEY,
    FAKE_SECRET,
    FAKE_TOKEN,
    FakeResponse,
    FakeSession,
    Sleeps,
    assert_no_secret,
    full_text,
)


# ---------------------------------------------------------------------------
# check_row_count
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "new,previous,passes",
    [
        (100, 100, True),
        (80, 100, True),   # exactly 80 percent passes
        (79, 100, False),  # one below fails
        (4, 5, True),      # 80 percent of 5, no float rounding trouble
        (3, 5, False),
        (1000, 100, True),  # growth is fine
        (0, 1, False),
        (1, 1, True),
    ],
)
def test_row_drop_boundary_is_exact(new, previous, passes):
    if passes:
        check_row_count("src", new, previous, expects_rows=False, allow_drop=False)
    else:
        with pytest.raises(SourceError, match="src"):
            check_row_count("src", new, previous, expects_rows=False, allow_drop=False)


def test_a_drop_is_excused_only_by_allow_drop():
    check_row_count("src", 10, 100, expects_rows=True, allow_drop=True)
    with pytest.raises(SourceError):
        check_row_count("src", 10, 100, expects_rows=True, allow_drop=False)


@pytest.mark.parametrize("allow_drop", [False, True])
@pytest.mark.parametrize("previous", [None, 0, 50])
def test_zero_rows_from_a_source_that_expects_rows_is_always_an_error(allow_drop, previous):
    with pytest.raises(SourceError, match="0 rows"):
        check_row_count("src", 0, previous, expects_rows=True, allow_drop=allow_drop)


def test_zero_rows_from_a_quiet_source_is_fine_on_a_first_run_or_after_nothing():
    check_row_count("seed_csv", 0, None, expects_rows=False, allow_drop=False)
    check_row_count("seed_csv", 0, 0, expects_rows=False, allow_drop=False)


def test_a_quiet_source_going_from_rows_to_none_is_still_a_drop():
    with pytest.raises(SourceError):
        check_row_count("seed_csv", 0, 10, expects_rows=False, allow_drop=False)
    check_row_count("seed_csv", 0, 10, expects_rows=False, allow_drop=True)


def test_first_run_has_no_drop_check():
    check_row_count("src", 1, None, expects_rows=True, allow_drop=False)


@pytest.mark.parametrize("new,previous", [(-1, 5), (5, -1), (-1, None)])
def test_negative_counts_are_a_programming_error(new, previous):
    with pytest.raises(ValueError):
        check_row_count("src", new, previous, expects_rows=False, allow_drop=False)


def test_the_error_names_the_source_and_the_threshold():
    with pytest.raises(SourceError) as info:
        check_row_count("dld_projects", 10, 100, expects_rows=True, allow_drop=False)
    assert "dld_projects" in str(info.value) and "80" in str(info.value) and "ALLOW_ROW_DROP" in str(info.value)


def test_source_result_defaults_are_independent_lists():
    a, b = SourceResult("a", [], "t"), SourceResult("b", [], "t")
    a.notes.append("x")
    a.candidates.append("y")
    assert b.notes == [] and b.candidates == [] and a.source_refreshed is None


# ---------------------------------------------------------------------------
# scrub_text
# ---------------------------------------------------------------------------
def test_a_known_secret_is_replaced():
    assert scrub_text(f"failed with {FAKE_KEY} today", [FAKE_KEY]) == f"failed with {REDACTED} today"


def test_a_known_secret_is_replaced_in_its_url_and_json_escaped_forms():
    from urllib.parse import quote, quote_plus

    secret = 's3cr3t va/lue&="x'
    for shown in (secret, quote(secret, safe=""), quote_plus(secret), secret.replace("\\", "\\\\").replace('"', '\\"')):
        out = scrub_text(f"prefix {shown} suffix", [secret])
        assert REDACTED in out
        assert "s3cr3t" not in out, shown


def test_the_longer_of_two_overlapping_secrets_is_replaced_whole():
    assert scrub_text("abcdefgh", ["abcd", "abcdefgh"]) == REDACTED


@pytest.mark.parametrize("junk", ["", "   ", None, 123, b"x"])
def test_empty_or_non_text_secrets_are_ignored_not_applied(junk):
    # An empty secret would otherwise be "replaced" between every character.
    assert scrub_text("abc", [junk]) == "abc"


def test_credential_shaped_text_is_scrubbed_without_knowing_the_secret():
    jwt = "eyJ" + "fake" * 4 + "." + "eyJ" + "fake" * 4 + "." + "sig"
    google_key = "AIza" + "F" * 35
    samples = {
        "GET https://h.example/x?api_key=ABCDEF123&q=ok": "ABCDEF123",
        "url https://h.example/x?client_secret=ZZZ999&q=ok": "ZZZ999",
        '{"client_secret": "abc123xyz", "other": "keep"}': "abc123xyz",
        '{"access_token":"tok-4567-fake"}': "tok-4567-fake",
        "Authorization: Bearer abcdef123456": "abcdef123456",
        "authorization=Basic dXNlcjpwYXNz": "dXNlcjpwYXNz",
        "the header was Bearer abcdef123456 here": "abcdef123456",
        "connect to https://user:hunter2@host.example/x": "hunter2",
        f"token {jwt} end": jwt,
        f"key {google_key} end": google_key,
    }
    for text, secret in samples.items():
        out = scrub_text(text)
        assert secret not in out, text
        assert REDACTED in out, text


def test_scrubbing_keeps_the_innocent_parts():
    out = scrub_text("GET https://h.example/x?api_key=K1&q=tower")
    assert "q=tower" in out and "h.example" in out
    assert scrub_text('{"client_secret": "abc", "other": "keep"}').count("keep") == 1


def test_arabic_and_unicode_survive_scrubbing():
    assert scrub_text("خطأ في الطلب – café") == "خطأ في الطلب – café"


def test_whitespace_and_control_characters_collapse_to_one_line():
    assert scrub_text("a\nb\tc\x00d\r\n e") == "a b c d e"


def test_output_is_truncated_and_marked():
    out = scrub_text("x" * 1000, limit=50)
    assert len(out) <= 50 and out.endswith("...")
    assert scrub_text("short", limit=50) == "short"


def test_a_huge_input_is_bounded():
    out = scrub_text("y" * 5_000_000)
    assert len(out) <= 200


def test_truncation_cannot_leave_half_a_secret_visible():
    # The secret straddles the cut: it must be replaced BEFORE the text is shortened.
    text = "x" * 190 + FAKE_KEY
    out = scrub_text(text, [FAKE_KEY], limit=200)
    assert "fake-k" not in out and "fake" not in out


@pytest.mark.parametrize("value", [None, 5, 3.5, ValueError("boom"), ["a"], {"k": "v"}])
def test_any_value_can_be_scrubbed(value):
    assert isinstance(scrub_text(value), str)


def test_scrub_text_of_none_is_empty():
    assert scrub_text(None) == ""


# ---------------------------------------------------------------------------
# scrub_url / url_has_credentials
# ---------------------------------------------------------------------------
def test_scrub_url_drops_userinfo_and_fragment_and_redacts_credential_values():
    out = scrub_url("https://user:pw@host.example:8443/p?api_key=K1&q=1#frag")
    assert out == f"https://host.example:8443/p?api_key={REDACTED}&q=1"


@pytest.mark.parametrize("name", ["sig", "SIG", "Auth", "authorization", "X-Goog-Signature", "client_secret", "apiKey", "pwd"])
def test_scrub_url_redacts_every_name_url_has_credentials_would_refuse(name):
    # The query-pair pattern in scrub_text is a second net; this proves the first one on its own,
    # including "sig", which no substring rule can safely cover.
    url = f"https://host.example/x?{name}=TOPSECRET123&q=1"
    assert url_has_credentials(url)
    out = scrub_url(url)
    assert "TOPSECRET123" not in out and f"{quote(name, safe='')}={REDACTED}" in out  # an Authorization-style pair may take the rest of the line with it


def test_scrub_url_keeps_ordinary_urls_readable():
    assert scrub_url("https://news.example.com/a/b?id=5") == "https://news.example.com/a/b?id=5"


def test_scrub_url_survives_an_invalid_port_and_still_hides_the_key():
    out = scrub_url("http://host:abc/x?key=SECRETVALUE")
    assert "SECRETVALUE" not in out


@pytest.mark.parametrize("value", [None, "", "not a url", 42])
def test_scrub_url_never_raises(value):
    assert isinstance(scrub_url(value), str)


@pytest.mark.parametrize(
    "url",
    [
        "https://u:p@h.example/x",
        "https://h.example/x?api_key=abc",
        "https://h.example/x?access_token=1",
        "https://h.example/x?client_secret=z",
        "https://h.example/x?auth=1",
        "https://h.example/x?X-Goog-Signature=abc",
        "https://h.example/x?page=1&token=abc",
        "http://[bad",
    ],
)
def test_urls_with_credentials_are_detected(url):
    assert url_has_credentials(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://news.example.com/a?id=5&keyword=tower",
        "https://h.example/?author=jane",
        "https://h.example/?key=",
        "https://h.example/x",
        "",
        None,
    ],
)
def test_ordinary_urls_are_not_flagged(url):
    assert not url_has_credentials(url)


# ---------------------------------------------------------------------------
# RowTally
# ---------------------------------------------------------------------------
def test_tally_counts_and_describes_in_a_stable_order():
    tally = RowTally()
    tally.drop("b reason")
    tally.drop("a reason")
    tally.drop("a reason")
    tally.adjust("c", 3)
    assert (tally.dropped, tally.adjusted) == (3, 3)
    assert tally.dropped_count("a reason") == 2 and tally.dropped_count("never") == 0
    assert tally.adjusted_count("c") == 3
    assert tally.dropped_summary() == "a reason (x2); b reason (x1)"
    assert tally.notes() == [
        "dropped 3 row(s): a reason (x2); b reason (x1)",
        "blanked a value on 3 occasion(s): c (x3)",
    ]


def test_an_empty_tally_says_nothing():
    tally = RowTally()
    assert tally.notes() == [] and tally.dropped == 0 and tally.dropped_summary() == ""


# ---------------------------------------------------------------------------
# send_with_retries
# ---------------------------------------------------------------------------
def run(script, *, secrets=(), policy=None, label="src"):
    session = FakeSession(script)
    sleeps = Sleeps()
    kwargs = {"label": label, "secrets": secrets, "sleep": sleeps}
    if policy is not None:
        kwargs["policy"] = policy
    result = send_with_retries(lambda: session.get("http://x.example/api"), **kwargs)
    return result, session, sleeps


def fail(script, *, secrets=(), policy=None, label="src"):
    session = FakeSession(script)
    sleeps = Sleeps()
    kwargs = {"label": label, "secrets": secrets, "sleep": sleeps}
    if policy is not None:
        kwargs["policy"] = policy
    with pytest.raises(SourceError) as info:
        send_with_retries(lambda: session.get("http://x.example/api"), **kwargs)
    return info.value, session, sleeps


def test_a_good_answer_returns_at_once():
    response, session, sleeps = run([FakeResponse(200, {"ok": 1})])
    assert response.json() == {"ok": 1} and len(session.calls) == 1 and sleeps == []


def test_a_server_error_is_retried_with_doubling_waits():
    response, session, sleeps = run([FakeResponse(503), FakeResponse(502), FakeResponse(500), FakeResponse(200, {"ok": 1})])
    assert response.status_code == 200 and len(session.calls) == 4
    assert sleeps == [2.0, 4.0, 8.0]


def test_giving_up_says_how_many_attempts_and_what_happened():
    # Changed on the public-log audit: this used to pin the response BODY ("upstream broke") in the message.
    # The Actions log is public and an error page can echo the query, so only the status is kept.
    error, session, sleeps = fail([FakeResponse(500, text="upstream broke")] * 4)
    assert "HTTP 500" in str(error) and "gave up after 4 attempts" in str(error)
    assert "upstream broke" not in str(error)
    assert len(session.calls) == 4 and len(sleeps) == 3


def test_the_wait_is_capped():
    policy = RetryPolicy(max_attempts=6, backoff_seconds=10, max_backoff_seconds=25)
    _, _, sleeps = fail([FakeResponse(500)] * 6, policy=policy)
    assert sleeps == [10, 20, 25, 25, 25]


def test_one_attempt_means_no_retry_and_no_gave_up_suffix():
    error, session, sleeps = fail([FakeResponse(500)], policy=RetryPolicy(max_attempts=1))
    assert len(session.calls) == 1 and sleeps == [] and "gave up" not in str(error)


def test_a_policy_needs_at_least_one_attempt():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)


@pytest.mark.parametrize(
    "retry_after,expected",
    [("7", 7.0), ("0", 0.0), ("9999", 60.0), ("abc", 2.0), ("-5", 2.0), ("nan", 2.0), ("inf", 2.0), ("Wed, 21 Oct 2026 07:28:00 GMT", 2.0)],
)
def test_429_honours_a_sane_retry_after(retry_after, expected):
    _, _, sleeps = run([FakeResponse(429, headers={"Retry-After": retry_after}), FakeResponse(200, {})])
    assert sleeps == [expected]


def test_429_without_a_header_uses_our_own_backoff():
    _, _, sleeps = run([FakeResponse(429), FakeResponse(200, {})])
    assert sleeps == [2.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
def test_client_errors_are_never_retried(status):
    error, session, sleeps = fail([FakeResponse(status, text="nope")])
    assert len(session.calls) == 1 and sleeps == []
    assert f"HTTP {status}" in str(error)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_say_so(status):
    error, _, _ = fail([FakeResponse(status)])
    assert "credentials rejected or access not approved" in str(error)


def test_a_timeout_is_retried_then_reported():
    response, _, sleeps = run([requests.exceptions.ReadTimeout("slow"), FakeResponse(200, {})])
    assert response.status_code == 200 and sleeps == [2.0]
    error, session, _ = fail([requests.exceptions.ConnectTimeout("slow")] * 4)
    assert "timed out" in str(error) and len(session.calls) == 4


@pytest.mark.parametrize("exc", [requests.exceptions.ConnectionError("reset"), requests.exceptions.ChunkedEncodingError("cut")])
def test_dropped_connections_are_retried(exc):
    response, session, _ = run([exc, FakeResponse(200, {})])
    assert response.status_code == 200 and len(session.calls) == 2


def test_a_connection_error_that_embeds_the_key_in_its_url_is_scrubbed():
    boom = requests.exceptions.ConnectionError(f"Max retries exceeded with url: /x?key={FAKE_KEY}&q=1")
    error, _, _ = fail([boom] * 4, secrets=[FAKE_KEY])
    assert_no_secret(str(error), FAKE_KEY)
    assert_no_secret(full_text(error), FAKE_KEY)
    assert error.__cause__ is None and error.__context__ is None


def test_an_unexpected_exception_is_not_retried_and_is_scrubbed():
    error, session, sleeps = fail([ValueError(f"bad value {FAKE_SECRET}")], secrets=[FAKE_SECRET])
    assert len(session.calls) == 1 and sleeps == []
    assert "ValueError" in str(error)
    assert_no_secret(full_text(error), FAKE_SECRET)


def test_an_error_body_that_echoes_secrets_never_reaches_the_error():
    # Changed on the public-log audit: the body used to be printed (scrubbed, truncated). It is no longer
    # printed at all, so there is nothing for a scrubber to miss; the status is what remains.
    body = json.dumps({"error": "invalid_client", "client_secret": FAKE_SECRET, "hint": f"Authorization: Bearer {FAKE_TOKEN}"})
    for secrets in ((FAKE_SECRET, FAKE_TOKEN), ()):
        error, _, _ = fail([FakeResponse(401, text=body)], secrets=secrets)
        assert_no_secret(full_text(error), FAKE_SECRET, FAKE_TOKEN)
        assert "invalid_client" not in str(error) and "HTTP 401" in str(error)


def test_a_response_with_no_status_is_reported_not_retried():
    error, session, _ = fail([SimpleNamespace(text="x")])
    assert "no status code" in str(error) and len(session.calls) == 1


def test_a_boolean_status_is_not_a_status():
    error, _, _ = fail([SimpleNamespace(status_code=True, text="x")])
    assert "no status code" in str(error)


def test_an_unreadable_body_does_not_hide_the_status():
    class BrokenBody:
        status_code = 500
        headers = {}

        @property
        def text(self):
            raise RuntimeError("cannot decode")

    error, _, _ = fail([BrokenBody()] * 4)
    assert "HTTP 500" in str(error)


def test_retries_are_logged_without_secrets(caplog):
    with caplog.at_level("DEBUG"):
        fail([requests.exceptions.ConnectionError(f"url ?key={FAKE_KEY}")] * 4, secrets=[FAKE_KEY])
    assert "retrying" in caplog.text
    assert_no_secret(caplog.text, FAKE_KEY)


# The Actions log of a public repository is public. This marker is not secret-shaped on purpose: no scrubber
# can know it, so only "never printed" keeps it out of a message or a log record.
LEAK_MARKER = "zq-hostile-marker-4471"  # SYNTHETIC


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_a_response_body_never_reaches_the_error_or_the_log(status, caplog):
    body = f"<html>Your query was: Fake Tower 7 {LEAK_MARKER}</html>"
    with caplog.at_level("DEBUG"):
        error, _, _ = fail([FakeResponse(status, text=body)] * 4)
    assert LEAK_MARKER not in full_text(error) and "Fake Tower" not in str(error)
    assert LEAK_MARKER not in caplog.text
    assert f"HTTP {status}" in str(error)  # what stays


@pytest.mark.parametrize(
    "exc,kept",
    [
        (requests.exceptions.ConnectionError(f"Max retries exceeded with url: /x?q=Fake+Tower+7 {LEAK_MARKER}"), "ConnectionError"),
        (requests.exceptions.ChunkedEncodingError(f"cut {LEAK_MARKER}"), "ChunkedEncodingError"),
        (requests.exceptions.ReadTimeout(f"timed out {LEAK_MARKER}"), "ReadTimeout"),
        (ValueError(f"bad value {LEAK_MARKER}"), "ValueError"),
    ],
    ids=lambda v: v if isinstance(v, str) else type(v).__name__,
)
def test_an_exceptions_text_never_reaches_the_error_or_the_log(exc, kept, caplog):
    with caplog.at_level("DEBUG"):
        error, _, _ = fail([exc] * 4)
    assert LEAK_MARKER not in full_text(error) and "Fake Tower" not in str(error)
    assert LEAK_MARKER not in caplog.text and "Fake Tower" not in caplog.text
    assert kept in str(error)  # the class name is the diagnosis that stays


def test_a_redirect_says_so_without_the_location_or_the_body():
    error, _, _ = fail([FakeResponse(307, text=LEAK_MARKER, headers={"Location": f"http://elsewhere.example/{LEAK_MARKER}"})])
    assert LEAK_MARKER not in full_text(error) and "HTTP 307" in str(error)


# ---------------------------------------------------------------------------
# parse_json_response
# ---------------------------------------------------------------------------
def test_json_is_returned_as_is():
    assert parse_json_response(FakeResponse(200, [1, 2]), label="src") == [1, 2]


def test_a_non_json_body_is_reported_by_status_and_size_never_by_content():
    response = FakeResponse(200, text="<html>PRIVATEBODY</html>", headers={"Content-Type": "text/html"})
    with pytest.raises(SourceError) as info:
        parse_json_response(response, label="src")
    message = str(info.value)
    assert "HTTP 200" in message and "24 chars" in message and "text/html" in message
    assert "PRIVATEBODY" not in message


@pytest.mark.parametrize(
    "header,shown",
    [
        ("application/problem+json; charset=utf-8", "application/problem+json"),
        (f"text/html; note={LEAK_MARKER}", "text/html"),
        (f"{LEAK_MARKER} Fake Tower 7", "unknown"),
        (f"text/{LEAK_MARKER}", "unknown"),  # shaped like a media type, but not one we know: still server text
        ("", "unknown"),
    ],
)
def test_the_content_type_is_shown_only_when_it_is_a_well_known_media_type(header, shown):
    # The header is the server's text too: parameters and any name outside the fixed list are never echoed.
    response = FakeResponse(200, text="<html>x</html>", headers={"Content-Type": header})
    with pytest.raises(SourceError) as info:
        parse_json_response(response, label="src")
    message = str(info.value)
    assert f"content-type {shown})" in message
    assert LEAK_MARKER not in message and "Fake Tower" not in message


def test_a_response_whose_text_also_fails_is_still_reported():
    class Broken:
        status_code = 200
        headers = None

        def json(self):
            raise ValueError("bad")

        @property
        def text(self):
            raise RuntimeError("no body")

    with pytest.raises(SourceError, match="not valid JSON"):
        parse_json_response(Broken(), label="src")


def test_absurdly_nested_json_is_an_error_not_a_crash():
    class Deep:
        status_code = 200
        headers = {}
        text = "[" * 10

        def json(self):
            raise RecursionError("too deep")

    with pytest.raises(SourceError, match="not valid JSON"):
        parse_json_response(Deep(), label="src")


# ---------------------------------------------------------------------------
# Tolerant records
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [("PROJECT_EN", "projecten"), ("projectEn", "projecten"), ("Project Name (EN)", "projectnameen"), ("  Zone ", "zone"), (5, "5"), ("", "")],
)
def test_field_key_ignores_case_and_punctuation(raw, expected):
    assert field_key(raw) == expected


def test_when_two_keys_collapse_the_first_wins():
    assert normalise_record({"Project_EN": "a", "PROJECT EN": "b", "zone": "z"}) == {"projecten": "a", "zone": "z"}


def test_first_match_follows_alias_priority_and_skips_unusable_values():
    record = {"b": "", "a": "x", "c": "y"}
    assert first_match(record, ("b", "a", "c"), as_text) == "x"
    assert first_match(record, ("missing",), as_text) is None
    assert first_match({}, ("a",), as_text) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("  Test   Tower ", "Test Tower"),
        ("مشروع اختبار", "مشروع اختبار"),
        (12, "12"),
        (3.0, "3"),
        (3.5, "3.5"),
        (None, None),
        (True, None),
        (float("nan"), None),
        (float("inf"), None),
        ([], None),
        ({}, None),
        ("N/A", None),
        ("-", None),
        ("", None),
        ("   ", None),
        (0, None),
    ],
)
def test_as_text(value, expected):
    assert as_text(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("  5 ", "5"), ("  ", None), (0, 0), ("0", "0"), (2.5, 2.5), (None, None), (True, None), (float("nan"), None), ([1], None)],
)
def test_as_cell_passes_numbers_through_for_the_model(value, expected):
    assert as_cell(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-10-25", "2026-10-25"),
        ("25-10-2026", "2026-10-25"),
        ("03-04-2026", "2026-04-03"),  # day first: 3 April, not 4 March
        ("2026-10-25T03:00:00Z", "2026-10-25"),
        ("1970-01-01", None),
        ("0001-01-01T00:00:00", None),
        ("2026-02-30", None),
        ("garbage", None),
        ("", None),
        (None, None),
    ],
)
def test_as_date(value, expected):
    assert as_date(value) == expected


def test_first_date_counts_what_it_cannot_read():
    tally = RowTally()
    assert first_date({"d": "soon"}, ("d",), tally) is None
    assert tally.adjusted_count("date not readable") == 1


def test_first_date_falls_through_to_the_next_alias_and_still_counts():
    tally = RowTally()
    assert first_date({"a": "soon", "b": "15-09-2026"}, ("a", "b"), tally) == "2026-09-15"
    assert tally.adjusted == 1


def test_first_date_treats_the_known_no_date_sentinels_quietly():
    tally = RowTally()
    assert first_date({"a": "0001-01-01T00:00:00", "b": "", "c": None}, ("a", "b", "c", "absent"), tally) is None
    assert tally.adjusted == 0
    assert first_date({"a": "1970-01-01", "b": "2027-01-02"}, ("a", "b"), tally) == "2027-01-02"
    assert tally.adjusted_count("epoch-zero date read as blank") == 1


@pytest.mark.parametrize("text,expected", [("مشروع", True), ("Tower مشروع", False), ("123", False), ("", False), ("Tower", False)])
def test_is_arabic_only(text, expected):
    assert is_arabic_only(text) is expected


# (code point, is it in one of the five Arabic blocks the AR column accepts). U+0600 and U+08FF are invisible, so
# the pattern is written with escapes and these cases are built with chr(), never typed as literals.
ARABIC_EDGES = [
    (0x05FF, False), (0x0600, True), (0x06FF, True), (0x0700, False),  # Arabic
    (0x074F, False), (0x0750, True), (0x077F, True), (0x0780, False),  # Arabic Supplement
    (0x089F, False), (0x08A0, True), (0x08FF, True), (0x0900, False),  # Arabic Extended-A
    (0xFB4F, False), (0xFB50, True), (0xFDFF, True), (0xFE00, False),  # Presentation Forms-A
    (0xFE6F, False), (0xFE70, True), (0xFEFF, True), (0xFF00, False),  # Presentation Forms-B
]


@pytest.mark.parametrize("code_point,inside", ARABIC_EDGES, ids=[f"U+{cp:04X}" for cp, _ in ARABIC_EDGES])
def test_is_arabic_only_matches_exactly_the_five_arabic_blocks(code_point, inside):
    assert is_arabic_only(chr(code_point)) is inside


def test_the_arabic_pattern_is_written_with_escapes_not_invisible_characters():
    line = next(l for l in Path(base.__file__).read_text(encoding="utf-8").splitlines() if l.startswith("_ARABIC_RE = "))
    assert line.isascii(), "a literal U+0600 or U+08FF in a regex class is invisible to a reviewer; use \\uXXXX escapes"
    for escape in ("\\u0600", "\\u06ff", "\\u0750", "\\u077f", "\\u08a0", "\\u08ff", "\\ufb50", "\\ufdff", "\\ufe70", "\\ufeff"):
        assert escape in line


# A name that came back in a server's answer can be DATA (an answer keyed by project name), and the Actions log
# of a public repository is public. describe_keys therefore counts the names and repeats only the ones this
# code defines itself (ENVELOPE_KEYS). SYNTHETIC marker, not secret-shaped on purpose.
KEY_MARKER = "ZZKEYZZ"


def test_describe_keys_gives_a_count_and_only_the_known_envelope_names():
    assert describe_keys([]) == "none"
    assert describe_keys(["data"]) == "1 name(s); known: data"
    assert describe_keys(["Total_Count", "DATA", "items"]) == "3 name(s); known: data, items, totalcount"
    assert describe_keys({"error": "x", "status": 1}) == "2 name(s); known: error, status"
    assert describe_keys(iter(["token"])) == "1 name(s); known: token"  # a one-shot iterator is fine


def test_describe_keys_never_repeats_a_name_that_is_not_a_known_envelope_name():
    keys = [f"{KEY_MARKER}-Fake Tower {n}" for n in range(40)] + ["x" * 5000, "a\nb", "", "data-" + KEY_MARKER, "datas"]
    shown = describe_keys(keys)
    assert shown == "45 name(s); none known"
    both = describe_keys({**dict.fromkeys(keys, 1), "Data": [], "STATUS": 1})
    assert both == "47 name(s); known: data, status"
    for text in (shown, both):
        assert KEY_MARKER.lower() not in text.lower() and "fake tower" not in text.lower() and "xxxx" not in text


def test_describe_keys_shows_a_known_name_the_way_the_code_spells_it_not_the_way_the_server_did():
    assert describe_keys(["D.A.T.A", " Total-Count "]) == "2 name(s); known: data, totalcount"


def test_the_envelope_names_cover_every_name_the_extractors_look_for():
    looked_for = {*base.LIST_KEYS_DEFAULT, *base._TOTAL_KEYS, *base._REFRESH_KEYS, "accesstoken", "token"}
    assert looked_for <= set(base.ENVELOPE_KEYS)
    assert all(name == field_key(name) for name in base.ENVELOPE_KEYS)  # compared after field_key: spelt that way
    assert len(set(base.ENVELOPE_KEYS)) == len(base.ENVELOPE_KEYS)


def test_describe_types_counts_by_name():
    assert describe_types([1, 2, "a", None]) == "NoneType x1, int x2, str x1"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ([1, 2], [1, 2]),
        ([], []),
        ({"data": [1]}, [1]),
        ({"Results": [2]}, [2]),
        ({"data": {"items": [3]}}, [3]),
        ({"items": [1], "data": [2]}, [2]),  # priority order: data before items
        ({"Data": [1], "DATA": [2]}, [1]),   # same normalised key: the first wins
        ({"data": "x"}, None),
        ({"data": {"nothing": 1}}, None),
        ({}, None),
        (None, None),
        ("text", None),
        (5, None),
    ],
)
def test_extract_records(payload, expected):
    assert extract_records(payload) == expected


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"total": 5}, 5),
        ({"Total": "12"}, 12),
        ({"total_records": 7}, 7),
        ({"recordsTotal": 8}, 8),
        ({"@odata.count": 9}, 9),
        ({"meta": {"totalCount": 7}}, 7),
        ({"count": 5}, None),       # a bare count is ambiguous: it may be this page's size
        ({"total": True}, None),
        ({"total": -1}, None),
        ({"total": 3.5}, None),
        ({"total": "abc"}, None),
        ({}, None),
        ([1], None),
        (None, None),
    ],
)
def test_extract_total_reads_only_unambiguous_totals(payload, expected):
    assert extract_total(payload) == expected


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"last_updated": "2026-10-20"}, "2026-10-20"),
        ({"lastModified": "20-10-2026"}, "2026-10-20"),
        ({"last_updated": "garbage"}, None),
        ({"last_updated": "1970-01-01"}, None),
        ({}, None),
        ([], None),
        (None, None),
    ],
)
def test_extract_refreshed(payload, expected):
    assert extract_refreshed(payload) == expected


def test_resolve_today_reduces_datetimes_to_their_date():
    assert resolve_today(date(2026, 10, 25)) == date(2026, 10, 25)
    assert resolve_today(datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc)) == date(2026, 10, 25)
    default = resolve_today(None)
    assert type(default) is date


def test_the_user_agent_identifies_the_project_and_is_not_a_browser():
    agent = user_agent(load_config(env={"GEOCODE_CONTACT": "ops@example.com"}))
    assert agent.startswith("uae-real-estate-scraper/") and "ops@example.com" in agent
    assert "Mozilla" not in agent


# ---------------------------------------------------------------------------
# session_scope
# ---------------------------------------------------------------------------
def test_a_supplied_session_is_used_and_left_open():
    session = FakeSession([])
    with session_scope(session) as used:
        assert used is session
    assert session.closed is False


def test_a_session_we_create_is_closed_even_when_the_body_fails(monkeypatch):
    made = []

    class Created(FakeSession):
        def __init__(self):
            super().__init__([])
            made.append(self)

    monkeypatch.setattr(base.requests, "Session", Created)
    with pytest.raises(RuntimeError):
        with session_scope(None) as session:
            assert session is made[0] and not session.closed
            raise RuntimeError("body failed")
    assert made[0].closed is True
