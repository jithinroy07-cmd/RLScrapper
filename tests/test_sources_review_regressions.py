"""
Regression tests from the adversarial review of src/sources/ (25 Oct 2026). Each test here
failed on the code as first delivered; the comment above it says what went wrong.
SYNTHETIC data only; no network (the redirect test uses an in-process requests adapter).
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
import requests

from src.sources import gemini_news
from src.sources.base import SourceError, parse_json_response
from src.sources.dld_projects import DldProjectsAdapter
from src.sources.gemini_news import GeminiNewsAdapter
from src.sources.srerd_projects import parse_srerd_records
from test_sources_support import (
    FAKE_KEY,
    FAKE_SECRET,
    TODAY,
    FakeGeminiClient,
    FakeResponse,
    FakeSession,
    Sleeps,
    dld_cfg,
    gemini_cfg,
    grounding_response,
    page,
    token_ok,
)


def dld_row(number: int) -> dict[str, Any]:
    return {"PROJECT_NUMBER": number, "PROJECT_NAME": f"Test Tower {number}", "DEVELOPER_NAME": "Example Developer LLC"}


def dld_adapter(session: Any, **kwargs: Any) -> DldProjectsAdapter:
    return DldProjectsAdapter(dld_cfg(), session=session, today=TODAY, sleep=Sleeps(), **kwargs)


# ---------------------------------------------------------------------------
# Redirects: the token request carries the client id and secret in its body. requests
# re-sends a POST body on a 307/308 to ANOTHER host (it only strips the Authorization
# header), so a redirecting token endpoint received the secret.
# ---------------------------------------------------------------------------
class RecordingAdapter(requests.adapters.BaseAdapter):
    """An in-process transport: answers every request with a redirect to another host."""

    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status
        self.sent: list[requests.PreparedRequest] = []

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        self.sent.append(request)
        response = requests.Response()
        response.status_code = self.status
        response.headers["Location"] = "http://other-host.example/collect"
        response.url = request.url or ""
        response.request = request
        response._content = b""
        response.raw = io.BytesIO(b"")
        return response

    def close(self) -> None:
        return None


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirecting_token_endpoint_never_receives_the_secret_elsewhere(status):
    transport = RecordingAdapter(status)
    session = requests.Session()
    session.mount("http://", transport)
    adapter = dld_adapter(session, token_url="http://token-host.example/oauth/token")
    with pytest.raises(SourceError) as info:
        adapter.fetch()
    assert f"HTTP {status}" in str(info.value) and "redirect" in str(info.value).lower()
    assert [r.url for r in transport.sent] == ["http://token-host.example/oauth/token"]  # nothing was sent to the other host
    assert FAKE_SECRET not in str(info.value) and FAKE_KEY not in str(info.value)


def test_no_call_that_carries_credentials_follows_redirects():
    session = FakeSession([token_ok(), page([dld_row(1)], total=1), page([])])
    dld_adapter(session, page_size=5).fetch()
    assert session.calls and all(call.kwargs.get("allow_redirects") is False for call in session.calls)


# ---------------------------------------------------------------------------
# The documented token request (data.dubai catalogue page for the projects dataset):
# the grant type is in the URL query string, client_id and client_secret in the body.
# ---------------------------------------------------------------------------
def test_the_token_request_follows_the_documented_shape():
    session = FakeSession([token_ok(), page([dld_row(1)], total=1), page([])])
    dld_adapter(session, page_size=5).fetch()
    token_call = session.calls[0]
    assert token_call.url.endswith("/oauth/client_credential/accesstoken?grant_type=client_credentials")
    assert token_call.kwargs["data"]["client_id"] == FAKE_KEY and token_call.kwargs["data"]["client_secret"] == FAKE_SECRET
    assert FAKE_KEY not in token_call.url and FAKE_SECRET not in token_call.url


# ---------------------------------------------------------------------------
# Paging must not end early. Two ways the old loop returned a partial register with no error:
#   * the envelope's "total" is the size of THIS page (or any number below the register);
#   * no total at all and the server serves fewer rows per page than asked (a clamped limit),
#     which looked like the last page.
# The end of the register is now an empty page; a probe request that fails is tolerated.
# ---------------------------------------------------------------------------
def numbered(first: int, last: int) -> list[dict[str, Any]]:
    return [dld_row(n) for n in range(first, last + 1)]


def test_a_total_that_is_only_the_size_of_one_page_does_not_end_paging():
    session = FakeSession(
        [token_ok(), page(numbered(1, 3), total=3), page(numbered(4, 6), total=3), page(numbered(7, 7), total=3), page([], total=3)]
    )
    result = dld_adapter(session, page_size=3).fetch()
    assert [r.registry_project_no for r in result.rows] == ["1", "2", "3", "4", "5", "6", "7"]
    assert any("stated total" in note for note in result.notes)


def test_a_server_that_clamps_the_page_size_does_not_end_the_register_at_the_first_page():
    session = FakeSession([token_ok(), page(numbered(1, 3)), page(numbered(4, 6)), page(numbered(7, 7)), page([])])
    result = dld_adapter(session, page_size=5).fetch()
    assert len(result.rows) == 7
    assert [c.kwargs["params"]["offset"] for c in session.calls if c.method == "GET"] == [0, 3, 6, 7]


def test_the_end_is_confirmed_by_an_empty_page_and_a_failing_confirmation_is_tolerated_with_a_note():
    session = FakeSession([token_ok(), page(numbered(1, 3), total=3), FakeResponse(400, text="offset out of range")])
    result = dld_adapter(session, page_size=3).fetch()
    assert len(result.rows) == 3
    assert any("not confirmed" in note for note in result.notes)


# ---------------------------------------------------------------------------
# JSON decoding: requests decodes a text/* body as ISO-8859-1 when no charset is sent, so a
# UTF-8 Arabic name came out as mojibake and was stored as if it were the name.
# ---------------------------------------------------------------------------
def _real_response(body: bytes, content_type: str) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = body
    response.headers["Content-Type"] = content_type
    response.encoding = requests.utils.get_encoding_from_headers(response.headers)
    return response


@pytest.mark.parametrize("content_type", ["text/plain", "text/html", "text/plain; charset=iso-8859-1", "application/json"])
def test_json_is_decoded_as_utf8_whatever_the_content_type_says(content_type):
    body = json.dumps({"data": [{"proName": "الشارقة"}]}, ensure_ascii=False).encode("utf-8")
    assert parse_json_response(_real_response(body, content_type), label="t")["data"][0]["proName"] == "الشارقة"


def test_a_utf8_bom_is_tolerated_and_garbage_bytes_are_still_an_error():
    assert parse_json_response(_real_response(b"\xef\xbb\xbf" + b'{"a": 1}', "text/plain"), label="t") == {"a": 1}
    with pytest.raises(SourceError):
        parse_json_response(_real_response(b"\xff\xfe<html>", "text/html"), label="t")


# ---------------------------------------------------------------------------
# SRERD: ids that are row positions must not become keys. Zero-based positions (0..N-1,
# the usual array index) and a run with one id missing slipped past the "exactly 1..N" test.
# ---------------------------------------------------------------------------
def srerd_project(pro_id: Any, n: int) -> dict[str, Any]:
    row: dict[str, Any] = {"projectNameEn": f"Test Gardens {n}"}
    if pro_id is not None:
        row["proId"] = pro_id
    return row


def test_zero_based_row_positions_are_not_ids():
    rows = parse_srerd_records([srerd_project(n, n) for n in range(0, 8)], today=TODAY)
    assert rows == []


def test_positions_with_one_id_missing_are_still_positions():
    ids = [1, 2, 3, None, 5, 6, 7]
    rows = parse_srerd_records([srerd_project(i, n) for n, i in enumerate(ids)], today=TODAY)
    assert rows == []


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
def lead(**kw: Any) -> dict[str, Any]:
    base = {
        "project_name": "Probe Tower", "developer": "Dev LLC", "emirate": "Dubai", "area": "Marina",
        "claim": "Launched", "source_url": "https://example.com/launch",
    }
    base.update(kw)
    return base


def run_gemini(text: str) -> Any:
    chunk = ("https://vertexaisearch.cloud.google.com/grounding-api-redirect/AAA", "example.com", "example.com")
    client = FakeGeminiClient([grounding_response(text, chunks=[chunk])])
    return GeminiNewsAdapter(gemini_cfg(), client=client, today=TODAY, sleep=Sleeps()).fetch()


# A 150,000 character URL was kept; Google Sheets refuses a cell over 50,000 characters, which
# would fail the whole candidate append and with it every other lead of the day.
def test_an_enormous_source_url_is_discarded_not_stored():
    result = run_gemini(json.dumps([lead(source_url="https://example.com/" + "a" * 150_000), lead(project_name="Other Tower")]))
    assert [c.project_name for c in result.candidates] == ["Other Tower"]
    assert all(len(c.source_url) <= gemini_news.MAX_URL_LEN for c in result.candidates)


# A reply with commentary AND citation markers around the list is refused (existing test
# test_an_answer_that_is_not_a_list_of_leads_is_an_error...): a design choice, reported to the owner, not changed.
def test_text_with_only_brackets_that_are_not_leads_is_still_an_error():
    with pytest.raises(SourceError, match="not the JSON list"):
        run_gemini("No launches found [1] and nothing else [2].")


# ---------------------------------------------------------------------------
# Control and bidi-override characters. Text that a search model copies from the web (or a feed
# carries) went into cells unchanged: NUL and BEL bytes, and U+202E, which makes a reviewer read a
# claim or a name back to front. Whitespace was already collapsed; these were not.
# ---------------------------------------------------------------------------
from src.sources.base import as_text  # noqa: E402


def test_as_text_removes_control_and_bidi_override_characters():
    assert as_text("Tower\x00A\x07") == "Tower A"
    assert as_text("Tower\u202eA\u2066B\u200bC\ufeff") == "TowerABC"
    assert as_text("برج الاختبار") == "برج الاختبار"  # real Arabic text is untouched


def test_a_lead_with_control_characters_is_stored_clean():
    result = run_gemini(json.dumps([lead(project_name="Probe\x00 Tower\u202e", claim="a\x07b c", area="Mar\u200bina")]))
    (candidate,) = result.candidates
    assert (candidate.project_name, candidate.claim, candidate.area) == ("Probe Tower", "a b c", "Marina")


def test_a_source_url_with_control_or_bidi_characters_is_discarded():
    for bad in ("https://example.com/a\x00b", "https://example.com/a\u202eb", "https://example.com/a\x07"):
        assert run_gemini(json.dumps([lead(source_url=bad)])).candidates == []


def test_the_seed_file_reader_removes_control_characters_too(tmp_path):
    from datetime import date

    from src.sources.seed_csv import SeedCsvAdapter

    path = tmp_path / "seed.csv"
    path.write_text(
        "Emirate,Project Name (EN),Developer,Source URL\nDubai,Tower\x00 A\u202e,Dev LLC,https://seed.example.com/x\n",
        encoding="utf-8",
    )
    (row,) = SeedCsvAdapter(path=path, today=date(2026, 10, 25)).fetch().rows
    assert row.project_name_en == "Tower A"


# ---------------------------------------------------------------------------
# Audit addition. The one signal that a register may be cut short ("the end of the register was not
# confirmed") sat AFTER the row-drop notes, so with a few unusable rows it fell off the end of the
# 280-character run-log note and nothing else reported it.
# ---------------------------------------------------------------------------
def test_an_unconfirmed_end_of_the_register_survives_into_the_run_log_note(caplog):
    from src.main import Report, run_source

    rows = [dld_row(n) for n in range(1, 41)] + [
        {"PROJECT_NUMBER": 1, "PROJECT_NAME": "Test Tower 1", "DEVELOPER_NAME": "Example Developer LLC"},  # duplicate
        {"PROJECT_NUMBER": "12x", "PROJECT_NAME": "Odd Number", "DEVELOPER_NAME": "Example Developer LLC"},
        {"PROJECT_NUMBER": "13y", "PROJECT_NAME": "Odd Number 2", "DEVELOPER_NAME": "Example Developer LLC"},
        {"PROJECT_NUMBER": 98},
        {"PROJECT_NUMBER": 97, "DEVELOPER_NAME": "Dev"},
    ]
    failing = [FakeResponse(500, text="x")] * 4
    session = FakeSession([token_ok(), page(rows, total=len(rows)), *failing])
    adapter = dld_adapter(session, page_size=100)
    with caplog.at_level("WARNING"):
        outcome = run_source(adapter, dld_cfg(), {}, Report([]))
    assert "not confirmed" in outcome.info.note
    assert "not confirmed" in caplog.text  # and a warning line, not only a cell


# ---------------------------------------------------------------------------
# Audit addition: the other ways the confirming request can end. None of these three branches of
# _fetch_pages had a test (removing each of them left the whole suite green).
# ---------------------------------------------------------------------------
def test_a_server_that_answers_the_confirming_request_with_its_last_page_again_is_tolerated_with_a_note():
    session = FakeSession([token_ok(), page(numbered(1, 3), total=3), page(numbered(1, 3), total=3)])
    result = dld_adapter(session, page_size=3).fetch()
    assert [r.registry_project_no for r in result.rows] == ["1", "2", "3"]  # the repeated rows were not added again
    assert any("repeated its last page" in note for note in result.notes)


def test_a_confirming_answer_without_a_list_of_rows_is_tolerated_with_a_note():
    session = FakeSession([token_ok(), page(numbered(1, 3), total=3), FakeResponse(200, {"message": "no more data"})])
    result = dld_adapter(session, page_size=3).fetch()
    assert len(result.rows) == 3
    assert any("no list of rows" in note for note in result.notes)


def test_reaching_the_page_limit_exactly_at_the_stated_total_is_a_note_not_an_error():
    session = FakeSession([token_ok(), page(numbered(1, 3), total=3)])
    result = dld_adapter(session, page_size=3, max_pages=1).fetch()
    assert len(result.rows) == 3
    assert any("page limit" in note for note in result.notes)
