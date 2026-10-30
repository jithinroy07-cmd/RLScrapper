"""
Google Sheets layer (docs/DESIGN.md sections 3 and 12).

The sheet is this project's only database and people edit it by hand, so the rules
here are about not hurting what a person put there:

  * Columns are found by NAME in the header row that is actually on the sheet.
    An analyst may reorder, insert or add columns; nothing here uses a column
    letter or a position. A column we do not know is carried along, never dropped.
  * Missing columns are added at the END of the header. Nothing is reordered or
    removed.
  * Every write is RAW (the text as given). USER_ENTERED would turn "00123" into
    123 and "2026-10-25" into a locale-dependent date.
  * A tab is replaced by writing the new rows BELOW the old ones and only then
    removing the old rows in a single request. Whatever happens in between (a
    quota error, a crash) the old data is still on the sheet; the worst case is
    duplicates, which the next run merges by Project Key. Overwriting in place
    and clearing the tail cannot promise that once a large write is chunked.
  * A configured SPREADSHEET_ID is never replaced by a new spreadsheet. If it
    cannot be opened the run fails and says who to share the sheet with.
  * 429 and 5xx are retried with exponential backoff, but only for requests that
    are safe to repeat. An append or a row delete whose outcome is unknown after
    a 5xx is NOT repeated: repeating it could duplicate rows or delete the wrong
    ones, so it fails loudly instead.
  * No secret (key, token, service-account JSON, the spreadsheet id, a URL with
    key=...) reaches a log line or an exception message.
  * Nor does anything else a third party or the sheet's owner wrote: this log is
    public. A failed API call is reported by its HTTP status and Google's fixed
    status token (PERMISSION_DENIED and the like), never by Google's message text
    (it can echo a range, a title or a project number); the spreadsheet's title,
    its name setting, the service account's e-mail and any cell value stay out of
    messages too, and a message says "the spreadsheet" or "the service account".
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import gspread
import requests
from google.auth import exceptions as google_exceptions
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, SpreadsheetNotFound
from gspread.utils import rowcol_to_a1
from pydantic import ValidationError

from src.config import (
    ALL_TABS,
    ROOT_DIR,
    SOURCE_NAMES,
    TAB_CANDIDATES,
    TAB_LISTINGS,
    TAB_OVERRIDES,
    TAB_PINS,
    TAB_REGISTRY,
    TAB_RUN_LOGS,
    Config,
)
from src.emirates import in_union_bbox
from src.listing_data import LISTING_HEADERS, load_listing_records, rows_to_add
from src.models import (
    CANDIDATE_HEADERS,
    OVERRIDE_HEADERS,
    REGISTRY_HEADERS,
    GeocodeOverride,
    LaunchCandidate,
    RegistryRow,
    format_dubai_time,
)

logger = logging.getLogger(__name__)

# Scopes. Drive is needed for exactly three things, all of which happen only when
# SPREADSHEET_ID is NOT set: finding a spreadsheet by name (client.open), creating
# one (client.create) and sharing it (spreadsheet.share). It is the full Drive scope
# (not drive.file) so a spreadsheet a person created and shared with the service
# account can be found by name; a service account can only ever see what has been
# shared with it. With SPREADSHEET_ID set, open_by_key, the tab, header and value
# calls and the batch updates are all Sheets API, so the credential is narrowed to
# the Sheets scope (least privilege: a leaked key then cannot read or change the
# other files shared with the account).
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SCOPES = (
    SHEETS_SCOPE,
    "https://www.googleapis.com/auth/drive",
)
SHEETS_ONLY_SCOPES = (SHEETS_SCOPE,)
REQUIRED_KEY_FIELDS = ("client_email", "private_key", "token_uri")


def _unwrap_key_text(text: str) -> str:
    """
    The service-account JSON as pasted by a person, made parseable where the intent is clear: a
    byte-order mark and surrounding whitespace are dropped, one pair of quotes wrapped around the
    whole object is removed, and a value that is the base64 encoding of the file (a common way to
    paste a multi-line file safely) is decoded. Anything else is returned as it came, so the JSON
    check still decides. Nothing here logs or raises.
    """
    cleaned = text.lstrip("\ufeff").strip()
    for quote in ('"', "'"):
        if len(cleaned) >= 2 and cleaned[0] == quote and cleaned[-1] == quote and cleaned[1:2] == "{":
            cleaned = cleaned[1:-1].strip()
            break
    if cleaned.startswith("{"):
        return cleaned
    compact = "".join(cleaned.split())
    if compact and re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        try:
            decoded = base64.b64decode(compact.replace("-", "+").replace("_", "/") + "=" * (-len(compact) % 4), validate=True)
            candidate = decoded.decode("utf-8-sig").strip()
        except (binascii.Error, ValueError):
            return cleaned
        if candidate.startswith("{"):
            return candidate
    return cleaned
HTTP_TIMEOUT_SECONDS = (10.0, 120.0)  # (connect, read); gspread's own default is no timeout at all

# --- retry policy ---------------------------------------------------------------
MAX_ATTEMPTS = 6
BACKOFF_BASE_SECONDS = 2.0  # 2, 4, 8, 16, 32: about a minute in total, one Sheets quota window
BACKOFF_CAP_SECONDS = 64.0
_RATE_LIMIT_REASONS = frozenset({"ratelimitexceeded", "userratelimitexceeded"})
# Google's canonical error codes (error.status in an API error body). A fixed enum, so safe to name; any other
# string there is the server's own text and is dropped.
_API_STATUS_TOKENS = frozenset({
    "INVALID_ARGUMENT", "FAILED_PRECONDITION", "OUT_OF_RANGE", "UNAUTHENTICATED", "PERMISSION_DENIED", "NOT_FOUND",
    "ABORTED", "ALREADY_EXISTS", "RESOURCE_EXHAUSTED", "CANCELLED", "DATA_LOSS", "UNKNOWN", "INTERNAL",
    "NOT_IMPLEMENTED", "UNIMPLEMENTED", "UNAVAILABLE", "DEADLINE_EXCEEDED",
})
_TRANSIENT_NETWORK = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.JSONDecodeError,  # a 200 whose body was cut off or replaced by a proxy page
    google_exceptions.TransportError,
)

# --- write shaping -----------------------------------------------------------------
WRITE_CHUNK_ROWS = 1000
WRITE_CHUNK_CELLS = 40_000  # keeps one request well under the 2 MB the Sheets docs recommend
APPEND_CHUNK_ROWS = 500
GRID_SLACK_ROWS = 50
MAX_CELL_CHARS = 50_000  # Google Sheets' hard limit per cell
RAW = "RAW"
INSERT_ROWS = "INSERT_ROWS"

# --- run log -----------------------------------------------------------------------
SOURCE_STATUSES = ("ok", "failed", "skipped", "disabled")
SOURCE_NOTE_LIMIT = 300
GEOCODE_TEXT_LIMIT = 200
NOTES_LIMIT = 2000
GIT_SHA_LIMIT = 64

RUN_LOG_HEADERS = (
    "Started At",
    "Started (Dubai time)",
    "Finished At",
    "Exit Code",
    "Sources",  # one compact JSON object: {source: {status, rows, note?}}
    "Geocode",  # one compact JSON object of geocode counters
    "Registry Rows",
    "Map Pins",
    "Git SHA",
    "Notes",
)

# Map_Pins is an export for people (Google My Maps imports the FIRST tab, hence index 0).
# It carries no permit numbers, no "Entered By" and no contact data.
MAP_PIN_HEADERS = (
    "Project Key",
    "Project Name",
    "Developer",
    "Emirate",
    "Area",
    "Status",
    "Precision",
    "Latitude",
    "Longitude",
    "Map Link",
)

# The one mapping from tab to header names. Sheet access elsewhere is by these names.
HEADERS: dict[str, tuple[str, ...]] = {
    TAB_REGISTRY: REGISTRY_HEADERS,
    TAB_LISTINGS: LISTING_HEADERS,
    TAB_CANDIDATES: CANDIDATE_HEADERS,
    TAB_PINS: MAP_PIN_HEADERS,
    TAB_OVERRIDES: OVERRIDE_HEADERS,
    TAB_RUN_LOGS: RUN_LOG_HEADERS,
}

_INITIAL_ROWS = {
    TAB_REGISTRY: 2000,
    TAB_LISTINGS: 1000,
    TAB_CANDIDATES: 500,
    TAB_PINS: 2000,
    TAB_OVERRIDES: 200,
    TAB_RUN_LOGS: 1000,
}
_PIN_RANK = {"exact": 0, "community": 1, "area": 2}  # blank precision is the weakest: rank 3


class SheetError(Exception):
    """Any failure of the Sheets layer. Its message never contains a secret."""

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class SourceRunInfo:
    """What happened to one source in a run. status: ok | failed | skipped | disabled."""

    status: str
    rows: int = 0
    note: str = ""

    def __post_init__(self) -> None:
        if self.status not in SOURCE_STATUSES:
            raise ValueError(f"status must be one of {SOURCE_STATUSES}, got {self.status!r}")
        if isinstance(self.rows, bool) or not isinstance(self.rows, int) or self.rows < 0:
            raise ValueError(f"rows must be a non-negative integer, got {self.rows!r}")


@dataclass
class RunLogEntry:
    """One Scrape_Run_Logs row. Timestamps are timezone-aware UTC ISO strings."""

    started_at: str
    finished_at: str
    exit_code: int
    sources: dict[str, SourceRunInfo]
    geocode: dict[str, "int | str"]
    registry_rows: int = 0
    map_pins: int = 0
    git_sha: str = ""
    notes: str = ""


# ---------------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------------
_REDACTED = "[redacted]"
_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)
_SECRET_NAMES = (
    r"private_key(?:_id)?|client_secret|x-goog-api-key|x-api-key|api[_-]?key|access_token|refresh_token|id_token"
    r"|token|password|secret"
)
_JSON_SECRET_RE = re.compile(rf'(?i)("(?:{_SECRET_NAMES})"\s*:\s*")(?:[^"\\]|\\.)*(")')
_PYREPR_SECRET_RE = re.compile(rf"(?i)('(?:{_SECRET_NAMES})'\s*:\s*')(?:[^'\\]|\\.)*(')")  # a dict printed by Python
_HEADER_SECRET_RE = re.compile(  # "token: value" / "X-Api-Key: value" as a header or log dump prints them
    r"(?i)(?<![A-Za-z0-9_-])((?:x-goog-api-key|x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token"
    r"|id[_-]?token|client[_-]?secret|password|passwd|token|secret)\s*:\s*)(?!\[redacted\])[^\s\"',;<>}\]]+"
)
_AUTH_RE = re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)(?:(?:bearer|basic)\s+)?[^\s,;\"'}]+")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_ASSIGN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])((?:api[_-]?key|access[_-]?token|id[_-]?token|refresh[_-]?token"
    r"|client[_-]?secret|key|token|secret|password|passwd|signature|sig)\s*=\s*)[^&\s\"'<>#,;]+"
)
_USERINFO_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_GOOGLE_API_KEY_RE = re.compile(r"AIza[0-9A-Za-z_-]{35}")
_GOOGLE_OAUTH_TOKEN_RE = re.compile(r"ya29\.[0-9A-Za-z._-]{10,}")
_SHEET_ID_RE = re.compile(r"(/(?:spreadsheets|d)/)[A-Za-z0-9_-]{20,}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MIN_SECRET_LENGTH = 4


def scrub_text(text: object, secrets: Iterable[str] = ()) -> str:
    """
    `text` with anything secret-shaped replaced by [redacted]. Exact secret values
    (`secrets`) go first, then patterns: private-key blocks, JSON secret fields,
    Authorization / Bearer values, key=... style parameters (so a URL carrying
    ?key=... is safe), user:password@ in URLs, Google API keys and OAuth tokens.
    Spreadsheet ids inside URLs are masked too. Over-redacting is acceptable; leaking is not.
    """
    out = "" if text is None else str(text)
    for secret in sorted((s for s in secrets if s and len(s) >= _MIN_SECRET_LENGTH), key=len, reverse=True):
        out = out.replace(secret, _REDACTED)
    out = _PEM_RE.sub(_REDACTED, out)
    out = _JSON_SECRET_RE.sub(rf"\1{_REDACTED}\2", out)
    out = _PYREPR_SECRET_RE.sub(rf"\1{_REDACTED}\2", out)
    out = _AUTH_RE.sub(rf"\1{_REDACTED}", out)
    out = _BEARER_RE.sub(rf"\1 {_REDACTED}", out)
    out = _ASSIGN_RE.sub(rf"\1{_REDACTED}", out)
    out = _HEADER_SECRET_RE.sub(rf"\1{_REDACTED}", out)
    out = _USERINFO_RE.sub(rf"\1{_REDACTED}@", out)
    out = _GOOGLE_API_KEY_RE.sub(_REDACTED, out)
    out = _GOOGLE_OAUTH_TOKEN_RE.sub(_REDACTED, out)
    return _SHEET_ID_RE.sub(r"\1[spreadsheet-id]", out)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _norm_keep(cell: object) -> str:
    """A cell as text for equality checks: None is blank, nothing else is altered."""
    return "" if cell is None else str(cell)


def _norm(cell: object) -> str:
    """A cell as comparable text: None is blank, runs of whitespace collapse."""
    return "" if cell is None else " ".join(str(cell).split())


# What identity_slug accepts, for the messages that say a row has no usable identity (names are never echoed).
_USABLE_NAMES = "each with a Latin letter, no letter or number from another script, and none of + # @"


def _why_invalid(exc: ValueError) -> str:
    """One line saying what is wrong with a row, naming fields but not echoing the whole row."""
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors(include_url=False, include_context=False, include_input=False):
            where = ".".join(str(p) for p in err.get("loc", ())).replace("_", " ") or "row"
            message = str(err.get("msg", "invalid")).removeprefix("Value error, ")
            parts.append(f"{where}: {message}")
        text = "; ".join(parts)
    else:
        text = f"unreadable ({type(exc).__name__})"  # a bare ValueError's text can hold a cell value; the log is public
    return _clip(" ".join(text.split()), 300)


def _status_of(exc: BaseException) -> Optional[int]:
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(code, int):
        return code
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) and code > 0 else None


def _is_quota_error(exc: BaseException, status: Optional[int]) -> bool:
    """A request the server REFUSED for rate reasons: nothing was applied, so any request may be repeated."""
    if status == 429:
        return True
    if status != 403:
        return False
    error = getattr(exc, "error", None)
    items = error.get("errors") if isinstance(error, Mapping) else None
    for item in items if isinstance(items, list) else ():
        if isinstance(item, Mapping):
            reason = str(item.get("reason", "")).replace("_", "").lower()
            if reason in _RATE_LIMIT_REASONS or item.get("domain") == "usageLimits":
                return True
    return False


def _is_server_error(status: Optional[int]) -> bool:
    return status is not None and (status >= 500 or status == 408)


@dataclass
class _Table:
    """
    One tab as read from the sheet. `header` is the actual header row padded with
    "" to the full data width (so a column without a name still has a position);
    `rows` are the non-blank data rows as (sheet row number, cells padded to that
    width); `total_rows` is the number of the last non-empty row (0 for an empty tab).
    """

    tab: str
    header: list[str]
    rows: list[tuple[int, list[str]]]
    total_rows: int
    first: dict[str, int]

    def get(self, cells: Sequence[str], name: str) -> str:
        index = self.first.get(name)
        return cells[index] if index is not None and index < len(cells) else ""

    def record(self, cells: Sequence[str]) -> dict[str, str]:
        return {name: cells[index] for name, index in self.first.items()}


class SheetManager:
    """
    Reads and writes the six tabs of the tracker spreadsheet.

    `client` is a gspread.Client-like object (tests pass tests/fake_gspread.py's);
    with none, the real one is built from the service-account key when open() runs.
    `sleep` is injectable so retry tests do not wait.

    Attributes main may read after the calls named:
      registry_problem_count / last_registry_problems  (read_registry)
      last_dropped_extras                              (write_registry)
      last_candidate_problems                          (read_candidates)
      share_problems, created_spreadsheet              (open)
    """

    def __init__(
        self, cfg: Config, *, client: Any = None, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._sleep = sleep
        self._spreadsheet: Any = None
        self._worksheets: dict[str, Any] = {}
        self._secrets: set[str] = {
            value
            for value in (
                cfg.gemini_api_key,
                cfg.gcp_service_account_key,
                cfg.spreadsheet_id,
                cfg.data_dubai_api_key,
                cfg.data_dubai_api_secret,
                cfg.locationiq_api_key,
                cfg.geoapify_api_key,
                cfg.google_maps_api_key,
            )
            if value
        }
        # The private key is a secret on its own, not only as part of the whole JSON: an
        # error message that echoes just the key must still be scrubbed, including when
        # a ready-made client is injected and _build_client never runs.
        self._remember_key_secrets(cfg.gcp_service_account_key)
        self.share_problems: list[str] = []
        self.created_spreadsheet = False
        self.last_registry_problems: list[str] = []
        self.last_dropped_extras: list[str] = []
        self.last_candidate_problems: list[str] = []

    # =============================================================================
    # Errors, retries
    # =============================================================================
    def _remember_key_secrets(self, key_text: object) -> None:
        """Register the secret fields of a service-account JSON (if it parses) for scrubbing."""
        if not isinstance(key_text, str) or not key_text.strip():
            return
        try:
            info = json.loads(key_text)
        except ValueError:
            return
        if isinstance(info, dict):
            self._secrets.update(
                value for name in ("private_key", "private_key_id") if isinstance(value := info.get(name), str) and value
            )

    def _scrub(self, text: object) -> str:
        return scrub_text(text, self._secrets)

    def _fail(self, message: str, *, status: Optional[int] = None) -> SheetError:
        return SheetError(self._scrub(message), status=status)

    @staticmethod
    def _api_text(exc: APIError, status: Optional[int]) -> str:
        """"HTTP 403 PERMISSION_DENIED": the status and Google's fixed token, never its message text (public log)."""
        error = getattr(exc, "error", None)
        token = error.get("status") if isinstance(error, Mapping) else None
        label = f"HTTP {status}" if status is not None else "an API error"
        return f"{label} {token}" if isinstance(token, str) and token in _API_STATUS_TOKENS else label

    def _call(
        self,
        what: str,
        fn: Callable[..., Any],
        *args: Any,
        idempotent: bool = True,
        **kwargs: Any,
    ) -> Any:
        """
        Call the Sheets API with backoff. 429 (and Drive's rate-limit 403) is retried
        for every call. 5xx and network errors are retried only when `idempotent`;
        otherwise the outcome is unknown and the call fails instead of repeating a
        write that may already have happened. Anything else is raised as a SheetError
        straight away; exceptions that are not API errors propagate untouched.
        """
        attempt = 1
        while True:
            status: Optional[int] = None
            try:
                return fn(*args, **kwargs)
            except APIError as exc:
                status = _status_of(exc)
                problem = self._api_text(exc, status)
                quota = _is_quota_error(exc, status)
                if not (quota or _is_server_error(status)):
                    raise self._fail(f"{what}: Google rejected the request ({problem})", status=status) from None
                if not quota and not idempotent:
                    raise self._fail(
                        f"{what}: failed with {problem}. The change may or may not have been applied, so it "
                        "was not repeated; re-read the tab before trying again",
                        status=status,
                    ) from None
            except _TRANSIENT_NETWORK as exc:
                problem = f"network error {type(exc).__name__}"  # the text of these carries the request URL
                if not idempotent:
                    raise self._fail(
                        f"{what}: {problem}. The change may or may not have been applied, so it was not "
                        "repeated; re-read the tab before trying again"
                    ) from None
            except google_exceptions.GoogleAuthError as exc:
                raise self._fail(
                    f"{what}: Google refused the service-account credentials ({type(exc).__name__}). "
                    "Check that the key is current and that this machine's clock is correct"
                ) from None
            except requests.exceptions.RequestException as exc:
                # Anything else from the HTTP layer: its text can carry the request URL, so only the type is kept.
                raise self._fail(f"{what}: the request failed ({type(exc).__name__})") from None
            if attempt >= MAX_ATTEMPTS:
                raise self._fail(f"{what}: gave up after {attempt} attempts ({problem})", status=status)
            delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
            logger.warning(
                "%s: %s; retrying in %.0f s (attempt %d of %d)", what, problem, delay, attempt + 1, MAX_ATTEMPTS
            )
            self._sleep(delay)
            attempt += 1

    # =============================================================================
    # Authentication
    # =============================================================================
    def _key_file(self) -> Optional[Path]:
        configured = Path(self._cfg.gcp_service_account_file)
        candidates = [configured] if configured.is_absolute() else [Path.cwd() / configured, ROOT_DIR / configured]
        return next((path for path in candidates if path.is_file()), None)

    def _load_service_account_info(self) -> dict[str, Any]:
        """
        The service-account JSON as a dict: GCP_SERVICE_ACCOUNT_KEY (CI) if set, else
        the local file. A bad inline key does not fall back to the file: that would
        hide a broken secret. Messages name fields and files, never any key text.
        """
        if self._cfg.has_gcp_credentials_inline:
            text, origin = self._cfg.gcp_service_account_key, "GCP_SERVICE_ACCOUNT_KEY"
        else:
            path = self._key_file()
            if path is None:
                raise self._fail(
                    "no Google credentials: set GCP_SERVICE_ACCOUNT_KEY (the service-account JSON, for CI) "
                    "or put the key file where GCP_SERVICE_ACCOUNT_FILE points (local runs); "
                    "the setting's value is not shown"
                )
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                # The path is a typed setting (and a local path); only the file's role and the error class show.
                raise self._fail(f"cannot read the service-account file ({type(exc).__name__})") from None
            origin = "the service-account file"
        text = _unwrap_key_text(text)
        try:
            info = json.loads(text)
        except ValueError:
            # A safe diagnosis only: the length and whether the value begins like a JSON object, never its text.
            shape = "begins with {" if text.startswith("{") else "does not begin with {"
            raise self._fail(
                f"{origin} is not valid JSON; it must be the full key file from Google Cloud "
                f"(the value given is {len(text)} character(s) long and {shape}; a base64 encoding of the file is accepted too)"
            ) from None
        if not isinstance(info, dict):
            raise self._fail(f"{origin} is not a JSON object")
        missing = [f for f in REQUIRED_KEY_FIELDS if not isinstance(info.get(f), str) or not info[f].strip()]
        if missing:
            raise self._fail(f"{origin} is missing field(s): {', '.join(missing)}")
        if info.get("type") != "service_account":
            raise self._fail(f"{origin} is not a service-account key (its type must be service_account)")
        return info

    def _build_client(self) -> Any:
        info = self._load_service_account_info()
        self._secrets.update(
            str(info[name]) for name in ("private_key", "private_key_id") if isinstance(info.get(name), str)
        )
        try:
            scopes = SHEETS_ONLY_SCOPES if self._cfg.spreadsheet_id else SCOPES
            credentials = Credentials.from_service_account_info(info, scopes=list(scopes))
        except Exception as exc:  # noqa: BLE001 - key parsers raise many types and some echo key text
            raise self._fail(
                f"the service-account key could not be loaded ({type(exc).__name__}); check that it is the "
                "complete, unmodified JSON key from Google Cloud"
            ) from None
        client = gspread.authorize(credentials)
        client.set_timeout(HTTP_TIMEOUT_SECONDS)
        return client

    @staticmethod
    def _share_hint() -> str:
        # The e-mail names the Google Cloud project and this log is public: the operator reads it from their own key file.
        return (
            "Share the spreadsheet with the service account (the client_email in its key file) as Editor "
            "and check SPREADSHEET_ID. Nothing was created."
        )

    # =============================================================================
    # open()
    # =============================================================================
    def open(self, *, read_only: bool = False) -> None:
        """
        Open (or create) the spreadsheet and make sure all six tabs exist with their
        header rows.

        `read_only=True` is for clean-up tools that must be able to promise a dry
        run writes nothing: it opens the EXISTING spreadsheet named by SPREADSHEET_ID
        (never creates, never searches by name, never shares), binds the tabs that
        already exist and changes nothing: no tab, no column, no freeze, no reorder.
        A tab that is missing is reported when it is first used.

        With SPREADSHEET_ID set the spreadsheet is opened by id and ONLY that: if it
        cannot be opened this raises with a "share it with the service account (the
        client_email in its key file)" hint and creates nothing; the address itself
        is never printed (the log is public). Without an id the spreadsheet named GOOGLE_SHEET_NAME is
        opened, or created and then shared per SHARE_WITH_EMAILS / SHARE_ROLE /
        SHARE_NOTIFY. Sharing happens ONLY at creation: re-sharing an existing sheet
        on every run would re-send notification e-mails and overwrite roles people
        changed by hand. A sheet nobody can open is worse than a failed run, so if
        a share fails the tabs are still built (the sheet is complete) and then this
        raises, naming the entries that failed; `share_problems` holds the same list.
        Missing tabs are added; missing columns are appended to the END of a header;
        existing columns are never reordered or dropped; Map_Pins ends up first.
        """
        if self._client is None:
            self._client = self._build_client()
        self.share_problems = []
        if read_only:
            if not self._cfg.spreadsheet_id:
                raise self._fail("a read-only open needs SPREADSHEET_ID: it never creates or searches for a spreadsheet")
            self._spreadsheet, self.created_spreadsheet = self._open_by_id(), False
            self._worksheets = {}
            for ws in self._call("list the tabs", self._spreadsheet.worksheets):
                if ws.title in ALL_TABS:
                    self._worksheets[ws.title] = ws
            logger.info("Opened the spreadsheet read-only; %d of %d tabs exist.", len(self._worksheets), len(ALL_TABS))
            return
        if self._cfg.spreadsheet_id:
            spreadsheet, created = self._open_by_id(), False
        else:
            spreadsheet, created = self._open_or_create_by_name()
        self._spreadsheet, self.created_spreadsheet = spreadsheet, created
        if created:
            self._share(spreadsheet)
        self._ensure_tabs(created=created)
        logger.info("Opened the spreadsheet (%s); all %d tabs ready.", "created" if created else "existing", len(ALL_TABS))
        if self.share_problems:
            raise self._fail(
                "the spreadsheet named by GOOGLE_SHEET_NAME was created and is complete, but it could not be "
                f"shared ({'; '.join(self.share_problems)}). Until it is shared only the service account "
                "(the client_email in its key file) can open it: share it by hand in Google "
                "Sheets, then set SPREADSHEET_ID so later runs open it by id"
            )

    def _open_by_id(self) -> Any:
        try:
            return self._call("open the configured spreadsheet", self._client.open_by_key, self._cfg.spreadsheet_id)
        except (SpreadsheetNotFound, PermissionError):
            raise self._fail(f"cannot open the spreadsheet named by SPREADSHEET_ID. {self._share_hint()}") from None
        except SheetError as exc:
            if exc.status in (403, 404):
                raise self._fail(
                    f"cannot open the spreadsheet named by SPREADSHEET_ID (HTTP {exc.status}). {self._share_hint()}",
                    status=exc.status,
                ) from None
            raise

    def _open_or_create_by_name(self) -> tuple[Any, bool]:
        name = self._cfg.google_sheet_name
        try:
            return self._call("open the spreadsheet by name", self._client.open, name), False
        except SpreadsheetNotFound:
            logger.info("No spreadsheet is named by GOOGLE_SHEET_NAME yet; creating it.")
        try:
            return self._call("create the spreadsheet", self._client.create, name, idempotent=False), True
        except SheetError as exc:
            # An unknown outcome (5xx / network) may still have created it. The next run would then find it
            # by name, call it "existing" and never share it, so say what to do while the operator is looking.
            raise self._fail(
                f"{exc}. If Google did create the spreadsheet named by GOOGLE_SHEET_NAME, only the service account "
                "can open it: find it in the service account's Drive, share it by hand, and set SPREADSHEET_ID so "
                "later runs open it by id",
                status=exc.status,
            ) from None

    def _share(self, spreadsheet: Any) -> None:
        cfg = self._cfg
        if not cfg.share_with_emails:
            logger.info("SHARE_WITH_EMAILS is empty: only the service account can see the spreadsheet.")
            return
        for position, address in enumerate(cfg.share_with_emails, start=1):
            try:
                self._call(
                    "share the spreadsheet", spreadsheet.share, address,
                    perm_type="user", role=cfg.share_role, notify=cfg.share_notify,
                )
            except SheetError as exc:
                problem = f"SHARE_WITH_EMAILS entry {position}: {_EMAIL_RE.sub('[email]', str(exc))}"
                self.share_problems.append(problem)
                logger.error("%s", problem)
        logger.info("Shared with %d of %d address(es) as %s.",
                    len(cfg.share_with_emails) - len(self.share_problems), len(cfg.share_with_emails), cfg.share_role)

    def _ensure_tabs(self, *, created: bool) -> None:
        by_title = {ws.title: ws for ws in self._call("list the tabs", self._spreadsheet.worksheets)}
        initial = list(by_title)
        for tab in (TAB_PINS, *(t for t in ALL_TABS if t != TAB_PINS)):
            if tab not in by_title:
                headers = HEADERS[tab]
                by_title[tab] = self._call(
                    f"add tab {tab}", self._spreadsheet.add_worksheet,
                    title=tab, rows=_INITIAL_ROWS[tab], cols=max(len(headers) + 2, 12),
                    index=0 if tab == TAB_PINS else None, idempotent=False,
                )
                logger.info("Added tab %s.", tab)
            self._worksheets[tab] = by_title[tab]
        for tab in ALL_TABS:
            self._ensure_header(tab)
        pins = self._worksheets[TAB_PINS]
        if pins.index != 0:
            self._call(f"move {TAB_PINS} to the first position", pins.update_index, 0)
        if created:
            for title in initial:
                self._drop_default_sheet(by_title[title])

    def _drop_default_sheet(self, default: Any) -> None:
        """
        A spreadsheet Google just created starts with one empty tab (named by locale:
        'Sheet1', 'Feuille 1', ...). Remove it, but only if it is not one of ours and
        is still completely empty: never delete something that holds data.
        """
        if default.title in ALL_TABS:
            return
        values = self._call("read the default tab", default.get_all_values)
        if all(not _norm(cell) for row in values for cell in row):
            try:
                self._call("remove the empty default tab", self._spreadsheet.del_worksheet, default)
            except SheetError as exc:  # cosmetic only (a retry after a lost reply gets "no such sheet"); never stop a run
                logger.warning("%s", exc)

    def _ensure_header(self, tab: str) -> None:
        """Header row present and complete; absent names are appended at the end, nothing else moves."""
        ws, expected = self._worksheets[tab], HEADERS[tab]
        current = [_norm(c) for c in self._call(f"read the {tab} header", ws.row_values, 1)]
        while current and not current[-1]:
            current.pop()
        duplicated = [h for h in expected if current.count(h) > 1]
        if duplicated:
            raise self._fail(f"tab {tab} has the column(s) {', '.join(duplicated)} more than once; remove the copies")
        missing = [h for h in expected if h not in current]
        if current and not missing:
            return
        # Slow path (first run for the tab, or a schema upgrade): the true data width is
        # needed so a new column never lands on top of an unnamed column that has data.
        values = self._call(f"read {tab}", ws.get_all_values)
        width = max((len(row) for row in values), default=0)
        if not current and any(_norm(c) for row in values for c in row):
            raise self._fail(f"tab {tab} has data but a blank header row; the column names belong in row 1")
        start = max(width, len(current)) + 1
        self._ensure_grid(ws, cols=start + len(missing) - 1)
        self._call(f"write the {tab} header", ws.update, values=[missing],
                   range_name=rowcol_to_a1(1, start), value_input_option=RAW)
        if not current:
            self._freeze_header(ws, tab)
        logger.info("%s: %s column(s) %s.", tab, "created" if not current else "added at the end:", ", ".join(missing))

    def _freeze_header(self, ws: Any, tab: str) -> None:
        try:
            self._call(f"freeze the {tab} header", ws.freeze, rows=1)
        except SheetError as exc:  # cosmetic only; a failure here must not stop a run
            logger.warning("%s", exc)

    def _ensure_grid(self, ws: Any, *, rows: int = 0, cols: int = 0) -> None:
        """Grow the sheet grid (never shrink it) so a write fits: the Sheets API rejects a write past the grid."""
        grow: dict[str, int] = {}
        if rows > ws.row_count:
            grow["rows"] = rows + GRID_SLACK_ROWS
        if cols > ws.col_count:
            grow["cols"] = cols
        if grow:
            self._call(f"resize {ws.title}", ws.resize, **grow)

    # =============================================================================
    # Reading
    # =============================================================================
    def _ws(self, tab: str) -> Any:
        if self._spreadsheet is None:
            raise self._fail("the spreadsheet is not open yet: call open() first")
        if tab not in self._worksheets:
            if tab in ALL_TABS:
                raise self._fail(f"tab {tab} does not exist in the spreadsheet; run open() to create it")
            raise self._fail(f"unknown tab {tab!r}; known tabs are {', '.join(ALL_TABS)}")
        return self._worksheets[tab]

    def _read_table(self, tab: str, *, require: Sequence[str] = ()) -> _Table:
        """
        One get_all_values() call, parsed against the ACTUAL header row. Cells stay
        strings exactly as the sheet shows them (no numericisation). Blank rows are
        skipped but the row numbers of the others are kept.
        """
        values = self._call(f"read {tab}", self._ws(tab).get_all_values)
        # gspread answers [[]] (not []) for a tab with no values at all, so test the header row itself.
        if not values or not any(_norm(c) for c in values[0]):
            raise self._fail(f"tab {tab} has no header row (row 1 is blank); run open() first")
        width = max(len(row) for row in values)
        header = [_norm(c) for c in values[0]]
        header += [""] * (width - len(header))
        first: dict[str, int] = {}
        for index, name in enumerate(header):
            if name:
                first.setdefault(name, index)
        duplicated = [h for h in HEADERS[tab] if header.count(h) > 1]
        if duplicated:
            raise self._fail(f"tab {tab} has the column(s) {', '.join(duplicated)} more than once; remove the copies")
        missing = [h for h in require if h not in first]
        if missing:
            raise self._fail(f"tab {tab} has no column {', '.join(missing)}; add it, or run open() to add missing columns")
        rows: list[tuple[int, list[str]]] = []
        for number, row in enumerate(values[1:], start=2):
            cells = ["" if c is None else str(c) for c in row]
            cells += [""] * (width - len(cells))
            if any(c.strip() for c in cells):
                rows.append((number, cells))
        return _Table(tab=tab, header=header, rows=rows, total_rows=len(values), first=first)

    @property
    def registry_problem_count(self) -> int:
        """How many rows the last read_registry() reported as unusable. Main must not write the registry back while this is above zero."""
        return len(self.last_registry_problems)

    def read_registry_numbered(self) -> tuple[list[tuple[int, RegistryRow]], list[str]]:
        """
        Like read_registry(), but each row comes with its sheet row number (what
        delete_rows() needs). Rows that fail validation, or that have no identity
        key, are left out and reported with their row number.
        """
        table = self._read_table(TAB_REGISTRY)
        good: list[tuple[int, RegistryRow]] = []
        problems: list[str] = []
        for number, cells in table.rows:
            try:
                row = RegistryRow.from_sheet_dict(table.record(cells))
            except ValueError as exc:
                problems.append(f"{TAB_REGISTRY} row {number}: {_why_invalid(exc)}")
                continue
            if not row.has_identity:
                problems.append(
                    f"{TAB_REGISTRY} row {number}: no usable identity (needs a valid registry id, "
                    f"or both Developer and Project Name (EN), {_USABLE_NAMES})"
                )
                continue
            good.append((number, row))
        self.last_registry_problems = problems
        for line in problems:
            logger.warning("%s", line)
        logger.info("%s: read %d row(s), %d problem(s).", TAB_REGISTRY, len(good), len(problems))
        return good, problems

    def read_registry(self) -> tuple[list[RegistryRow], list[str]]:
        """
        (rows, problems). A row that fails validation is EXCLUDED from `rows` and
        reported in `problems` with its sheet row number; it is never dropped
        silently. Because an excluded row would vanish from the registry if main
        wrote `rows` back, main must refuse to call write_registry() while
        `registry_problem_count` (or len(problems)) is above zero, and tell a human
        which rows to fix.
        """
        numbered, problems = self.read_registry_numbered()
        return [row for _, row in numbered], problems

    def read_listing_records(self) -> dict[str, dict]:
        table = self._read_table(TAB_LISTINGS, require=("Project Key",))
        return load_listing_records(table.record(cells) for _, cells in table.rows)

    def read_overrides(self) -> tuple[dict[str, GeocodeOverride], list[str]]:
        """Human pins by Project Key plus one problem line per unusable row. For a repeated key the later row wins (a person appends a correction)."""
        table = self._read_table(TAB_OVERRIDES)
        found: dict[str, tuple[int, GeocodeOverride]] = {}
        problems: list[str] = []
        for number, cells in table.rows:
            try:
                override = GeocodeOverride.from_sheet_dict(table.record(cells))
            except ValueError as exc:
                problems.append(f"{TAB_OVERRIDES} row {number}: {_why_invalid(exc)}")
                continue
            earlier = found.get(override.project_key)
            if earlier:
                # The key is a registry number or a developer and project name: row numbers only in this public log.
                logger.warning("%s: row %d replaces row %d (same Project Key)", TAB_OVERRIDES, number, earlier[0])
            found[override.project_key] = (number, override)
        for line in problems:
            logger.warning("%s", line)
        return {key: pair[1] for key, pair in found.items()}, problems

    def read_candidates(self) -> list[LaunchCandidate]:
        """Valid candidate rows. An invalid row is skipped with a warning naming its row number (also kept in last_candidate_problems)."""
        table = self._read_table(TAB_CANDIDATES)
        out: list[LaunchCandidate] = []
        problems: list[str] = []
        for number, cells in table.rows:
            try:
                out.append(LaunchCandidate.from_sheet_dict(table.record(cells)))
            except ValueError as exc:
                problems.append(f"{TAB_CANDIDATES} row {number}: {_why_invalid(exc)}")
        self.last_candidate_problems = problems
        for line in problems:
            logger.warning("%s", line)
        return out

    # =============================================================================
    # Writing
    # =============================================================================
    def _write_block(self, ws: Any, tab: str, start_row: int, matrix: list[list[str]]) -> None:
        width = max((len(r) for r in matrix), default=1)
        per_call = max(1, min(WRITE_CHUNK_ROWS, WRITE_CHUNK_CELLS // max(width, 1)))
        for offset in range(0, len(matrix), per_call):
            chunk = matrix[offset : offset + per_call]
            first = start_row + offset
            self._call(
                f"write {tab} rows {first}-{first + len(chunk) - 1}", ws.update,
                values=chunk, range_name=rowcol_to_a1(first, 1), value_input_option=RAW,
            )

    def _check_cell_sizes(self, tab: str, names: Sequence[str], matrix: Sequence[Sequence[str]]) -> None:
        """
        Sheets rejects any cell over 50,000 characters. Find such a cell BEFORE the
        first write: a chunk that fails halfway through staging leaves the earlier
        chunks behind as duplicates, a few more on every daily run. Names the row
        (1-based within the data being written) and the column, never the content.
        """
        for number, row in enumerate(matrix, start=1):
            for index, cell in enumerate(row):
                if len(cell) > MAX_CELL_CHARS:
                    column = names[index] if index < len(names) and names[index] else f"column {index + 1}"
                    raise self._fail(
                        f"{tab}: new row {number}, column {column!r} holds {len(cell):,} characters but Google "
                        f"Sheets allows {MAX_CELL_CHARS:,} per cell; nothing was written. Shorten the source value",
                        status=400,  # what Google would have answered, only without the half-written tab
                    )

    def _swap_rows(self, tab: str, table: _Table, layout: list[str], matrix: list[list[str]]) -> None:
        """
        Replace every data row of `tab` with `matrix` (rows aligned to `layout`):
        extend the header if `layout` is wider, write the new rows BELOW the old
        ones, then remove the old rows in one request. At every instant the sheet
        holds all of the old data; a failure leaves duplicates, never a loss.
        """
        self._check_cell_sizes(tab, layout, matrix)
        ws = self._ws(tab)
        old_last = table.total_rows
        if len(layout) > len(table.header):
            self._ensure_grid(ws, cols=len(layout))
            self._call(f"extend the {tab} header", ws.update, values=[layout[len(table.header):]],
                       range_name=rowcol_to_a1(1, len(table.header) + 1), value_input_option=RAW)
        if matrix:
            self._ensure_grid(ws, rows=old_last + len(matrix) + 1, cols=len(layout))
            self._write_block(ws, tab, old_last + 1, matrix)
        elif old_last >= 2:
            self._ensure_grid(ws, rows=old_last + 1)  # the API refuses to delete every non-frozen row
        if old_last >= 2:
            if matrix:
                self._verify_staged(ws, tab, old_last + 1, matrix[0])
            try:
                self._call(f"remove the superseded {tab} rows", ws.delete_rows, 2, old_last, idempotent=False)
            except SheetError as exc:
                raise SheetError(
                    f"{exc}. The new {tab} rows are in place below the old ones but the old rows were NOT "
                    "removed, so the tab holds duplicates until the next run merges them by Project Key",
                    status=exc.status,
                ) from None
        logger.info("%s: wrote %d row(s), replaced %d.", tab, len(matrix), max(old_last - 1, 0))

    def _verify_staged(self, ws: Any, tab: str, row: int, expected: list[str]) -> None:
        """
        Before the old rows are removed, check that the first staged row is still on
        the row we wrote it to. If a person inserted or deleted a row above it since
        the read, the line numbers we are about to delete are wrong and deleting
        would destroy their data, so stop and leave the harmless duplicates.
        """
        wanted = list(expected)
        while wanted and wanted[-1] == "":
            wanted.pop()
        found = [_norm_keep(c) for c in self._call(f"re-check {tab} row {row}", ws.row_values, row)]
        if found != [_norm_keep(c) for c in wanted]:
            raise self._fail(
                f"{tab} changed while it was being rewritten (the new rows are no longer at row {row}). The old "
                "rows were NOT removed, so the tab holds duplicates; nothing was lost. Run again once nobody is "
                "editing the tab"
            )

    def _append(self, tab: str, rows: list[list[str]], names: Sequence[str] = ()) -> None:
        ws = self._ws(tab)
        self._check_cell_sizes(tab, names, rows)  # every chunk, before the first append (appends are never repeated)
        for offset in range(0, len(rows), APPEND_CHUNK_ROWS):
            chunk = rows[offset : offset + APPEND_CHUNK_ROWS]
            self._call(
                f"append {len(chunk)} row(s) to {tab}", ws.append_rows, chunk,
                value_input_option=RAW, insert_data_option=INSERT_ROWS, table_range="A1", idempotent=False,
            )

    @staticmethod
    def _checked_registry_rows(rows: Iterable[RegistryRow]) -> list[RegistryRow]:
        if rows is None or isinstance(rows, (str, bytes, Mapping)):
            raise SheetError(f"write_registry needs a sequence of RegistryRow, got {type(rows).__name__}")
        checked = list(rows)
        for item in checked:
            if not isinstance(item, RegistryRow):
                raise SheetError(f"write_registry needs RegistryRow objects, got {type(item).__name__}")
        # Names and keys stay out of messages: this text reaches the public Actions log.
        keyless = [position for position, r in enumerate(checked, start=1) if not r.has_identity]
        if keyless:
            raise SheetError(
                f"{len(keyless)} registry row(s) have no identity key (no valid registry id and no usable "
                f"Developer + Project Name (EN), {_USABLE_NAMES}); positions in the new data: "
                f"{', '.join(str(n) for n in keyless[:5])}{' ...' if len(keyless) > 5 else ''}; nothing was written"
            )
        key_counts = Counter(r.project_key for r in checked)
        duplicates = sum(1 for seen in key_counts.values() if seen > 1)
        if duplicates:
            logger.warning("write_registry: %d duplicate Project Key(s) in the new rows", duplicates)
        return checked

    def write_registry(self, rows: Iterable[RegistryRow]) -> None:
        """
        Replace the data rows of Registry_Projects with `rows`.

        Raises (writing nothing) to replace a non-empty tab with zero rows, or when
        a row has no identity key. Columns are written by header name into the
        layout the sheet really has. Columns an analyst added that we do not know
        are carried across to the new row order by the stored Project Key (matched
        against ANY identity key of the new rows); values whose key is no longer in
        the new data cannot follow anything and are reported in a warning and in
        `last_dropped_extras`. The write itself is the safe stage-then-swap
        described in the module docstring and is chunked.
        """
        new_rows = self._checked_registry_rows(rows)
        table = self._read_table(TAB_REGISTRY)
        if not new_rows:
            if table.rows:
                raise self._fail(
                    f"refusing to replace {len(table.rows)} existing {TAB_REGISTRY} row(s) with zero rows; "
                    "if the registry really is empty, clear the tab by hand"
                )
            logger.info("%s: nothing to write.", TAB_REGISTRY)
            return
        layout = list(table.header) + [h for h in REGISTRY_HEADERS if h not in table.first]
        carried = self._carry_extras(table, new_rows)
        matrix = []
        for row, extras in zip(new_rows, carried):
            named = row.to_sheet_dict()
            matrix.append([named.get(name, extras.get(index, "")) if name else extras.get(index, "")
                           for index, name in enumerate(layout)])
        self._swap_rows(TAB_REGISTRY, table, layout, matrix)

    def _carry_extras(self, table: _Table, new_rows: list[RegistryRow]) -> list[dict[int, str]]:
        """
        For each new row, the values of the columns we do not know (named or not),
        {column index: text}, taken from the old row that is the same project.
        """
        known = set(REGISTRY_HEADERS)
        extra_columns = [i for i, name in enumerate(table.header) if name not in known]
        self.last_dropped_extras = []
        if not extra_columns:
            return [{} for _ in new_rows]
        owner: dict[str, int] = {}
        payload: dict[int, dict[int, str]] = {}
        label: dict[int, str] = {}
        for number, cells in table.rows:
            extras = {i: cells[i] for i in extra_columns if cells[i].strip()}
            if not extras:
                continue
            stored = _norm(table.get(cells, "Project Key"))
            keys = {stored} if stored else set()
            try:
                keys.update(RegistryRow.from_sheet_dict(table.record(cells)).identity_keys())
            except ValueError:
                pass  # a row we cannot parse can still be matched by its stored key
            payload[number], label[number] = extras, stored or f"row {number} (no Project Key)"
            for key in sorted(keys):
                owner.setdefault(key, number)
        claimed: set[int] = set()
        carried: list[dict[int, str]] = []
        for row in new_rows:
            match = next((owner[k] for k in row.identity_keys() if k in owner and owner[k] not in claimed), None)
            if match is None:
                carried.append({})
                continue
            claimed.add(match)
            carried.append(payload[match])
        dropped = [label[n] for n in sorted(payload) if n not in claimed]
        if dropped:
            self.last_dropped_extras = dropped
            # The keys are in `last_dropped_extras` (main puts them in the private run log), never in this public log.
            logger.warning(
                "%s: values in analyst-added column(s) on %d row(s) were not carried over because their "
                "Project Key is no longer in the registry", TAB_REGISTRY, len(dropped),
            )
        return carried

    def apply_listing_seed(self, seed_rows: Sequence[Sequence[str]]) -> int:
        """
        Append seed rows (aligned to LISTING_HEADERS) for Project Keys the tab does
        not have yet; returns how many were added. A key already present is never
        touched, so an analyst's entry always outranks the seed. Cells are placed by
        header name, so the tab's column order does not matter.
        """
        seed = [list(row) for row in seed_rows]
        if not seed:
            return 0
        table = self._read_table(TAB_LISTINGS, require=("Project Key",))
        fresh = rows_to_add([table.get(cells, "Project Key") for _, cells in table.rows], seed)
        if not fresh:
            logger.info("%s already has every seed row.", TAB_LISTINGS)
            return 0
        matrix = []
        for row in fresh:
            named = dict(zip(LISTING_HEADERS, row))
            matrix.append([named.get(name, "") for name in table.header])
        self._append(TAB_LISTINGS, matrix, table.header)
        logger.info("%s: added %d seed row(s).", TAB_LISTINGS, len(matrix))
        return len(matrix)

    def add_candidates(self, candidates: Iterable[LaunchCandidate]) -> int:
        """
        Append candidates whose Candidate Key is not on the tab yet; returns the
        count added. An existing row is never edited (a person owns its Status).
        Keys already on the tab count even when that row is otherwise invalid.
        """
        batch = list(candidates)
        if not batch:
            return 0
        table = self._read_table(TAB_CANDIDATES, require=("Candidate Key",))
        seen: set[str] = set()
        for _, cells in table.rows:
            stored = _norm(table.get(cells, "Candidate Key")).casefold()
            if stored:
                seen.add(stored)
            try:  # a row typed by hand may have no key cell; its computed key still counts
                seen.add(LaunchCandidate.from_sheet_dict(table.record(cells)).candidate_key.casefold())
            except ValueError:
                pass
        fresh: list[list[str]] = []
        for candidate in batch:
            if not isinstance(candidate, LaunchCandidate):
                raise self._fail(f"add_candidates needs LaunchCandidate objects, got {type(candidate).__name__}")
            key = candidate.candidate_key.casefold()
            if key in seen:
                continue
            seen.add(key)
            named = candidate.to_sheet_dict()
            fresh.append([named.get(name, "") for name in table.header])
        if not fresh:
            return 0
        self._append(TAB_CANDIDATES, fresh, table.header)
        logger.info("%s: added %d candidate(s).", TAB_CANDIDATES, len(fresh))
        return len(fresh)

    # -- Map_Pins -----------------------------------------------------------------
    @staticmethod
    def _pin_cells(row: RegistryRow) -> dict[str, str]:
        named = row.to_sheet_dict()
        return {
            "Project Key": named["Project Key"],
            "Project Name": row.project_name_en or row.project_name_ar,
            "Developer": row.developer,
            "Emirate": row.emirate,
            "Area": row.area_en,
            "Status": row.status,
            "Precision": row.location_precision or "unknown",  # unknown precision must never read as exact
            "Latitude": named["Latitude"],
            "Longitude": named["Longitude"],
            "Map Link": row.build_map_pin_link(),
        }

    def _select_pins(self, rows: Iterable[RegistryRow]) -> list[dict[str, str]]:
        chosen: list[RegistryRow] = []
        outside = 0
        for row in rows:
            if not isinstance(row, RegistryRow):
                raise self._fail(f"write_map_pins needs RegistryRow objects, got {type(row).__name__}")
            if not row.has_coordinates():
                continue
            if not in_union_bbox(row.latitude, row.longitude):
                outside += 1  # null island, swapped lat/lon, a pin on another continent: not a UAE pin
                continue
            chosen.append(row)
        if outside:
            logger.warning("write_map_pins: %d row(s) with coordinates outside the UAE were left out", outside)
        chosen.sort(key=lambda r: (_PIN_RANK.get(r.location_precision, 3), r.emirate,
                                   r.project_name_en.casefold(), r.project_key, r.latitude, r.longitude))
        return [self._pin_cells(r) for r in chosen]

    def write_map_pins(self, rows: Iterable[RegistryRow]) -> int:
        """
        Replace Map_Pins with the rows that have real coordinates: exact first, then
        community, then area, then unknown precision (shown as 'unknown'). Returns
        the number of pins. Map_Pins is derived data, so zero pins clears the tab
        (with a warning) instead of refusing.
        """
        pins = self._select_pins(rows)
        table = self._read_table(TAB_PINS)
        if not pins and table.rows:
            logger.warning("%s: no pins this run; clearing the %d previous pin(s)", TAB_PINS, len(table.rows))
        layout = list(table.header) + [h for h in MAP_PIN_HEADERS if h not in table.first]
        self._swap_rows(TAB_PINS, table, layout, [[pin.get(name, "") for name in layout] for pin in pins])
        return len(pins)

    # -- run log ------------------------------------------------------------------
    def _geocode_value(self, value: object) -> "int | float | str | bool | None":
        """A geocode counter as something JSON can hold: numbers stay numbers, text is scrubbed and clipped."""
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
        return _clip(self._scrub(" ".join(str(value).split())), GEOCODE_TEXT_LIMIT)

    def append_run_log(self, entry: RunLogEntry) -> None:
        """
        Append one row to Scrape_Run_Logs. Per-source info goes in ONE compact JSON
        column; exit code, registry rows, map pins and git sha get their own
        columns. Notes and URLs are scrubbed and clipped first.
        """
        if not isinstance(entry, RunLogEntry):
            raise self._fail(f"append_run_log needs a RunLogEntry, got {type(entry).__name__}")
        if not isinstance(entry.sources, Mapping) or not isinstance(entry.geocode, Mapping):
            raise self._fail("sources and geocode must be mappings")
        if isinstance(entry.exit_code, bool) or not isinstance(entry.exit_code, int):
            raise self._fail(f"exit_code must be an integer, got {entry.exit_code!r}")
        for label, stamp in (("started_at", entry.started_at), ("finished_at", entry.finished_at)):
            if not isinstance(stamp, str):
                raise self._fail(f"{label} must be a timestamp string, got {type(stamp).__name__}")
        for label, count in (("registry_rows", entry.registry_rows), ("map_pins", entry.map_pins)):
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise self._fail(f"{label} must be a non-negative integer, got {count!r}")
        sources = {}
        for name in sorted(entry.sources):
            info = entry.sources[name]
            if not isinstance(info, SourceRunInfo):
                raise self._fail(f"source {name!r} must be a SourceRunInfo")
            payload: dict[str, Any] = {"status": info.status, "rows": info.rows}
            note = _clip(self._scrub(" ".join(str(info.note).split())), SOURCE_NOTE_LIMIT)
            if note:
                payload["note"] = note
            sources[name] = payload
        geocode = {
            str(k): self._geocode_value(v) for k, v in sorted(entry.geocode.items(), key=lambda kv: str(kv[0]))
        }
        values = {
            "Started At": entry.started_at,
            "Started (Dubai time)": format_dubai_time(entry.started_at),
            "Finished At": entry.finished_at,
            "Exit Code": str(entry.exit_code),
            "Sources": _compact_json(sources),
            "Geocode": _compact_json(geocode),
            "Registry Rows": str(entry.registry_rows),
            "Map Pins": str(entry.map_pins),
            "Git SHA": _clip(self._scrub(entry.git_sha).strip(), GIT_SHA_LIMIT),
            "Notes": _clip(self._scrub(" ".join(str(entry.notes).split())), NOTES_LIMIT),
        }
        header = [_norm(c) for c in self._call(f"read the {TAB_RUN_LOGS} header", self._ws(TAB_RUN_LOGS).row_values, 1)]
        absent = [name for name in RUN_LOG_HEADERS if name not in header]
        if absent:
            raise self._fail(f"tab {TAB_RUN_LOGS} has no column {', '.join(absent)}; run open() to add it")
        self._append(TAB_RUN_LOGS, [[values.get(name, "") for name in header]], header)

    def last_source_counts(self) -> dict[str, int]:
        """
        Per source, the row count from the MOST RECENT run whose status for that
        source was ok. Failed, skipped and disabled runs never count, so a bad
        run cannot lower the baseline the row-drop guard compares against. 'Most
        recent' is by Started At (then sheet order), so re-sorting the tab by hand
        does not change the answer. Unreadable log rows are skipped with a warning.

        Only the names in config.SOURCE_NAMES are kept. The Sources cell is a cell a
        person can edit, its names would travel into the run state and the public log,
        and a name this code does not know has no adapter to compare a count with anyway.
        Those entries are counted in one log line and never named.
        """
        table = self._read_table(TAB_RUN_LOGS, require=("Started At", "Sources"))
        best: dict[str, tuple[tuple[str, int], int]] = {}
        unknown = 0
        for number, cells in table.rows:
            sources, not_known = self._parse_sources(table.get(cells, "Sources"), number)
            unknown += not_known
            stamp = (table.get(cells, "Started At").strip(), number)
            for name, (status, rows) in sources.items():
                if status == "ok" and (name not in best or stamp > best[name][0]):
                    best[name] = (stamp, rows)
        if unknown:
            logger.info(
                "%s: %d readable entry(ies) of the Sources cells have a name this code does not know; "
                "ignored (names not shown)",
                TAB_RUN_LOGS, unknown,
            )
        return {name: rows for name, (_, rows) in sorted(best.items())}

    @staticmethod
    def _parse_sources(text: str, number: int) -> tuple[dict[str, tuple[str, int]], int]:
        """
        The readable entries of one Sources cell for KNOWN sources (config.SOURCE_NAMES), and how
        many readable entries were for a name this code does not know (counted, never named).
        """
        if not text.strip():
            return {}, 0
        try:
            data = json.loads(text)
        except ValueError:
            logger.warning("%s row %d: the Sources cell is not valid JSON; ignored", TAB_RUN_LOGS, number)
            return {}, 0
        if not isinstance(data, dict):
            logger.warning("%s row %d: the Sources cell is not a JSON object; ignored", TAB_RUN_LOGS, number)
            return {}, 0
        out: dict[str, tuple[str, int]] = {}
        not_known = 0
        for position, (name, info) in enumerate(data.items(), start=1):
            status = info.get("status") if isinstance(info, dict) else None
            rows = info.get("rows") if isinstance(info, dict) else None
            if not (status in SOURCE_STATUSES and isinstance(rows, int) and not isinstance(rows, bool) and rows >= 0):
                # The position only: the source name is a cell a person can edit, and this log is public.
                logger.warning(
                    "%s row %d: entry %d of the Sources cell is unreadable; ignored", TAB_RUN_LOGS, number, position
                )
            elif name in SOURCE_NAMES:
                out[name] = (status, rows)
            else:
                not_known += 1
        return out, not_known

    # -- maintenance --------------------------------------------------------------
    def delete_rows(
        self, tab: str, row_numbers: Iterable[int], *, expect_keys: Optional[Mapping[int, str]] = None
    ) -> int:
        """
        Delete sheet rows (1-based numbers as the sheet shows them); returns how
        many. Bottom-up, so earlier numbers stay valid, and contiguous rows go in
        one request. The header row (1) is refused, as is any number beyond the
        last data row; nothing is deleted unless every number is acceptable.

        `expect_keys` ({row: Project Key}) is an optional guard against stale row
        numbers: if the key on a row is not the one expected, nothing is deleted.
        A delete is never repeated after an unknown outcome (see the module docstring).
        """
        ws = self._ws(tab)
        if (
            row_numbers is None
            or isinstance(row_numbers, (str, bytes, Mapping))
            or not hasattr(row_numbers, "__iter__")
        ):
            raise self._fail(f"row_numbers must be a sequence of integers, got {type(row_numbers).__name__}")
        numbers = list(row_numbers)
        for n in numbers:
            if isinstance(n, bool) or not isinstance(n, int):
                raise self._fail(f"row numbers must be integers, got {n!r}")
            if n == 1:
                raise self._fail(f"refusing to delete row 1 of {tab}: it is the header row")
            if n < 1:
                raise self._fail(f"{n} is not a valid row number")
        if not numbers:
            return 0
        wanted = sorted(set(numbers), reverse=True)
        table = self._read_table(tab, require=("Project Key",) if expect_keys else ())
        if wanted[0] > table.total_rows:
            raise self._fail(f"row {wanted[0]} is beyond the last data row ({table.total_rows}) of {tab}")
        if expect_keys:
            by_number = dict(table.rows)
            for number, expected in sorted(expect_keys.items()):
                actual = _norm(table.get(by_number.get(number, []), "Project Key"))
                if actual != _norm(expected):
                    # Row numbers only: a Project Key is a registry number or a developer and project name.
                    raise self._fail(f"{tab} row {number} no longer holds the Project Key it held when it was read; "
                                     "the sheet changed since, so nothing was deleted")
        runs: list[list[int]] = []
        for n in wanted:
            if runs and n == runs[-1][0] - 1:
                runs[-1][0] = n
            else:
                runs.append([n, n])
        if ws.row_count - len(wanted) < 2:  # Sheets refuses a delete that would leave no non-frozen row
            self._ensure_grid(ws, rows=len(wanted) + 2)
        for start, end in runs:
            self._call(f"delete {tab} rows {start}-{end}", ws.delete_rows, start, end, idempotent=False)
        logger.info("%s: deleted %d row(s).", tab, len(wanted))
        return len(wanted)


def _compact_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
