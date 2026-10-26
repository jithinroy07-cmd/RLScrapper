"""
Tests for src/main.py: the daily run, end to end.

The run is exercised over the REAL SheetManager on the in-memory client in
tests/fake_gspread.py (strict about RAW vs USER_ENTERED, grid limits and cell size),
with fake adapters, a fake geocoder session or callable, and a fixed `today`. Nothing
here touches the network or a credential; every key, secret, spreadsheet id and address
is SYNTHETIC and obviously fake, and each is distinct so a leak is attributable.

Groups
    helpers          secret list, scrubber, error text, row checks, merge counts, snapshot
    logging          the secret filter and configure_logging
    clean runs       first run, idempotent second run, merge by name key, never-merge ids
    sources          failed / empty / shrunk / skipped / disabled adapters, REQUIRE_SOURCES
    sheet problems   unreadable rows, the registry write guard, overrides, failed reads
    geocode stage    crash, partial flush, abort, stop, configuration error, flush guard
    persistence      one failed write never costs the others, run log best effort
    dry run          nothing written
    hostile input    formulas, unicode and huge text stay inert
    secrets          nothing secret in any log, run-log cell or snapshot
    main(argv)       wiring with patched collaborators, plus two subprocess smoke runs
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import os
import subprocess
import sys
import warnings
from dataclasses import dataclass
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

import src.main as app
from src import log_policy
from src.config import (
    ROOT_DIR,
    TAB_CANDIDATES,
    TAB_LISTINGS,
    TAB_OVERRIDES,
    TAB_PINS,
    TAB_REGISTRY,
    TAB_RUN_LOGS,
    Config,
    load_config,
)
from src.geocoder import GeocodeStats, Geocoder, GeocoderConfigError, geocode_pending
from src.gsheet_manager import SheetError, SheetManager, SourceRunInfo
from src.listing_data import LISTING_HEADERS, SeedFileError
from src.listing_data import read_seed as real_read_seed
from src.models import LaunchCandidate, RegistryRow
from src.sources.base import SourceError, SourceResult
from tests.fake_gspread import FakeClient

TODAY = date(2026, 10, 25)
LATER = date(2026, 10, 26)

# SYNTHETIC secrets: obviously fake and distinct.
SHEET_ID = "fake-spreadsheet-id-main-0001"
KEY_GEMINI = "fake-key-GEMINI-4821"
KEY_DUBAI = "fake-key-DUBAI-7714"
SECRET_DUBAI = "fake-secret-DUBAI-3390"
KEY_LIQ = "fake-key-LIQ-5502"
PRIVATE_KEY_ID = "fake-private-key-id-8812"
PEM_LINE_ONE = "FAKEPRIVATEKEYBODYLINEONE0123456789abcdefghij"
PEM_LINE_TWO = "FAKEPRIVATEKEYBODYLINETWO9876543210zyxwvutsrqpo"
PRIVATE_KEY = f"-----BEGIN PRIVATE KEY-----\n{PEM_LINE_ONE}\n{PEM_LINE_TWO}\n-----END PRIVATE KEY-----\n"
SA_JSON = json.dumps(
    {
        "type": "service_account",
        "project_id": "fake-project",
        "private_key_id": PRIVATE_KEY_ID,
        "private_key": PRIVATE_KEY,
        "client_email": "fake-sa@fake-project.iam.example.invalid",
        "client_id": "1234567890",
    }
)
CONTACT = "ops@example.invalid"
ALL_SECRETS = (
    SHEET_ID, KEY_GEMINI, KEY_DUBAI, SECRET_DUBAI, KEY_LIQ, PRIVATE_KEY_ID, PEM_LINE_ONE, PEM_LINE_TWO, CONTACT,
)

DOWNTOWN = (25.1972, 55.2744)  # a real point inside Dubai, for the geocoder fake


# ---------------------------------------------------------------------------
# Builders and fakes
# ---------------------------------------------------------------------------
def dld(n: int = 1, *, day: date = TODAY, **extra: Any) -> RegistryRow:
    data: dict[str, Any] = dict(
        emirate="Dubai", registry="DLD", registry_project_no=str(100000 + n), project_name_en=f"Fake Tower {n}",
        developer="Fake Developer", source_dataset="fake-dld", first_seen=day.isoformat(), last_seen=day.isoformat(),
    )
    data.update(extra)
    return RegistryRow(**data)


def srerd(n: int = 1, *, day: date = TODAY, **extra: Any) -> RegistryRow:
    data: dict[str, Any] = dict(
        emirate="Sharjah", registry="SRERD", registry_project_no=f"SH-{n:04d}", project_name_en=f"Fake Court {n}",
        developer="Fake Sharjah Developer", source_dataset="fake-srerd", first_seen=day.isoformat(),
        last_seen=day.isoformat(),
    )
    data.update(extra)
    return RegistryRow(**data)


def seed_row(name: str = "Fake Tower 1", developer: str = "Fake Developer", *, day: date = TODAY, **extra: Any) -> RegistryRow:
    data: dict[str, Any] = dict(
        emirate="Dubai", registry="SEED", project_name_en=name, developer=developer, first_seen=day.isoformat(),
        last_seen=day.isoformat(),
    )
    data.update(extra)
    return RegistryRow(**data)


def candidate(name: str = "Fake Launch One") -> LaunchCandidate:
    return LaunchCandidate(
        emirate="Dubai", project_name=name, developer="Fake Developer", source_url="https://example.invalid/news/1",
        found_at="2026-10-25T03:00:00Z",
    )


class FakeAdapter:
    """A source adapter with scripted behaviour. `rows` and `error` may be changed between runs."""

    def __init__(
        self, name: str, rows: Any = (), *, expects_rows: bool = True, enabled: bool = True, reason: str = "",
        error: Optional[BaseException] = None, candidates: Any = (), notes: Any = (), result: Any = None,
    ):
        self.name = name
        self.rows = list(rows)
        self.expects_rows = expects_rows
        self._enabled = enabled
        self._reason = reason
        self.error = error
        self.candidates = list(candidates)
        self.notes = list(notes)
        self.result = result
        self.fetches = 0

    def enabled(self, cfg: Config) -> bool:
        return self._enabled

    def skip_reason(self, cfg: Config) -> str:
        return "" if self._enabled else self._reason

    def fetch(self) -> Any:
        self.fetches += 1
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return SourceResult(
            source=self.name, rows=list(self.rows), fetched_at="2026-10-25T03:00:00Z", notes=list(self.notes),
            candidates=list(self.candidates),
        )


def feeds(*, dld_rows=None, srerd_rows=None, seed=None, candidates=None) -> list[FakeAdapter]:
    """The adapters a test names; each is only present when given."""
    out: list[FakeAdapter] = []
    if dld_rows is not None:
        out.append(FakeAdapter("dld_projects", dld_rows))
    if srerd_rows is not None:
        out.append(FakeAdapter("srerd_projects", srerd_rows))
    if seed is not None:
        out.append(FakeAdapter("seed_csv", seed, expects_rows=False))
    if candidates is not None:
        out.append(FakeAdapter("gemini_news", [], expects_rows=False, candidates=candidates))
    return out


@dataclass
class Env:
    client: FakeClient
    cfg: Config
    mgr: SheetManager
    snapshot: Path
    tmp: Path

    @property
    def sheet(self) -> Any:
        return self.client.spreadsheets[SHEET_ID]

    def dump(self, tab: str) -> list[list[str]]:
        return self.sheet.tab(tab).dump()

    def records(self, tab: str) -> list[dict[str, str]]:
        values = self.dump(tab)
        if not values:
            return []
        header = values[0]
        return [dict(zip(header, row)) for row in values[1:] if any(row)]

    def registry(self) -> dict[str, dict[str, str]]:
        return {r["Project Key"]: r for r in self.records(TAB_REGISTRY)}

    def snapshot_data(self) -> dict[str, Any]:
        return json.loads(self.snapshot.read_text(encoding="utf-8"))

    def last_log(self) -> dict[str, str]:
        return self.records(TAB_RUN_LOGS)[-1]

    def writes(self, tab: Optional[str] = None) -> list[Any]:
        return [c for c in self.client.calls if c.method in ("update", "append_rows", "delete_rows", "clear") and (tab is None or c.tab == tab)]


def make_env(tmp_path: Path, **overrides: Any) -> Env:
    client = FakeClient()
    client.add_spreadsheet("Fake Tracker For Tests", sheet_id=SHEET_ID)
    settings: dict[str, Any] = dict(
        spreadsheet_id=SHEET_ID, geocode_enabled=False, gemini_api_key=KEY_GEMINI, data_dubai_api_key=KEY_DUBAI,
        data_dubai_api_secret=SECRET_DUBAI, locationiq_api_key=KEY_LIQ, gcp_service_account_key=SA_JSON,
        geocode_contact=CONTACT,
    )
    settings.update(overrides)
    cfg = Config(**settings)
    mgr = SheetManager(cfg, client=client, sleep=lambda seconds: None)
    return Env(client=client, cfg=cfg, mgr=mgr, snapshot=tmp_path / "snapshot.json", tmp=tmp_path)


def go(env: Env, adapters: list, *, today: date = TODAY, **kwargs: Any) -> int:
    kwargs.setdefault("snapshot_path", env.snapshot)
    return app.run(env.cfg, adapters=adapters, sheets=env.mgr, today=today, **kwargs)


def source_info(log: dict[str, str]) -> dict[str, Any]:
    return json.loads(log["Sources"])


def append_registry_cells(env: Env, **cells: str) -> None:
    """Add a row by header name straight to the Registry tab, the way a person typing would."""
    ws = env.sheet.tab(TAB_REGISTRY)
    header = ws.dump()[0]
    ws.put([[cells.get(h, "") for h in header]], start_row=ws.used_rows() + 1)


def bad_registry_row(env: Env, n: int = 1) -> None:
    append_registry_cells(env, **{"Emirate": "Atlantis", "Registry": "DLD", "Project Name (EN)": f"Bad Row {n}", "Developer": "X"})


def all_text(env: Env, caplog: pytest.LogCaptureFixture, capsys: Optional[pytest.CaptureFixture] = None) -> str:
    """Everything a stranger could read: logs, stdout/stderr, every cell of every tab, the snapshot."""
    parts = [caplog.text]
    if capsys is not None:
        out = capsys.readouterr()
        parts += [out.out, out.err]
    for title in env.sheet.titles():
        parts += [" ".join(row) for row in env.sheet.tab(title).dump()]
    if env.snapshot.exists():
        parts.append(env.snapshot.read_text(encoding="utf-8"))
    return "\n".join(parts)


def assert_no_secrets(text: str) -> None:
    for secret in ALL_SECRETS:
        assert secret not in text, f"secret leaked: {secret!r}"


def main_messages(caplog: pytest.LogCaptureFixture, level: int = logging.INFO) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "src.main" and r.levelno >= level]


# -- geocoder fakes (copied in small form: tests must not import each other) ----------
class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None):
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is None else json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, handler: Callable[[dict], Any]):
        self.handler = handler
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None, **extra):
        call = {"url": url, "params": dict(params or {}), "headers": dict(headers or {})}
        self.calls.append(call)
        reply = self.handler(call)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def osm_place(lat: float, lon: float, name: str) -> dict[str, Any]:
    return {
        "place_id": 1, "lat": f"{lat:.7f}", "lon": f"{lon:.7f}", "category": "building", "type": "apartments",
        "place_rank": 30, "addresstype": "building",
        "display_name": f"{name}, Example Community, Dubai, United Arab Emirates",
        "address": {
            "building": name, "suburb": "Example Community", "state": "Dubai", "country": "United Arab Emirates",
            "country_code": "ae",
        },
        "boundingbox": ["25.0", "25.1", "55.0", "55.1"],
    }


def real_geocoder(cfg: Config, tmp: Path, handler: Callable[[dict], Any]) -> tuple[Callable[..., Any], FakeSession]:
    """geocode_pending wired to a real Geocoder whose HTTP, clock and cache are fakes."""
    clock = FakeClock()
    session = FakeSession(handler)
    geo = Geocoder(cfg, session=session, cache_file=tmp / "geocode_cache.json", sleep=clock.sleep, clock=clock.time, use_cache=False)
    return partial(geocode_pending, geocoder=geo), session


def hit_everything(call: dict) -> FakeResponse:
    name = call["params"].get("q", "Fake").split(",")[0]
    return FakeResponse(200, [osm_place(*DOWNTOWN, name)])


@pytest.fixture(autouse=True)
def _tidy_logging():
    """main() configures logging globally; put it back so tests do not leak into each other."""
    root, src_logger, export_logger = logging.getLogger(), logging.getLogger("src"), logging.getLogger("export_map_pins")
    saved = (root.level, src_logger.level, export_logger.level)
    yield
    for handler in [h for h in root.handlers if getattr(h, app._LOG_HANDLER_MARK, False)]:
        root.removeHandler(handler)
    logging.captureWarnings(False)  # configure_logging routes warnings into logging; undo it for the next test
    root.setLevel(saved[0])
    src_logger.setLevel(saved[1])
    export_logger.setLevel(saved[2])


@pytest.fixture(autouse=True)
def _keep_the_interpreters_excepthook():
    """main() installs the log-safe hook for the process; give the next test the hook it came with."""
    saved = sys.excepthook
    yield
    sys.excepthook = saved


@pytest.fixture(autouse=True)
def _loud(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG, logger="src")


@pytest.fixture(autouse=True)
def _no_seed_file(monkeypatch):
    """Runs read the real seed CSV (header only today). Tests that need seed rows patch app.read_seed."""
    monkeypatch.setattr(app, "read_seed", lambda *a, **k: [])


# ---------------------------------------------------------------------------
# Module facts
# ---------------------------------------------------------------------------
def test_importing_the_module_has_no_side_effects(tmp_path):
    code = (
        "import logging, os, sys\n"
        "import src.main\n"
        "root = logging.getLogger()\n"
        "print(len(root.handlers), root.level, logging.getLogger('src').level, sorted(os.listdir('.')))\n"
        "print('scripts' in sys.modules)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(ROOT_DIR))
    done = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    first, second = done.stdout.strip().splitlines()
    assert first == "0 30 0 []"  # no handler, root still WARNING, "src" untouched, no file created
    assert second == "False"  # src never imports scripts


def test_exit_code_constants_and_documented_table():
    assert (app.EXIT_OK, app.EXIT_FAILED, app.EXIT_SETUP) == (0, 1, 2)
    doc = app.__doc__
    assert "Exit codes" in doc
    for line in ("    0  ", "    1  ", "    2  "):
        assert line in doc


def test_default_snapshot_is_the_file_the_workflow_uploads():
    assert app.SNAPSHOT_FILENAME == "scraped_projects_latest.json"
    assert app.default_snapshot_path() == ROOT_DIR / "scraped_projects_latest.json"
    workflow = (ROOT_DIR / ".github" / "workflows" / "daily_uae_real_estate.yml").read_text(encoding="utf-8")
    assert app.SNAPSHOT_FILENAME in workflow


def test_git_sha_is_truncated_and_blank_when_unset():
    sha = "0123456789abcdef" * 2 + "01234567"  # 40 hex digits, assembled: the hygiene scan rejects a literal one
    assert app.git_sha({"GITHUB_SHA": sha + "\n"}) == "0123456789ab"
    assert app.git_sha({}) == ""
    assert app.git_sha({"GITHUB_SHA": "   "}) == ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def test_secret_values_cover_hidden_fields_json_members_pem_lines_and_the_contact():
    cfg = Config(
        gemini_api_key=KEY_GEMINI, gcp_service_account_key=SA_JSON, spreadsheet_id=SHEET_ID, data_dubai_api_key=KEY_DUBAI,
        data_dubai_api_secret=SECRET_DUBAI, locationiq_api_key=KEY_LIQ, share_with_emails=("a@example.invalid",),
        geocode_contact=CONTACT, google_sheet_name="Fake Tracker For Tests",
    )
    values = app.secret_values(cfg)
    for expected in (KEY_GEMINI, SA_JSON, SHEET_ID, KEY_DUBAI, SECRET_DUBAI, KEY_LIQ, "a@example.invalid", CONTACT,
                     PRIVATE_KEY_ID, PEM_LINE_ONE, PEM_LINE_TWO):
        assert expected in values
    assert "Fake Tracker For Tests" not in values  # an ordinary setting is not a secret
    assert "" not in values and len(values) == len(set(values))


def test_secret_values_of_an_empty_config_is_empty():
    assert app.secret_values(Config()) == []


# A key file built here, never a real-looking one: every part is a distinct, obviously fake word.
ZZ_EMAIL = "zz-runner@zz-proj-demo-01.iam.example.invalid"
ZZ_PROJECT = "zz-proj-demo-01"
ZZ_CLIENT_ID = "99887766554433"
ZZ_KEY_ID = "zz-key-id-00112233"


def zz_key_json(**over: str) -> str:
    members = {
        "type": "service_account", "project_id": ZZ_PROJECT, "private_key_id": ZZ_KEY_ID, "private_key": PRIVATE_KEY,
        "client_email": ZZ_EMAIL, "client_id": ZZ_CLIENT_ID, "token_uri": "https://oauth2.example.invalid/token",
    }
    members.update(over)
    return json.dumps(members)


def test_secret_values_cover_the_service_account_identifiers_and_the_parts_of_the_e_mail():
    # REGRESSION (audit): the e-mail, client id and project id of the key were not secrets, so a library line that
    # quoted the lookup URL "/serviceAccounts/<e-mail>/allowedLocations" printed the project's name in a public log.
    values = app.secret_values(Config(gcp_service_account_key=zz_key_json()))
    for expected in (ZZ_EMAIL, "zz-runner", "zz-proj-demo-01.iam.example.invalid", ZZ_PROJECT, ZZ_CLIENT_ID, ZZ_KEY_ID):
        assert expected in values, expected
    # each one, inside a longer string, is removed (the whole e-mail first, so no half of it is left behind)
    line = (
        f"GET /v1/projects/-/serviceAccounts/{ZZ_EMAIL}/allowedLocations for client {ZZ_CLIENT_ID} of {ZZ_PROJECT}; "
        f"account zz-runner key {ZZ_KEY_ID} on host zz-proj-demo-01.iam.example.invalid"
    )
    out = app.scrub_for_log(line, values)
    for gone in (ZZ_EMAIL, "zz-runner", ZZ_PROJECT, ZZ_CLIENT_ID, ZZ_KEY_ID, "iam.example.invalid"):
        assert gone not in out, gone
    assert "allowedLocations" in out and "GET /v1/projects/-/serviceAccounts/" in out  # the rest of the line stays readable


def test_service_account_identifiers_too_short_to_be_one_are_not_secrets_so_ordinary_words_survive():
    short = zz_key_json(project_id="ab", client_email="a@b.com", client_id="12")
    values = app.secret_values(Config(gcp_service_account_key=short))
    for tiny in ("ab", "a", "b.com", "12"):
        assert tiny not in values
    assert app.scrub_for_log("a table of 12 rows about abc", values) == "a table of 12 rows about abc"
    # an e-mail member that is not an e-mail, or a member that is not text, adds nothing and does not crash
    odd = zz_key_json(client_email="no-at-sign-here", client_id=12345678)
    assert "no-at-sign-here" in app.secret_values(Config(gcp_service_account_key=odd))


def test_the_configured_handler_scrubs_the_service_account_identifiers_from_an_own_record():
    cfg = Config(gcp_service_account_key=zz_key_json())
    handler = app.configure_logging(verbose=True, secrets=app.secret_values(cfg))
    stream = io.StringIO()
    handler.setStream(stream)
    logging.getLogger("src.gsheet_manager").warning("lookup for %s failed (project %s)", ZZ_EMAIL, ZZ_PROJECT)
    out = stream.getvalue()
    assert "lookup for" in out and "failed" in out
    for gone in (ZZ_EMAIL, "zz-runner", ZZ_PROJECT):
        assert gone not in out, gone


def test_scrub_for_log_removes_secrets_and_credential_shapes_and_is_one_short_line():
    text = f"failed key={KEY_DUBAI}\n\tdetail {SECRET_DUBAI} Authorization: Bearer abcdefghijklmnop contact {CONTACT}"
    out = app.scrub_for_log(text, [KEY_DUBAI, SECRET_DUBAI, CONTACT])
    for secret in (KEY_DUBAI, SECRET_DUBAI, CONTACT, "abcdefghijklmnop"):
        assert secret not in out
    assert "\n" not in out and "\t" not in out
    assert len(app.scrub_for_log("x " * 1000, (), limit=50)) <= 50


def test_describe_error_names_the_class_and_scrubs_and_locates_only_unplanned_errors():
    secrets = [KEY_DUBAI]
    planned = app.describe_error(SourceError(f"bad answer {KEY_DUBAI}"), secrets)
    assert planned.startswith("SourceError: ") and KEY_DUBAI not in planned and "(at " not in planned
    try:
        raise RuntimeError(f"boom {KEY_DUBAI}")
    except RuntimeError as exc:
        unplanned = app.describe_error(exc, secrets)
    # Deliberately changed: an unplanned class used to print "RuntimeError: <scrubbed text>"; its text can carry a
    # cell or a response body, so a public log now gets the class name and the location only.
    assert unplanned.startswith("RuntimeError (at ") and KEY_DUBAI not in unplanned and "boom" not in unplanned
    assert "(at test_main.py:" in unplanned and "test_describe_error" in unplanned
    assert app.describe_error(ValueError(), secrets).startswith("ValueError")


# What an unplanned error's text can carry: a cell value, a project name, a response body.
LEAK = "MARKER-LEAK Alpha Residences Tower"


class UnrecognisedError(Exception):
    """A class this project has never heard of (an SDK, a library, a bug)."""


@pytest.mark.parametrize(
    "exc",
    [UnrecognisedError(LEAK), KeyError(LEAK), ValueError(LEAK), RuntimeError(LEAK), OSError(LEAK), UnrecognisedError(KEY_DUBAI + LEAK)],
)
def test_describe_error_never_shows_the_text_of_an_unplanned_class(exc):
    try:
        raise exc
    except Exception as caught:
        shown = app.describe_error(caught, [KEY_DUBAI])
        assert shown.startswith(type(caught).__name__ + " (at test_main.py:") and "in test_describe_error_never" in shown
        assert "MARKER-LEAK" not in shown and "Alpha" not in shown and KEY_DUBAI not in shown
        # The private run-log note may keep the text, still scrubbed of the known secrets.
        private = app.describe_error(caught, [KEY_DUBAI], private=True)
        assert "MARKER-LEAK" in private and KEY_DUBAI not in private
        assert app.Report([KEY_DUBAI]).error_text(caught) == shown
        assert app.Report([KEY_DUBAI]).error_text(caught, private=True) == private


@pytest.mark.parametrize(
    "exc",
    [
        SourceError("dld_projects: answered HTTP 503"),
        SheetError("Registry_Projects: row 4 is not usable"),
        GeocoderConfigError("GEOCODE_BACKEND is not one of the known backends"),
        app.SetupError("cannot open the spreadsheet (SheetError: x)"),
        app.RegistryChanged("Registry_Projects changed while the run was in progress (2 readable row(s))"),
    ],
)
def test_describe_error_keeps_the_text_of_this_projects_own_error_types(exc):
    shown = app.describe_error(exc)
    assert shown == f"{type(exc).__name__}: {exc}" and "(at " not in shown
    assert app.describe_error(exc, private=True) == shown


def test_error_location_without_a_traceback_is_unknown():
    assert app.error_location(ValueError("never raised")) == "unknown"


@pytest.mark.parametrize(
    "build, expected",
    [
        (lambda: dld(1), None),
        (lambda: seed_row(), None),
        (lambda: RegistryRow.model_construct(**{**dld(1).model_dump(), "emirate": "Abu Dhabi"}), "emirate outside"),
        (lambda: RegistryRow.model_construct(**{**dld(1).model_dump(), "registry_project_no": None, "developer": ""}), "no identity key"),
        (lambda: dld(1, project_name_en="N" * 10_001), "longer than"),
        (lambda: dld(1, project_name_en="N" * 10_000), None),
    ],
)
def test_row_problem(build, expected):
    problem = app.row_problem(build())
    if expected is None:
        assert problem is None
    else:
        assert problem and expected in problem


def test_row_problem_never_quotes_the_row():
    row = RegistryRow.model_construct(**{**dld(1).model_dump(), "emirate": f"Abu Dhabi {KEY_DUBAI}"})
    assert KEY_DUBAI not in app.row_problem(row)


def test_accept_rows_rejects_anything_that_is_not_a_registry_row():
    with pytest.raises(SourceError, match="dict"):
        app.accept_rows("dld_projects", [dld(1), {"Emirate": "Dubai"}])
    kept, dropped = app.accept_rows("x", [dld(1), dld(2, project_name_en="N" * 10_001)])
    assert [r.registry_project_no for r in kept] == ["100001"] and sum(dropped.values()) == 1


def test_accept_candidates_rejects_anything_that_is_not_a_candidate():
    with pytest.raises(SourceError, match="str"):
        app.accept_candidates("gemini_news", [candidate(), "Fake Launch"])
    assert app.accept_candidates("gemini_news", [candidate()])[0].project_name == "Fake Launch One"


def test_unknown_required_sources():
    cfg = Config(require_sources=("dld_projects", "dld_projectz", "nonsense"))
    assert app.unknown_required_sources(cfg, feeds(dld_rows=[dld(1)])) == ["dld_projectz", "nonsense"]
    assert app.unknown_required_sources(Config(), feeds(dld_rows=[dld(1)])) == []


def test_merge_feeds_counts_each_kind_of_project_and_puts_fresh_rows_first():
    existing_unchanged = dld(1, day=date(2026, 10, 1))
    existing_changed = dld(2, day=date(2026, 10, 1))
    existing_kept = dld(3, day=date(2026, 10, 1))
    fresh = [
        dld(1, day=TODAY),  # same content, newer Last Seen only: unchanged
        dld(2, day=TODAY, status="Under Construction"),  # a field changed: updated
        dld(4, day=TODAY),  # not in the sheet: new
    ]
    merged, stats = app.merge_feeds(fresh, [existing_unchanged, existing_changed, existing_kept])
    assert (stats.new, stats.updated, stats.unchanged, stats.kept_not_seen) == (1, 1, 1, 1)
    by_no = {r.registry_project_no: r for r in merged}
    assert set(by_no) == {"100001", "100002", "100003", "100004"}
    assert by_no["100001"].first_seen == "2026-10-01" and by_no["100001"].last_seen == TODAY.isoformat()
    assert by_no["100002"].status == "Under Construction"
    assert by_no["100003"].last_seen == "2026-10-01"  # kept, untouched
    assert "1 new, 1 updated, 1 unchanged, 1 kept" in stats.describe()


def test_merge_feeds_fresh_value_wins_a_conflict_over_the_sheet():
    old = dld(1, day=date(2026, 10, 1), status="Old Status", area_en="Old Area")
    new = dld(1, day=TODAY, status="New Status")
    merged, _ = app.merge_feeds([new], [old])
    assert merged[0].status == "New Status"
    assert merged[0].area_en == "Old Area"  # a field the fresh row leaves blank is filled from the sheet


def test_merge_feeds_with_no_existing_rows_counts_everything_as_new():
    merged, stats = app.merge_feeds([dld(1), dld(2)], [])
    assert len(merged) == 2 and stats.new == 2 and stats.kept_not_seen == 0


def test_unmatched_listing_keys():
    rows = [dld(1)]
    assert app.unmatched_listing_keys({"dld:dubai:100001": {}, "dld:dubai:5": {}}, rows) == ["dld:dubai:5"]


def test_build_snapshot_has_the_documented_fields():
    outcomes = [
        app.SourceOutcome("dld_projects", SourceRunInfo("ok", 2)),
        app.SourceOutcome("srerd_projects", SourceRunInfo("skipped", 0, "not configured")),
    ]
    snap = app.build_snapshot([dld(1), dld(2)], outcomes, generated_at="2026-10-25T03:00:00Z", sha="abc")
    assert snap["generated_at"] == "2026-10-25T03:00:00Z" and snap["git_sha"] == "abc"
    assert snap["sources"] == {"dld_projects": {"status": "ok", "rows": 2}, "srerd_projects": {"status": "skipped", "rows": 0}}
    assert snap["registry_rows"] == 2 and len(snap["rows"]) == 2
    assert snap["rows"][0]["Project Key"] == "dld:dubai:100001" and all(isinstance(v, str) for v in snap["rows"][0].values())


def test_write_snapshot_is_atomic_and_ascii(tmp_path, monkeypatch):
    target = tmp_path / "out" / "snap.json"
    app.write_snapshot(target, {"rows": ["مشروع \u202e =HYPERLINK(1)"], "n": 1})
    assert target.read_bytes().isascii()
    assert json.loads(target.read_text(encoding="utf-8"))["rows"] == ["مشروع \u202e =HYPERLINK(1)"]

    before = target.read_text(encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        app.write_snapshot(target, {"rows": [], "n": 2})
    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == before  # the old file is intact
    assert [p.name for p in target.parent.iterdir()] == ["snap.json"]  # and no temp file is left


def test_write_snapshot_refuses_nan(tmp_path):
    with pytest.raises(ValueError):
        app.write_snapshot(tmp_path / "s.json", {"x": float("nan")})
    assert list(tmp_path.iterdir()) == []


def test_registry_writer_refuses_when_blocked_and_skips_a_repeat():
    class Sheets:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def write_registry(self, rows):
            self.calls.append(len(list(rows)))

    sheets = Sheets()
    blocked = app.RegistryWriter(sheets, blocked_reason="dry run")
    assert blocked.write([dld(1)]) is False and sheets.calls == [] and blocked.rows_written is None

    writer = app.RegistryWriter(sheets)
    assert writer.write([]) is False  # never an empty write
    assert writer.write([dld(1)]) is True and writer.write([dld(1)]) is True
    assert sheets.calls == [1] and writer.rows_written == 1  # the repeat cost no request
    assert writer.write([dld(1), dld(2)]) is True and sheets.calls == [1, 2]


def test_registry_block_reason_order():
    ok = app.SheetState()
    assert app.registry_block_reason(ok, merge_failed=False, dry_run=False) == ""
    assert "dry run" in app.registry_block_reason(ok, merge_failed=False, dry_run=True)
    assert "merge" in app.registry_block_reason(ok, merge_failed=True, dry_run=False)
    unreadable = app.SheetState(registry_readable=False)
    assert "could not be read" in app.registry_block_reason(unreadable, merge_failed=False, dry_run=False)
    problems = app.SheetState(registry_problems=["row 2: bad"])
    assert "1 row(s)" in app.registry_block_reason(problems, merge_failed=False, dry_run=False)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def test_secret_filter_scrubs_message_args_and_tracebacks():
    flt = app.SecretFilter([KEY_DUBAI])
    record = logging.LogRecord("x", logging.INFO, "f.py", 1, "calling %s with %s", ("https://h/?a=1", KEY_DUBAI), None)
    assert flt.filter(record) is True
    assert KEY_DUBAI not in record.getMessage() and record.args is None

    try:
        raise ValueError(f"nope {KEY_DUBAI}")
    except ValueError:
        import sys as _sys

        record = logging.LogRecord("x", logging.ERROR, "f.py", 1, "failed", (), _sys.exc_info())
    flt.filter(record)
    assert record.exc_info is None and KEY_DUBAI not in (record.exc_text or "") and "ValueError" in record.exc_text


def test_secret_filter_survives_a_broken_format_string():
    record = logging.LogRecord("x", logging.INFO, "f.py", 1, "needs %s and %s", ("only one",), None)
    assert app.SecretFilter([]).filter(record) is True


def test_configure_logging_levels_and_single_handler():
    handler = app.configure_logging(verbose=False, secrets=[KEY_DUBAI])
    again = app.configure_logging(verbose=True, secrets=[KEY_DUBAI])
    root = logging.getLogger()
    marked = [h for h in root.handlers if getattr(h, app._LOG_HANDLER_MARK, False)]
    assert marked == [again] and handler not in root.handlers
    assert root.level > logging.CRITICAL  # a logger that is not ours says nothing at any level, with or without -v
    assert logging.getLogger("src").level == logging.DEBUG and logging.getLogger("export_map_pins").level == logging.DEBUG
    for foreign in ("urllib3.connectionpool", "google.auth._default", "gspread.http_client", "google_genai.models"):
        assert not logging.getLogger(foreign).isEnabledFor(logging.CRITICAL), foreign
    app.configure_logging(verbose=False)
    assert logging.getLogger("src").level == logging.INFO and logging.getLogger("export_map_pins").level == logging.INFO


def test_configured_handler_output_never_contains_a_secret():
    handler = app.configure_logging(verbose=True, secrets=[KEY_DUBAI])
    stream = io.StringIO()
    handler.setStream(stream)
    logging.getLogger("src.somewhere").debug("GET https://example.invalid/?key=%s", KEY_DUBAI)
    logging.getLogger("urllib3.connectionpool").debug("never shown: %s", KEY_DUBAI)
    logging.getLogger("src.somewhere").error("also %s", KEY_DUBAI)
    out = stream.getvalue()
    assert "src.somewhere" in out and "never shown" not in out
    assert KEY_DUBAI not in out


# The public log of this repository holds only what this project wrote. A library's record, at any level, is
# dropped by policy (src/log_policy.py), not by a list of library names: the next dependency is covered too.
FOREIGN_HEADERS = f"{LEAK}-Header: value-{LEAK}\r\n"


def public_log(secrets=()) -> io.StringIO:
    """Install the real handler (as main() does, at -v) and point it at a buffer."""
    handler = app.configure_logging(verbose=True, secrets=secrets)
    stream = io.StringIO()
    handler.setStream(stream)
    return stream


def nothing_foreign_recorded(caplog) -> bool:
    return not [r for r in caplog.records if not (r.name == "src" or r.name.startswith("src."))]


def test_a_urllib3_header_warning_with_a_search_url_and_raw_header_bytes_is_not_emitted(caplog):
    # REGRESSION (audit): "Failed to parse headers (url=https://host/search?q=<project name>)" plus the header bytes
    # went to the public log, because every third-party record at WARNING passed the handler.
    stream = public_log()
    logging.getLogger("urllib3.connection").warning(
        "Failed to parse headers (url=%s): %s", f"https://host.example.invalid/search?q={LEAK}", FOREIGN_HEADERS
    )
    assert stream.getvalue() == ""
    assert LEAK not in caplog.text and nothing_foreign_recorded(caplog)


def test_a_google_auth_retry_warning_with_the_service_account_e_mail_is_not_emitted(caplog):
    # REGRESSION (audit): google-auth's background lookup retries "/serviceAccounts/<e-mail>/allowedLocations" and
    # urllib3 logs that URL at WARNING. No secret is registered here on purpose: the policy does not need to know it.
    stream = public_log()
    url = f"/v1/projects/-/serviceAccounts/{ZZ_EMAIL}/allowedLocations"
    logging.getLogger("urllib3.connectionpool").warning(
        "Retrying (%r) after connection broken by '%r': %s", "Retry(total=2)", "RemoteDisconnected()", url
    )
    logging.getLogger("google.auth.compute_engine._metadata").warning("lookup of %s failed", url)
    assert stream.getvalue() == ""
    assert ZZ_EMAIL not in caplog.text and nothing_foreign_recorded(caplog)


def test_a_python_warning_from_a_library_is_not_emitted(capsys):
    # REGRESSION (audit): google-genai warns (warnings.warn) with the value the server sent. captureWarnings sends
    # it through logging, where the policy drops it; without that it goes straight to stderr.
    def to_stderr(message, category, filename, lineno, file=None, line=None):
        sys.stderr.write(warnings.formatwarning(message, category, filename, lineno, line))

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.showwarning = to_stderr  # what an unconfigured process does with a warning; restored on exit
        stream = public_log()
        warnings.warn_explicit(
            f"{LEAK} is not a valid FinishReason", UserWarning, "site-packages/google/genai/_common.py", 7,
            module="google.genai._common",
        )
        logging.captureWarnings(False)  # while the stand-in is still the hook, so it is the one put back
    assert stream.getvalue() == ""
    seen = capsys.readouterr()
    assert LEAK not in seen.out + seen.err


def test_own_records_are_still_emitted_and_scrubbed():
    stream = public_log([KEY_DUBAI, ZZ_EMAIL])
    logging.getLogger("src.somewhere").info("checking %s for %s", "9 row(s)", ZZ_EMAIL)
    logging.getLogger("src.main").error("failed with key %s", KEY_DUBAI)
    logging.getLogger("export_map_pins").warning("exporter says %s", KEY_DUBAI)
    logging.getLogger("src").debug("the package logger itself")
    out = stream.getvalue()
    for kept in ("src.somewhere", "checking 9 row(s) for", "src.main", "failed with key", "export_map_pins", "exporter says", "the package logger"):
        assert kept in out, kept
    assert KEY_DUBAI not in out and ZZ_EMAIL not in out


def test_a_foreign_error_with_a_traceback_is_not_emitted(caplog):
    stream = public_log([KEY_DUBAI])
    try:
        raise ValueError(f"nope {LEAK} {KEY_DUBAI}")
    except ValueError:
        logging.getLogger("urllib3.connectionpool").error("request failed", exc_info=True)
        logging.getLogger("google.auth").exception("refresh failed")
        logging.getLogger().critical("root says %s", LEAK, exc_info=True)
    assert stream.getvalue() == ""
    assert "nope" not in caplog.text and LEAK not in caplog.text and nothing_foreign_recorded(caplog)


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL])
@pytest.mark.parametrize("name", ["urllib3", "requests.adapters", "google.auth.transport.requests", "gspread", "httpx", "absl", "root"])
def test_no_foreign_logger_speaks_at_any_level(name, level, caplog):
    stream = public_log()
    logging.getLogger("" if name == "root" else name).log(level, "marker %s", LEAK)
    assert stream.getvalue() == "" and LEAK not in caplog.text


def test_a_library_that_sets_its_own_level_is_still_dropped_by_the_handler():
    # A library may lower its own logger's level (urllib3 users do), which lets the record exist and reach the root
    # handlers; the handler's own filter is what keeps it out of the public log.
    stream = public_log()
    foreign = logging.getLogger("zz_library")
    foreign.setLevel(logging.DEBUG)
    try:
        foreign.warning("Failed to parse headers (url=%s)", LEAK)
    finally:
        foreign.setLevel(logging.NOTSET)
    assert stream.getvalue() == ""


def test_a_name_that_only_starts_like_ours_is_not_ours():
    stream = public_log()
    lookalikes = [logging.getLogger("srcother"), logging.getLogger("export_map_pins_extra")]
    try:
        for lookalike in lookalikes:
            lookalike.setLevel(logging.DEBUG)  # so the record exists and only the handler's filter can stop it
            lookalike.warning("not ours %s", LEAK)
    finally:
        for lookalike in lookalikes:
            lookalike.setLevel(logging.NOTSET)
    assert stream.getvalue() == ""


def _public_code_files():
    files = sorted((ROOT_DIR / "src").rglob("*.py")) + [ROOT_DIR / "scripts" / "export_map_pins.py"]
    return [path for path in files if "__pycache__" not in path.parts]


def _modules_that_log():
    """Everything above except log_policy.py, whose getLogger(name) calls are the loop that configures the allowed names."""
    return [path for path in _public_code_files() if path.name != "log_policy.py"]


def test_every_logger_the_public_code_creates_is_one_the_policy_lets_through():
    # Tripwire: a logger named after a module that can run as __main__, or outside the allowed names, would be
    # silently dropped from the public log (a lost error line, not a leak).
    import ast

    from src.log_policy import OWN_LOGGERS

    seen = 0
    for path in _modules_that_log():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "getLogger"):
                continue
            if not node.args:
                continue  # the root logger: configure_logging sets it, nothing logs through it
            seen += 1
            arg = node.args[0]
            if isinstance(arg, ast.Name) and arg.id == "__name__":
                assert path.parent.name in {"src", "sources"} and path.name != "main.py", (
                    f"{path.name}: getLogger(__name__) would be '__main__' when the file is run as a module"
                )
            else:
                assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), f"{path.name}: a computed logger name"
                assert any(arg.value == own or arg.value.startswith(own + ".") for own in OWN_LOGGERS), (
                    f"{path.name}: logger {arg.value!r} is not one of {OWN_LOGGERS}"
                )
    assert seen >= 10  # the scan really found the loggers (src modules plus the exporter)


def test_the_public_code_never_calls_warnings_warn():
    # Tripwire for captureWarnings: it silences every warning, which is only right while this project raises none.
    import ast

    for path in _public_code_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if node.func.value.id == "warnings":
                    assert node.func.attr not in {"warn", "warn_explicit", "showwarning"}, f"{path.name}:{node.lineno}"
            if isinstance(node, ast.ImportFrom) and node.module == "warnings":
                raise AssertionError(f"{path.name}:{node.lineno}: imports from warnings")


# ---------------------------------------------------------------------------
# Clean runs
# ---------------------------------------------------------------------------
def test_clean_first_run_on_an_empty_sheet(tmp_path, caplog):
    env = make_env(tmp_path)
    adapters = feeds(dld_rows=[dld(1), dld(2)], srerd_rows=[srerd(1)], seed=[], candidates=[candidate()])
    assert go(env, adapters) == 0

    registry = env.registry()
    assert set(registry) == {"dld:dubai:100001", "dld:dubai:100002", "srerd:sharjah:SH-0001"}
    assert registry["dld:dubai:100001"]["First Seen"] == TODAY.isoformat()
    assert [c["Candidate Key"] != "" for c in env.records(TAB_CANDIDATES)] == [True]
    assert env.records(TAB_PINS) == []  # nothing is geocoded, so nothing is pinned

    log = env.last_log()
    assert log["Exit Code"] == "0" and log["Registry Rows"] == "3" and log["Map Pins"] == "0"
    assert source_info(log) == {
        "dld_projects": {"status": "ok", "rows": 2},
        "srerd_projects": {"status": "ok", "rows": 1},
        "seed_csv": {"status": "ok", "rows": 0},
        "gemini_news": {"status": "ok", "rows": 0, "note": "1 candidate(s)"},
    }
    snap = env.snapshot_data()
    assert snap["registry_rows"] == 3 and {r["Project Key"] for r in snap["rows"]} == set(registry)
    assert snap["sources"]["dld_projects"] == {"status": "ok", "rows": 2}
    assert "RUN FAILED" not in caplog.text and "run finished cleanly" in caplog.text
    assert "3 new, 0 updated, 0 unchanged, 0 kept" in caplog.text


def test_second_run_is_idempotent_keeps_first_seen_and_updates_last_seen(tmp_path, caplog):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1), dld(2)], srerd_rows=[srerd(1)], candidates=[candidate()])) == 0
    first = env.registry()
    caplog.clear()

    again = feeds(dld_rows=[dld(1, day=LATER), dld(2, day=LATER)], srerd_rows=[srerd(1, day=LATER)], candidates=[candidate()])
    assert go(env, again, today=LATER) == 0

    second = env.registry()
    assert set(second) == set(first)
    for key, row in second.items():
        assert row["First Seen"] == TODAY.isoformat() and row["Last Seen"] == LATER.isoformat()
        assert {k: v for k, v in row.items() if k != "Last Seen"} == {k: v for k, v in first[key].items() if k != "Last Seen"}
    assert "0 new, 0 updated, 3 unchanged, 0 kept" in caplog.text
    assert len(env.records(TAB_CANDIDATES)) == 1  # a candidate already on the tab is not added again
    assert len(env.records(TAB_RUN_LOGS)) == 2


def test_seed_row_merges_into_the_registry_row_by_name_key(tmp_path):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1), dld(2)])) == 0
    seed = seed_row("Fake Tower 1", "Fake Developer", area_en="Fake Area", master_project="Fake Master")
    assert go(env, feeds(dld_rows=[dld(1), dld(2)], seed=[seed]), today=LATER) == 0
    registry = env.registry()
    assert set(registry) == {"dld:dubai:100001", "dld:dubai:100002"}  # no third row for the seed
    row = registry["dld:dubai:100001"]
    assert row["Registry"] == "DLD" and row["Registry Project No."] == "100001"  # the registry row outranks the seed
    assert row["Area (EN)"] == "Fake Area" and row["Master Project"] == "Fake Master"  # and the seed fills its blanks


def test_a_seed_row_that_precedes_its_registry_row_is_taken_over_by_it(tmp_path):
    env = make_env(tmp_path)
    assert go(env, feeds(seed=[seed_row("Fake Tower 1", "Fake Developer", area_en="Fake Area")])) == 0
    assert list(env.registry()) == ["name:dubai:fake-developer:fake-tower-1"]
    assert go(env, feeds(dld_rows=[dld(1)]), today=LATER) == 0
    registry = env.registry()
    assert list(registry) == ["dld:dubai:100001"]  # one project, now under its registry key
    assert registry["dld:dubai:100001"]["Area (EN)"] == "Fake Area"
    assert registry["dld:dubai:100001"]["First Seen"] == TODAY.isoformat()


def test_a_dld_row_with_a_different_registry_id_never_merges(tmp_path):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    twin = dld(1, registry_project_no="999999")  # same name and developer, another id: another project
    assert go(env, feeds(dld_rows=[twin]), today=LATER) == 0
    assert set(env.registry()) == {"dld:dubai:100001", "dld:dubai:999999"}


def test_rows_in_no_feed_this_run_are_kept(tmp_path, caplog):
    # ALLOW_ROW_DROP lets the feed shrink so that the keep rule can be looked at on its own
    env = make_env(tmp_path, allow_row_drop=True)
    assert go(env, feeds(dld_rows=[dld(1), dld(2), dld(3)])) == 0
    caplog.clear()
    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER) == 0
    registry = env.registry()
    assert set(registry) == {"dld:dubai:100001", "dld:dubai:100002", "dld:dubai:100003"}
    assert registry["dld:dubai:100003"]["Last Seen"] == TODAY.isoformat()  # untouched
    assert "2 kept" in caplog.text


def test_real_adapters_with_nothing_configured_make_a_green_run_with_a_loud_warning(tmp_path, caplog):
    from src.sources import build_adapters

    env = make_env(tmp_path, data_dubai_api_key="", data_dubai_api_secret="", gemini_api_key="")
    adapters = build_adapters(env.cfg, today=TODAY)
    assert go(env, adapters) == 0
    loud = [r for r in caplog.records if r.levelno == logging.WARNING and "NO SOURCE PRODUCED" in r.getMessage()]
    assert len(loud) == 1
    text = loud[0].getMessage()
    assert "nothing will change" in text and "DATA_DUBAI_API_KEY" in text and "ENABLE_SRERD" in text
    statuses = {name: info["status"] for name, info in source_info(env.last_log()).items()}
    assert statuses["dld_projects"] == "skipped" and statuses["srerd_projects"] == "disabled"
    assert statuses["seed_csv"] == "ok" and statuses["gemini_news"] == "disabled"
    assert env.registry() == {}


def test_the_no_source_warning_is_absent_when_a_source_delivered(tmp_path, caplog):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    assert "NO SOURCE PRODUCED" not in caplog.text


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def test_a_failed_adapter_merges_nothing_but_the_others_are_persisted(tmp_path, caplog):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1), dld(2)], srerd_rows=[srerd(1)])) == 0
    broken = FakeAdapter("dld_projects", [dld(7, day=LATER)], error=RuntimeError(f"upstream exploded key={KEY_DUBAI}"))
    healthy = FakeAdapter("srerd_projects", [srerd(1, day=LATER), srerd(2, day=LATER)])
    caplog.clear()
    assert go(env, [broken, healthy], today=LATER) == 1

    registry = env.registry()
    assert "dld:dubai:100007" not in registry  # nothing from the failed source
    assert {"dld:dubai:100001", "dld:dubai:100002"} <= set(registry)  # what it gave before is kept
    assert "srerd:sharjah:SH-0002" in registry  # the healthy source is merged and written
    log = env.last_log()
    assert log["Exit Code"] == "1"
    info = source_info(log)
    assert info["dld_projects"]["status"] == "failed" and info["dld_projects"]["rows"] == 0
    assert "RuntimeError" in info["dld_projects"]["note"] and KEY_DUBAI not in log["Sources"]
    assert info["srerd_projects"] == {"status": "ok", "rows": 2}
    assert "dld_projects: FAILED" in caplog.text and "RUN FAILED (exit 1)" in caplog.text
    assert_no_secrets(all_text(env, caplog))


def test_a_source_error_is_a_failure_too(tmp_path):
    env = make_env(tmp_path)
    adapters = [FakeAdapter("dld_projects", error=SourceError("dld_projects: returned a block page"))]
    assert go(env, adapters) == 1
    assert source_info(env.last_log())["dld_projects"]["status"] == "failed"


def test_an_adapter_returning_the_wrong_type_fails_without_merging(tmp_path):
    env = make_env(tmp_path)
    junk = SourceResult(source="dld_projects", rows=[dld(1), {"Emirate": "Dubai"}], fetched_at="x")
    assert go(env, [FakeAdapter("dld_projects", result=junk), FakeAdapter("srerd_projects", [srerd(1)])]) == 1
    assert set(env.registry()) == {"srerd:sharjah:SH-0001"}


def test_a_result_that_is_not_a_result_fails(tmp_path):
    env = make_env(tmp_path)
    assert go(env, [FakeAdapter("dld_projects", result=["not", "a", "result"])]) == 1


def test_zero_rows_from_a_source_that_always_has_some_is_a_failure(tmp_path):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1), dld(2)])) == 0
    assert go(env, feeds(dld_rows=[]), today=LATER) == 1
    assert set(env.registry()) == {"dld:dubai:100001", "dld:dubai:100002"}  # the registry is untouched
    assert source_info(env.last_log())["dld_projects"]["status"] == "failed"


def test_allow_row_drop_never_excuses_an_empty_answer(tmp_path):
    env = make_env(tmp_path, allow_row_drop=True)
    assert go(env, feeds(dld_rows=[dld(1), dld(2)])) == 0
    assert go(env, feeds(dld_rows=[]), today=LATER) == 1


def test_a_first_run_with_zero_rows_from_an_expects_rows_source_fails(tmp_path):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[])) == 1


def test_the_80_percent_guard_boundary(tmp_path):
    env = make_env(tmp_path)
    ten = [dld(n) for n in range(1, 11)]
    assert go(env, feeds(dld_rows=ten)) == 0
    assert go(env, feeds(dld_rows=ten[:8]), today=LATER) == 0  # exactly 80 percent passes
    env_fail = make_env(tmp_path / "x")
    (tmp_path / "x").mkdir()
    assert go(env_fail, feeds(dld_rows=ten)) == 0
    assert go(env_fail, feeds(dld_rows=ten[:7]), today=LATER) == 1  # 70 percent does not


def test_a_guard_failed_source_contributes_no_rows_even_new_ones(tmp_path):
    env = make_env(tmp_path)
    ten = [dld(n) for n in range(1, 11)]
    assert go(env, feeds(dld_rows=ten)) == 0
    shrunk = [dld(n, day=LATER) for n in range(1, 7)] + [dld(99, day=LATER)]  # 7 rows, one of them brand new
    assert go(env, feeds(dld_rows=shrunk), today=LATER) == 1
    registry = env.registry()
    assert "dld:dubai:100099" not in registry and len(registry) == 10
    assert all(row["Last Seen"] == TODAY.isoformat() for row in registry.values())  # not even a Last Seen moved
    assert source_info(env.last_log())["dld_projects"]["status"] == "failed"


def test_a_failed_run_does_not_lower_the_baseline(tmp_path):
    env = make_env(tmp_path)
    ten = [dld(n) for n in range(1, 11)]
    assert go(env, feeds(dld_rows=ten)) == 0
    assert go(env, feeds(dld_rows=ten[:5]), today=LATER) == 1  # failed: not a new baseline
    assert go(env, feeds(dld_rows=ten[:7]), today=LATER) == 1  # still judged against 10, not against 5
    assert go(env, feeds(dld_rows=ten[:8]), today=LATER) == 0


def test_allow_row_drop_excuses_a_shrunk_source(tmp_path):
    env = make_env(tmp_path, allow_row_drop=True)
    ten = [dld(n) for n in range(1, 11)]
    assert go(env, feeds(dld_rows=ten)) == 0
    assert go(env, feeds(dld_rows=ten[:3]), today=LATER) == 0
    assert len(env.registry()) == 10  # a shrunk feed deletes nothing


def test_pruning_the_hand_kept_seed_file_does_not_fail_the_run(tmp_path):
    """The shrink guard is for remote registries. A seed file is edited on purpose: five rows down to three is fine."""
    env = make_env(tmp_path)
    seeds = [seed_row(f"Seed Tower {n}") for n in range(5)]
    assert go(env, feeds(seed=seeds)) == 0
    assert go(env, feeds(seed=seeds[:3]), today=LATER) == 0
    assert len(env.registry()) == 5  # nothing is deleted; the pruned rows simply stop being refreshed
    assert go(env, feeds(seed=[]), today=LATER) == 0  # an emptied seed file is not a failure either


def test_a_row_a_person_adds_while_the_run_is_geocoding_is_never_deleted(tmp_path, caplog):
    """
    A run can take most of an hour and every registry write replaces all data rows. A row typed in
    between must survive: the write is refused (exit 1) instead of silently deleting it.
    """
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0

    def slow_geocode(rows, overrides, cfg, **kwargs):
        append_registry_cells(env, **{
            "Emirate": "Dubai", "Registry": "SEED", "Project Name (EN)": "Hand Typed Tower", "Developer": "Hand Dev",
        })
        return geocode_pending(rows, overrides, cfg, **kwargs)

    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER, geocode=slow_geocode) == 1
    assert "Hand Typed Tower" in [r["Project Name (EN)"] for r in env.records(TAB_REGISTRY)]
    assert "changed while the run was in progress" in caplog.text
    # the next run starts from what the person left and carries on normally
    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER) == 0
    assert "Hand Typed Tower" in [r["Project Name (EN)"] for r in env.records(TAB_REGISTRY)]


def test_the_lost_update_guard_does_not_trip_on_a_quiet_sheet(tmp_path):
    """Two consecutive writes by the same run (geocode flush, then final) must not look like someone else's edit."""
    env = make_env(tmp_path, geocode_enabled=True)
    geo, _ = real_geocoder(env.cfg, tmp_path, hit_everything)
    assert go(env, feeds(dld_rows=[dld(1), dld(2)]), geocode=geo) == 0
    geo2, _ = real_geocoder(env.cfg, tmp_path, hit_everything)
    assert go(env, feeds(dld_rows=[dld(1, day=LATER), dld(3, day=LATER)]), today=LATER, geocode=geo2) == 0
    assert len(env.registry()) == 3


def test_a_later_write_by_the_same_run_is_not_mistaken_for_someone_elses_edit(tmp_path, caplog):
    """
    Audit addition. After a write the guard must expect what THIS run just wrote. The quiet-sheet test above never
    makes two different writes (its final write equals the last flush and is skipped), so it cannot tell.
    """
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0

    def two_flushes(rows, overrides, cfg, *, today, flush):
        first = [r.model_copy(update={"zone": "First"}) for r in rows]
        flush(first)  # a different write ...
        return [r.model_copy(update={"zone": "Second"}) for r in first], {}  # ... then a different final write

    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER, geocode=two_flushes) == 0
    assert "changed while the run was in progress" not in caplog.text
    assert {r["Zone"] for r in env.records(TAB_REGISTRY) if r["Registry"] == "DLD"} == {"Second"}


def test_the_count_checked_is_the_count_after_main_drops_unusable_rows(tmp_path):
    env = make_env(tmp_path)
    ten = [dld(n) for n in range(1, 11)]
    assert go(env, feeds(dld_rows=ten)) == 0
    bad = RegistryRow.model_construct(**{**dld(50).model_dump(), "emirate": "Abu Dhabi"})
    # 8 good rows plus 2 out-of-scope rows: 10 returned, but only 8 are usable (exactly 80 percent)
    assert go(env, feeds(dld_rows=ten[:8] + [bad, bad]), today=LATER) == 0
    assert source_info(env.last_log())["dld_projects"]["rows"] == 8

    other = make_env(tmp_path / "other")
    (tmp_path / "other").mkdir()
    assert go(other, feeds(dld_rows=ten)) == 0
    assert go(other, feeds(dld_rows=ten[:7] + [bad, bad, bad]), today=LATER) == 1  # 10 returned, only 7 usable


def test_rows_outside_the_two_emirates_are_dropped_and_counted_not_merged(tmp_path, caplog):
    env = make_env(tmp_path)
    stray = RegistryRow.model_construct(**{**dld(9).model_dump(), "emirate": "Abu Dhabi"})
    assert go(env, feeds(dld_rows=[dld(1), stray])) == 0
    assert list(env.registry()) == ["dld:dubai:100001"]
    note = source_info(env.last_log())["dld_projects"]["note"]
    assert "dropped 1 row(s)" in note and "emirate outside Dubai and Sharjah" in note
    assert "dropped 1 row(s) before merging" in caplog.text


def test_rows_without_any_identity_are_dropped_so_they_cannot_block_the_registry_write(tmp_path):
    env = make_env(tmp_path)
    keyless = RegistryRow.model_construct(**{**dld(9).model_dump(), "registry_project_no": None, "developer": ""})
    assert go(env, feeds(dld_rows=[dld(1), keyless])) == 0  # write_registry would have raised on the keyless row
    assert list(env.registry()) == ["dld:dubai:100001"]
    assert "no identity key" in source_info(env.last_log())["dld_projects"]["note"]


def test_a_skipped_source_is_logged_and_recorded_and_is_green_by_default(tmp_path, caplog):
    env = make_env(tmp_path)
    adapters = [
        FakeAdapter("dld_projects", enabled=False, reason="not configured: DATA_DUBAI_API_KEY is not set"),
        FakeAdapter("srerd_projects", [srerd(1)]),
    ]
    assert go(env, adapters) == 0
    info = source_info(env.last_log())
    assert info["dld_projects"]["status"] == "skipped" and "DATA_DUBAI_API_KEY" in info["dld_projects"]["note"]
    assert "dld_projects: SKIPPED (not configured: DATA_DUBAI_API_KEY is not set)" in caplog.text
    assert adapters[0].fetches == 0


def test_a_required_source_that_was_skipped_fails_the_run(tmp_path, caplog):
    env = make_env(tmp_path, require_sources=("dld_projects",))
    adapters = [
        FakeAdapter("dld_projects", enabled=False, reason="not configured: DATA_DUBAI_API_KEY is not set"),
        FakeAdapter("srerd_projects", [srerd(1)]),
    ]
    assert go(env, adapters) == 1
    assert "required source dld_projects was SKIPPED" in caplog.text
    assert set(env.registry()) == {"srerd:sharjah:SH-0001"}  # the rest of the run still persisted
    assert env.last_log()["Exit Code"] == "1"


def test_a_required_source_that_is_switched_off_fails_the_run(tmp_path, caplog):
    env = make_env(tmp_path, require_sources=("srerd_projects",))
    adapters = [
        FakeAdapter("dld_projects", [dld(1)]),
        FakeAdapter("srerd_projects", enabled=False, reason="ENABLE_SRERD is not enabled (this source is off by default)"),
    ]
    assert go(env, adapters) == 1
    assert source_info(env.last_log())["srerd_projects"]["status"] == "disabled"
    assert "required source srerd_projects was DISABLED" in caplog.text


def test_a_required_source_that_delivered_is_fine(tmp_path):
    env = make_env(tmp_path, require_sources=("dld_projects",))
    assert go(env, feeds(dld_rows=[dld(1)])) == 0


def test_an_unknown_name_in_require_sources_is_a_setup_failure_before_the_sheet_is_opened(tmp_path, caplog):
    env = make_env(tmp_path, require_sources=("dld_projectz",))
    adapters = feeds(dld_rows=[dld(1)])
    assert go(env, adapters) == 2
    assert env.client.calls == []  # open() was never reached
    assert adapters[0].fetches == 0
    assert not env.snapshot.exists()
    # Deliberately changed: the message used to repeat the typed name. A setting's typed value does not belong in a
    # public log, so it says how many names matched nothing and lists the real ones to compare against.
    assert "REQUIRE_SOURCES names 1 source(s) this scraper does not have" in caplog.text
    assert "Known sources: dld_projects" in caplog.text
    assert "dld_projectz" not in caplog.text


def test_one_adapter_crashing_does_not_stop_the_next(tmp_path):
    env = make_env(tmp_path)
    first = FakeAdapter("dld_projects", error=KeyError("boom"))
    second = FakeAdapter("srerd_projects", [srerd(1)])
    assert go(env, [first, second]) == 1
    assert second.fetches == 1 and set(env.registry()) == {"srerd:sharjah:SH-0001"}


def test_an_adapter_whose_enabled_check_crashes_is_a_failure_not_a_crash(tmp_path):
    class Odd(FakeAdapter):
        def enabled(self, cfg):
            raise RuntimeError("config lookup failed")

    env = make_env(tmp_path)
    assert go(env, [Odd("dld_projects", [dld(1)]), FakeAdapter("srerd_projects", [srerd(1)])]) == 1
    assert source_info(env.last_log())["dld_projects"]["status"] == "failed"


def test_candidates_come_only_from_sources_that_succeeded(tmp_path):
    env = make_env(tmp_path)
    good = FakeAdapter("gemini_news", [], expects_rows=False, candidates=[candidate("Fake Launch Good")])
    bad = FakeAdapter("other_news", [], expects_rows=False, candidates=[candidate("Fake Launch Bad")])
    bad.error = SourceError("other_news: bad grounding")
    assert go(env, [FakeAdapter("dld_projects", [dld(1)]), good, bad]) == 1
    assert [c["Project Name"] for c in env.records(TAB_CANDIDATES)] == ["Fake Launch Good"]


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------
def seed_line(key: str, url: str = "https://example.invalid/seed") -> list[str]:
    cells = [""] * len(LISTING_HEADERS)
    cells[LISTING_HEADERS.index("Project Key")] = key
    cells[LISTING_HEADERS.index("Source URL")] = url
    cells[LISTING_HEADERS.index("Price From (AED)")] = "1,000,000"
    return cells


def test_the_listing_seed_is_applied_once(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "read_seed", lambda *a, **k: [seed_line("dld:dubai:100001")])
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    assert [r["Project Key"] for r in env.records(TAB_LISTINGS)] == ["dld:dubai:100001"]
    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER) == 0
    assert len(env.records(TAB_LISTINGS)) == 1


def test_bad_seed_rows_fail_the_run_but_the_good_rows_and_the_rest_still_go_through(tmp_path, monkeypatch, caplog):
    rows = [seed_line("dld:dubai:100001"), seed_line("not-a-key"), seed_line("dld:dubai:100002", url="")]
    monkeypatch.setattr(app, "read_seed", lambda *a, **k: rows)
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 1
    assert [r["Project Key"] for r in env.records(TAB_LISTINGS)] == ["dld:dubai:100001"]
    assert "2 bad Listing_Data seed row(s)" in caplog.text
    assert "seed data row 2" in caplog.text and "seed data row 3" in caplog.text
    assert set(env.registry()) == {"dld:dubai:100001"} and env.last_log()["Exit Code"] == "1"


def test_an_unreadable_seed_file_fails_the_run_without_crashing_it(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise ValueError("seed_projects.csv: the header row has no 'Project Key' column")

    monkeypatch.setattr(app, "read_seed", broken)
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 1
    assert set(env.registry()) == {"dld:dubai:100001"}


def test_a_seed_that_cannot_be_applied_fails_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "read_seed", lambda *a, **k: [seed_line("dld:dubai:100001")])
    env = make_env(tmp_path)

    def broken(rows):
        raise SheetError("boom")

    monkeypatch.setattr(env.mgr, "apply_listing_seed", broken)
    assert go(env, feeds(dld_rows=[dld(1)])) == 1
    assert set(env.registry()) == {"dld:dubai:100001"}


# ---------------------------------------------------------------------------
# Sheet problems
# ---------------------------------------------------------------------------
def test_unreadable_registry_rows_block_every_registry_write_and_fail_the_run(tmp_path, caplog):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1), dld(2)])) == 0
    bad_registry_row(env)
    before = env.dump(TAB_REGISTRY)
    env.client.reset_calls()
    caplog.clear()

    fresh = feeds(dld_rows=[dld(1, day=LATER), dld(2, day=LATER), dld(3, day=LATER)], candidates=[candidate()])
    assert go(env, fresh, today=LATER) == 1

    assert env.dump(TAB_REGISTRY) == before  # byte-identical: nothing written, not even the new project
    assert env.writes(TAB_REGISTRY) == []
    assert "1 Registry_Projects row(s) are unusable" in caplog.text and "NOT written" in caplog.text
    # everything else still ran
    assert [c["Project Name"] for c in env.records(TAB_CANDIDATES)] == ["Fake Launch One"]
    assert env.last_log()["Exit Code"] == "1"
    assert env.snapshot_data()["registry_rows"] == 3  # the snapshot holds the merge a person can read
    assert source_info(env.last_log())["dld_projects"] == {"status": "ok", "rows": 3}


def test_unreadable_registry_rows_also_block_the_geocode_flush(tmp_path, caplog):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    bad_registry_row(env)
    before = env.dump(TAB_REGISTRY)
    env.client.reset_calls()
    flushed: list[int] = []

    def geo(rows, overrides, cfg, *, today, flush):
        assert cfg.geocode_enabled is False  # no lookups when the result could not be kept
        flush(list(rows))
        flushed.append(len(rows))
        return list(rows), GeocodeStats(backend="fake")

    cfg_geocoding = dataclasses.replace(env.cfg, geocode_enabled=True)
    env.cfg = cfg_geocoding
    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER, geocode=geo) == 1
    assert flushed == [1] and env.dump(TAB_REGISTRY) == before
    assert env.writes(TAB_REGISTRY) == []


def test_problem_lines_are_capped_in_the_log(tmp_path, caplog):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    for n in range(25):
        bad_registry_row(env, n)
    caplog.clear()
    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER) == 1
    lines = [m for m in main_messages(caplog, logging.WARNING) if m.startswith("Registry_Projects problem:")]
    assert len(lines) == app.MAX_PROBLEM_LINES_LOGGED + 1
    assert lines[-1].endswith("... and 5 more")
    assert all(len(m) < 400 for m in lines)


def test_a_registry_that_cannot_be_read_blocks_the_write_but_not_the_rest(tmp_path, monkeypatch, caplog):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    before = env.dump(TAB_REGISTRY)

    def broken():
        raise SheetError("Registry_Projects: Google rejected the request (HTTP 403)")

    monkeypatch.setattr(env.mgr, "read_registry", broken)
    env.client.reset_calls()
    assert go(env, feeds(dld_rows=[dld(2, day=LATER)], candidates=[candidate()]), today=LATER) == 1
    assert env.dump(TAB_REGISTRY) == before  # never overwritten from a partial picture
    assert env.writes(TAB_REGISTRY) == []
    assert env.writes(TAB_PINS) == []  # derived data is not rebuilt from fresh rows alone either
    assert len(env.records(TAB_CANDIDATES)) == 1 and env.last_log()["Exit Code"] == "1"
    assert "could not be read, so the pin set would be incomplete" in caplog.text


def test_override_problems_fail_the_run_but_valid_overrides_are_still_applied(tmp_path, caplog):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    ws = env.sheet.tab(TAB_OVERRIDES)
    ws.put([["dld:dubai:100001", "25.1972", "55.2744", "exact", "placed by hand"], ["bogus", "x", "y", "", ""]], start_row=2)
    caplog.clear()
    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER) == 1
    row = env.registry()["dld:dubai:100001"]
    assert row["Latitude"] == "25.1972" and row["Longitude"] == "55.2744" and row["Location Precision"] == "exact"
    assert "1 Geocode_Overrides row(s) are unusable" in caplog.text
    assert [p["Project Key"] for p in env.records(TAB_PINS)] == ["dld:dubai:100001"]


def test_overrides_apply_even_when_geocoding_is_switched_off(tmp_path):
    env = make_env(tmp_path, geocode_enabled=False)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    env.sheet.tab(TAB_OVERRIDES).put([["dld:dubai:100001", "25.2", "55.3", "community", ""]], start_row=2)
    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER) == 0
    assert env.registry()["dld:dubai:100001"]["Location Precision"] == "community"
    assert env.last_log()["Map Pins"] == "1"


def test_an_unreadable_override_tab_fails_the_run_and_turns_lookups_off(tmp_path, monkeypatch):
    env = make_env(tmp_path, geocode_enabled=True)
    seen: list[bool] = []

    def geo(rows, overrides, cfg, *, today, flush):
        seen.append(cfg.geocode_enabled)
        return list(rows), GeocodeStats(backend="fake")

    def broken():
        raise SheetError("Geocode_Overrides: Google rejected the request (HTTP 403)")

    monkeypatch.setattr(env.mgr, "read_overrides", broken)
    assert go(env, feeds(dld_rows=[dld(1)]), geocode=geo) == 1
    assert seen == [False]  # a human pin might be about to be overwritten, so no lookups
    assert set(env.registry()) == {"dld:dubai:100001"}


def test_previous_counts_that_cannot_be_read_fail_the_run_but_not_the_data(tmp_path, monkeypatch, caplog):
    env = make_env(tmp_path)

    def broken():
        raise SheetError("Scrape_Run_Logs has no column Sources")

    monkeypatch.setattr(env.mgr, "last_source_counts", broken)
    assert go(env, feeds(dld_rows=[dld(1)])) == 1
    assert set(env.registry()) == {"dld:dubai:100001"}
    assert "row-count guard is OFF" in caplog.text


class StubSheets:
    """Just the reads read_sheet_state makes; last_source_counts answers whatever the test hands in."""

    def __init__(self, counts):
        self._counts = counts

    def read_registry(self):
        return [], []

    def read_overrides(self):
        return {}, []

    def read_listing_records(self):
        return {}

    def last_source_counts(self):
        return self._counts


def test_previous_counts_keep_only_the_names_of_the_adapters_and_count_the_rest(caplog):
    # The names come from the Sources cell of Scrape_Run_Logs, which a person can edit, and the Actions log is public.
    counts = {"dld_projects": 100, "seed_csv": 0, "ZZNAMEZZ-Alpha Residences": 7, "ZZNAMEZZ-Beta": 3, "dld_projects ": 5}
    with caplog.at_level(logging.INFO, logger="src.main"):
        state = app.read_sheet_state(StubSheets(counts), app.Report([]))
    assert state.previous_counts == {"dld_projects": 100, "seed_csv": 0}
    assert "previous counts {'dld_projects': 100, 'seed_csv': 0} (+3 for names this code does not know, not shown)" in caplog.text
    assert "ZZNAMEZZ" not in caplog.text and "Alpha" not in caplog.text and "Beta" not in caplog.text


def test_previous_counts_with_only_known_names_log_without_the_extra_note(caplog):
    with caplog.at_level(logging.INFO, logger="src.main"):
        state = app.read_sheet_state(StubSheets({"srerd_projects": 4}), app.Report([]))
    assert state.previous_counts == {"srerd_projects": 4}
    assert "previous counts {'srerd_projects': 4}" in caplog.text and "not shown" not in caplog.text


def test_an_unreadable_listing_tab_fails_the_run_but_not_the_data(tmp_path, monkeypatch):
    env = make_env(tmp_path)

    def broken():
        raise SheetError("Listing_Data: Google rejected the request (HTTP 403)")

    monkeypatch.setattr(env.mgr, "read_listing_records", broken)
    assert go(env, feeds(dld_rows=[dld(1)])) == 1
    assert set(env.registry()) == {"dld:dubai:100001"}


def test_a_merge_failure_blocks_the_registry_write_and_nothing_else(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    before = env.dump(TAB_REGISTRY)

    def broken(fresh, existing):
        raise RuntimeError("merge exploded")

    monkeypatch.setattr(app, "merge_feeds", broken)
    assert go(env, feeds(dld_rows=[dld(2, day=LATER)], candidates=[candidate()]), today=LATER) == 1
    assert env.dump(TAB_REGISTRY) == before
    assert len(env.records(TAB_CANDIDATES)) == 1 and env.last_log()["Exit Code"] == "1"


# ---------------------------------------------------------------------------
# Geocode stage
# ---------------------------------------------------------------------------
def test_geocoding_resolves_rows_flushes_and_writes_map_pins(tmp_path):
    env = make_env(tmp_path, geocode_enabled=True)
    geocode, session = real_geocoder(env.cfg, tmp_path, hit_everything)
    assert go(env, feeds(dld_rows=[dld(1), dld(2)]), geocode=geocode) == 0
    assert len(session.calls) >= 2
    registry = env.registry()
    assert all(r["Latitude"] and r["Longitude"] and r["Location Precision"] for r in registry.values())
    assert {p["Project Key"] for p in env.records(TAB_PINS)} == set(registry)
    log = env.last_log()
    assert log["Map Pins"] == "2" and json.loads(log["Geocode"])["resolved"] == 2


def test_a_geocoder_crash_persists_the_pre_geocode_merge_and_fails_the_run(tmp_path, caplog):
    env = make_env(tmp_path)

    def crash(rows, overrides, cfg, *, today, flush):
        raise RuntimeError(f"provider client blew up with key={KEY_LIQ}")

    assert go(env, feeds(dld_rows=[dld(1), dld(2)], candidates=[candidate()]), geocode=crash) == 1
    assert set(env.registry()) == {"dld:dubai:100001", "dld:dubai:100002"}
    assert len(env.records(TAB_CANDIDATES)) == 1
    log = env.last_log()
    assert log["Exit Code"] == "1" and json.loads(log["Geocode"]) == {"error": "RuntimeError"}
    assert "the geocode stage crashed (RuntimeError" in caplog.text
    assert_no_secrets(all_text(env, caplog))


def test_a_crash_after_a_partial_flush_keeps_the_flushed_progress(tmp_path):
    env = make_env(tmp_path)

    def partial_then_crash(rows, overrides, cfg, *, today, flush):
        pinned = [
            r.model_copy(update=dict(latitude=DOWNTOWN[0], longitude=DOWNTOWN[1], location_precision="exact", geocode_status="resolved"))
            for r in rows
        ]
        flush(pinned)
        raise RuntimeError("died after the first flush")

    assert go(env, feeds(dld_rows=[dld(1)]), geocode=partial_then_crash) == 1
    row = env.registry()["dld:dubai:100001"]
    assert row["Latitude"] == "25.1972" and row["Location Precision"] == "exact"
    assert env.last_log()["Map Pins"] == "1"


def test_a_geocoder_returning_the_wrong_number_of_rows_is_treated_as_a_crash(tmp_path):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1), dld(2)]), geocode=lambda rows, ov, cfg, **kw: ([], GeocodeStats(backend="x"))) == 1
    assert len(env.registry()) == 2  # the merge survived


def test_a_geocoder_returning_junk_rows_is_treated_as_a_crash(tmp_path):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)]), geocode=lambda rows, ov, cfg, **kw: (["junk"], GeocodeStats(backend="x"))) == 1
    assert set(env.registry()) == {"dld:dubai:100001"}


def test_a_geocode_abort_is_not_an_error(tmp_path, caplog):
    env = make_env(tmp_path, geocode_enabled=True)
    geocode, session = real_geocoder(env.cfg, tmp_path, lambda call: FakeResponse(429, {}))
    assert go(env, feeds(dld_rows=[dld(1), dld(2), dld(3)]), geocode=geocode) == 0
    assert json.loads(env.last_log()["Geocode"])["aborted"] == "HTTP 429"
    assert any("geocoding aborted (HTTP 429)" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    assert set(env.registry()) == {"dld:dubai:100001", "dld:dubai:100002", "dld:dubai:100003"}
    assert "RUN FAILED" not in caplog.text


@pytest.mark.parametrize("status", [401, 402, 403])
def test_a_refused_geocoder_key_fails_the_run_but_everything_else_persists(tmp_path, caplog, status):
    # Unlike a quota stop, nothing fixes a revoked key by itself: the run must not stay green.
    env = make_env(tmp_path, geocode_enabled=True)
    geocode, session = real_geocoder(env.cfg, tmp_path, lambda call: FakeResponse(status, {}))
    assert go(env, feeds(dld_rows=[dld(1), dld(2)]), geocode=geocode) == 1
    assert json.loads(env.last_log()["Geocode"])["aborted"] == f"HTTP {status}"
    assert env.last_log()["Exit Code"] == "1"
    assert f"geocoding aborted (HTTP {status})" in caplog.text
    assert "Not an error" not in caplog.text
    assert set(env.registry()) == {"dld:dubai:100001", "dld:dubai:100002"}


def test_a_geocoder_that_keeps_failing_fails_the_run(tmp_path, caplog):
    env = make_env(tmp_path, geocode_enabled=True)
    geocode, session = real_geocoder(env.cfg, tmp_path, lambda call: FakeResponse(500, {}))
    rows = [dld(i) for i in range(1, 9)]
    assert go(env, feeds(dld_rows=rows), geocode=geocode) == 1
    assert "consecutive backend failures" in json.loads(env.last_log()["Geocode"])["aborted"]
    assert len(env.registry()) == 8


def test_a_budget_stop_is_not_an_error(tmp_path, caplog):
    env = make_env(tmp_path)

    def stopped(rows, overrides, cfg, *, today, flush):
        return list(rows), GeocodeStats(backend="fake", stop_reason="lookup budget", skipped_over_budget=2)

    assert go(env, feeds(dld_rows=[dld(1)]), geocode=stopped) == 0
    assert "geocoding stopped early (lookup budget)" in caplog.text
    assert json.loads(env.last_log()["Geocode"])["stop_reason"] == "lookup budget"


def test_a_geocoder_configuration_error_fails_the_run_but_everything_else_persists(tmp_path, caplog):
    env = make_env(tmp_path, geocode_enabled=True, geocode_backend="google")  # google without a key
    assert go(env, feeds(dld_rows=[dld(1)], candidates=[candidate()])) == 1  # the real geocode_pending
    assert set(env.registry()) == {"dld:dubai:100001"}
    assert len(env.records(TAB_CANDIDATES)) == 1
    log = env.last_log()
    assert log["Exit Code"] == "1" and json.loads(log["Geocode"]) == {"error": "GeocoderConfigError"}
    assert "geocoding is misconfigured" in caplog.text


def test_a_geocoder_config_error_keeps_what_the_stage_flushed(tmp_path):
    env = make_env(tmp_path)

    def misconfigured(rows, overrides, cfg, *, today, flush):
        flush([r.model_copy(update=dict(latitude=25.2, longitude=55.3, location_precision="community")) for r in rows])
        raise GeocoderConfigError("GEOCODE_BACKEND=google needs GOOGLE_MAPS_API_KEY")

    assert go(env, feeds(dld_rows=[dld(1)]), geocode=misconfigured) == 1
    assert env.registry()["dld:dubai:100001"]["Location Precision"] == "community"


def test_the_geocode_flush_goes_through_the_registry_write_guard(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    written: list[list[str]] = []
    original = env.mgr.write_registry

    def spy(rows):
        rows = list(rows)
        written.append([r.latitude for r in rows])
        original(rows)

    monkeypatch.setattr(env.mgr, "write_registry", spy)

    def geo(rows, overrides, cfg, *, today, flush):
        pinned = [r.model_copy(update=dict(latitude=25.2, longitude=55.3, location_precision="exact")) for r in rows]
        flush(pinned)
        return pinned, GeocodeStats(backend="fake", resolved=len(pinned))

    assert go(env, feeds(dld_rows=[dld(1)]), geocode=geo) == 0
    assert written == [[25.2]]  # one flush; the identical final write cost no second request
    assert env.registry()["dld:dubai:100001"]["Latitude"] == "25.2"


def test_a_failing_flush_does_not_stop_the_final_write(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    calls = {"n": 0}
    original = env.mgr.write_registry

    def flaky(rows):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SheetError("transient write failure")
        original(rows)

    monkeypatch.setattr(env.mgr, "write_registry", flaky)
    result = go(env, feeds(dld_rows=[dld(1)]))
    assert result == 0 and calls["n"] == 2  # geocoding off: one flush (failed, swallowed), then the final write
    assert set(env.registry()) == {"dld:dubai:100001"}


def test_geocoding_is_called_with_lookups_off_when_the_registry_is_blocked(tmp_path):
    env = make_env(tmp_path, geocode_enabled=True)
    first_run_geocode, _ = real_geocoder(env.cfg, tmp_path, hit_everything)  # no first-run lookup may reach a real host
    assert go(env, feeds(dld_rows=[dld(1)]), geocode=first_run_geocode) == 0
    bad_registry_row(env)
    session_calls: list[dict] = []

    def never(call):
        session_calls.append(call)
        return FakeResponse(200, [])

    geocode, session = real_geocoder(env.cfg, tmp_path, never)
    # dld(1) is already pinned by run 1, so only the coordinate-less dld(2) could cost a lookup: if lookups were
    # switched on for a blocked registry, this is the row that would reach the (fake) host
    assert go(env, feeds(dld_rows=[dld(1, day=LATER), dld(2, day=LATER)]), today=LATER, geocode=geocode) == 1
    assert session.calls == []


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method", ["write_registry", "add_candidates", "write_map_pins"])
def test_one_failed_write_never_costs_the_others(tmp_path, monkeypatch, method):
    env = make_env(tmp_path)

    def broken(*a, **k):
        raise SheetError(f"{method}: Google rejected the request (HTTP 400)")

    monkeypatch.setattr(env.mgr, method, broken)
    assert go(env, feeds(dld_rows=[dld(1)], candidates=[candidate()])) == 1
    assert (len(env.registry()) == 1) is (method != "write_registry")
    assert (len(env.records(TAB_CANDIDATES)) == 1) is (method != "add_candidates")
    assert env.snapshot_data()["registry_rows"] == 1  # the snapshot is written whatever the sheet did
    assert env.last_log()["Exit Code"] == "1"


def test_a_failed_snapshot_write_still_writes_the_sheet_and_the_run_log(tmp_path):
    env = make_env(tmp_path)
    blocker = tmp_path / "is_a_directory"
    blocker.mkdir()
    assert go(env, feeds(dld_rows=[dld(1)]), snapshot_path=blocker) == 1
    assert set(env.registry()) == {"dld:dubai:100001"} and env.last_log()["Exit Code"] == "1"
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_the_run_log_is_written_even_when_every_source_failed(tmp_path):
    env = make_env(tmp_path)
    adapters = [FakeAdapter("dld_projects", error=SourceError("dld_projects: down")), FakeAdapter("srerd_projects", error=KeyError("k"))]
    assert go(env, adapters) == 1
    log = env.last_log()
    assert log["Exit Code"] == "1" and log["Registry Rows"] == "0"
    assert {n: i["status"] for n, i in source_info(log).items()} == {"dld_projects": "failed", "srerd_projects": "failed"}
    assert "dld_projects: FAILED" in log["Notes"]


def test_a_run_log_append_failure_is_logged_and_keeps_the_exit_code(tmp_path, caplog):
    env = make_env(tmp_path)
    env.client.inject("append_rows", 400, tab=TAB_RUN_LOGS, times=None)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0  # a clean run stays clean
    assert "could not append the run log" in caplog.text
    assert set(env.registry()) == {"dld:dubai:100001"}

    env2 = make_env(tmp_path / "failing")
    (tmp_path / "failing").mkdir()
    env2.client.inject("append_rows", 400, tab=TAB_RUN_LOGS, times=None)
    assert go(env2, [FakeAdapter("dld_projects", error=SourceError("down"))]) == 1  # and a failing run stays failing


def test_the_run_log_records_the_git_sha_and_the_exit_code_it_returns(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "feedfacefeedfacefeedfacefeedfacefeedface")
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    assert env.last_log()["Git SHA"] == "feedfacefeed"
    assert env.snapshot_data()["git_sha"] == "feedfacefeed"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------
def snapshot_of_sheet(env: Env) -> dict[str, list[list[str]]]:
    return {title: env.sheet.tab(title).dump() for title in env.sheet.titles()}


def test_a_dry_run_writes_nothing(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(app, "read_seed", lambda *a, **k: [seed_line("dld:dubai:100001"), seed_line("bad-key")])
    env = make_env(tmp_path, geocode_enabled=True)
    first_run_geocode, _ = real_geocoder(env.cfg, tmp_path, hit_everything)  # no first-run lookup may reach a real host
    assert go(env, feeds(dld_rows=[dld(1), dld(2)], candidates=[candidate()]), geocode=first_run_geocode) == 1  # the bad seed row fails it
    sheet_before = snapshot_of_sheet(env)
    env.snapshot.unlink()
    env.client.reset_calls()
    caplog.clear()

    geocode, session = real_geocoder(env.cfg, tmp_path, hit_everything)
    adapters = feeds(dld_rows=[dld(1, day=LATER), dld(3, day=LATER)], candidates=[candidate("Fake Launch Two")])
    assert go(env, adapters, today=LATER, dry_run=True, geocode=geocode) == 1  # still reports the bad seed row
    assert snapshot_of_sheet(env) == sheet_before
    assert env.writes() == []
    assert set(env.client.methods()) <= {"open_by_key", "worksheets", "get_all_values", "row_values"}
    assert not env.snapshot.exists()
    assert len(env.records(TAB_RUN_LOGS)) == 1  # no run-log row for a dry run
    assert session.calls == []  # no lookups
    assert "dry run" in caplog.text.lower()


def test_a_clean_dry_run_is_green(tmp_path):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    before = snapshot_of_sheet(env)
    assert go(env, feeds(dld_rows=[dld(1, day=LATER), dld(2, day=LATER)]), today=LATER, dry_run=True) == 0
    assert snapshot_of_sheet(env) == before


def test_a_dry_run_does_not_add_seed_rows_the_sheet_does_not_have_yet(tmp_path, monkeypatch, caplog):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0  # a normal run first, with an empty seed
    monkeypatch.setattr(app, "read_seed", lambda *a, **k: [seed_line("dld:dubai:100001")])
    before = snapshot_of_sheet(env)
    env.client.reset_calls()
    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER, dry_run=True) == 0
    assert env.records(TAB_LISTINGS) == []
    assert snapshot_of_sheet(env) == before
    assert env.writes() == []
    assert "was not applied" in caplog.text


def test_a_dry_run_still_applies_overrides_in_memory_only(tmp_path):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    env.sheet.tab(TAB_OVERRIDES).put([["dld:dubai:100001", "25.2", "55.3", "exact", ""]], start_row=2)
    before = snapshot_of_sheet(env)
    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER, dry_run=True) == 0
    assert snapshot_of_sheet(env) == before


def test_a_dry_run_without_a_spreadsheet_id_is_refused_because_open_could_create_one(tmp_path):
    env = make_env(tmp_path, spreadsheet_id="")
    assert go(env, feeds(dld_rows=[dld(1)]), dry_run=True) == 2
    assert env.client.calls == []


# ---------------------------------------------------------------------------
# Setup failures (exit 2)
# ---------------------------------------------------------------------------
def test_a_spreadsheet_that_cannot_be_opened_is_a_setup_failure(tmp_path, caplog):
    env = make_env(tmp_path)
    env.client.forbidden_ids.add(SHEET_ID)
    adapters = feeds(dld_rows=[dld(1)])
    assert go(env, adapters) == 2
    assert adapters[0].fetches == 0 and not env.snapshot.exists()
    assert "cannot open the spreadsheet" in caplog.text and "no sheet to write a run log to" in caplog.text
    assert env.writes() == []
    assert_no_secrets(caplog.text)


def test_an_unexpected_error_while_opening_is_scrubbed_and_is_a_setup_failure(tmp_path, monkeypatch, caplog):
    env = make_env(tmp_path)

    def broken():
        raise RuntimeError(f"auth library says key={KEY_GEMINI} and {PRIVATE_KEY_ID}")

    monkeypatch.setattr(env.mgr, "open", broken)
    assert go(env, feeds(dld_rows=[dld(1)])) == 2
    assert "RuntimeError" in caplog.text
    assert_no_secrets(caplog.text)


def test_a_failure_to_build_the_adapters_is_a_setup_failure(tmp_path, monkeypatch, caplog):
    env = make_env(tmp_path)

    def broken(cfg, **kw):
        raise ValueError(f"cannot construct with {KEY_DUBAI}")

    monkeypatch.setattr(app, "build_adapters", broken)
    assert app.run(env.cfg, sheets=env.mgr, today=TODAY, snapshot_path=env.snapshot) == 2
    assert env.client.calls == []
    assert_no_secrets(caplog.text)


def test_run_builds_its_own_sheet_manager_and_a_bad_inline_key_is_a_clean_exit_2(tmp_path, caplog):
    cfg = Config(gcp_service_account_key="fake-not-json-key-0000", spreadsheet_id=SHEET_ID, geocode_enabled=False)
    assert app.run(cfg, adapters=feeds(dld_rows=[dld(1)]), today=TODAY, snapshot_path=tmp_path / "s.json") == 2
    assert "fake-not-json-key-0000" not in caplog.text and "SheetError" in caplog.text


# ---------------------------------------------------------------------------
# Hostile input
# ---------------------------------------------------------------------------
HOSTILE_NAME = '=HYPERLINK("https://evil.example.invalid/?x="&A1,"Fake Tower")'
HOSTILE_DEV = "+cmd|' /C calc'!A0"
HOSTILE_AREA = "@SUM(1+1)*cmd|' /C notepad'!Z0"
UNICODE_NAME = "برج الاختبار \u202e\u200b 🏙️ \u0000 Fake Ünïcode \u202e"


def hostile_rows() -> list[RegistryRow]:
    return [
        dld(1, project_name_en=HOSTILE_NAME, developer=HOSTILE_DEV, area_en=HOSTILE_AREA,
            latitude=DOWNTOWN[0], longitude=DOWNTOWN[1], location_precision="exact"),
        dld(2, project_name_en=UNICODE_NAME, project_name_ar=UNICODE_NAME),
        dld(3, project_name_en="X" * 9_000, status="-" + "Y" * 3_000),
        dld(4, project_name_en="Fake Tower 4", latitude=0.0, longitude=0.0, location_precision="exact"),  # null island
        dld(5, project_name_en="Fake Tower 5", latitude=55.2744, longitude=25.1972, location_precision="exact"),  # swapped
        dld(6, project_name_en="Z" * 12_000),  # past the cap: dropped, counted
    ]


def test_hostile_text_stays_inert_text_everywhere(tmp_path, caplog):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=hostile_rows())) == 0

    # every request that wrote cell text said RAW, so Sheets never parses a formula
    for call in env.writes():
        if call.method in ("update", "append_rows"):
            assert call.detail["option"] == "RAW"

    registry = env.registry()
    first = registry["dld:dubai:100001"]
    assert first["Project Name (EN)"] == HOSTILE_NAME and first["Developer"] == HOSTILE_DEV and first["Area (EN)"] == HOSTILE_AREA
    assert not any(v.startswith("#") for r in registry.values() for v in r.values())  # no #NAME? or #ERROR!
    assert "dld:dubai:100006" not in registry  # the 12,000 character row never reached the sheet
    assert len(registry["dld:dubai:100003"]["Project Name (EN)"]) == 9_000
    assert "dropped 1 row(s)" in source_info(env.last_log())["dld_projects"]["note"]

    # the map: only plausible UAE coordinates are pinned, the link carries numbers and never the name
    pins = {p["Project Key"]: p for p in env.records(TAB_PINS)}
    assert set(pins) == {"dld:dubai:100001"}
    assert pins["dld:dubai:100001"]["Project Name"] == HOSTILE_NAME
    assert pins["dld:dubai:100001"]["Map Link"] == "https://www.google.com/maps/search/?api=1&query=25.1972,55.2744"

    # the snapshot is data: ASCII JSON that round-trips the exact text
    raw = env.snapshot.read_bytes()
    assert raw.isascii()
    rows = {r["Project Key"]: r for r in json.loads(raw)["rows"]}
    assert rows["dld:dubai:100001"]["Project Name (EN)"] == HOSTILE_NAME
    assert rows["dld:dubai:100001"]["Developer"] == HOSTILE_DEV

    # and it survives a second run unchanged (read back from the sheet, merged, written again)
    assert go(env, feeds(dld_rows=hostile_rows()), today=LATER) == 0
    assert env.registry()["dld:dubai:100001"]["Project Name (EN)"] == HOSTILE_NAME
    assert "evil.example.invalid" not in caplog.text and "HYPERLINK" not in caplog.text


def test_hostile_text_never_reaches_the_logs(tmp_path, caplog):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=hostile_rows())) == 0
    log_text = caplog.text
    for fragment in ("HYPERLINK", "calc", "notepad", "Fake Ünïcode", "XXXXXXXX"):
        assert fragment not in log_text


def test_a_registry_row_with_a_formula_in_the_sheet_is_read_as_text_and_kept(tmp_path):
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    append_registry_cells(
        env, **{"Emirate": "Dubai", "Registry": "DLD", "Registry Project No.": "100050",
                "Project Name (EN)": HOSTILE_NAME, "Developer": HOSTILE_DEV, "First Seen": "2026-10-01", "Last Seen": "2026-10-01"},
    )
    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER) == 0
    assert env.registry()["dld:dubai:100050"]["Project Name (EN)"] == HOSTILE_NAME


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
def test_no_secret_reaches_any_log_cell_or_snapshot_on_failure_paths(tmp_path, caplog, capsys):
    env = make_env(tmp_path, geocode_enabled=True)
    leaky = (
        f"HTTP 401 for https://api.example.invalid/token?client_secret={SECRET_DUBAI}&key={KEY_DUBAI} "
        f"Authorization: Bearer {KEY_GEMINI} contact {CONTACT} sheet {SHEET_ID} {PEM_LINE_ONE} {PRIVATE_KEY_ID}"
    )
    adapters = [
        FakeAdapter("dld_projects", error=RuntimeError(leaky)),
        FakeAdapter("srerd_projects", error=SourceError(leaky)),
        FakeAdapter("gemini_news", [], expects_rows=False, error=ValueError(leaky)),
        FakeAdapter("seed_csv", [seed_row()], expects_rows=False),
    ]

    def crash(rows, overrides, cfg, *, today, flush):
        raise RuntimeError(leaky + KEY_LIQ)

    assert go(env, adapters, geocode=crash) == 1
    text = all_text(env, caplog, capsys)
    assert_no_secrets(text)
    assert "RuntimeError" in text  # the class name survives; only the secrets go


def test_no_secret_reaches_the_log_when_the_sheet_layer_fails(tmp_path, monkeypatch, caplog, capsys):
    env = make_env(tmp_path)

    def broken(*a, **k):
        raise SheetError(f"Registry_Projects: rejected key={KEY_GEMINI} {PEM_LINE_TWO} {SHEET_ID}")

    monkeypatch.setattr(env.mgr, "write_registry", broken)
    monkeypatch.setattr(env.mgr, "read_listing_records", broken)
    assert go(env, feeds(dld_rows=[dld(1)])) == 1
    assert_no_secrets(all_text(env, caplog, capsys))


def public_text(caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture) -> str:
    """What a stranger reading the public Actions log could see: every log record, stdout and stderr."""
    out = capsys.readouterr()
    return "\n".join([caplog.text, out.out, out.err])


@pytest.mark.parametrize(
    "error",
    [UnrecognisedError(LEAK), KeyError(LEAK), ValueError(f"bad cell {LEAK}"), OSError(f"[Errno 2] No such file: '{LEAK}'")],
    ids=["unrecognised", "key-error", "value-error", "os-error"],
)
@pytest.mark.parametrize(
    "method",
    ["read_registry", "read_overrides", "read_listing_records", "last_source_counts", "apply_listing_seed",
     "write_registry", "add_candidates", "write_map_pins", "append_run_log"],
)
def test_the_text_of_an_unplanned_sheet_error_reaches_no_public_output(tmp_path, monkeypatch, caplog, capsys, method, error):
    env = make_env(tmp_path)

    def boom(*a, **k):
        raise error

    monkeypatch.setattr(env.mgr, method, boom)
    with caplog.at_level(logging.DEBUG):
        go(env, feeds(dld_rows=[dld(1)], candidates=[candidate()]))
    text = public_text(caplog, capsys)
    assert "MARKER-LEAK" not in text and "Alpha Residences" not in text
    assert f"{type(error).__name__} (at test_main.py:" in text and "in boom" in text, "the class and the place stay"


def test_the_text_of_an_unplanned_source_error_stays_in_the_private_note_only(tmp_path, caplog, capsys):
    env = make_env(tmp_path)
    adapters = [
        FakeAdapter("dld_projects", error=KeyError(LEAK)),
        FakeAdapter("srerd_projects", error=UnrecognisedError(f"HTTP 500 body: {LEAK}")),
        FakeAdapter("seed_csv", [seed_row()], expects_rows=False),
    ]
    with caplog.at_level(logging.DEBUG):
        assert go(env, adapters) == 1
    text = public_text(caplog, capsys)
    assert "MARKER-LEAK" not in text and "Alpha Residences" not in text
    assert "dld_projects: FAILED" in text and "KeyError (at " in text and "UnrecognisedError (at " in text
    log = env.last_log()
    assert "MARKER-LEAK" not in log["Notes"], "the failure lines in the run-log Notes are the public ones"
    notes = {name: info.get("note", "") for name, info in source_info(log).items()}
    assert "MARKER-LEAK" in notes["dld_projects"] and "MARKER-LEAK" in notes["srerd_projects"], "the private note keeps the text"


def test_the_text_of_an_unplanned_geocode_or_snapshot_error_reaches_no_public_output(tmp_path, caplog, capsys):
    env = make_env(tmp_path, geocode_enabled=True)
    blocker = tmp_path / "MARKER-LEAK-not-a-folder"
    blocker.write_text("x", encoding="utf-8")

    def crash(rows, overrides, cfg, *, today, flush):
        raise UnrecognisedError(LEAK)

    with caplog.at_level(logging.DEBUG):
        assert go(env, feeds(dld_rows=[dld(1)]), geocode=crash, snapshot_path=blocker / "snapshot.json") == 1
    text = public_text(caplog, capsys)
    assert "MARKER-LEAK" not in text and "Alpha Residences" not in text
    assert "the geocode stage crashed (UnrecognisedError (at test_main.py:" in text
    assert "writing the snapshot failed (" in text


def test_a_failed_geocode_flush_logs_only_the_class_name(tmp_path, monkeypatch, caplog, capsys):
    env = make_env(tmp_path, geocode_enabled=False)

    def boom(*a, **k):
        raise UnrecognisedError(LEAK)

    monkeypatch.setattr(env.mgr, "write_registry", boom)
    with caplog.at_level(logging.DEBUG):
        go(env, feeds(dld_rows=[dld(1)]))
    text = public_text(caplog, capsys)
    assert "geocode flush failed (FlushFailed)" in text
    assert "MARKER-LEAK" not in text


def test_the_text_of_an_unplanned_seed_file_error_reaches_no_public_output(tmp_path, monkeypatch, caplog, capsys):
    def broken_seed(*a, **k):
        raise ValueError(f"no Project Key column in {LEAK}")

    monkeypatch.setattr(app, "read_seed", broken_seed)
    env = make_env(tmp_path)
    with caplog.at_level(logging.DEBUG):
        assert go(env, feeds(dld_rows=[dld(1)])) == 1
    text = public_text(caplog, capsys)
    assert "could not read the Listing_Data seed file (ValueError (at test_main.py:" in text
    assert "MARKER-LEAK" not in text


def test_a_seed_file_without_a_key_column_is_reported_by_its_fixed_message_and_nothing_else(tmp_path, monkeypatch, caplog, capsys):
    # The real read_seed on a real file: the header cell and the file name carry a marker, the message must not.
    seed = tmp_path / "seed-MARKER-LEAK-file.csv"
    seed.write_text("MARKER-LEAK-Column,Developer\nMARKER-LEAK Alpha Residences,Imaginary\n", encoding="utf-8")
    monkeypatch.setattr(app, "read_seed", lambda *a, **k: real_read_seed(seed))
    env = make_env(tmp_path)
    with caplog.at_level(logging.DEBUG):
        assert go(env, feeds(dld_rows=[dld(1)])) == 1
    text = public_text(caplog, capsys)
    # Before: "ValueError (at listing_data.py:N in read_seed)", which hid the one thing an operator needs to know.
    assert (
        "could not read the Listing_Data seed file "
        "(SeedFileError: the Listing_Data seed file has no 'Project Key' column in its header row)"
    ) in text
    assert "MARKER-LEAK" not in text and "ValueError (at " not in text


def test_only_the_seed_files_own_error_shows_its_text():
    own = app.describe_error(SeedFileError("the Listing_Data seed file has no 'Project Key' column in its header row"))
    assert own == "SeedFileError: the Listing_Data seed file has no 'Project Key' column in its header row"
    other = app.describe_error(ValueError("the Listing_Data seed file has no 'Project Key' column in its header row"))
    assert other.startswith("ValueError (at ") and "Project Key" not in other  # a plain ValueError stays hidden


def test_the_startup_log_shows_only_the_safe_description_of_the_config(tmp_path, caplog):
    env = make_env(tmp_path, require_sources=("dld_projects",))
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    text = caplog.text
    assert "config GEMINI_API_KEY: set" in text and "config REQUIRE_SOURCES: dld_projects" in text
    assert "Config(" not in text and "gemini_api_key=" not in text  # never repr(cfg)
    assert_no_secrets(text)


def test_config_warnings_are_logged_scrubbed(tmp_path, caplog):
    env = make_env(tmp_path, warnings=(f"GEOCODE_MAX_PER_RUN='abc' is not a whole number {KEY_DUBAI}",))
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    assert "config warning: GEOCODE_MAX_PER_RUN" in caplog.text and KEY_DUBAI not in caplog.text


def test_the_geocode_contact_in_the_user_agent_is_not_logged(tmp_path, caplog):
    cfg = load_config(
        {"GEOCODE_CONTACT": CONTACT, "SPREADSHEET_ID": SHEET_ID, "GEOCODE_ENABLED": "false", "GCP_SERVICE_ACCOUNT_KEY": SA_JSON}
    )
    assert CONTACT in cfg.geocode_user_agent  # the geocoder sends it ...
    # Deliberately changed: this used to assert that describe() echoed the contact ("the config layer echoes it; main
    # must not"), leaving the scrubber as the only guard. The description no longer holds it, so the log cannot.
    assert CONTACT not in "\n".join(cfg.describe().values())
    env = make_env(tmp_path)
    env.cfg = cfg
    env.mgr = SheetManager(cfg, client=env.client, sleep=lambda s: None)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    assert CONTACT not in caplog.text


# ---------------------------------------------------------------------------
# main(argv)
# ---------------------------------------------------------------------------
class SpyRun:
    def __init__(self, result: int = 0):
        self.result = result
        self.cfg: Optional[Config] = None
        self.kwargs: dict[str, Any] = {}

    def __call__(self, cfg, **kwargs):
        self.cfg, self.kwargs = cfg, kwargs
        return self.result


class MarkerSheets:
    def __init__(self, cfg):
        self.cfg = cfg


def patch_main(monkeypatch, cfg: Config, *, adapters: Optional[list] = None, run: Optional[SpyRun] = None) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_load(env=None, *, env_file=None):
        seen["env_file"] = env_file
        return cfg

    def fake_build(c, *, session=None, today=None):
        seen["adapters_cfg"], seen["today"] = c, today
        return adapters if adapters is not None else []

    monkeypatch.setattr(app, "load_config", fake_load)
    monkeypatch.setattr(app, "SheetManager", MarkerSheets)
    monkeypatch.setattr(app, "build_adapters", fake_build)
    if run is not None:
        monkeypatch.setattr(app, "run", run)
    return seen


def test_main_wires_the_real_collaborators_into_run(monkeypatch, tmp_path):
    cfg = Config(spreadsheet_id=SHEET_ID, geocode_enabled=True)
    spy = SpyRun(result=1)
    marker = [FakeAdapter("dld_projects", [dld(1)])]
    seen = patch_main(monkeypatch, cfg, adapters=marker, run=spy)
    assert app.main(["--snapshot", str(tmp_path / "s.json")]) == 1  # run's code is main's code
    assert spy.cfg is cfg and spy.kwargs["adapters"] is marker
    assert isinstance(spy.kwargs["sheets"], MarkerSheets) and spy.kwargs["sheets"].cfg is cfg
    assert spy.kwargs["snapshot_path"] == tmp_path / "s.json" and spy.kwargs["dry_run"] is False
    assert isinstance(spy.kwargs["today"], date) and seen["today"] == spy.kwargs["today"]
    assert seen["adapters_cfg"] is cfg and seen["env_file"] is None


def test_main_flags(monkeypatch, tmp_path):
    cfg = Config(spreadsheet_id=SHEET_ID, geocode_enabled=True)
    spy = SpyRun()
    env_file = tmp_path / "x.env"
    env_file.write_text("GOOGLE_SHEET_NAME=Whatever\n", encoding="utf-8")
    seen = patch_main(monkeypatch, cfg, run=spy)
    assert app.main(["--dry-run", "--no-geocode", "--env-file", str(env_file), "-v"]) == 0
    assert spy.kwargs["dry_run"] is True and spy.kwargs["snapshot_path"] is None
    assert spy.cfg.geocode_enabled is False and cfg.geocode_enabled is True  # replaced, not mutated
    assert seen["env_file"] == env_file
    assert logging.getLogger("src").level == logging.DEBUG


def test_main_default_logging_is_info_and_one_handler(monkeypatch):
    patch_main(monkeypatch, Config(), run=SpyRun())
    app.main([])
    app.main([])
    root = logging.getLogger()
    assert logging.getLogger("src").level == logging.INFO and root.level > logging.CRITICAL  # only our loggers speak
    assert len([h for h in root.handlers if getattr(h, app._LOG_HANDLER_MARK, False)]) == 1


def test_main_installs_the_log_safe_excepthook_and_only_when_it_runs(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(sys, "excepthook", sentinel)
    patch_main(monkeypatch, Config(), run=SpyRun())
    assert sys.excepthook is sentinel  # patching and importing change nothing
    assert app.main([]) == 0
    assert sys.excepthook is log_policy.excepthook


def test_main_installs_the_hook_before_it_parses_the_command_line(monkeypatch, capsys):
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    assert app.main(["--definitely-not-a-flag"]) == 2
    capsys.readouterr()
    assert sys.excepthook is log_policy.excepthook


def test_main_with_a_missing_env_file_is_a_setup_failure(monkeypatch, tmp_path, capsys):
    called: list[str] = []
    monkeypatch.setattr(app, "load_config", lambda *a, **k: called.append("load") or Config())
    assert app.main(["--env-file", str(tmp_path / "nope.env")]) == 2
    assert called == []
    assert "does not exist" in capsys.readouterr().err


def test_main_with_a_bad_flag_returns_2_and_help_returns_0(capsys):
    assert app.main(["--definitely-not-a-flag"]) == 2
    assert "usage" in capsys.readouterr().err.lower()
    assert app.main(["-h"]) == 0
    out = capsys.readouterr().out
    for flag in ("--dry-run", "--no-geocode", "--snapshot", "--env-file", "-v"):
        assert flag in out


def test_main_survives_a_configuration_error(monkeypatch, capsys):
    def broken(*a, **k):
        raise ValueError(f"bad value {KEY_DUBAI}")

    monkeypatch.setattr(app, "load_config", broken)
    assert app.main([]) == 2
    err = capsys.readouterr().err
    assert "could not load the configuration" in err and "ValueError" in err


def test_main_survives_a_failure_building_the_sheet_manager(monkeypatch, capsys):
    cfg = Config(data_dubai_api_key=KEY_DUBAI)
    patch_main(monkeypatch, cfg, run=SpyRun())

    def broken(c):
        raise RuntimeError(f"cannot authorise with {KEY_DUBAI}")

    monkeypatch.setattr(app, "SheetManager", broken)
    assert app.main([]) == 2
    err = capsys.readouterr().err
    assert "could not set up the run" in err and KEY_DUBAI not in err


def test_main_turns_an_escaped_exception_into_exit_1(monkeypatch, capsys):
    cfg = Config(data_dubai_api_key=KEY_DUBAI)

    def exploding(c, **kw):
        raise RuntimeError(f"unexpected {KEY_DUBAI}")

    patch_main(monkeypatch, cfg, run=exploding)
    assert app.main([]) == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err and KEY_DUBAI not in err


def test_main_never_prints_the_text_of_an_unplanned_error_on_any_of_its_three_failure_paths(monkeypatch, capsys):
    cfg = Config(data_dubai_api_key=KEY_DUBAI)

    def refuse(*a, **k):
        raise UnrecognisedError(f"{LEAK} {KEY_DUBAI}")

    def explode(c, **kw):
        raise KeyError(LEAK)

    patch_main(monkeypatch, cfg, run=explode)
    monkeypatch.setattr(app, "load_config", refuse)
    assert app.main([]) == 2
    first = capsys.readouterr().err
    patch_main(monkeypatch, cfg, run=explode)
    monkeypatch.setattr(app, "SheetManager", refuse)
    assert app.main([]) == 2
    second = capsys.readouterr().err
    patch_main(monkeypatch, cfg, run=explode)
    assert app.main([]) == 1
    third = capsys.readouterr().err
    assert "could not load the configuration (UnrecognisedError (at test_main.py:" in first
    assert "could not set up the run (UnrecognisedError (at test_main.py:" in second
    assert "the run stopped on an unexpected error (KeyError (at test_main.py:" in third
    for err in (first, second, third):
        assert "MARKER-LEAK" not in err and "Alpha Residences" not in err and KEY_DUBAI not in err


def test_main_scrubs_the_secrets_of_the_loaded_config_from_every_log_line(monkeypatch, capsys):
    cfg = Config(data_dubai_api_key=KEY_DUBAI, geocode_contact=CONTACT)

    def chatty(c, **kw):
        logging.getLogger("src.elsewhere").info("a library line with %s and %s", KEY_DUBAI, CONTACT)
        return 0

    patch_main(monkeypatch, cfg, run=chatty)
    assert app.main([]) == 0
    err = capsys.readouterr().err
    assert "a library line" in err and KEY_DUBAI not in err and CONTACT not in err


def test_main_runs_a_whole_pipeline_over_the_fake_sheet(monkeypatch, tmp_path):
    client = FakeClient()
    client.add_spreadsheet("Fake Tracker For Tests", sheet_id=SHEET_ID)
    cfg = Config(spreadsheet_id=SHEET_ID, geocode_enabled=False, gemini_api_key=KEY_GEMINI)
    monkeypatch.setattr(app, "load_config", lambda env=None, *, env_file=None: cfg)
    monkeypatch.setattr(app, "SheetManager", lambda c: SheetManager(c, client=client, sleep=lambda s: None))
    monkeypatch.setattr(app, "build_adapters", lambda c, *, session=None, today=None: feeds(dld_rows=[dld(1)], seed=[]))
    snapshot = tmp_path / "snap.json"
    assert app.main(["--snapshot", str(snapshot)]) == 0
    assert json.loads(snapshot.read_text(encoding="utf-8"))["registry_rows"] == 1
    sheet = client.spreadsheets[SHEET_ID]
    assert len(sheet.tab(TAB_RUN_LOGS).dump()) == 2  # header plus one run

    assert app.main(["--snapshot", str(snapshot), "--dry-run"]) == 0
    assert len(sheet.tab(TAB_RUN_LOGS).dump()) == 2  # a dry run adds none


def test_module_help_runs_as_a_script_without_side_effects(tmp_path):
    done = subprocess.run(
        [sys.executable, "-m", "src.main", "--help"], cwd=ROOT_DIR, capture_output=True, text=True, timeout=120,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )
    assert done.returncode == 0 and "--dry-run" in done.stdout and done.stderr == ""


def test_module_run_without_credentials_exits_2_offline(tmp_path):
    """A real process, a real SheetManager, no client: a malformed inline key fails before any network call."""
    secret = "fake-not-json-key-1111"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GCP_", "SPREADSHEET", "DATA_DUBAI", "GEMINI", "GEOCODE", "REQUIRE", "ENABLE"))}
    env.update(GCP_SERVICE_ACCOUNT_KEY=secret, SPREADSHEET_ID=SHEET_ID, PYTHONPATH=str(ROOT_DIR), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run(
        [sys.executable, "-m", "src.main", "--snapshot", str(tmp_path / "s.json")], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 2, done.stderr
    output = done.stdout + done.stderr
    assert "cannot open the spreadsheet" in output and "SheetError" in output
    assert secret not in output and SHEET_ID not in output
    assert not (tmp_path / "s.json").exists()


# ---------------------------------------------------------------------------
# Review additions: a Listing_Data row that stops matching a project must be visible
# ---------------------------------------------------------------------------
def test_listing_rows_that_match_no_project_are_counted_in_the_log_and_named_in_the_private_run_log(tmp_path, caplog):
    """
    A name-keyed Listing_Data row stops matching when the feed renames the project. That must not be
    silent: a price that vanishes from the map needs a trail. The public Actions log gets the count;
    the keys (they carry developer and project names) go only to the private Scrape_Run_Logs row.
    """
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0
    orphan = "name:dubai:fake-developer:old-name"
    env.sheet.tab(TAB_LISTINGS).put([seed_line(orphan)], start_row=2)
    assert orphan in env.mgr.read_listing_records()
    caplog.clear()
    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER) == 0  # informational, not a failure
    log_lines = " ".join(main_messages(caplog))
    assert "1 Listing_Data key(s) match no registry row" in log_lines
    assert orphan not in log_lines and "fake-developer" not in caplog.text, "a public log must not carry project or developer names"
    note = env.last_log()["Notes"]
    assert "1 Listing_Data key(s) match no registry row" in note
    assert orphan in note


def test_analyst_column_values_that_lost_their_project_are_counted_in_the_log_and_named_in_the_private_run_log(
    tmp_path, caplog, monkeypatch
):
    """Same rule as the unmatched Listing_Data keys: the manager keeps the dropped keys, main files them privately."""
    env = make_env(tmp_path)
    dropped_key = "name:dubai:fictional-builder:imaginary-heights"
    real_write = env.mgr.write_registry

    def write_then_report_a_drop(rows):
        real_write(rows)
        env.mgr.last_dropped_extras = [dropped_key]

    monkeypatch.setattr(env.mgr, "write_registry", write_then_report_a_drop)
    with caplog.at_level(logging.DEBUG):
        assert go(env, feeds(dld_rows=[dld(1)])) == 0  # informational, not a failure
    assert "fictional-builder" not in caplog.text and "imaginary" not in caplog.text
    note = env.last_log()["Notes"]
    assert "1 Registry_Projects row(s) had analyst-added column values" in note and dropped_key in note


def test_the_run_log_notes_name_at_most_ten_keys_and_count_the_rest():
    notes = app.build_run_log(
        started_at="2026-10-25T03:00:00Z", exit_code=0, outcomes=[], geocode_stats={}, registry_rows=0, map_pins=0,
        merge_stats=None, report=app.Report([]), dropped_extras=[f"dld:dubai:{n}" for n in range(1, 13)],
    ).notes
    assert "12 Registry_Projects row(s)" in notes and "dld:dubai:10" in notes and "dld:dubai:11" not in notes
    assert "and 2 more" in notes


def test_an_edit_made_between_two_geocode_flushes_is_never_overwritten(tmp_path, caplog):
    """The guard covers the flush path too: the first flush writes, a person types a row, the second flush and the final write are refused."""
    env = make_env(tmp_path)
    assert go(env, feeds(dld_rows=[dld(1)])) == 0

    def two_flushes(rows, overrides, cfg, *, today, flush):
        first = [r.model_copy(update={"zone": "First"}) for r in rows]
        flush(first)
        append_registry_cells(env, **{
            "Emirate": "Dubai", "Registry": "SEED", "Project Name (EN)": "Typed Between Flushes", "Developer": "Hand Dev",
        })
        second = [r.model_copy(update={"zone": "Second"}) for r in first]
        try:
            flush(second)
        except Exception:  # noqa: BLE001 - the real geocoder logs a failed flush and carries on
            pass
        return second, {}

    assert go(env, feeds(dld_rows=[dld(1, day=LATER)]), today=LATER, geocode=two_flushes) == 1
    registry = env.records(TAB_REGISTRY)
    assert [r["Project Name (EN)"] for r in registry if r["Registry"] == "SEED"] == ["Typed Between Flushes"]
    assert {r["Zone"] for r in registry if r["Registry"] == "DLD"} == {"First"}  # the first flush stayed, the second never landed
    assert "changed while the run was in progress" in caplog.text
    assert env.last_log()["Exit Code"] == "1"
