"""
Tests for src/gsheet_manager.py against the in-memory client in tests/fake_gspread.py.

No network, no credentials: the one test that needs a service-account key builds a
throwaway RSA key at run time (nothing is stored). Every spreadsheet, name, key and
e-mail address below is SYNTHETIC.

The fake is strict on purpose (it rejects writes past the grid, refuses to delete
every non-frozen row, models RAW vs USER_ENTERED), so a test passing here means the
call sequence would also be accepted by Google, not merely that a mock was satisfied.
"""

from __future__ import annotations

import base64
import inspect
import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import gspread
import pytest
import requests
from google.auth import exceptions as google_exceptions

from src import gsheet_manager as gm
from src.config import (
    ALL_TABS,
    SOURCE_NAMES,
    TAB_CANDIDATES,
    TAB_LISTINGS,
    TAB_OVERRIDES,
    TAB_PINS,
    TAB_REGISTRY,
    TAB_RUN_LOGS,
    Config,
)
from src.gsheet_manager import (
    HEADERS,
    MAP_PIN_HEADERS,
    RUN_LOG_HEADERS,
    RunLogEntry,
    SheetError,
    SheetManager,
    SourceRunInfo,
    scrub_text,
)
from src.listing_data import LISTING_HEADERS
from src.models import (
    CANDIDATE_HEADERS,
    OVERRIDE_HEADERS,
    REGISTRY_HEADERS,
    LaunchCandidate,
    RegistryRow,
)
from tests import fake_gspread as fake
from tests.fake_gspread import DEFAULT_SA_EMAIL, FakeClient, make_api_error

SHEET_ID = "fake-spreadsheet-id-for-tests-0001"  # SYNTHETIC
SECRET_GEMINI = "fake-key-GEMINI-9999"  # SYNTHETIC
SECRET_MAPS = "fake-key-MAPS-7777"  # SYNTHETIC
SECRET_PRIVATE = "fake-key-PRIVATE-5555"  # SYNTHETIC
SECRET_URLKEY = "fake-key-URLKEY-3333"  # SYNTHETIC
SECRET_BEARER = "fake-token-BEARER-1234567890"  # SYNTHETIC
ALL_SECRETS = (SECRET_GEMINI, SECRET_MAPS, SECRET_PRIVATE, SECRET_URLKEY, SECRET_BEARER)
LEAK_MARKER = "zq-hostile-marker-4471"  # SYNTHETIC; not secret-shaped, so the scrubber would never catch it


# ---------------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------------
def make_cfg(**overrides: Any) -> Config:
    base: dict[str, Any] = {"google_sheet_name": "Fake Tracker For Tests"}
    base.update(overrides)
    return Config(**base)


@dataclass
class Env:
    client: FakeClient
    mgr: SheetManager
    sleeps: list[float]
    cfg: Config

    @property
    def sheet(self) -> fake.FakeSpreadsheet:
        return next(iter(self.client.spreadsheets.values()))

    def tab(self, name: str) -> fake.FakeWorksheet:
        return self.sheet.tab(name)

    def records(self, name: str) -> list[dict[str, str]]:
        values = self.tab(name).dump()
        if not values:
            return []
        return [dict(zip(values[0], row)) for row in values[1:]]


def new_env(*, tabs: dict[str, list[list[str]]] | None = None, open_it: bool = True, **cfg_overrides: Any) -> Env:
    """A manager on a fake client. With `tabs`, an existing spreadsheet is configured by id; else one is created."""
    client = FakeClient()
    if tabs is not None:
        client.add_spreadsheet("Existing Fake Spreadsheet", sheet_id=SHEET_ID, tabs=tabs)
        cfg_overrides.setdefault("spreadsheet_id", SHEET_ID)
    cfg = make_cfg(**cfg_overrides)
    sleeps: list[float] = []
    mgr = SheetManager(cfg, client=client, sleep=sleeps.append)
    if open_it:
        mgr.open()
    return Env(client, mgr, sleeps, cfg)


def dubai(n: int, **kw: Any) -> RegistryRow:
    fields: dict[str, Any] = dict(
        emirate="Dubai", registry="DLD", registry_project_no=str(100000 + n),
        project_name_en=f"Fake Tower {n}", developer="Fake Developer",
    )
    fields.update(kw)
    return RegistryRow(**fields)


def sharjah(code: str, **kw: Any) -> RegistryRow:
    fields: dict[str, Any] = dict(
        emirate="Sharjah", registry="SRERD", registry_project_no=code,
        project_name_en=f"Fake Villas {code}", developer="Fake Developer Two",
    )
    fields.update(kw)
    return RegistryRow(**fields)


def seed_row(name: str, developer: str = "Fake Developer", emirate: str = "Dubai", **kw: Any) -> RegistryRow:
    return RegistryRow(emirate=emirate, registry="SEED", project_name_en=name, developer=developer, **kw)


def candidate(name: str, **kw: Any) -> LaunchCandidate:
    fields: dict[str, Any] = dict(
        emirate="Dubai", project_name=name, developer="Fake Developer",
        source_url="https://example.invalid/news/1", found_at="2026-10-25T03:00:00Z",
    )
    fields.update(kw)
    return LaunchCandidate(**fields)


def keys_on_sheet(env: Env) -> list[str]:
    return [r["Project Key"] for r in env.records(TAB_REGISTRY)]


def calls(env: Env, *methods: str) -> list[fake.Call]:
    return [c for c in env.client.calls if c.method in methods]


def run_entry(started: str = "2026-10-25T03:00:00Z", **kw: Any) -> RunLogEntry:
    fields: dict[str, Any] = dict(
        started_at=started, finished_at="2026-10-25T03:05:00Z", exit_code=0,
        sources={"dld_projects": SourceRunInfo("ok", 120)}, geocode={"attempted": 3, "backend": "nominatim"},
        registry_rows=120, map_pins=80, git_sha="abc1234",
    )
    fields.update(kw)
    return RunLogEntry(**fields)


@lru_cache(maxsize=1)
def throwaway_key_json() -> str:
    """A service-account JSON with an RSA key generated right now. Never written to disk."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return json.dumps(
        {
            "type": "service_account", "project_id": "fake-project", "private_key_id": "fake-key-id-0001",
            "private_key": pem, "client_email": DEFAULT_SA_EMAIL, "client_id": "1",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


# =================================================================================
# The fake itself (a lenient fake would make every test below meaningless)
# =================================================================================
class TestFakeIsHonest:
    def _ws(self, rows: int = 5, cols: int = 3) -> fake.FakeWorksheet:
        client = FakeClient()
        sheet = client.add_spreadsheet("T", tabs={"A": []})
        ws = sheet.tab("A")
        ws.resize(rows=rows, cols=cols)
        return ws

    @pytest.mark.parametrize(
        "real, mine",
        [
            (gspread.Worksheet.get_all_values, fake.FakeWorksheet.get_all_values),
            (gspread.Worksheet.row_values, fake.FakeWorksheet.row_values),
            (gspread.Worksheet.update, fake.FakeWorksheet.update),
            (gspread.Worksheet.append_rows, fake.FakeWorksheet.append_rows),
            (gspread.Worksheet.delete_rows, fake.FakeWorksheet.delete_rows),
            (gspread.Worksheet.clear, fake.FakeWorksheet.clear),
            (gspread.Worksheet.resize, fake.FakeWorksheet.resize),
            (gspread.Worksheet.freeze, fake.FakeWorksheet.freeze),
            (gspread.Worksheet.update_index, fake.FakeWorksheet.update_index),
            (gspread.Spreadsheet.worksheets, fake.FakeSpreadsheet.worksheets),
            (gspread.Spreadsheet.worksheet, fake.FakeSpreadsheet.worksheet),
            (gspread.Spreadsheet.add_worksheet, fake.FakeSpreadsheet.add_worksheet),
            (gspread.Spreadsheet.del_worksheet, fake.FakeSpreadsheet.del_worksheet),
            (gspread.Spreadsheet.share, fake.FakeSpreadsheet.share),
            (gspread.Client.open, fake.FakeClient.open),
            (gspread.Client.open_by_key, fake.FakeClient.open_by_key),
            (gspread.Client.create, fake.FakeClient.create),
            (gspread.Client.set_timeout, fake.FakeClient.set_timeout),
        ],
        ids=lambda f: getattr(f, "__qualname__", str(f)),
    )
    def test_signatures_follow_the_real_library(self, real, mine):
        """Same parameter names in the same order (the fake may stop early), same defaults."""
        real_params = list(inspect.signature(real).parameters.values())
        fake_params = list(inspect.signature(mine).parameters.values())
        names = [p.name for p in real_params]
        position = -1
        for param in fake_params:
            assert param.name in names, f"{mine.__qualname__} has a parameter {param.name} the real method lacks"
            assert names.index(param.name) > position, f"{param.name} is out of order"
            position = names.index(param.name)
            default = real_params[names.index(param.name)].default
            if default is not inspect.Parameter.empty and param.default is not inspect.Parameter.empty:
                assert str(getattr(default, "value", default)) == str(getattr(param.default, "value", param.default))

    def test_update_past_the_grid_is_rejected_not_grown(self):
        ws = self._ws(rows=5, cols=3)
        with pytest.raises(gspread.exceptions.APIError) as caught:
            ws.update([["x"]] * 6, range_name="A1")
        assert caught.value.code == 400
        with pytest.raises(gspread.exceptions.APIError):
            ws.update([["a", "b", "c", "d"]], range_name="A1")
        assert ws.dump() == []  # nothing was half-written

    def test_range_smaller_than_the_data_is_rejected(self):
        ws = self._ws()
        with pytest.raises(gspread.exceptions.APIError):
            ws.update([["a", "b"]], range_name="A1:A1")

    @pytest.mark.parametrize("bad", [None, [1], {"a": 1}, object(), float("nan")])
    def test_values_that_cannot_be_sent_as_json_scalars_raise(self, bad):
        with pytest.raises(TypeError):
            self._ws().update([[bad]], range_name="A1")

    def test_a_row_must_be_a_list(self):
        with pytest.raises(gspread.exceptions.APIError):
            self._ws().update(["flat", "list"], range_name="A1")

    def test_cell_over_50000_characters_is_rejected(self):
        with pytest.raises(gspread.exceptions.APIError):
            self._ws(cols=3).update([["x" * 50_001]], range_name="A1")

    def test_raw_keeps_text_and_user_entered_would_not(self):
        ws = self._ws()
        ws.update([["00123", "2026-10-25", "=1+1"]], range_name="A1", value_input_option="RAW")
        assert ws.dump() == [["00123", "2026-10-25", "=1+1"]]
        ws.update([["00123", "2026-10-25", "=1+1"]], range_name="A2", value_input_option="USER_ENTERED")
        assert ws.dump()[1][0] == "123"
        assert ws.dump()[1][1] != "2026-10-25"
        assert ws.dump()[1][2] == "#NAME?"

    def test_cannot_delete_every_non_frozen_row(self):
        ws = self._ws(rows=3, cols=3)
        ws.freeze(rows=1)
        with pytest.raises(gspread.exceptions.APIError) as caught:
            ws.delete_rows(2, 3)
        assert "non-frozen" in str(caught.value)
        ws.delete_rows(2, 2)
        assert ws.row_count == 2

    def test_delete_outside_the_grid_is_rejected(self):
        ws = self._ws(rows=5)
        for start, end in ((0, 1), (3, 2), (4, 9)):
            with pytest.raises(gspread.exceptions.APIError):
                ws.delete_rows(start, end)

    def test_get_all_values_is_rectangular_and_trimmed(self):
        ws = self._ws(rows=10, cols=6)
        ws.put([["a", "b"], [], ["c"], ["", "", "", "z"]])
        ws.update([[""] * 3], range_name="A8")  # trailing blank row/cols are not returned
        assert ws.get_all_values() == [["a", "b", "", ""], ["", "", "", ""], ["c", "", "", ""], ["", "", "", "z"]]
        assert ws.row_values(1) == ["a", "b"]
        assert ws.row_values(4) == ["", "", "", "z"]
        assert ws.row_values(9) == []

    def test_an_empty_tab_reads_as_one_empty_row_like_gspread_6_2_1(self):
        """gspread's fill_gaps() turns "no values at all" into [[]], never []; code that tests `not values` is wrong."""
        from gspread.utils import fill_gaps

        assert fill_gaps([]) == [[]]  # the real library's behaviour, pinned from the installed version
        ws = self._ws(rows=5, cols=3)
        assert ws.get_all_values() == [[]]
        ws.update([["a"]], range_name="A1")
        ws.clear()
        assert ws.get_all_values() == [[]]

    def test_append_inserts_after_the_table_and_default_mode_overwrites_below_a_gap(self):
        ws = self._ws(rows=10, cols=3)
        ws.put([["h"], ["1"], ["2"], [], ["later"]])
        ws.append_rows([["x"]], insert_data_option="INSERT_ROWS", table_range="A1")
        assert ws.dump() == [["h"], ["1"], ["2"], ["x"], [""], ["later"]]
        ws2 = self._ws(rows=10, cols=3)
        ws2.put([["h"], ["1"], ["2"], [], ["later"]])
        ws2.append_rows([["x"], ["y"]])  # the API default is OVERWRITE: "later" is destroyed
        assert [r[0] for r in ws2.dump()] == ["h", "1", "2", "x", "y"]

    def test_duplicate_tab_title_and_removing_the_last_tab_are_rejected(self):
        client = FakeClient()
        sheet = client.add_spreadsheet("T")
        with pytest.raises(gspread.exceptions.APIError):
            sheet.add_worksheet("Sheet1", 10, 5)
        with pytest.raises(gspread.exceptions.APIError):
            sheet.del_worksheet(sheet.tab("Sheet1"))

    def test_ten_million_cell_cap_applies_to_add_and_resize(self):
        client = FakeClient()
        client.cell_limit = 5000
        sheet = client.add_spreadsheet("T", tabs={"A": []})
        with pytest.raises(gspread.exceptions.APIError):
            sheet.add_worksheet("B", 1000, 26)
        with pytest.raises(gspread.exceptions.APIError):
            sheet.tab("A").resize(rows=10_000)
        assert sheet.titles() == ["A"]

    def test_open_by_key_distinguishes_missing_from_forbidden(self):
        client = FakeClient()
        client.add_spreadsheet("T", sheet_id="fake-id-1")
        client.forbidden_ids.add("fake-id-1")
        with pytest.raises(PermissionError):
            client.open_by_key("fake-id-1")
        with pytest.raises(gspread.exceptions.SpreadsheetNotFound):
            client.open_by_key("fake-id-nope")

    def test_fault_injection_times_skip_applied_and_tab_filter(self):
        client = FakeClient()
        sheet = client.add_spreadsheet("T", tabs={"A": [], "B": []})
        a, b = sheet.tab("A"), sheet.tab("B")
        client.inject("get_all_values", 429, times=2, tab="A", skip=1)
        a.get_all_values()  # skipped (let through)
        for _ in range(2):
            with pytest.raises(gspread.exceptions.APIError) as caught:
                a.get_all_values()
            assert caught.value.code == 429
        a.get_all_values()  # exhausted
        b.get_all_values()  # other tab never affected
        client.inject("append_rows", 500, applied=True)
        with pytest.raises(gspread.exceptions.APIError):
            b.append_rows([["done"]])
        assert b.dump() == [["done"]]  # the work happened although the caller saw an error
        client.inject("update", exception=RuntimeError("process died"))
        with pytest.raises(RuntimeError):
            a.update([["x"]], range_name="A1")

    def test_make_api_error_is_a_real_gspread_error(self):
        err = make_api_error(429, reason="rateLimitExceeded")
        assert isinstance(err, gspread.exceptions.APIError)
        assert err.code == 429 and err.response.status_code == 429
        assert err.error["errors"][0]["reason"] == "rateLimitExceeded"
        garbled = fake.make_garbled_api_error(502)
        assert garbled.code == -1 and garbled.response.status_code == 502


# =================================================================================
# Secrets
# =================================================================================
class TestScrub:
    @pytest.mark.parametrize(
        "text, leaked",
        [
            ("GET https://sheets.example.invalid/v4/x?key=fake-key-ABCDEF123&alt=json", "fake-key-ABCDEF123"),
            ("https://x.invalid/?api_key=fake-key-ABCDEF123", "fake-key-ABCDEF123"),
            ("https://x.invalid/?access_token=fake-token-ABCDEF123", "fake-token-ABCDEF123"),
            ("Authorization: Bearer fake-token-ABCDEF1234567890", "fake-token-ABCDEF1234567890"),
            ("rejected Bearer fake-token-ABCDEF1234567890 today", "fake-token-ABCDEF1234567890"),
            ('{"private_key": "-----BEGIN PRIVATE KEY-----\\nFAKEFAKEFAKE\\n-----END PRIVATE KEY-----\\n"}', "FAKEFAKEFAKE"),
            ("-----BEGIN PRIVATE KEY-----\nFAKEFAKEFAKE\n-----END PRIVATE KEY-----", "FAKEFAKEFAKE"),
            ('{"client_secret": "fake-secret-value-1"}', "fake-secret-value-1"),
            ("https://user:fake-password-1@host.invalid/path", "fake-password-1"),
            ("token=fake-token-ABCDEF123; other=1", "fake-token-ABCDEF123"),
            ("see AIza" + "A" * 35 + " for details", "A" * 35),
            ("ya29." + "b" * 30, "b" * 30),
            ("https://sheets.googleapis.com/v4/spreadsheets/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/values", "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"),
        ],
    )
    def test_secret_shapes_are_redacted(self, text, leaked):
        assert leaked not in scrub_text(text)

    @pytest.mark.parametrize(
        "text",
        [
            "{'api_key': 'fake-key-ABCDEF123'}",  # a Python dict repr: single quotes, not JSON
            "{'access_token': 'fake-key-ABCDEF123', 'expires_in': 3599}",
            "{'Authorization': 'fake-key-ABCDEF123'}",  # a header value with no scheme word
            "headers={'X-Api-Key': 'fake-key-ABCDEF123'}",
            "token: fake-key-ABCDEF123",
            "api_key: fake-key-ABCDEF123",
            "X-Api-Key: fake-key-ABCDEF123",
            "x-goog-api-key: fake-key-ABCDEF123",
            "client_secret: fake-key-ABCDEF123 was rejected",
        ],
    )
    def test_secret_shapes_from_reprs_and_header_dumps_are_redacted(self, text):
        # Reviewed: only double-quoted JSON, key=value and Bearer forms were covered, so an exception that
        # printed a dict repr or a header dump carried the value into the run log and the console.
        out = scrub_text(text)
        assert "fake-key-ABCDEF123" not in out and "[redacted]" in out

    @pytest.mark.parametrize("text", ["token: fake-key-ABCDEF123", "X-Api-Key: fake-key-ABCDEF123", "client_secret: fake-key-ABCDEF123 x"])
    def test_scrubbing_twice_changes_nothing(self, text):
        # Text is scrubbed at several layers (the exception, then the log line): a second pass must not
        # mangle the first pass's marker into "[redacted]]".
        once = scrub_text(text)
        assert scrub_text(once) == once and "[redacted]]" not in once

    def test_ordinary_colon_text_survives_the_header_dump_patterns(self):
        text = "Listing_Data row 4: key columns: Project Key, Registry; keyword: tower; tokens: 3 of 5"
        assert scrub_text(text) == text

    def test_exact_known_secrets_are_redacted_wherever_they_appear(self):
        out = scrub_text("boom fake-key-XYZ-12345 and again fake-key-XYZ-12345", ["fake-key-XYZ-12345"])
        assert "XYZ-12345" not in out and out.count("[redacted]") == 2

    def test_longer_secret_is_replaced_before_its_substring(self):
        out = scrub_text("abcd-efgh-ijkl", ["abcd", "abcd-efgh-ijkl"])
        assert "efgh" not in out and "ijkl" not in out

    def test_none_empty_and_tiny_secrets_are_safe(self):
        assert scrub_text(None) == ""
        assert scrub_text("plain text", ["", "ab"]) == "plain text"
        assert scrub_text(12345) == "12345"

    def test_ordinary_text_is_left_alone(self):
        text = "Registry_Projects row 7: emirate: must be one of Dubai, Sharjah"
        assert scrub_text(text) == text


class TestNoSecretLeaks:
    """Every failure path, with a server error that echoes secrets back, must stay clean."""

    def _env(self) -> Env:
        return new_env(
            gemini_api_key=SECRET_GEMINI, google_maps_api_key=SECRET_MAPS,
            gcp_service_account_key=json.dumps({"type": "service_account", "private_key": SECRET_PRIVATE}),
        )

    def _hostile(self, status: int) -> str:
        return (
            f"denied {SECRET_GEMINI} {SECRET_MAPS} {SECRET_PRIVATE} Bearer {SECRET_BEARER} "
            f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/A1?key={SECRET_URLKEY}&alt=json"
        )

    def _assert_clean(self, text: str) -> None:
        for secret in ALL_SECRETS:
            assert secret not in text, f"leaked {secret!r} in: {text[:400]}"

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
    def test_api_error_text_is_scrubbed_in_exception_and_logs(self, status, caplog):
        env = self._env()
        caplog.set_level(logging.DEBUG)
        env.client.inject("get_all_values", status, times=None, message=self._hostile(status))
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        self._assert_clean(str(caught.value))
        self._assert_clean(caplog.text)
        self._assert_clean(repr(caught.value))

    def test_non_json_error_body_is_scrubbed(self, caplog):
        env = self._env()
        caplog.set_level(logging.DEBUG)
        env.client.inject("get_all_values", 502, garbled=True, times=None)
        # a configured key echoed bare (exact-match scrub) and an unknown one inside key=... (shape scrub)
        garbled_body = f"<html>gateway {SECRET_MAPS} ?key={SECRET_URLKEY}</html>"
        env.client.faults[0].make_error = lambda: fake.make_garbled_api_error(502, garbled_body)
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        self._assert_clean(str(caught.value))
        self._assert_clean(caplog.text)

    def test_network_error_text_is_never_included(self, caplog):
        env = self._env()
        caplog.set_level(logging.DEBUG)
        url_error = requests.exceptions.ReadTimeout(
            f"HTTPSConnectionPool(host='sheets.googleapis.com'): Read timed out. url=/v4/x?key={SECRET_URLKEY}"
        )
        env.client.inject("get_all_values", exception=url_error, times=None)
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        self._assert_clean(str(caught.value) + caplog.text)
        assert "ReadTimeout" in str(caught.value)

    def test_other_http_layer_errors_are_reduced_to_their_type(self):
        env = self._env()
        env.client.inject("get_all_values", exception=requests.exceptions.InvalidURL(f"bad ?key={SECRET_URLKEY}"))
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        self._assert_clean(str(caught.value))
        assert "InvalidURL" in str(caught.value)

    def test_credential_refusal_is_reduced_to_its_type(self):
        env = self._env()
        env.client.inject("get_all_values", exception=google_exceptions.RefreshError(f"invalid_grant {SECRET_PRIVATE}"))
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        self._assert_clean(str(caught.value))
        assert "credentials" in str(caught.value)

    def test_unopenable_spreadsheet_message_hides_the_id(self):
        env = new_env(open_it=False, spreadsheet_id=SHEET_ID, gemini_api_key=SECRET_GEMINI)
        with pytest.raises(SheetError) as caught:
            env.mgr.open()
        assert SHEET_ID not in str(caught.value)
        assert "SPREADSHEET_ID" in str(caught.value)

    def test_errors_do_not_chain_the_original_exception(self):
        env = self._env()
        env.client.inject("get_all_values", 400, message=self._hostile(400))
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__

    def test_run_log_text_fields_are_scrubbed_before_they_reach_the_sheet(self):
        env = self._env()
        entry = run_entry(
            sources={"dld_projects": SourceRunInfo("failed", 0, f"HTTP 403 https://x.invalid/a?apikey={SECRET_URLKEY}")},
            geocode={"note": f"Authorization: Bearer {SECRET_BEARER}"},
            notes=f"used {SECRET_GEMINI} and {SECRET_MAPS}", git_sha=f"abc {SECRET_MAPS}",
        )
        env.mgr.append_run_log(entry)
        self._assert_clean(json.dumps(env.tab(TAB_RUN_LOGS).dump()))

    def test_no_print_statements_and_no_url_logging_in_the_module(self):
        source = Path(gm.__file__).read_text(encoding="utf-8")
        assert not re.search(r"^\s*print\(", source, re.M)
        assert "response.url" not in source and ".request.url" not in source


def hostile_api_error(status: int, marker: str) -> gspread.exceptions.APIError:
    """A Google error that carries `marker` in every free-text field AND in the status token, as a hostile server could."""
    response = requests.Response()
    response.status_code = status
    response.encoding = "utf-8"
    response.url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}?q={marker}"
    error = {
        "code": status, "message": marker, "status": marker,
        "errors": [{"message": marker, "domain": "global", "reason": marker}],
        "details": [{"detail": marker}],
    }
    response._content = json.dumps({"error": error}).encode("utf-8")
    response.headers["Content-Type"] = "application/json; charset=UTF-8"
    return gspread.exceptions.APIError(response)


class TestPublicLogHygiene:
    """
    The Actions log is public. Nothing Google or the network says in free text, no service-account e-mail, no
    spreadsheet title or name, no Project Key may reach an exception message or a log record. The marker is not
    secret-shaped on purpose: only "never printed" keeps it out, not the secret scrubber.
    """

    @staticmethod
    def _assert_clean(*texts: str) -> None:
        for text in texts:
            assert LEAK_MARKER not in text, f"leaked the marker in: {text[:400]}"

    @pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503])
    def test_googles_error_text_never_reaches_the_message_or_the_log(self, status, caplog):
        env = new_env()
        caplog.set_level(logging.DEBUG)
        env.client.inject("get_all_values", status, times=None, message=LEAK_MARKER, reason=LEAK_MARKER)
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        text = str(caught.value)
        self._assert_clean(text, repr(caught.value), caplog.text)
        # what stays: the HTTP status and Google's fixed status token
        assert f"HTTP {status} {fake._STATUS_NAMES[status]}" in text
        assert caught.value.status == status

    @pytest.mark.parametrize("status", [400, 503])
    def test_a_status_token_outside_the_known_list_is_dropped(self, status, caplog):
        env = new_env()
        caplog.set_level(logging.DEBUG)
        env.client.inject("get_all_values", exception=hostile_api_error(status, LEAK_MARKER), times=None)
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        self._assert_clean(str(caught.value), repr(caught.value), caplog.text)
        assert f"HTTP {status}" in str(caught.value)

    def test_retry_warnings_carry_the_status_and_token_only(self, caplog):
        env = new_env()
        caplog.set_level(logging.DEBUG)
        env.client.inject("get_all_values", 429, times=2, message=LEAK_MARKER)
        env.mgr.read_registry()  # recovers on the third try
        self._assert_clean(caplog.text)
        assert "HTTP 429 RESOURCE_EXHAUSTED; retrying" in caplog.text

    @pytest.mark.parametrize("method", ["create", "add_worksheet"])
    def test_an_unknown_outcome_message_carries_no_google_text(self, method, caplog):
        caplog.set_level(logging.DEBUG)
        if method == "create":
            env = new_env(open_it=False)
        else:
            env = new_env(tabs={TAB_REGISTRY: [list(REGISTRY_HEADERS)]}, open_it=False)
        env.client.inject(method, 500, times=None, applied=True, message=LEAK_MARKER)
        with pytest.raises(SheetError) as caught:
            env.mgr.open()
        self._assert_clean(str(caught.value), caplog.text)
        assert "HTTP 500 INTERNAL" in str(caught.value)

    @pytest.mark.parametrize("error", [
        requests.exceptions.ReadTimeout(f"HTTPSConnectionPool(host='x.invalid'): timed out {LEAK_MARKER}"),
        requests.exceptions.InvalidURL(f"bad url {LEAK_MARKER}"),
        google_exceptions.RefreshError(f"invalid_grant {LEAK_MARKER}"),
    ], ids=lambda e: type(e).__name__)
    def test_network_and_credential_exceptions_are_reduced_to_their_class(self, error, caplog):
        env = new_env()
        caplog.set_level(logging.DEBUG)
        env.client.inject("get_all_values", exception=error, times=None)
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        self._assert_clean(str(caught.value), caplog.text)
        assert type(error).__name__ in str(caught.value)

    def test_neither_the_service_account_email_nor_a_spreadsheet_title_is_logged(self, caplog):
        caplog.set_level(logging.DEBUG)
        env = new_env(share_with_emails=("fake.person@example.invalid",))  # creates by name, shares, builds tabs
        SheetManager(env.cfg, client=env.client, sleep=lambda s: None).open()  # finds it again by name
        by_id = new_env(tabs={TAB_REGISTRY: [list(REGISTRY_HEADERS)]})  # "Existing Fake Spreadsheet", by id
        SheetManager(by_id.cfg, client=by_id.client, sleep=lambda s: None).open(read_only=True)
        text = caplog.text
        assert "Opened the spreadsheet" in text and "creating it" in text  # the records under test exist
        for value in (DEFAULT_SA_EMAIL, "Fake Tracker For Tests", "Existing Fake Spreadsheet", "fake.person@example.invalid"):
            assert value not in text, f"{value!r} reached the log"

    def test_a_failed_share_names_the_entry_not_the_address_or_googles_text(self, caplog):
        caplog.set_level(logging.DEBUG)
        env = new_env(open_it=False, share_with_emails=("fake.person@example.invalid",))
        env.client.inject("share", 400, times=None, message=f"{LEAK_MARKER} fake.person@example.invalid")
        with pytest.raises(SheetError) as caught:
            env.mgr.open()
        self._assert_clean(str(caught.value), caplog.text)
        assert "fake.person@example.invalid" not in str(caught.value) + caplog.text
        assert "SHARE_WITH_EMAILS entry 1" in str(caught.value)

    def test_a_stale_delete_names_the_row_not_the_project_keys(self, caplog):
        caplog.set_level(logging.DEBUG)
        env = new_env()
        env.mgr.write_registry([dubai(i) for i in range(4)])
        with pytest.raises(SheetError) as caught:
            env.mgr.delete_rows(TAB_REGISTRY, [3], expect_keys={3: "dld:dubai:999999"})  # row 3 holds ...100001
        text = str(caught.value) + caplog.text
        assert "row 3" in text and "no longer holds" in text
        assert "dld:dubai:999999" not in text and "dld:dubai:100001" not in text
        assert len(keys_on_sheet(env)) == 4  # and nothing was deleted


# =================================================================================
# Authentication
# =================================================================================
class TestAuthentication:
    def _mgr(self, tmp_path: Path, **kw: Any) -> SheetManager:
        kw.setdefault("gcp_service_account_file", str(tmp_path / "absent.json"))  # absolute: hermetic
        return SheetManager(make_cfg(**kw))

    def test_inline_key_builds_a_real_client_offline(self, tmp_path):
        mgr = self._mgr(tmp_path, gcp_service_account_key=throwaway_key_json())
        client = mgr._build_client()
        assert isinstance(client, gspread.Client)
        assert client.http_client.auth.service_account_email == DEFAULT_SA_EMAIL
        assert client.http_client.timeout == gm.HTTP_TIMEOUT_SECONDS
        assert set(client.http_client.auth.scopes) == set(gm.SCOPES)

    def test_the_requested_scopes_are_exactly_sheets_and_drive(self):
        """Drive is needed to create and share a tracker; nothing broader (mail, calendar, cloud-platform) is asked for."""
        assert set(gm.SCOPES) == {
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        }

    def test_a_configured_spreadsheet_id_gets_a_sheets_only_credential(self, tmp_path):
        """
        Least privilege. With SPREADSHEET_ID set nothing touches Drive (open_by_key,
        tabs, values: all Sheets API; creating and sharing happen only without an id),
        so the key must not be able to read or change the rest of the Drive either.
        """
        mgr = self._mgr(tmp_path, gcp_service_account_key=throwaway_key_json(), spreadsheet_id=SHEET_ID)
        client = mgr._build_client()
        assert set(client.http_client.auth.scopes) == {"https://www.googleapis.com/auth/spreadsheets"}

    def test_without_an_id_the_drive_scope_is_kept_for_find_by_name_create_and_share(self, tmp_path):
        mgr = self._mgr(tmp_path, gcp_service_account_key=throwaway_key_json())
        client = mgr._build_client()
        assert set(client.http_client.auth.scopes) == set(gm.SCOPES)

    def test_every_drive_dependent_call_is_unreachable_when_an_id_is_configured(self):
        """The reason the narrow scope is safe: with an id, open() makes no create / share / find-by-name call."""
        env = new_env(open_it=False, spreadsheet_id=SHEET_ID)
        env.client.add_spreadsheet("Existing Fake Spreadsheet", sheet_id=SHEET_ID, tabs={})
        env.mgr.open()
        assert {"open_by_key", "add_worksheet"} <= set(env.client.methods())  # the log is live: the check is not vacuous
        used = {name for name in ("create", "open", "share", "del_worksheet") if env.client.calls_to(name)}
        assert used == set()

    def test_key_file_is_used_when_there_is_no_inline_key(self, tmp_path):
        path = tmp_path / "key.json"
        path.write_text(throwaway_key_json(), encoding="utf-8")
        mgr = self._mgr(tmp_path, gcp_service_account_file=str(path))
        assert isinstance(mgr._build_client(), gspread.Client)

    def test_key_file_with_a_byte_order_mark_is_accepted(self, tmp_path):
        path = tmp_path / "key.json"
        path.write_bytes(b"\xef\xbb\xbf" + throwaway_key_json().encode())
        assert isinstance(self._mgr(tmp_path, gcp_service_account_file=str(path))._build_client(), gspread.Client)

    def test_inline_key_wins_over_the_file(self, tmp_path):
        path = tmp_path / "key.json"
        path.write_text("{ this file is not even json", encoding="utf-8")
        mgr = self._mgr(tmp_path, gcp_service_account_key=throwaway_key_json(), gcp_service_account_file=str(path))
        assert isinstance(mgr._build_client(), gspread.Client)

    def test_a_broken_inline_key_does_not_fall_back_to_a_good_file(self, tmp_path):
        path = tmp_path / "key.json"
        path.write_text(throwaway_key_json(), encoding="utf-8")
        mgr = self._mgr(tmp_path, gcp_service_account_key="{broken", gcp_service_account_file=str(path))
        with pytest.raises(SheetError) as caught:
            mgr._build_client()
        assert "GCP_SERVICE_ACCOUNT_KEY" in str(caught.value)

    def test_no_credentials_at_all_names_both_places_to_put_them(self, tmp_path):
        with pytest.raises(SheetError) as caught:
            self._mgr(tmp_path).open()
        text = str(caught.value)
        # Changed on the audit of typed setting values: this used to pin the key file's path in the message.
        # Both places are still named, by their setting names, and the file setting's value is not shown.
        assert "GCP_SERVICE_ACCOUNT_KEY" in text and "GCP_SERVICE_ACCOUNT_FILE" in text
        assert "absent.json" not in text

    @pytest.mark.parametrize(
        "bad, secret_fragment",
        [
            ("{not json fake-key-SECRETPART", "SECRETPART"),
            ("", None),
            ("[1, 2, 3]", None),
            ('"just a string fake-key-SECRETPART"', "SECRETPART"),
            (json.dumps({"type": "service_account", "client_email": "a@b.invalid", "private_key": "fake-key-SECRETPART"}), "SECRETPART"),
            (json.dumps({"type": "authorized_user", "client_email": "a@b.invalid", "private_key": "x", "token_uri": "https://t.invalid"}), None),
            (json.dumps({"type": "service_account", "client_email": "a@b.invalid", "private_key": "fake-key-SECRETPART-NOT-PEM",
                         "token_uri": "https://t.invalid"}), "SECRETPART"),
            (json.dumps({"type": "service_account", "client_email": 5, "private_key": ["fake-key-SECRETPART"], "token_uri": "t"}), "SECRETPART"),
        ],
    )
    def test_bad_inline_keys_raise_a_clear_error_that_never_echoes_the_text(self, tmp_path, bad, secret_fragment):
        mgr = self._mgr(tmp_path, gcp_service_account_key=bad) if bad else self._mgr(tmp_path)
        with pytest.raises(SheetError) as caught:
            mgr._build_client()
        text = str(caught.value)
        assert text and len(text) < 500
        if secret_fragment:
            assert secret_fragment not in text
        assert caught.value.__cause__ is None

    @pytest.mark.parametrize(
        "wrap",
        [
            lambda s: "\ufeff" + s,                       # a byte-order mark from an editor
            lambda s: "  \n" + s + "\n\n",                 # surrounding whitespace
            lambda s: '"' + s + '"',                      # quoted as a whole
            lambda s: "'" + s + "'",
            lambda s: base64.b64encode(s.encode()).decode(),  # the file base64-encoded
            lambda s: "\n".join(                          # base64 wrapped at 76 columns, as `base64` prints it
                [base64.b64encode(s.encode()).decode()[i:i + 76] for i in range(0, len(base64.b64encode(s.encode())), 76)]
            ),
            lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("="),
        ],
    )
    def test_a_key_pasted_with_a_bom_quotes_whitespace_or_base64_is_still_read(self, tmp_path, wrap):
        raw = throwaway_key_json()
        mgr = self._mgr(tmp_path, gcp_service_account_key=wrap(raw))
        assert mgr._load_service_account_info() == json.loads(raw)

    def test_an_unreadable_inline_key_is_described_by_length_and_first_character_only(self, tmp_path):
        mgr = self._mgr(tmp_path, gcp_service_account_key="C:/keys/fake-key-SECRETPART.json")
        with pytest.raises(SheetError) as caught:
            mgr._build_client()
        text = str(caught.value)
        assert "not valid JSON" in text and "does not begin with {" in text and "32 character(s)" in text
        assert "SECRETPART" not in text and "keys" not in text
        mgr = self._mgr(tmp_path, gcp_service_account_key="{broken fake-key-SECRETPART")
        with pytest.raises(SheetError) as caught:
            mgr._build_client()
        assert "begins with {" in str(caught.value) and "SECRETPART" not in str(caught.value)

    def test_base64_that_does_not_decode_to_an_object_is_not_treated_as_the_key(self, tmp_path):
        mgr = self._mgr(tmp_path, gcp_service_account_key=base64.b64encode(b"fake-key-SECRETPART").decode())
        with pytest.raises(SheetError) as caught:
            mgr._build_client()
        assert "not valid JSON" in str(caught.value) and "SECRETPART" not in str(caught.value)

    def test_missing_fields_are_listed_by_name_only(self, tmp_path):
        mgr = self._mgr(tmp_path, gcp_service_account_key=json.dumps({"type": "service_account", "client_email": "a@b.invalid"}))
        with pytest.raises(SheetError) as caught:
            mgr._build_client()
        assert "private_key" in str(caught.value) and "token_uri" in str(caught.value)

    def test_unreadable_key_file_is_a_clear_error(self, tmp_path):
        path = tmp_path / "key.json"
        path.write_bytes(b"\xff\xfe\x00bad")
        with pytest.raises(SheetError) as caught:
            self._mgr(tmp_path, gcp_service_account_file=str(path))._build_client()
        assert "cannot read" in str(caught.value) or "not valid JSON" in str(caught.value)

    def test_no_credentials_message_never_repeats_the_key_file_setting(self, tmp_path):
        # Regression: the message printed GCP_SERVICE_ACCOUNT_FILE's value, a typed setting.
        marker = "zq7keyfile"
        mgr = self._mgr(tmp_path, gcp_service_account_file=str(tmp_path / f"{marker}.json"))
        with pytest.raises(SheetError) as caught:
            mgr._build_client()
        assert "GCP_SERVICE_ACCOUNT_KEY" in str(caught.value) and "GCP_SERVICE_ACCOUNT_FILE" in str(caught.value)
        assert marker not in str(caught.value)

    @pytest.mark.parametrize("content", [b"\xff\xfe\x00bad", b"{broken", b"[]", b"{}"])
    def test_a_bad_key_file_message_never_repeats_its_path(self, tmp_path, content):
        marker = "zq7keydir"
        folder = tmp_path / marker
        folder.mkdir()
        path = folder / "key.json"
        path.write_bytes(content)
        with pytest.raises(SheetError) as caught:
            self._mgr(tmp_path, gcp_service_account_file=str(path))._build_client()
        assert "the service-account file" in str(caught.value)
        assert marker not in str(caught.value)

    def test_an_unreachable_spreadsheet_hint_points_at_the_key_file_not_the_service_account_email(
        self, tmp_path, monkeypatch, caplog
    ):
        # Changed on the public-log audit: this used to pin the service account's e-mail IN the message. The
        # e-mail names the Google Cloud project and the message reaches the public Actions log, so the hint now
        # says where the operator finds it (their own key file) and the address is never printed.
        caplog.set_level(logging.DEBUG)
        sa_client = FakeClient()
        monkeypatch.setattr(gm.gspread, "authorize", lambda credentials, **kw: sa_client)
        mgr = SheetManager(
            make_cfg(gcp_service_account_key=throwaway_key_json(), spreadsheet_id=SHEET_ID,
                     gcp_service_account_file=str(tmp_path / "absent.json")),
            sleep=lambda s: None,
        )
        with pytest.raises(SheetError) as caught:
            mgr.open()
        assert "the client_email in its key file" in str(caught.value)
        assert DEFAULT_SA_EMAIL not in str(caught.value) and DEFAULT_SA_EMAIL not in caplog.text
        assert SHEET_ID not in str(caught.value)
        assert sa_client.spreadsheets == {} and sa_client.calls_to("create") == []


# =================================================================================
# open()
# =================================================================================
class TestOpen:
    def test_creates_the_spreadsheet_and_all_tabs_with_their_headers(self):
        env = new_env()
        assert env.mgr.created_spreadsheet is True
        assert env.sheet.title == "Fake Tracker For Tests"
        assert sorted(env.sheet.titles()) == sorted(ALL_TABS)
        for tab in ALL_TABS:
            assert env.tab(tab).dump() == [list(HEADERS[tab])]

    def test_map_pins_is_the_first_tab_and_the_default_tab_is_gone(self):
        env = new_env()
        assert env.sheet.titles()[0] == TAB_PINS
        assert env.tab(TAB_PINS).index == 0
        assert "Sheet1" not in env.sheet.titles()

    def test_the_default_tab_is_found_by_position_not_by_english_name(self):
        client = FakeClient()
        client.create = lambda title, folder_id=None: client.add_spreadsheet(title, tabs={"Feuille 1": []})
        mgr = SheetManager(make_cfg(), client=client, sleep=lambda s: None)
        mgr.open()
        assert "Feuille 1" not in next(iter(client.spreadsheets.values())).titles()

    def test_a_default_tab_that_holds_data_is_never_deleted(self):
        client = FakeClient()

        def create(title, folder_id=None):
            sheet = client.add_spreadsheet(title, tabs={"Feuille 1": [["someone typed this"]]})
            return sheet

        client.create = create
        SheetManager(make_cfg(), client=client, sleep=lambda s: None).open()
        sheet = next(iter(client.spreadsheets.values()))
        assert "Feuille 1" in sheet.titles()

    def test_header_row_is_frozen_on_new_tabs(self):
        env = new_env()
        assert all(env.tab(t).frozen_row_count == 1 for t in ALL_TABS)

    def test_a_failure_to_freeze_does_not_stop_the_run(self, caplog):
        env = new_env(open_it=False)
        env.client.inject("freeze", 400, times=None)
        env.mgr.open()
        assert env.tab(TAB_REGISTRY).dump() == [list(REGISTRY_HEADERS)]
        assert "freeze" in caplog.text

    def test_a_lost_reply_when_removing_the_default_tab_does_not_stop_the_run(self, caplog):
        # Reviewed: a 503 whose delete DID happen was retried, the retry got "no sheet with that id" (400)
        # and open() failed on a spreadsheet that was already complete. Removing the empty tab is cosmetic.
        env = new_env(open_it=False)
        env.client.inject("del_worksheet", 503, times=1, applied=True)
        env.mgr.open()
        assert env.sheet.titles()[0] == TAB_PINS and "Sheet1" not in env.sheet.titles()
        assert "default tab" in caplog.text

    def test_headers_are_exactly_the_models_and_listing_headers(self):
        assert HEADERS[TAB_REGISTRY] == REGISTRY_HEADERS
        assert HEADERS[TAB_CANDIDATES] == CANDIDATE_HEADERS
        assert HEADERS[TAB_OVERRIDES] == OVERRIDE_HEADERS
        assert HEADERS[TAB_LISTINGS] == LISTING_HEADERS
        assert HEADERS[TAB_RUN_LOGS] == RUN_LOG_HEADERS
        assert HEADERS[TAB_PINS] == MAP_PIN_HEADERS
        assert set(HEADERS) == set(ALL_TABS)

    def test_run_log_has_scalar_columns_for_exit_code_rows_pins_and_sha_plus_one_json_column(self):
        for name in ("Exit Code", "Registry Rows", "Map Pins", "Git SHA", "Sources"):
            assert name in RUN_LOG_HEADERS
        assert len(set(RUN_LOG_HEADERS)) == len(RUN_LOG_HEADERS)

    def test_map_pins_has_no_permit_numbers_or_entered_by(self):
        assert "Permit No." not in MAP_PIN_HEADERS and "Entered By" not in MAP_PIN_HEADERS

    def test_reopening_makes_no_writes_and_does_not_share_again(self):
        env = new_env(share_with_emails=("one@example.invalid",))
        env.client.reset_calls()
        again = SheetManager(env.cfg, client=env.client, sleep=lambda s: None)
        again.open()
        assert again.created_spreadsheet is False
        assert set(env.client.methods()) <= {"open", "worksheets", "row_values"}
        assert len(env.sheet.permissions) == 1  # shared once, at creation

    def test_a_steady_state_open_costs_one_read_per_tab(self):
        env = new_env()
        env.client.reset_calls()
        SheetManager(env.cfg, client=env.client, sleep=lambda s: None).open()
        assert len(env.client.calls_to("row_values")) == len(ALL_TABS)

    # -- SPREADSHEET_ID ---------------------------------------------------------
    def test_opens_by_id_and_creates_nothing(self):
        env = new_env(tabs={TAB_REGISTRY: [list(REGISTRY_HEADERS)]})
        assert env.mgr.created_spreadsheet is False
        assert env.client.calls_to("create") == [] and env.client.calls_to("open") == []
        assert sorted(env.sheet.titles()) == sorted(ALL_TABS)

    def test_an_id_that_cannot_be_opened_fails_loudly_and_never_creates_another_sheet(self):
        client = FakeClient()
        client.add_spreadsheet("Fake Tracker For Tests")  # a sheet with the configured NAME exists
        before = set(client.spreadsheets)
        mgr = SheetManager(make_cfg(spreadsheet_id="fake-id-that-does-not-exist-1"), client=client, sleep=lambda s: None)
        with pytest.raises(SheetError) as caught:
            mgr.open()
        text = str(caught.value)
        assert "SPREADSHEET_ID" in text and "the client_email in its key file" in text and "Nothing was created" in text
        assert DEFAULT_SA_EMAIL not in text  # public log: the operator reads the address from their key file
        assert client.calls_to("create") == [] and client.calls_to("open") == []
        assert set(client.spreadsheets) == before

    def test_an_id_that_exists_but_is_not_shared_gives_the_same_hint(self):
        client = FakeClient()
        client.add_spreadsheet("Private", sheet_id=SHEET_ID)
        client.forbidden_ids.add(SHEET_ID)
        with pytest.raises(SheetError) as caught:
            SheetManager(make_cfg(spreadsheet_id=SHEET_ID), client=client, sleep=lambda s: None).open()
        assert "the client_email in its key file" in str(caught.value) and client.calls_to("create") == []
        assert DEFAULT_SA_EMAIL not in str(caught.value)

    @pytest.mark.parametrize("status", [403, 404])
    def test_api_level_403_and_404_on_open_give_the_hint(self, status):
        env = new_env(tabs={}, open_it=False)
        env.client.inject("open_by_key", status)
        with pytest.raises(SheetError) as caught:
            env.mgr.open()
        assert "the client_email in its key file" in str(caught.value) and caught.value.status == status
        assert DEFAULT_SA_EMAIL not in str(caught.value)
        assert env.client.calls_to("create") == []

    def test_other_api_errors_on_open_are_not_dressed_up_as_a_sharing_problem(self):
        env = new_env(tabs={}, open_it=False)
        env.client.inject("open_by_key", 400)
        with pytest.raises(SheetError) as caught:
            env.mgr.open()
        assert "Share the spreadsheet" not in str(caught.value)

    def test_a_transient_error_on_open_by_id_is_retried(self):
        env = new_env(tabs={}, open_it=False)
        env.client.inject("open_by_key", 503, times=2)
        env.mgr.open()
        assert env.sleeps == [2.0, 4.0]

    # -- sharing ------------------------------------------------------------------
    def test_a_new_spreadsheet_is_shared_per_the_settings(self):
        env = new_env(share_with_emails=("one@example.invalid", "two@example.invalid"), share_role="commenter", share_notify=True)
        assert env.sheet.permissions == [
            {"email": "one@example.invalid", "type": "user", "role": "commenter", "notify": True},
            {"email": "two@example.invalid", "type": "user", "role": "commenter", "notify": True},
        ]

    def test_sharing_does_not_send_notification_mail_unless_asked(self):
        env = new_env(share_with_emails=("one@example.invalid",))
        assert env.sheet.permissions == [{"email": "one@example.invalid", "type": "user", "role": "writer", "notify": False}]

    def test_no_share_settings_means_no_share_calls(self):
        assert new_env().client.calls_to("share") == []

    def test_a_failed_share_still_builds_the_sheet_then_fails_loudly(self):
        env = new_env(open_it=False, share_with_emails=("one@example.invalid", "two@example.invalid"))
        env.client.inject("share", 400, times=None)
        with pytest.raises(SheetError) as caught:
            env.mgr.open()
        text = str(caught.value)
        assert "could not be shared" in text and "SPREADSHEET_ID" in text
        assert "one@example.invalid" not in text and "two@example.invalid" not in text
        assert len(env.mgr.share_problems) == 2
        assert sorted(env.sheet.titles()) == sorted(ALL_TABS)  # complete, so it can be shared by hand

    def test_one_failed_share_does_not_stop_the_others(self):
        env = new_env(open_it=False, share_with_emails=("one@example.invalid", "two@example.invalid"))
        env.client.inject("share", 400, times=1)
        with pytest.raises(SheetError):
            env.mgr.open()
        assert [p["email"] for p in env.sheet.permissions] == ["two@example.invalid"]

    # -- existing sheets ----------------------------------------------------------
    def test_existing_columns_are_never_reordered_and_missing_ones_go_on_the_end(self):
        reordered = list(reversed(REGISTRY_HEADERS[:-1]))  # also lacks "Map Pin Link"
        row = [f"v{i}" for i in range(len(reordered))]
        env = new_env(tabs={TAB_REGISTRY: [reordered, row]})
        after = env.tab(TAB_REGISTRY).dump()
        assert after[0][: len(reordered)] == reordered
        assert after[0][len(reordered):] == ["Map Pin Link"]
        assert after[1][: len(reordered)] == row
        assert env.sheet.titles()[0] == TAB_PINS

    def test_new_columns_start_after_the_widest_data_not_on_top_of_an_unnamed_column(self):
        header = ["Project Key", "Emirate", "Analyst Note"]
        env = new_env(tabs={TAB_REGISTRY: [header, ["k", "Dubai", "n", "unnamed column data"]]})
        after = env.tab(TAB_REGISTRY).dump()
        assert after[1][:4] == ["k", "Dubai", "n", "unnamed column data"]
        assert after[0][:4] == ["Project Key", "Emirate", "Analyst Note", ""]
        assert after[0][4] == "Registry"

    def test_unknown_extra_header_columns_are_kept(self):
        header = list(REGISTRY_HEADERS) + ["Analyst Note"]
        env = new_env(tabs={TAB_REGISTRY: [header]})
        assert env.tab(TAB_REGISTRY).dump() == [header]

    def test_map_pins_is_moved_to_the_front_when_it_exists_elsewhere(self):
        tabs = {TAB_REGISTRY: [list(REGISTRY_HEADERS)], "Notes": [["x"]], TAB_PINS: [list(MAP_PIN_HEADERS)]}
        env = new_env(tabs=tabs)
        assert env.sheet.titles()[0] == TAB_PINS
        assert env.client.calls_to("update_index")

    def test_a_duplicated_known_header_is_refused_not_guessed_at(self):
        header = list(REGISTRY_HEADERS) + ["Developer"]
        with pytest.raises(SheetError) as caught:
            new_env(tabs={TAB_REGISTRY: [header]})
        assert "Developer" in str(caught.value) and "more than once" in str(caught.value)

    def test_a_header_duplicated_by_hand_between_runs_is_refused_on_the_next_read(self):
        env = new_env()
        env.mgr.write_registry([dubai(1)])
        position = env.tab(TAB_REGISTRY).dump()[0].index("Master Developer") + 1
        env.tab(TAB_REGISTRY).update([["Developer"]], range_name=gspread.utils.rowcol_to_a1(1, position))
        for action in (env.mgr.read_registry, lambda: env.mgr.write_registry([dubai(1)])):
            with pytest.raises(SheetError) as caught:
                action()
            assert "Developer" in str(caught.value) and "more than once" in str(caught.value)

    def test_a_tab_with_data_but_no_header_row_is_refused(self):
        with pytest.raises(SheetError) as caught:
            new_env(tabs={TAB_REGISTRY: [[], ["Dubai", "DLD"]]})
        assert "header" in str(caught.value)

    def test_an_empty_existing_tab_gets_its_header(self):
        env = new_env(tabs={TAB_REGISTRY: []})
        assert env.tab(TAB_REGISTRY).dump() == [list(REGISTRY_HEADERS)]

    def test_methods_before_open_say_so(self):
        mgr = SheetManager(make_cfg(), client=FakeClient(), sleep=lambda s: None)
        with pytest.raises(SheetError) as caught:
            mgr.read_registry()
        assert "open()" in str(caught.value)

    def test_unknown_tab_name_is_refused(self):
        env = new_env()
        with pytest.raises(SheetError):
            env.mgr.delete_rows("No_Such_Tab", [2])


# =================================================================================
# Reading
# =================================================================================
class TestReadRegistry:
    def _env_with(self, header: list[str], *rows: list[str]) -> Env:
        return new_env(tabs={TAB_REGISTRY: [header, *rows]})

    def _line(self, header: list[str], **values: str) -> list[str]:
        return [values.get(name, "") for name in header]

    def test_columns_are_found_by_name_whatever_their_order(self):
        header = ["Analyst Note", *reversed(REGISTRY_HEADERS[1:]), "Project Key", ""]
        row = self._line(header, **{"Emirate": "Dubai", "Registry": "DLD", "Registry Project No.": "00123",
                                   "Project Name (EN)": "Fake Tower", "Developer": "Fake Developer",
                                   "Analyst Note": "keep me", "Percent Completed": "67.5", "Units": "1,200"})
        env = self._env_with(header, row)
        rows, problems = env.mgr.read_registry()
        assert problems == [] and len(rows) == 1
        assert rows[0].registry_project_no == "00123"  # leading zeros survive
        assert rows[0].percent_completed == 67.5 and rows[0].units == 1200
        assert rows[0].project_key == "dld:dubai:00123"

    def test_a_bad_row_is_reported_with_its_sheet_row_number_and_left_out(self):
        h = list(REGISTRY_HEADERS)
        good = self._line(h, Emirate="Dubai", Registry="DLD", **{"Registry Project No.": "1", "Project Name (EN)": "Fake A", "Developer": "D"})
        bad = self._line(h, Emirate="Atlantis", Registry="DLD", **{"Registry Project No.": "2"})
        wrong_pair = self._line(h, Emirate="Sharjah", Registry="DLD", **{"Registry Project No.": "3"})
        blank = [""] * len(h)
        env = self._env_with(h, good, bad, blank, wrong_pair)
        rows, problems = env.mgr.read_registry()
        assert [r.registry_project_no for r in rows] == ["1"]
        assert len(problems) == 2
        assert problems[0].startswith("Registry_Projects row 3:") and "emirate" in problems[0].lower()
        assert problems[1].startswith("Registry_Projects row 5:")  # row 4 was blank: numbering follows the sheet
        assert env.mgr.registry_problem_count == 2

    def test_problem_count_resets_on_a_clean_read(self):
        h = list(REGISTRY_HEADERS)
        env = self._env_with(h, self._line(h, Emirate="Nowhere", Registry="DLD"))
        env.mgr.read_registry()
        assert env.mgr.registry_problem_count == 1
        env.tab(TAB_REGISTRY).put([self._line(h, Emirate="Dubai", Registry="DLD", **{"Registry Project No.": "1"})], start_row=2)
        env.mgr.read_registry()
        assert env.mgr.registry_problem_count == 0

    def test_a_half_written_row_is_a_problem_not_a_crash_and_not_a_silent_drop(self):
        h = list(REGISTRY_HEADERS)
        env = self._env_with(h, ["dld:dubai:5", "Dubai"])  # the writer died after two cells
        rows, problems = env.mgr.read_registry()
        assert rows == [] and len(problems) == 1 and "row 2" in problems[0]

    def test_a_row_with_no_identity_is_reported(self):
        h = list(REGISTRY_HEADERS)
        env = self._env_with(h, self._line(h, Emirate="Dubai", Registry="SEED", **{"Project Name (EN)": "Name Only"}))
        rows, problems = env.mgr.read_registry()
        assert rows == [] and "identity" in problems[0]

    @pytest.mark.parametrize(
        "name, developer",
        [
            ("Name Only", ""),
            ("Fake Tower A+", "Fake Developer"),
            ("Fake C#", "Fake Developer"),
            ("Fake @ Marina", "Fake Developer"),
            ("Fake Tower", "Fake+ Developer"),
            ("Fake T\u00f8wer", "Fake Developer"),  # o-stroke: a letter that does not fold to ASCII
        ],
    )
    def test_the_no_identity_problem_says_what_makes_a_name_usable_and_echoes_no_name(self, name, developer, caplog):
        h = list(REGISTRY_HEADERS)
        env = self._env_with(
            h, self._line(h, Emirate="Dubai", Registry="SEED", **{"Project Name (EN)": name, "Developer": developer})
        )
        with caplog.at_level(logging.DEBUG):
            rows, problems = env.mgr.read_registry()
        assert rows == [] and len(problems) == 1
        line = problems[0]
        assert line.startswith("Registry_Projects row 2: no usable identity (")
        assert "Latin letter" in line and "another script" in line and "+ # @" in line
        assert "Developer and Project Name (EN)" in line
        for text in (line, caplog.text):
            assert "Tower" not in text and "Marina" not in text and "Fake" not in text

    def test_a_row_error_that_is_not_a_validation_error_shows_only_its_class(self, monkeypatch):
        h = list(REGISTRY_HEADERS)
        env = self._env_with(h, self._line(h, Emirate="Dubai", Registry="DLD", **{"Registry Project No.": "1"}))

        def broken(cls, data):
            raise ValueError("MARKER cell value Alpha Residences")

        monkeypatch.setattr(RegistryRow, "from_sheet_dict", classmethod(broken))
        rows, problems = env.mgr.read_registry()
        assert rows == [] and problems == ["Registry_Projects row 2: unreadable (ValueError)"]

    def test_a_row_holding_only_analyst_notes_is_reported_not_ignored(self):
        h = list(REGISTRY_HEADERS) + ["Analyst Note"]
        env = self._env_with(h, self._line(h, **{"Analyst Note": "call the developer"}))
        rows, problems = env.mgr.read_registry()
        assert rows == [] and len(problems) == 1

    def test_whitespace_only_rows_count_as_blank_rows_with_data_none(self):
        h = list(REGISTRY_HEADERS)
        env = self._env_with(h, [""] * len(h))
        assert env.mgr.read_registry() == ([], [])

    def test_unicode_and_arabic_survive_a_write_read_cycle(self):
        env = new_env()
        row = dubai(1, project_name_en="Tower — Ünïcode ✓", project_name_ar="برج الاختبار", area_ar="دبي مارينا", developer="Fake Developer")
        env.mgr.write_registry([row])
        (back,), problems = env.mgr.read_registry()
        assert problems == [] and back.project_name_ar == "برج الاختبار" and back.project_name_en == "Tower — Ünïcode ✓"

    def test_read_registry_numbered_gives_the_sheet_row_numbers(self):
        h = list(REGISTRY_HEADERS)
        mk = lambda n: self._line(h, Emirate="Dubai", Registry="DLD", **{"Registry Project No.": str(n), "Project Name (EN)": f"T{n}", "Developer": "D"})
        env = self._env_with(h, mk(1), [""] * len(h), mk(2))
        numbered, problems = env.mgr.read_registry_numbered()
        assert [(n, r.registry_project_no) for n, r in numbered] == [(2, "1"), (4, "2")]

    def test_one_read_call_per_table_read(self):
        env = new_env()
        env.mgr.write_registry([dubai(i) for i in range(5)])
        env.client.reset_calls()
        env.mgr.read_registry()
        assert env.client.methods() == ["get_all_values"]


class TestOtherReads:
    def test_listing_records_by_header_name_with_reordered_columns_and_text_kept(self):
        header = ["Source URL", "Project Key", "Price From (AED)", "Extra"]
        row = ["https://example.invalid/p", "dld:dubai:00123", "1,200,000", "x"]
        env = new_env(tabs={TAB_LISTINGS: [header, row]})
        records = env.mgr.read_listing_records()
        assert list(records) == ["dld:dubai:00123"]
        assert records["dld:dubai:00123"]["price_from"] == 1_200_000

    def test_listing_tab_without_a_project_key_column_is_refused(self):
        env = new_env(tabs={TAB_LISTINGS: [["Source URL"]]})
        # open() added the missing columns; blank "Project Key" to simulate a hand-damaged header
        position = env.tab(TAB_LISTINGS).dump()[0].index("Project Key") + 1
        env.tab(TAB_LISTINGS).update([[""]], range_name=gspread.utils.rowcol_to_a1(1, position))
        with pytest.raises(SheetError) as caught:
            env.mgr.read_listing_records()
        assert "Project Key" in str(caught.value)

    def test_overrides_are_keyed_with_problems_listed_and_the_later_row_wins(self, caplog):
        h = list(OVERRIDE_HEADERS)
        rows = [
            ["dld:dubai:1", "25.2", "55.27", "exact", "first"],
            ["not a key", "25.2", "55.27", "exact", ""],
            ["dld:dubai:1", "25.3", "55.28", "exact", "corrected — بعد التعديل"],
            ["srerd:sharjah:S1", "999", "55.4", "", ""],
            ["srerd:sharjah:S2", "25.35", "55.40", "", ""],
        ]
        env = new_env(tabs={TAB_OVERRIDES: [h, *rows]})
        found, problems = env.mgr.read_overrides()
        assert sorted(found) == ["dld:dubai:1", "srerd:sharjah:S2"]
        assert found["dld:dubai:1"].latitude == 25.3 and "بعد" in found["dld:dubai:1"].note
        assert found["srerd:sharjah:S2"].precision == ""
        assert [p.split(":")[0] for p in problems] == ["Geocode_Overrides row 3", "Geocode_Overrides row 5"]
        # the replaced row is reported by number; neither its key nor the text of a bad key cell reaches the log
        assert "row 4 replaces row 2" in caplog.text
        assert "dld:" not in caplog.text and "not a key" not in caplog.text

    def test_overrides_on_an_empty_tab(self):
        assert new_env().mgr.read_overrides() == ({}, [])

    def test_a_bad_cell_value_is_never_echoed_into_a_problem_line_or_the_log(self, caplog):
        private = "Zzyzx"
        env = new_env(tabs={
            TAB_OVERRIDES: [
                list(OVERRIDE_HEADERS),
                [f"{private} Marina Heights by Fictional Builder", "25.2", "55.27", "exact", ""],
                ["dld:dubai:1", f"not-a-number-{private}", "55.27", "exact", ""],
                ["dld:dubai:2", "25.2", f"not-a-number-{private}", "exact", ""],
            ],
            TAB_CANDIDATES: [
                list(CANDIDATE_HEADERS),
                ["", "Dubai", "Fake Launch", "Fake Dev", "", "claim", "https://example.invalid/a", "", f"{private}-pending"],
                ["", f"{private} Emirate", "Other Launch", "Dev", "", "claim", "https://example.invalid/b", "", "new"],
            ],
        })
        env.mgr.write_registry([dubai(1)])
        ws = env.tab(TAB_REGISTRY)
        ws.update([[f"{private} Emirate"]], range_name=gspread.utils.rowcol_to_a1(2, ws.dump()[0].index("Emirate") + 1))
        with caplog.at_level(logging.DEBUG, logger=gm.logger.name):
            _, override_problems = env.mgr.read_overrides()
            env.mgr.read_candidates()
            _, registry_problems = env.mgr.read_registry()
        lines = override_problems + env.mgr.last_candidate_problems + registry_problems
        assert len(override_problems) == 3 and len(env.mgr.last_candidate_problems) == 2 and len(registry_problems) == 1
        assert private not in "\n".join(lines) and private not in caplog.text
        assert all("row" in line for line in lines)  # the row number still tells a person where to look

    def test_candidates_skip_invalid_rows_and_say_which(self, caplog):
        h = list(CANDIDATE_HEADERS)
        good = ["", "Dubai", "Fake Launch", "Fake Dev", "", "claim", "https://example.invalid/a", "2026-10-25T03:00:00Z", "new"]
        no_url = ["", "Dubai", "No Source", "Dev", "", "claim", "", "", "new"]
        env = new_env(tabs={TAB_CANDIDATES: [h, good, no_url]})
        found = env.mgr.read_candidates()
        assert [c.project_name for c in found] == ["Fake Launch"]
        assert env.mgr.last_candidate_problems and "row 3" in env.mgr.last_candidate_problems[0]
        assert "row 3" in caplog.text


class TestReadOnlyOpen:
    """open(read_only=True): look at an existing spreadsheet without adding a tab, a column, a freeze or a share."""

    MUTATIONS = ("update", "append_rows", "delete_rows", "clear", "resize", "freeze", "update_index",
                 "add_worksheet", "del_worksheet", "share", "create")

    def _old_sheet(self, with_registry: bool = True) -> Env:
        """An existing spreadsheet from an older schema: one tab, last three registry columns missing."""
        old = list(REGISTRY_HEADERS)[:-3]
        tabs = {TAB_REGISTRY: [old, ["x"] * len(old)]} if with_registry else {"Notes": [["hand written"]]}
        return new_env(tabs=tabs, open_it=False)

    def _mutations(self, env: Env) -> list[str]:
        return [c.method for c in env.client.calls if c.method in self.MUTATIONS]

    def test_nothing_is_written_and_the_existing_tab_can_be_read(self):
        env = self._old_sheet()
        before = {ws.title: ws.dump() for ws in env.sheet._sheets}
        env.mgr.open(read_only=True)
        rows, problems = env.mgr.read_registry()
        assert self._mutations(env) == []
        assert env.sheet.titles() == [TAB_REGISTRY]
        assert {ws.title: ws.dump() for ws in env.sheet._sheets} == before
        assert env.mgr.created_spreadsheet is False

    def test_it_needs_an_id_and_never_creates_or_searches(self):
        env = new_env(open_it=False)  # no SPREADSHEET_ID
        with pytest.raises(SheetError) as caught:
            env.mgr.open(read_only=True)
        assert "SPREADSHEET_ID" in str(caught.value)
        assert env.client.spreadsheets == {} and env.client.calls_to("create") == []
        # Audit: refused before any lookup at all (open_by_key("") would otherwise be sent to Google), and the
        # message says why this mode cannot continue, not just that an id is missing.
        assert env.client.calls_to("open_by_key") == [] and env.client.calls_to("open") == []
        assert "read-only" in str(caught.value)

    def test_only_the_six_known_tabs_are_bound_so_a_stray_tab_is_still_unknown(self):
        env = new_env(tabs={TAB_REGISTRY: [list(REGISTRY_HEADERS)], "Notes": [["hand written"]]}, open_it=False)
        env.mgr.open(read_only=True)
        with pytest.raises(SheetError) as caught:
            env.mgr._ws("Notes")
        assert "unknown tab" in str(caught.value)

    def test_a_tab_that_is_not_there_says_so_instead_of_pretending_the_sheet_is_unopened(self):
        env = self._old_sheet(with_registry=False)
        env.mgr.open(read_only=True)
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        text = str(caught.value)
        assert TAB_REGISTRY in text and "does not exist" in text and "not open yet" not in text
        assert self._mutations(env) == []

    def test_a_manager_that_was_never_opened_still_says_so(self):
        with pytest.raises(SheetError) as caught:
            new_env(open_it=False).mgr.read_registry()
        assert "not open yet" in str(caught.value)

    def test_write_registry_after_a_read_only_open_changes_only_the_registry_tab(self):
        env = self._old_sheet()
        env.mgr.open(read_only=True)
        env.mgr.write_registry([dubai(1), dubai(2)])
        assert env.sheet.titles() == [TAB_REGISTRY]
        assert env.tab(TAB_REGISTRY).dump()[0][-3:] == list(REGISTRY_HEADERS)[-3:]  # the write itself completes the header
        assert [c.tab for c in env.client.calls if c.method == "add_worksheet"] == []

    def test_a_full_open_afterwards_still_completes_the_spreadsheet(self):
        env = self._old_sheet()
        env.mgr.open(read_only=True)
        env.mgr.open()
        assert sorted(env.sheet.titles()) == sorted(ALL_TABS)


class TestHeaderlessTabs:
    """A tab with no header row is an error to read, not a silent empty table (gspread reports it as [[]])."""

    READERS = ("read_registry", "read_overrides", "read_candidates", "read_listing_records", "last_source_counts")

    @pytest.mark.parametrize("reader", READERS)
    def test_a_fully_cleared_tab_is_reported_not_read_as_empty(self, reader):
        env = new_env()
        tab = {"read_registry": TAB_REGISTRY, "read_overrides": TAB_OVERRIDES, "read_candidates": TAB_CANDIDATES,
               "read_listing_records": TAB_LISTINGS, "last_source_counts": TAB_RUN_LOGS}[reader]
        env.tab(tab).clear()
        with pytest.raises(SheetError) as caught:
            getattr(env.mgr, reader)()
        assert "no header row" in str(caught.value) and tab in str(caught.value)

    def test_write_registry_refuses_a_headerless_tab_and_writes_nothing(self):
        env = new_env()
        env.tab(TAB_REGISTRY).clear()
        env.client.reset_calls()
        with pytest.raises(SheetError):
            env.mgr.write_registry([dubai(1)])
        assert env.tab(TAB_REGISTRY).dump() == []
        assert not [c for c in env.client.calls if c.method in ("update", "append_rows", "delete_rows")]

    def test_a_blank_header_row_above_data_is_reported_instead_of_misparsed(self):
        env = new_env()
        ws = env.tab(TAB_REGISTRY)
        ws.clear()
        ws.put([[], ["a", "b", "c"]])
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        assert "no header row" in str(caught.value)


class TestOversizedCells:
    """Sheets rejects a cell over 50,000 characters. That must be caught BEFORE the first write: a later chunk
    failing leaves the earlier staged chunks behind as duplicates, a few more on every daily run."""

    WRITES = ("update", "append_rows", "delete_rows", "resize", "add_worksheet")

    def _writes(self, env):
        return [c for c in env.client.calls if c.method in self.WRITES]

    def test_write_registry_refuses_before_writing_anything(self, monkeypatch):
        monkeypatch.setattr(gm, "WRITE_CHUNK_ROWS", 2)  # several chunks: the bad row is in the last one
        env = new_env()
        env.mgr.write_registry([dubai(n) for n in range(1, 6)])
        before = env.tab(TAB_REGISTRY).dump()
        env.client.reset_calls()
        rows = [dubai(n) for n in range(1, 6)]
        rows[4] = dubai(5, project_name_en="X" * 50_001)
        with pytest.raises(SheetError) as caught:
            env.mgr.write_registry(rows)
        text = str(caught.value)
        assert "50,000" in text and "Project Name (EN)" in text and "XXXX" not in text
        assert self._writes(env) == []
        assert env.tab(TAB_REGISTRY).dump() == before

    def test_a_cell_of_exactly_50000_characters_is_still_written(self):
        env = new_env()
        env.mgr.write_registry([dubai(1, project_name_en="X" * 50_000)])
        assert len(env.records(TAB_REGISTRY)[0]["Project Name (EN)"]) == 50_000

    def test_write_map_pins_checks_too(self):
        env = new_env()
        env.client.reset_calls()
        row = dubai(1, latitude=25.2, longitude=55.27, location_precision="exact", project_name_en="X" * 50_001)
        with pytest.raises(SheetError):
            env.mgr.write_map_pins([row])
        assert self._writes(env) == []

    def test_seed_append_checks_every_chunk_before_the_first_append(self, monkeypatch):
        monkeypatch.setattr(gm, "APPEND_CHUNK_ROWS", 1)
        env = new_env()
        env.client.reset_calls()
        seed = [listing_seed("dld:dubai:1"), listing_seed("dld:dubai:2"), listing_seed("dld:dubai:3", price="9" * 50_001)]
        with pytest.raises(SheetError) as caught:
            env.mgr.apply_listing_seed(seed)
        assert "50,000" in str(caught.value)
        assert env.client.calls_to("append_rows") == []
        assert "Price From (AED)" in str(caught.value)  # the column is named on every append path, not "column 2"

    def test_candidate_append_names_the_column(self):
        env = new_env()
        env.client.reset_calls()
        with pytest.raises(SheetError) as caught:
            env.mgr.add_candidates([candidate("X" * 50_001)])
        assert "'Candidate Key'" in str(caught.value) and "XXXX" not in str(caught.value)  # the key is built from the name
        assert env.client.calls_to("append_rows") == []

    def test_run_log_append_names_the_column(self):
        env = new_env()
        env.client.reset_calls()
        with pytest.raises(SheetError) as caught:
            env.mgr.append_run_log(run_entry(geocode={f"counter_{n}": n for n in range(6_000)}))  # a JSON cell over 50,000 characters
        assert "'Geocode'" in str(caught.value)
        assert env.client.calls_to("append_rows") == []


# =================================================================================
# write_registry
# =================================================================================
class TestWriteRegistry:
    def test_round_trip_and_text_is_stored_raw(self):
        env = new_env()
        rows = [dubai(1, registry_project_no="00123", developer="=HYPERLINK(\"https://example.invalid\")", first_seen="2026-10-25"),
                sharjah("S-9")]
        env.mgr.write_registry(rows)
        stored = env.records(TAB_REGISTRY)
        assert stored[0]["Registry Project No."] == "00123"
        assert stored[0]["Developer"].startswith("=HYPERLINK")  # text, not a formula
        assert stored[0]["First Seen"] == "2026-10-25"
        back, problems = env.mgr.read_registry()
        assert problems == [] and [r.project_key for r in back] == [r.project_key for r in rows]

    def test_every_write_uses_raw(self):
        env = new_env()
        env.mgr.write_registry([dubai(i) for i in range(3)])
        env.mgr.write_map_pins([dubai(1, latitude=25.2, longitude=55.27, location_precision="exact")])
        env.mgr.add_candidates([candidate("Fake Launch")])
        env.mgr.append_run_log(run_entry())
        env.mgr.apply_listing_seed([["dld:dubai:1", "", "", "", "", "", "", "", "", "", "", "https://example.invalid/s", "manual", "", ""]])
        writes = calls(env, "update", "append_rows")
        assert writes and all(c.detail["option"] == "RAW" for c in writes)

    def test_appends_use_insert_rows_so_a_gap_cannot_make_them_overwrite_data(self):
        env = new_env()
        env.mgr.add_candidates([candidate("Fake Launch")])
        assert env.client.calls_to("append_rows")[0].detail["insert"] == "INSERT_ROWS"

    def test_duplicate_key_check_is_linear_not_quadratic(self, monkeypatch):
        # Reviewed: `keys.count(k)` inside a comprehension made the duplicate warning O(n^2)
        # (20,000 rows spent about 6 s there; the full feed is far larger). Count key
        # comparisons with a str subclass: a linear check does about n, the old one n*n.
        compared = {"n": 0}

        class CountingKey(str):
            __hash__ = str.__hash__

            def __eq__(self, other):
                compared["n"] += 1
                return str.__eq__(self, other)

        n = 600
        rows = [dubai(i) for i in range(n)]
        monkeypatch.setattr(RegistryRow, "project_key", property(lambda self: CountingKey(f"k:{id(self)}")))
        env = new_env()
        checked = env.mgr._checked_registry_rows(rows)
        assert len(checked) == n
        assert compared["n"] < 10 * n, f"{compared['n']} key comparisons for {n} rows"

    def test_duplicate_keys_in_the_new_rows_are_still_warned_about(self, caplog):
        env = new_env()
        rows = [dubai(1), dubai(1, developer="Other"), dubai(2)]
        with caplog.at_level(logging.WARNING, logger=gm.logger.name):
            env.mgr._checked_registry_rows(rows)
        assert "1 duplicate Project Key" in caplog.text
        assert "dld:" not in caplog.text and "100001" not in caplog.text  # a key is row content; the public log gets the count

    def test_new_rows_are_written_before_the_old_ones_are_removed(self):
        env = new_env()
        env.mgr.write_registry([dubai(i) for i in range(4)])
        env.client.reset_calls()
        env.mgr.write_registry([dubai(i) for i in range(10, 13)])
        order = env.client.methods()
        assert "delete_rows" in order
        assert max(i for i, m in enumerate(order) if m == "update") < order.index("delete_rows")
        assert keys_on_sheet(env) == [f"dld:dubai:{100000 + i}" for i in range(10, 13)]

    def test_a_crash_before_the_old_rows_are_removed_loses_nothing(self):
        env = new_env()
        old = [dubai(i) for i in range(4)]
        env.mgr.write_registry(old)
        env.client.inject("delete_rows", exception=RuntimeError("process died"))
        with pytest.raises(RuntimeError):
            env.mgr.write_registry([dubai(i) for i in range(10, 13)])
        keys = keys_on_sheet(env)
        assert all(r.project_key in keys for r in old)  # every old row is still there
        assert len(keys) == 4 + 3  # plus the staged copy: duplicates, never a loss

    def test_a_crash_between_chunks_loses_nothing_and_the_next_run_heals_it(self, monkeypatch):
        monkeypatch.setattr(gm, "WRITE_CHUNK_ROWS", 2)
        env = new_env()
        old = [dubai(i) for i in range(5)]
        env.mgr.write_registry(old)
        env.client.reset_calls()
        env.client.inject("update", exception=RuntimeError("process died"), skip=1)
        new = [dubai(i) for i in range(20, 26)]
        with pytest.raises(RuntimeError):
            env.mgr.write_registry(new)
        keys = keys_on_sheet(env)
        assert all(r.project_key in keys for r in old)
        env.client.clear_faults()
        env.mgr.write_registry(new)  # a later run replaces everything, duplicates included
        assert keys_on_sheet(env) == [r.project_key for r in new]

    def test_a_failed_delete_after_staging_says_duplicates_exist(self):
        env = new_env()
        env.mgr.write_registry([dubai(i) for i in range(3)])
        env.client.inject("delete_rows", 503, applied=True)
        with pytest.raises(SheetError) as caught:
            env.mgr.write_registry([dubai(i) for i in range(5, 7)])
        assert "duplicates" in str(caught.value) and "may or may not" in str(caught.value)
        assert len(env.client.calls_to("delete_rows")) == 1  # never repeated

    def test_large_writes_are_chunked_by_rows(self, monkeypatch):
        monkeypatch.setattr(gm, "WRITE_CHUNK_ROWS", 7)
        env = new_env()
        env.client.reset_calls()
        env.mgr.write_registry([dubai(i) for i in range(30)])
        staged = [c for c in env.client.calls_to("update") if c.detail["rows"] > 1]
        assert [c.detail["rows"] for c in staged] == [7, 7, 7, 7, 2]
        assert len(keys_on_sheet(env)) == 30

    def test_large_writes_are_also_chunked_by_cell_count(self, monkeypatch):
        monkeypatch.setattr(gm, "WRITE_CHUNK_CELLS", len(REGISTRY_HEADERS) * 3)
        env = new_env()
        env.client.reset_calls()
        env.mgr.write_registry([dubai(i) for i in range(10)])
        assert max(c.detail["rows"] for c in env.client.calls_to("update")) <= 3
        assert len(keys_on_sheet(env)) == 10

    def test_thousands_of_rows_exceed_the_initial_grid_and_the_grid_is_grown(self):
        env = new_env()
        initial = env.tab(TAB_REGISTRY).row_count
        n = initial + 600
        env.mgr.write_registry([dubai(i) for i in range(n)])
        assert env.tab(TAB_REGISTRY).row_count >= n + 1
        assert env.client.calls_to("resize")
        back, problems = env.mgr.read_registry()
        assert problems == [] and len(back) == n

    def test_fewer_rows_than_before_leaves_no_surplus(self):
        env = new_env()
        env.mgr.write_registry([dubai(i) for i in range(10)])
        env.mgr.write_registry([dubai(i) for i in range(3)])
        assert len(keys_on_sheet(env)) == 3
        assert len(env.tab(TAB_REGISTRY).dump()) == 4

    def test_refuses_to_replace_a_non_empty_tab_with_zero_rows(self):
        env = new_env()
        env.mgr.write_registry([dubai(i) for i in range(3)])
        before = env.tab(TAB_REGISTRY).dump()
        env.client.reset_calls()
        with pytest.raises(SheetError) as caught:
            env.mgr.write_registry([])
        assert "zero rows" in str(caught.value)
        assert env.tab(TAB_REGISTRY).dump() == before
        assert not calls(env, "update", "delete_rows", "append_rows")

    def test_zero_rows_on_an_empty_tab_is_a_no_op(self):
        env = new_env()
        env.client.reset_calls()
        env.mgr.write_registry([])
        assert not calls(env, "update", "delete_rows", "append_rows")

    def test_a_generator_of_rows_is_accepted(self):
        env = new_env()
        env.mgr.write_registry(dubai(i) for i in range(3))
        assert len(keys_on_sheet(env)) == 3

    @pytest.mark.parametrize("bad", [None, "rows", ["not a row"], [{"Emirate": "Dubai"}], {"a": 1}])
    def test_wrong_types_are_refused_before_anything_is_written(self, bad):
        env = new_env()
        env.mgr.write_registry([dubai(1)])
        env.client.reset_calls()
        with pytest.raises(SheetError):
            env.mgr.write_registry(bad)
        assert not calls(env, "update", "delete_rows")
        assert len(keys_on_sheet(env)) == 1

    def test_a_row_with_no_identity_key_refuses_the_whole_write(self):
        env = new_env()
        env.mgr.write_registry([dubai(1)])
        env.client.reset_calls()
        with pytest.raises(SheetError) as caught:
            env.mgr.write_registry([dubai(2), RegistryRow(emirate="Dubai", registry="SEED", project_name_en="Name Only")])
        assert "identity" in str(caught.value) and "positions in the new data: 2" in str(caught.value)
        assert "Name Only" not in str(caught.value)  # the message reaches the public Actions log
        assert "Latin letter" in str(caught.value) and "another script" in str(caught.value) and "+ # @" in str(caught.value)
        assert not calls(env, "update", "delete_rows")

    def test_duplicate_keys_in_the_new_rows_are_written_with_a_warning(self, caplog):
        env = new_env()
        env.mgr.write_registry([dubai(1), dubai(1, status="Active")])
        assert len(keys_on_sheet(env)) == 2
        assert "duplicate Project Key" in caplog.text

    def test_an_oversized_cell_fails_loudly_and_leaves_the_old_data_alone(self):
        env = new_env()
        env.mgr.write_registry([dubai(1), dubai(2)])
        before = env.tab(TAB_REGISTRY).dump()
        with pytest.raises(SheetError) as caught:
            env.mgr.write_registry([dubai(3, developer="x" * 60_000)])
        assert caught.value.status == 400
        assert env.tab(TAB_REGISTRY).dump() == before

    def test_a_concurrent_insert_above_the_staged_rows_stops_the_delete(self):
        env = new_env()
        env.mgr.write_registry([dubai(i) for i in range(3)])
        ws = env.tab(TAB_REGISTRY)
        real_update = ws.update

        def update_then_someone_inserts_a_row(values, range_name=None, **kw):
            out = real_update(values, range_name, **kw)
            ws._data.insert(1, ["a person inserted this row"])
            ws._rows_count += 1
            return out

        ws.update = update_then_someone_inserts_a_row
        with pytest.raises(SheetError) as caught:
            env.mgr.write_registry([dubai(i) for i in range(10, 12)])
        assert "changed while" in str(caught.value) and "NOT removed" in str(caught.value)
        assert env.client.calls_to("delete_rows") == []
        assert "a person inserted this row" in [r[0] for r in ws.dump()]

    def test_a_sheet_missing_a_column_gets_it_added_when_rows_are_written(self):
        header = [h for h in REGISTRY_HEADERS if h != "Zone"]  # an older layout
        env = new_env(tabs={TAB_REGISTRY: [header]})
        assert "Zone" in env.tab(TAB_REGISTRY).dump()[0]  # open() already added it, at the end
        env.mgr.write_registry([dubai(1, zone="Fake Zone")])
        assert env.records(TAB_REGISTRY)[0]["Zone"] == "Fake Zone"

    def test_a_reordered_sheet_is_written_in_its_own_order(self):
        header = list(reversed(REGISTRY_HEADERS))
        env = new_env(tabs={TAB_REGISTRY: [header]})
        env.mgr.write_registry([dubai(1, project_name_en="Fake Tower One")])
        assert env.tab(TAB_REGISTRY).dump()[0][: len(header)] == header
        assert env.records(TAB_REGISTRY)[0]["Project Name (EN)"] == "Fake Tower One"

    # -- analyst columns ------------------------------------------------------------
    def _analyst_env(self):
        header = ["Analyst Note", *REGISTRY_HEADERS, "", "Reviewed By"]
        env = new_env(tabs={TAB_REGISTRY: [header]})
        env.mgr.write_registry([dubai(1), dubai(2), dubai(3)])
        ws = env.tab(TAB_REGISTRY)
        notes = {"dld:dubai:100001": ("note one", "unnamed one", "analyst-a"),
                 "dld:dubai:100002": ("note two", "", "analyst-b"),
                 "dld:dubai:100003": ("", "unnamed three", "")}
        head = ws.dump()[0]
        key_col = head.index("Project Key")
        for number, row in enumerate(ws.dump()[1:], start=2):
            note, unnamed, reviewer = notes[row[key_col]]
            ws.update([[note]], range_name=f"A{number}")
            ws.update([[unnamed]], range_name=gspread.utils.rowcol_to_a1(number, len(REGISTRY_HEADERS) + 2))
            ws.update([[reviewer]], range_name=gspread.utils.rowcol_to_a1(number, len(REGISTRY_HEADERS) + 3))
        return env, notes

    def _extras_by_key(self, env: Env) -> dict[str, tuple[str, str, str]]:
        values = env.tab(TAB_REGISTRY).dump()
        head, rows = values[0], values[1:]
        k = head.index("Project Key")
        n = len(REGISTRY_HEADERS)
        return {r[k]: (r[0], r[n + 1], r[n + 2]) for r in rows}

    def test_analyst_columns_follow_their_project_when_rows_are_reordered_dropped_and_added(self):
        env, notes = self._analyst_env()
        env.mgr.write_registry([dubai(3), dubai(4), dubai(1)])  # reordered; 2 removed; 4 new
        got = self._extras_by_key(env)
        assert got["dld:dubai:100001"] == notes["dld:dubai:100001"]
        assert got["dld:dubai:100003"] == notes["dld:dubai:100003"]
        assert got["dld:dubai:100004"] == ("", "", "")
        assert list(got) == ["dld:dubai:100003", "dld:dubai:100004", "dld:dubai:100001"]

    def test_extras_of_a_project_that_left_the_registry_are_reported_not_silently_lost(self, caplog):
        env, _ = self._analyst_env()
        env.mgr.write_registry([dubai(1), dubai(3)])
        assert env.mgr.last_dropped_extras == ["dld:dubai:100002"]  # the key is kept for the private run log ...
        assert "1 row(s)" in caplog.text and "not carried over" in caplog.text
        assert "dld:" not in caplog.text and "100002" not in caplog.text  # ... and kept out of the public one

    def test_extras_are_matched_on_any_identity_key_not_just_the_primary_one(self):
        header = list(REGISTRY_HEADERS) + ["Analyst Note"]
        env = new_env(tabs={TAB_REGISTRY: [header]})
        env.mgr.write_registry([seed_row("Fake Tower", "Fake Developer")])  # keyed by name
        old_key = env.records(TAB_REGISTRY)[0]["Project Key"]
        assert old_key.startswith("name:dubai:")
        env.tab(TAB_REGISTRY).update([["phase 2 checked"]], range_name=gspread.utils.rowcol_to_a1(2, len(REGISTRY_HEADERS) + 1))
        # the same project later gets a registry number: its primary key changes, its name key does not
        env.mgr.write_registry([dubai(7, project_name_en="Fake Tower", developer="Fake Developer")])
        row = env.records(TAB_REGISTRY)[0]
        assert row["Project Key"] == "dld:dubai:100007"
        assert row["Analyst Note"] == "phase 2 checked"

    def test_extras_are_matched_by_the_stored_key_even_if_the_analyst_edited_the_name(self):
        header = list(REGISTRY_HEADERS) + ["Analyst Note"]
        env = new_env(tabs={TAB_REGISTRY: [header]})
        env.mgr.write_registry([seed_row("Fake Tower", "Fake Developer")])
        ws = env.tab(TAB_REGISTRY)
        name_col = ws.dump()[0].index("Project Name (EN)") + 1
        ws.update([["Fake Tower Renamed By Analyst"]], range_name=gspread.utils.rowcol_to_a1(2, name_col))
        ws.update([["keep this"]], range_name=gspread.utils.rowcol_to_a1(2, len(REGISTRY_HEADERS) + 1))
        env.mgr.write_registry([seed_row("Fake Tower", "Fake Developer")])
        assert env.records(TAB_REGISTRY)[0]["Analyst Note"] == "keep this"

    def test_one_old_row_never_donates_its_extras_to_two_new_rows(self):
        header = list(REGISTRY_HEADERS) + ["Analyst Note"]
        env = new_env(tabs={TAB_REGISTRY: [header]})
        env.mgr.write_registry([dubai(1)])
        env.tab(TAB_REGISTRY).update([["only once"]], range_name=gspread.utils.rowcol_to_a1(2, len(REGISTRY_HEADERS) + 1))
        env.mgr.write_registry([dubai(1), dubai(1, status="Active")])
        assert [r["Analyst Note"] for r in env.records(TAB_REGISTRY)] == ["only once", ""]

    def test_a_sheet_with_no_extra_columns_pays_for_no_extra_work(self):
        env = new_env()
        env.mgr.write_registry([dubai(1)])
        env.mgr.write_registry([dubai(1)])
        assert env.mgr.last_dropped_extras == []


# =================================================================================
# Retry, backoff, quota
# =================================================================================
class TestRetry:
    def test_a_429_is_retried_with_exponential_backoff_then_succeeds(self):
        env = new_env()
        env.client.reset_calls()
        env.client.inject("get_all_values", 429, times=3, tab=TAB_REGISTRY)
        assert env.mgr.read_registry() == ([], [])
        assert env.sleeps == [2.0, 4.0, 8.0]
        assert len(env.client.calls_to("get_all_values", TAB_REGISTRY)) == 4

    def test_it_gives_up_loudly_after_the_attempt_limit(self):
        env = new_env()
        env.client.reset_calls()
        env.client.inject("get_all_values", 429, times=None, tab=TAB_REGISTRY)
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        assert "gave up after 6 attempts" in str(caught.value) and caught.value.status == 429
        assert env.sleeps == [2.0, 4.0, 8.0, 16.0, 32.0]
        assert len(env.client.calls_to("get_all_values", TAB_REGISTRY)) == gm.MAX_ATTEMPTS == 6

    def test_the_backoff_is_capped(self, monkeypatch):
        monkeypatch.setattr(gm, "MAX_ATTEMPTS", 10)
        env = new_env()
        env.client.inject("get_all_values", 503, times=None, tab=TAB_REGISTRY)
        with pytest.raises(SheetError):
            env.mgr.read_registry()
        assert max(env.sleeps) == gm.BACKOFF_CAP_SECONDS == 64.0
        assert env.sleeps[:6] == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0]

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 408])
    def test_server_errors_on_reads_are_retried(self, status):
        env = new_env()
        env.client.inject("get_all_values", status, times=1, tab=TAB_REGISTRY)
        env.mgr.read_registry()
        assert env.sleeps == [2.0]

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
    def test_client_errors_are_not_retried(self, status):
        env = new_env()
        env.client.reset_calls()
        env.client.inject("get_all_values", status, times=None, tab=TAB_REGISTRY)
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        assert env.sleeps == [] and caught.value.status == status
        assert len(env.client.calls_to("get_all_values", TAB_REGISTRY)) == 1

    def test_drive_style_403_rate_limit_is_retried(self):
        env = new_env()
        env.client.inject("get_all_values", 403, times=1, tab=TAB_REGISTRY, reason="userRateLimitExceeded")
        env.mgr.read_registry()
        assert env.sleeps == [2.0]

    def test_a_timeout_on_a_read_is_retried(self):
        env = new_env()
        env.client.inject("get_all_values", exception=requests.exceptions.ReadTimeout("slow"), times=2, tab=TAB_REGISTRY)
        env.mgr.read_registry()
        assert env.sleeps == [2.0, 4.0]

    def test_a_connection_error_that_never_clears_gives_up_with_its_type(self):
        env = new_env()
        env.client.inject("get_all_values", exception=requests.exceptions.ConnectionError("down"), times=None, tab=TAB_REGISTRY)
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        assert "gave up" in str(caught.value) and "ConnectionError" in str(caught.value)

    def test_a_malformed_ok_response_is_retried(self):
        env = new_env()
        env.client.inject("get_all_values", exception=requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0), times=1, tab=TAB_REGISTRY)
        env.mgr.read_registry()
        assert env.sleeps == [2.0]

    def test_an_error_body_that_is_not_json_still_retries_on_its_status(self):
        env = new_env()
        env.client.inject("get_all_values", 502, garbled=True, times=1, tab=TAB_REGISTRY)
        env.mgr.read_registry()
        assert env.sleeps == [2.0]

    def test_a_credentials_failure_is_not_retried(self):
        env = new_env()
        env.client.inject("get_all_values", exception=google_exceptions.RefreshError("invalid_grant"), times=None, tab=TAB_REGISTRY)
        with pytest.raises(SheetError) as caught:
            env.mgr.read_registry()
        assert env.sleeps == [] and "credentials" in str(caught.value)

    def test_unexpected_exceptions_are_not_swallowed_or_retried(self):
        env = new_env()
        env.client.inject("get_all_values", exception=ZeroDivisionError("a bug"), times=None, tab=TAB_REGISTRY)
        with pytest.raises(ZeroDivisionError):
            env.mgr.read_registry()
        assert env.sleeps == []

    def test_nothing_sleeps_when_nothing_fails(self):
        env = new_env()
        env.mgr.write_registry([dubai(1)])
        env.mgr.read_registry()
        assert env.sleeps == []

    # -- writes that must not be repeated blindly -------------------------------------
    def test_a_429_on_append_is_retried_and_the_row_lands_once(self):
        env = new_env()
        env.client.inject("append_rows", 429, times=2)
        assert env.mgr.add_candidates([candidate("Fake Launch")]) == 1
        assert len(env.records(TAB_CANDIDATES)) == 1

    @pytest.mark.parametrize("applied", [True, False])
    def test_a_5xx_on_append_is_not_repeated_so_rows_are_never_duplicated(self, applied):
        env = new_env()
        env.client.inject("append_rows", 500, times=None, applied=applied)
        with pytest.raises(SheetError) as caught:
            env.mgr.add_candidates([candidate("Fake Launch")])
        assert "may or may not" in str(caught.value)
        assert len(env.client.calls_to("append_rows")) == 1
        assert len(env.records(TAB_CANDIDATES)) == (1 if applied else 0)

    def test_a_timeout_on_append_is_not_repeated(self):
        env = new_env()
        env.client.inject("append_rows", exception=requests.exceptions.ReadTimeout("slow"), applied=True, times=None)
        with pytest.raises(SheetError):
            env.mgr.append_run_log(run_entry())
        assert len(env.client.calls_to("append_rows")) == 1
        assert len(env.records(TAB_RUN_LOGS)) == 1

    def test_a_429_on_delete_is_retried_but_a_5xx_is_not(self):
        env = new_env()
        env.mgr.write_registry([dubai(i) for i in range(4)])
        env.client.inject("delete_rows", 429, times=1)
        env.mgr.delete_rows(TAB_REGISTRY, [3])
        assert len(keys_on_sheet(env)) == 3
        env.client.inject("delete_rows", 503, times=None)
        with pytest.raises(SheetError):
            env.mgr.delete_rows(TAB_REGISTRY, [2])
        assert len(env.client.calls_to("delete_rows")) == 3  # 429 + success + one 503
        assert len(keys_on_sheet(env)) == 3

    def test_a_5xx_while_staging_is_retried_because_the_write_is_idempotent(self):
        env = new_env()
        env.client.inject("update", 503, times=1, applied=True)
        env.mgr.write_registry([dubai(1), dubai(2)])
        assert keys_on_sheet(env) == ["dld:dubai:100001", "dld:dubai:100002"]

    def test_creating_a_spreadsheet_is_not_repeated_after_an_unknown_outcome(self):
        env = new_env(open_it=False)
        env.client.inject("create", 500, times=None, applied=True)
        with pytest.raises(SheetError):
            env.mgr.open()
        assert len(env.client.calls_to("create")) == 1
        assert len(env.client.spreadsheets) == 1  # the one the lost reply created; not two

    def test_a_lost_create_reply_tells_the_operator_how_to_recover(self):
        # Reviewed: after a 5xx whose create DID happen, the next run finds the sheet by name, treats it as
        # existing and never shares it. The error is the only moment to say so.
        env = new_env(open_it=False, share_with_emails=("fake.person@example.invalid",))
        env.client.inject("create", 502, times=1, applied=True)
        with pytest.raises(SheetError) as caught:
            env.mgr.open()
        text = str(caught.value)
        # Changed on the public-log audit: the hint used to quote the spreadsheet's name. The name is an
        # operator-chosen value that reaches the public log, so it points at the GOOGLE_SHEET_NAME setting instead.
        assert "GOOGLE_SHEET_NAME" in text and "SPREADSHEET_ID" in text and "share" in text.lower()
        assert env.cfg.google_sheet_name not in text and DEFAULT_SA_EMAIL not in text
        assert caught.value.status == 502  # the HTTP status still reaches the caller; the hint does not hide it

    def test_a_definite_refusal_to_create_keeps_its_status_too(self):
        env = new_env(open_it=False)
        env.client.inject("create", 403, times=1)
        with pytest.raises(SheetError) as caught:
            env.mgr.open()
        assert caught.value.status == 403

    def test_adding_a_tab_is_not_repeated_after_an_unknown_outcome(self):
        env = new_env(tabs={TAB_REGISTRY: [list(REGISTRY_HEADERS)]}, open_it=False)
        env.client.inject("add_worksheet", 500, times=None, applied=True)
        with pytest.raises(SheetError):
            env.mgr.open()
        assert len(env.client.calls_to("add_worksheet")) == 1


# =================================================================================
# Run log
# =================================================================================
class TestRunLog:
    def test_a_row_has_scalar_columns_and_one_compact_json_column(self):
        env = new_env()
        env.mgr.append_run_log(run_entry(
            sources={"dld_projects": SourceRunInfo("ok", 120), "srerd_projects": SourceRunInfo("failed", 0, "HTTP 503 after 3 tries")},
            geocode={"attempted": 3, "cached": 10, "backend": "nominatim"}, exit_code=2, registry_rows=777, map_pins=55, git_sha="deadbee",
        ))
        (row,) = env.records(TAB_RUN_LOGS)
        assert row["Exit Code"] == "2" and row["Registry Rows"] == "777" and row["Map Pins"] == "55" and row["Git SHA"] == "deadbee"
        assert json.loads(row["Sources"]) == {"dld_projects": {"rows": 120, "status": "ok"},
                                              "srerd_projects": {"note": "HTTP 503 after 3 tries", "rows": 0, "status": "failed"}}
        assert json.loads(row["Geocode"]) == {"attempted": 3, "backend": "nominatim", "cached": 10}
        assert ", " not in row["Sources"] and ": " not in row["Sources"]  # compact
        assert row["Started At"] == "2026-10-25T03:00:00Z"
        assert row["Started (Dubai time)"] == "2026-10-25 07:00 GST"

    def test_rows_are_appended_below_the_previous_ones(self):
        env = new_env()
        for i in range(3):
            env.mgr.append_run_log(run_entry(started=f"2026-10-2{i}T03:00:00Z", registry_rows=i))
        assert [r["Registry Rows"] for r in env.records(TAB_RUN_LOGS)] == ["0", "1", "2"]

    def test_unicode_and_long_notes_are_clipped_not_rejected(self):
        env = new_env()
        env.mgr.append_run_log(run_entry(notes="ملاحظة " * 2000, sources={"x": SourceRunInfo("ok", 1, "é" * 5000)}))
        (row,) = env.records(TAB_RUN_LOGS)
        assert len(row["Notes"]) <= gm.NOTES_LIMIT and "ملاحظة" in row["Notes"]
        assert len(json.loads(row["Sources"])["x"]["note"]) <= gm.SOURCE_NOTE_LIMIT

    def test_non_json_geocode_values_are_made_safe(self):
        env = new_env()
        env.mgr.append_run_log(run_entry(geocode={"ratio": float("nan"), "tags": {"a"}, "none": None, 7: "seven"}))  # type: ignore[arg-type]
        data = json.loads(env.records(TAB_RUN_LOGS)[0]["Geocode"])
        assert data["ratio"] == "nan" and data["none"] is None and data["7"] == "seven" and "a" in data["tags"]

    def test_the_dubai_time_column_leaves_unparseable_text_alone(self):
        env = new_env()
        env.mgr.append_run_log(run_entry(started="yesterday"))
        assert env.records(TAB_RUN_LOGS)[0]["Started (Dubai time)"] == "yesterday"

    @pytest.mark.parametrize(
        "field, value",
        [("exit_code", "0"), ("exit_code", True), ("exit_code", None), ("started_at", None), ("finished_at", 5),
         ("registry_rows", -1), ("map_pins", "3"), ("sources", None), ("geocode", [1, 2]), ("sources", {"a": "ok"})],
    )
    def test_wrong_types_are_refused_and_nothing_is_appended(self, field, value):
        env = new_env()
        with pytest.raises(SheetError):
            env.mgr.append_run_log(run_entry(**{field: value}))
        assert env.records(TAB_RUN_LOGS) == []

    def test_not_a_run_log_entry_is_refused(self):
        with pytest.raises(SheetError):
            new_env().mgr.append_run_log({"started_at": "x"})  # type: ignore[arg-type]

    def test_source_run_info_validates_itself(self):
        for bad in ("ok-ish", "", None, "OK"):
            with pytest.raises(ValueError):
                SourceRunInfo(bad)  # type: ignore[arg-type]
        for rows in (-1, "3", 1.5, True, None):
            with pytest.raises(ValueError):
                SourceRunInfo("ok", rows)  # type: ignore[arg-type]
        assert SourceRunInfo("disabled") == SourceRunInfo("disabled", 0, "")

    def test_a_run_log_missing_a_column_is_refused_with_the_column_named(self):
        env = new_env()
        env.tab(TAB_RUN_LOGS).update([[""]], range_name="D1")  # blank out "Exit Code"
        with pytest.raises(SheetError) as caught:
            env.mgr.append_run_log(run_entry())
        assert "Exit Code" in str(caught.value)

    def test_the_log_is_written_by_header_name_so_a_reordered_log_still_works(self):
        header = list(reversed(RUN_LOG_HEADERS))
        env = new_env(tabs={TAB_RUN_LOGS: [header]})
        env.mgr.append_run_log(run_entry(registry_rows=42))
        assert env.records(TAB_RUN_LOGS)[0]["Registry Rows"] == "42"


class TestLastSourceCounts:
    def _log(self, env: Env, started: str, **sources: SourceRunInfo) -> None:
        env.mgr.append_run_log(run_entry(started=started, sources=dict(sources)))

    def test_an_empty_log_gives_an_empty_baseline(self):
        assert new_env().mgr.last_source_counts() == {}

    def test_the_most_recent_ok_run_wins(self):
        env = new_env()
        self._log(env, "2026-10-20T03:00:00Z", dld_projects=SourceRunInfo("ok", 100))
        self._log(env, "2026-10-21T03:00:00Z", dld_projects=SourceRunInfo("ok", 130))
        assert env.mgr.last_source_counts() == {"dld_projects": 130}

    def test_a_failed_run_does_not_lower_the_baseline(self):
        env = new_env()
        self._log(env, "2026-10-20T03:00:00Z", dld_projects=SourceRunInfo("ok", 100), seed_csv=SourceRunInfo("ok", 9))
        self._log(env, "2026-10-21T03:00:00Z", dld_projects=SourceRunInfo("failed", 0, "boom"), seed_csv=SourceRunInfo("ok", 10))
        assert env.mgr.last_source_counts() == {"dld_projects": 100, "seed_csv": 10}

    def test_a_source_that_only_ever_failed_has_no_baseline(self):
        env = new_env()
        self._log(env, "2026-10-20T03:00:00Z", srerd_projects=SourceRunInfo("failed", 0))
        assert env.mgr.last_source_counts() == {}

    @pytest.mark.parametrize("status", ["skipped", "disabled", "failed"])
    def test_other_statuses_never_count_even_with_a_row_count(self, status):
        env = new_env()
        self._log(env, "2026-10-20T03:00:00Z", seed_csv=SourceRunInfo("ok", 50))
        self._log(env, "2026-10-21T03:00:00Z", seed_csv=SourceRunInfo(status, 999))
        assert env.mgr.last_source_counts() == {"seed_csv": 50}

    def test_recency_is_by_started_at_not_by_sheet_order(self):
        env = new_env()
        self._log(env, "2026-10-22T03:00:00Z", seed_csv=SourceRunInfo("ok", 22))
        self._log(env, "2026-10-21T03:00:00Z", seed_csv=SourceRunInfo("ok", 21))  # appended later, but older
        assert env.mgr.last_source_counts() == {"seed_csv": 22}

    def test_equal_timestamps_fall_back_to_the_later_row(self):
        env = new_env()
        self._log(env, "2026-10-21T03:00:00Z", seed_csv=SourceRunInfo("ok", 1))
        self._log(env, "2026-10-21T03:00:00Z", seed_csv=SourceRunInfo("ok", 2))
        assert env.mgr.last_source_counts() == {"seed_csv": 2}

    def test_a_legitimate_zero_from_an_ok_run_is_the_baseline(self):
        env = new_env()
        self._log(env, "2026-10-20T03:00:00Z", seed_csv=SourceRunInfo("ok", 50))
        self._log(env, "2026-10-21T03:00:00Z", seed_csv=SourceRunInfo("ok", 0))
        assert env.mgr.last_source_counts() == {"seed_csv": 0}

    def test_sources_are_independent(self):
        env = new_env()
        self._log(env, "2026-10-20T03:00:00Z", dld_projects=SourceRunInfo("ok", 1), srerd_projects=SourceRunInfo("ok", 2))
        self._log(env, "2026-10-21T03:00:00Z", dld_projects=SourceRunInfo("ok", 10))
        assert env.mgr.last_source_counts() == {"dld_projects": 10, "srerd_projects": 2}

    def test_unreadable_log_cells_are_skipped_with_a_warning_not_a_crash(self, caplog):
        env = new_env()
        self._log(env, "2026-10-20T03:00:00Z", seed_csv=SourceRunInfo("ok", 5))
        ws = env.tab(TAB_RUN_LOGS)
        h = ws.dump()[0]
        src = h.index("Sources") + 1
        bad_cells = [
            '{"seed_csv":{"status":"ok","ro',  # a half-written cell
            "not json at all",
            "[1,2,3]",
            '{"seed_csv":"ok"}',
            '{"seed_csv":{"status":"ok","rows":"12"}}',
            '{"seed_csv":{"status":"ok","rows":-4}}',
            '{"seed_csv":{"status":"great","rows":4}}',
            '{"seed_csv":{"status":"ok","rows":true}}',
            "",
        ]
        for i, cell in enumerate(bad_cells):
            row = 3 + i
            ws.update([[f"2026-10-2{1 + i % 8}T03:00:00Z"]], range_name=f"A{row}")
            if cell:
                ws.update([[cell]], range_name=gspread.utils.rowcol_to_a1(row, src))
        assert env.mgr.last_source_counts() == {"seed_csv": 5}
        assert "Sources cell is not valid JSON" in caplog.text

    def test_an_unreadable_entry_is_reported_by_position_never_by_the_source_name(self, caplog):
        env = new_env()
        self._log(env, "2026-10-20T03:00:00Z", gemini_news=SourceRunInfo("ok", 5))
        ws = env.tab(TAB_RUN_LOGS)
        src = ws.dump()[0].index("Sources") + 1
        cell = '{"gemini_news":{"status":"ok","rows":7},"MARKER-Alpha Residences":{"status":"great","rows":4},"MARKER-Beta":"ok"}'
        ws.update([["2026-10-21T03:00:00Z"]], range_name="A3")
        ws.update([[cell]], range_name=gspread.utils.rowcol_to_a1(3, src))
        with caplog.at_level(logging.DEBUG):
            assert env.mgr.last_source_counts() == {"gemini_news": 7}
        assert "row 3: entry 2 of the Sources cell is unreadable; ignored" in caplog.text
        assert "row 3: entry 3 of the Sources cell is unreadable; ignored" in caplog.text
        assert "MARKER" not in caplog.text and "Alpha" not in caplog.text and "Beta" not in caplog.text

    @pytest.mark.parametrize("name", SOURCE_NAMES)
    def test_every_adapter_name_is_kept(self, name):
        env = new_env()
        self._log(env, "2026-10-20T03:00:00Z", **{name: SourceRunInfo("ok", 12)})
        assert env.mgr.last_source_counts() == {name: 12}

    def test_a_name_that_is_not_a_known_source_is_counted_never_kept_or_logged(self, caplog):
        env = new_env()
        self._log(env, "2026-10-20T03:00:00Z", dld_projects=SourceRunInfo("ok", 5))
        ws = env.tab(TAB_RUN_LOGS)
        src = ws.dump()[0].index("Sources") + 1
        cell = (
            '{"dld_projects":{"status":"ok","rows":7},'
            '"ZZNAMEZZ-Alpha Residences":{"status":"ok","rows":4},'
            '"ZZNAMEZZ-Beta":{"status":"failed","rows":0},'
            '"seed_csv":{"status":"ok","rows":2}}'
        )
        ws.update([["2026-10-21T03:00:00Z"]], range_name="A3")
        ws.update([[cell]], range_name=gspread.utils.rowcol_to_a1(3, src))
        with caplog.at_level(logging.DEBUG):
            counts = env.mgr.last_source_counts()
        assert counts == {"dld_projects": 7, "seed_csv": 2}  # the marker names are in no state, however well-formed
        assert "2 readable entry(ies) of the Sources cells have a name this code does not know" in caplog.text
        assert "(names not shown)" in caplog.text
        assert "ZZNAMEZZ" not in caplog.text and "Alpha" not in caplog.text and "Beta" not in caplog.text

    @pytest.mark.parametrize("name", ["DLD_PROJECTS", "dld_projects ", " dld_projects", "dld_projects2", "dld-projects", ""])
    def test_a_name_must_match_an_adapter_name_exactly(self, name):
        env = new_env()
        self._log(env, "2026-10-20T03:00:00Z", seed_csv=SourceRunInfo("ok", 3))
        ws = env.tab(TAB_RUN_LOGS)
        src = ws.dump()[0].index("Sources") + 1
        cell = json.dumps({"seed_csv": {"status": "ok", "rows": 3}, name: {"status": "ok", "rows": 9}})
        ws.update([["2026-10-21T03:00:00Z"]], range_name="A3")
        ws.update([[cell]], range_name=gspread.utils.rowcol_to_a1(3, src))
        assert env.mgr.last_source_counts() == {"seed_csv": 3}

    def test_a_log_tab_without_the_needed_columns_is_refused(self):
        env = new_env()
        env.tab(TAB_RUN_LOGS).update([[""]], range_name="E1")  # blank out "Sources"
        with pytest.raises(SheetError) as caught:
            env.mgr.last_source_counts()
        assert "Sources" in str(caught.value)


# =================================================================================
# Listing seed and candidates
# =================================================================================
def listing_seed(key: str, url: str = "https://example.invalid/source", price: str = "") -> list[str]:
    row = [""] * len(LISTING_HEADERS)
    row[LISTING_HEADERS.index("Project Key")] = key
    row[LISTING_HEADERS.index("Source URL")] = url
    row[LISTING_HEADERS.index("Price From (AED)")] = price
    row[LISTING_HEADERS.index("Source Licence")] = "manual"
    return row


class TestListingSeed:
    def test_only_unseen_keys_are_appended_and_a_rerun_adds_nothing(self):
        env = new_env()
        seed = [listing_seed("dld:dubai:1"), listing_seed("srerd:sharjah:S1")]
        assert env.mgr.apply_listing_seed(seed) == 2
        assert env.mgr.apply_listing_seed(seed) == 0
        assert [r["Project Key"] for r in env.records(TAB_LISTINGS)] == ["dld:dubai:1", "srerd:sharjah:S1"]

    def test_an_analysts_row_outranks_the_seed_and_is_never_edited(self):
        h = list(LISTING_HEADERS)
        mine = listing_seed("dld:dubai:1", price="1,000,000")
        env = new_env(tabs={TAB_LISTINGS: [h, mine]})
        assert env.mgr.apply_listing_seed([listing_seed("dld:dubai:1", price="9"), listing_seed("dld:dubai:2")]) == 1
        assert env.records(TAB_LISTINGS)[0]["Price From (AED)"] == "1,000,000"

    def test_cells_are_placed_by_header_name_in_a_reordered_tab(self):
        header = ["Source URL", "Notes", *reversed([h for h in LISTING_HEADERS if h != "Source URL"])]
        env = new_env(tabs={TAB_LISTINGS: [header]})
        env.mgr.apply_listing_seed([listing_seed("dld:dubai:00123", url="https://example.invalid/x", price="1,500,000")])
        (row,) = env.records(TAB_LISTINGS)
        assert row["Project Key"] == "dld:dubai:00123" and row["Source URL"] == "https://example.invalid/x"
        assert row["Price From (AED)"] == "1,500,000" and row["Notes"] == ""

    def test_invalid_seed_rows_are_dropped_and_zero_is_returned_for_nothing(self):
        env = new_env()
        assert env.mgr.apply_listing_seed([]) == 0
        assert env.mgr.apply_listing_seed([listing_seed("not-a-key"), listing_seed("dld:dubai:1", url="")]) == 0
        assert env.records(TAB_LISTINGS) == []

    def test_the_latest_duplicate_inside_the_seed_wins(self):
        env = new_env()
        env.mgr.apply_listing_seed([listing_seed("dld:dubai:1", price="1"), listing_seed("dld:dubai:1", price="2")])
        assert [r["Price From (AED)"] for r in env.records(TAB_LISTINGS)] == ["2"]

    def test_leading_zeros_and_unicode_are_kept(self):
        env = new_env()
        row = listing_seed("dld:dubai:00123")
        row[LISTING_HEADERS.index("Payment Plan")] = "٦٠/٤٠ — 60/40"
        env.mgr.apply_listing_seed([row])
        stored = env.records(TAB_LISTINGS)[0]
        assert stored["Project Key"] == "dld:dubai:00123" and stored["Payment Plan"] == "٦٠/٤٠ — 60/40"

    def test_a_listing_tab_with_a_hand_added_column_still_takes_seed_rows(self):
        header = [*LISTING_HEADERS, "Analyst Note"]
        env = new_env(tabs={TAB_LISTINGS: [header]})
        env.mgr.apply_listing_seed([listing_seed("dld:dubai:1")])
        assert env.records(TAB_LISTINGS)[0]["Analyst Note"] == ""

    def test_large_seed_is_appended_in_batches(self, monkeypatch):
        monkeypatch.setattr(gm, "APPEND_CHUNK_ROWS", 4)
        env = new_env()
        assert env.mgr.apply_listing_seed([listing_seed(f"dld:dubai:{i}") for i in range(1, 11)]) == 10
        assert [c.detail["rows"] for c in env.client.calls_to("append_rows")] == [4, 4, 2]


class TestCandidates:
    def test_new_candidates_are_appended_and_existing_ones_are_never_edited(self):
        env = new_env()
        assert env.mgr.add_candidates([candidate("Fake Launch One"), candidate("Fake Launch Two")]) == 2
        ws = env.tab(TAB_CANDIDATES)
        status_col = ws.dump()[0].index("Status") + 1
        ws.update([["confirmed"]], range_name=gspread.utils.rowcol_to_a1(2, status_col))  # a human decides
        assert env.mgr.add_candidates([candidate("Fake Launch One", claim="a different claim"), candidate("Fake Launch Three")]) == 1
        recs = env.records(TAB_CANDIDATES)
        assert [r["Project Name"] for r in recs] == ["Fake Launch One", "Fake Launch Two", "Fake Launch Three"]
        assert recs[0]["Status"] == "confirmed" and recs[0]["Claim"] == ""

    def test_the_same_candidate_twice_in_one_batch_is_added_once(self):
        env = new_env()
        assert env.mgr.add_candidates([candidate("Fake Launch"), candidate("fake  launch")]) == 1

    def test_keys_compare_case_insensitively(self):
        env = new_env()
        env.mgr.add_candidates([candidate("Fake Launch")])
        key = env.records(TAB_CANDIDATES)[0]["Candidate Key"]
        env.tab(TAB_CANDIDATES).update([[key.upper()]], range_name="A2")
        assert env.mgr.add_candidates([candidate("Fake Launch")]) == 0

    def test_a_hand_typed_row_without_a_key_cell_still_blocks_a_duplicate(self):
        h = list(CANDIDATE_HEADERS)
        typed = ["", "Dubai", "Fake Launch", "Fake Developer", "", "", "https://example.invalid/a", "", "new"]
        env = new_env(tabs={TAB_CANDIDATES: [h, typed]})
        assert env.mgr.add_candidates([candidate("Fake Launch")]) == 0

    def test_a_row_with_a_key_but_otherwise_invalid_still_counts_as_seen(self):
        h = list(CANDIDATE_HEADERS)
        stored = ["CAND:DUBAI:FAKE-DEVELOPER:FAKE-LAUNCH", "Dubai", "", "", "", "", "", "", ""]  # upper case, no usable data
        env = new_env(tabs={TAB_CANDIDATES: [h, stored]})
        assert candidate("Fake Launch").candidate_key.casefold() == stored[0].casefold()
        assert env.mgr.add_candidates([candidate("Fake Launch")]) == 0

    def test_empty_input_is_zero_and_writes_nothing(self):
        env = new_env()
        env.client.reset_calls()
        assert env.mgr.add_candidates([]) == 0
        assert env.client.calls == []

    def test_wrong_types_are_refused_before_any_append(self):
        env = new_env()
        with pytest.raises(SheetError):
            env.mgr.add_candidates([candidate("Ok"), "not a candidate"])  # type: ignore[list-item]
        assert env.records(TAB_CANDIDATES) == []

    def test_candidates_are_written_by_header_name(self):
        header = list(reversed(CANDIDATE_HEADERS))
        env = new_env(tabs={TAB_CANDIDATES: [header]})
        env.mgr.add_candidates([candidate("Fake Launch", area="دبي")])
        r = env.records(TAB_CANDIDATES)[0]
        assert r["Project Name"] == "Fake Launch" and r["Area"] == "دبي" and r["Status"] == "new"


# =================================================================================
# Map_Pins
# =================================================================================
class TestMapPins:
    def _pinned(self, n: int, precision: str, lat: float = 25.2, lon: float = 55.27, **kw: Any) -> RegistryRow:
        return dubai(n, latitude=lat, longitude=lon, location_precision=precision, **kw)

    def test_only_rows_with_real_coordinates_are_pinned_best_precision_first(self):
        env = new_env()
        rows = [
            self._pinned(1, "area", project_name_en="Z Area"),
            self._pinned(2, "", project_name_en="Y Unknown"),
            dubai(3, project_name_en="No Coordinates"),
            self._pinned(4, "exact", project_name_en="B Exact"),
            self._pinned(5, "community", project_name_en="C Community"),
            self._pinned(6, "exact", project_name_en="A Exact"),
        ]
        assert env.mgr.write_map_pins(rows) == 5
        recs = env.records(TAB_PINS)
        assert [r["Project Name"] for r in recs] == ["A Exact", "B Exact", "C Community", "Z Area", "Y Unknown"]
        assert [r["Precision"] for r in recs] == ["exact", "exact", "community", "area", "unknown"]

    def test_unknown_precision_is_labelled_unknown_never_exact(self):
        env = new_env()
        env.mgr.write_map_pins([self._pinned(1, "")])
        assert env.records(TAB_PINS)[0]["Precision"] == "unknown"

    def test_pins_outside_the_uae_are_left_out_with_a_warning(self, caplog):
        env = new_env()
        rows = [self._pinned(1, "exact"), self._pinned(2, "exact", lat=0.0, lon=0.0),
                self._pinned(3, "exact", lat=55.27, lon=25.2),  # swapped
                self._pinned(4, "exact", lat=51.5, lon=-0.12)]
        assert env.mgr.write_map_pins(rows) == 1
        assert "outside the UAE" in caplog.text

    def test_a_pin_carries_a_map_link_and_no_permit_or_contact_columns(self):
        env = new_env()
        env.mgr.write_map_pins([self._pinned(1, "exact", permit_no="PERMIT-1")])
        (rec,) = env.records(TAB_PINS)
        assert rec["Map Link"].startswith("https://www.google.com/maps/search/?api=1&query=25.2,55.27")
        assert rec["Latitude"] == "25.2" and rec["Longitude"] == "55.27"
        assert "PERMIT-1" not in json.dumps(env.tab(TAB_PINS).dump())

    def test_the_tab_is_replaced_not_appended_to(self):
        env = new_env()
        env.mgr.write_map_pins([self._pinned(i, "exact") for i in range(5)])
        env.mgr.write_map_pins([self._pinned(9, "exact")])
        assert [r["Project Key"] for r in env.records(TAB_PINS)] == ["dld:dubai:100009"]

    def test_clearing_a_tab_whose_grid_is_exactly_full_does_not_hit_the_delete_everything_error(self):
        """Google refuses to delete every non-frozen row; the grid must be grown by a row first."""
        env = new_env()
        env.mgr.write_map_pins([self._pinned(1, "exact"), self._pinned(2, "exact")])
        env.tab(TAB_PINS).resize(rows=3)  # header + exactly the two pins
        assert env.mgr.write_map_pins([]) == 0
        assert env.tab(TAB_PINS).dump() == [list(MAP_PIN_HEADERS)]

    def test_zero_pins_clears_the_tab_with_a_warning(self, caplog):
        env = new_env()
        env.mgr.write_map_pins([self._pinned(i, "exact") for i in range(3)])
        assert env.mgr.write_map_pins([dubai(1)]) == 0
        assert env.tab(TAB_PINS).dump() == [list(MAP_PIN_HEADERS)]
        assert "clearing" in caplog.text

    def test_map_pins_stays_the_first_tab(self):
        env = new_env()
        env.mgr.write_map_pins([self._pinned(1, "exact")])
        assert env.sheet.titles()[0] == TAB_PINS

    def test_the_order_is_deterministic_for_identical_precision(self):
        env = new_env()
        rows = [self._pinned(i, "exact", project_name_en="Same Name") for i in (3, 1, 2)]
        env.mgr.write_map_pins(rows)
        first = [r["Project Key"] for r in env.records(TAB_PINS)]
        env.mgr.write_map_pins(list(reversed(rows)))
        assert [r["Project Key"] for r in env.records(TAB_PINS)] == first

    def test_wrong_types_are_refused(self):
        env = new_env()
        with pytest.raises(SheetError):
            env.mgr.write_map_pins(["nope"])  # type: ignore[list-item]

    def test_many_pins_cross_the_initial_grid(self):
        env = new_env()
        n = env.tab(TAB_PINS).row_count + 100
        assert env.mgr.write_map_pins([self._pinned(i, "exact") for i in range(n)]) == n
        assert len(env.records(TAB_PINS)) == n


# =================================================================================
# delete_rows
# =================================================================================
class TestDeleteRows:
    def _env(self, n: int = 8) -> Env:
        env = new_env()
        env.mgr.write_registry([dubai(i) for i in range(n)])
        env.client.reset_calls()
        return env

    def test_deletes_bottom_up_in_contiguous_runs_and_returns_the_count(self):
        env = self._env()
        assert env.mgr.delete_rows(TAB_REGISTRY, [3, 7, 4, 3]) == 3
        deletes = env.client.calls_to("delete_rows")
        assert [(c.detail["start"], c.detail["end"]) for c in deletes] == [(7, 7), (3, 4)]
        # data row i sits on sheet row i + 2, so sheet rows 3, 4 and 7 are rows 1, 2 and 5
        assert keys_on_sheet(env) == [f"dld:dubai:{100000 + i}" for i in (0, 3, 4, 6, 7)]

    def test_every_data_row_can_be_deleted_even_when_the_grid_has_no_spare_rows(self):
        # Reviewed: Sheets answers 400 "can't delete all the non-frozen rows" when the delete would leave
        # none; write_registry already makes room first, this public method did not.
        env = self._env(3)
        env.tab(TAB_REGISTRY).resize(rows=4)  # header + 3 rows fill the grid exactly
        assert env.mgr.delete_rows(TAB_REGISTRY, [2, 3, 4]) == 3
        assert keys_on_sheet(env) == []

    def test_the_header_row_is_refused_and_nothing_at_all_is_deleted(self):
        env = self._env()
        for bad in ([1], [3, 1], [0], [-2]):
            with pytest.raises(SheetError):
                env.mgr.delete_rows(TAB_REGISTRY, bad)
        assert env.client.calls_to("delete_rows") == []
        assert len(keys_on_sheet(env)) == 8

    def test_a_row_beyond_the_data_is_refused_before_anything_is_deleted(self):
        env = self._env()
        with pytest.raises(SheetError) as caught:
            env.mgr.delete_rows(TAB_REGISTRY, [3, 10])
        assert "beyond the last data row" in str(caught.value)
        assert env.client.calls_to("delete_rows") == []

    @pytest.mark.parametrize("bad", [[2.0], ["2"], [None], [True], None, "23", 5])
    def test_wrong_types_are_refused(self, bad):
        env = self._env()
        with pytest.raises(SheetError):
            env.mgr.delete_rows(TAB_REGISTRY, bad)
        assert env.client.calls_to("delete_rows") == []

    def test_an_empty_list_deletes_nothing(self):
        env = self._env()
        assert env.mgr.delete_rows(TAB_REGISTRY, []) == 0
        assert env.client.calls == []

    def test_a_generator_of_numbers_is_accepted(self):
        env = self._env()
        assert env.mgr.delete_rows(TAB_REGISTRY, (n for n in (2, 3))) == 2

    def test_expect_keys_stops_a_delete_against_stale_row_numbers(self):
        env = self._env()
        stale = {3: "dld:dubai:100001"}  # row 3 holds ...100001 now
        assert env.mgr.delete_rows(TAB_REGISTRY, [3], expect_keys={3: "dld:dubai:100001"}) == 1
        with pytest.raises(SheetError) as caught:
            env.mgr.delete_rows(TAB_REGISTRY, [3], expect_keys=stale)  # row 3 now holds ...100002
        assert "no longer holds" in str(caught.value)
        assert len(keys_on_sheet(env)) == 7

    def test_deleting_every_data_row_is_allowed_while_the_header_stays(self):
        env = self._env(3)
        env.tab(TAB_REGISTRY).resize(rows=4)
        env.mgr.delete_rows(TAB_REGISTRY, [2, 3])
        assert len(keys_on_sheet(env)) == 1
        # Changed on review: this used to pin Google's refusal to delete the last non-frozen row of a full
        # grid. delete_rows now makes room first (see test_every_data_row_can_be_deleted_...), so the last
        # row goes too; what must still hold is that a real 400 from Google is surfaced, not swallowed.
        env.client.inject("delete_rows", 400, times=1)
        with pytest.raises(SheetError) as caught:
            env.mgr.delete_rows(TAB_REGISTRY, [2])
        assert caught.value.status == 400
        assert len(keys_on_sheet(env)) == 1
        assert env.mgr.delete_rows(TAB_REGISTRY, [2]) == 1 and keys_on_sheet(env) == []

    def test_works_on_other_tabs(self):
        env = new_env()
        env.mgr.add_candidates([candidate(f"Fake Launch {n}") for n in "ABC"])
        assert env.mgr.delete_rows(TAB_CANDIDATES, [3]) == 1
        assert [r["Project Name"] for r in env.records(TAB_CANDIDATES)] == ["Fake Launch A", "Fake Launch C"]


# =================================================================================
# Static rules from the contract
# =================================================================================
class TestContractRules:
    SOURCE = Path(gm.__file__).read_text(encoding="utf-8")

    def test_no_positional_cell_access_and_no_hardcoded_column_letters(self):
        assert not re.search(r"\brow\[\s*\d", self.SOURCE)
        literals = set(re.findall(r"""["']([A-Z]{1,2}[0-9]+(?::[A-Z]{1,2}[0-9]*)?)["']""", self.SOURCE))
        assert literals <= {"A1"}  # the one anchor handed to append_rows as table_range

    def test_values_are_never_numericised_by_the_module(self):
        assert "USER_ENTERED" not in re.sub(r'"""[\s\S]*?"""|#.*', "", self.SOURCE)
        assert "get_all_records" not in self.SOURCE  # it numericises "00123"
        assert "numericise" not in self.SOURCE.lower().replace("no numericisation", "")

    def test_no_certificate_verification_bypass_and_no_user_agent_spoofing(self):
        assert "verify=False" not in self.SOURCE and "User-Agent" not in self.SOURCE

    def test_module_documents_the_read_registry_problem_contract(self):
        doc = gm.SheetManager.read_registry.__doc__
        assert "registry_problem_count" in doc and "write_registry" in doc

    def test_public_surface_matches_the_design(self):
        for name in ("open", "read_registry", "write_registry", "read_listing_records", "apply_listing_seed",
                     "read_overrides", "read_candidates", "add_candidates", "write_map_pins",
                     "append_run_log", "last_source_counts", "delete_rows"):
            assert callable(getattr(SheetManager, name)), name
        params = inspect.signature(SheetManager.__init__).parameters
        assert list(params)[:2] == ["self", "cfg"] and params["client"].kind is inspect.Parameter.KEYWORD_ONLY
        assert [f for f in RunLogEntry.__dataclass_fields__] == [
            "started_at", "finished_at", "exit_code", "sources", "geocode", "registry_rows", "map_pins", "git_sha", "notes"]
        assert [f for f in SourceRunInfo.__dataclass_fields__] == ["status", "rows", "note"]
