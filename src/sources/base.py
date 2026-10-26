"""
Source adapter contract (docs/DESIGN.md sections 4 and 12) and the small toolbox
every adapter shares.

The contract is deliberately blunt. An adapter either returns rows it can stand
behind or raises SourceError; it never returns a smaller or staler answer and
hopes nobody notices (DESIGN section 0, "fail loud"). Everything else in this
module exists to make that cheap to do correctly:

  * scrub_text / scrub_url: how text that has to be shown (a URL without its
    credentials, a header value) is made safe for a log line. A secret that is
    about to be echoed is replaced first. They are NOT permission to show a
    response body: the run log of a public repository may hold only counts, source
    names, field names, row numbers, HTTP status codes and fixed phrases, so a
    body, a provider's error text, a query or a URL never goes into a message.
  * send_with_retries: bounded retries on timeouts, 429 and 5xx; none on 401/403
    or other 4xx. A failure is reported by status code or exception class name
    only. SourceError is always raised OUTSIDE the except block, so the original
    exception (which can carry the secret) is neither the __cause__ nor the
    __context__ of what the caller sees.
  * RowTally: counts why rows were dropped or blanked, using fixed reason strings
    only, so notes can say what happened without ever quoting registry data.
  * record helpers: tolerant field lookup, because the live API schemas are not
    verified and a renamed column must produce a clear error, not empty rows.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Protocol, Sequence, TypeVar, runtime_checkable
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit, urlunsplit

import requests

from src.config import Config
from src.models import LaunchCandidate, RegistryRow, is_placeholder, parse_source_date

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
class SourceError(Exception):
    """Any failure of a source. Never swallowed: it makes the run exit non-zero."""


@dataclass
class SourceResult:
    """What one adapter produced. Registry adapters fill `rows`, gemini_news fills `candidates`."""

    source: str
    rows: list[RegistryRow]
    fetched_at: str
    source_refreshed: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    candidates: list[LaunchCandidate] = field(default_factory=list)


@runtime_checkable
class SourceAdapter(Protocol):
    name: str
    expects_rows: bool  # True when zero rows means the source is broken, not quiet

    def enabled(self, cfg: Config) -> bool:
        """Switched on AND configured."""

    def skip_reason(self, cfg: Config) -> str:
        """'' when enabled, else why not. Names settings, never their values."""

    def fetch(self) -> SourceResult:
        """Raises SourceError; never returns partial data silently."""


# A source returning fewer than this share of its previous run's rows is treated as
# failed (DESIGN section 4). Integer percent so the boundary is exact: 80 of 100 passes.
MIN_ROW_SHARE_PERCENT = 80


def check_row_count(
    source: str,
    new_count: int,
    previous_count: Optional[int],
    *,
    expects_rows: bool,
    allow_drop: bool,
) -> None:
    """
    Raise SourceError when a run's row count says the source is broken.

      * expects_rows and zero rows: always an error. ALLOW_ROW_DROP excuses a
        shrinking source, never an empty one (an empty answer from a registry is
        a block page or a schema change, not a quiet day).
      * previous_count set (and not zero) and the new count is below 80 percent of
        it: an error unless allow_drop. previous_count None is a first run.
    """
    if new_count < 0 or (previous_count is not None and previous_count < 0):
        raise ValueError("row counts cannot be negative")
    if expects_rows and new_count == 0:
        raise SourceError(
            f"{source}: returned 0 rows, but this source always has some "
            "(ALLOW_ROW_DROP does not excuse an empty answer)"
        )
    if previous_count and not allow_drop and new_count * 100 < previous_count * MIN_ROW_SHARE_PERCENT:
        raise SourceError(
            f"{source}: returned {new_count} row(s) against {previous_count} on the previous run, "
            f"below {MIN_ROW_SHARE_PERCENT} percent (set ALLOW_ROW_DROP=true only if the drop is real)"
        )


# ---------------------------------------------------------------------------
# Scrubbing: nothing from a response, an exception or a URL reaches a log or an
# error message without passing through here.
# ---------------------------------------------------------------------------
REDACTED = "[redacted]"
SNIPPET_LIMIT = 200
_SCAN_LIMIT = 4000  # bound regex work on a huge error page

_SENSITIVE_WORD = r"(?:key|secret|token|password|passwd|pwd|auth|signature)"
_SENSITIVE_NAME = rf"[\w.-]*{_SENSITIVE_WORD}[\w.-]*"
_SENSITIVE_NAME_RE = re.compile(_SENSITIVE_WORD, re.IGNORECASE)
_QUERY_PAIR_RE = re.compile(rf"(?<![\w.-])({_SENSITIVE_NAME})=([^&\s#\"']+)", re.IGNORECASE)
_JSON_PAIR_RE = re.compile(rf'("{_SENSITIVE_NAME}"\s*:\s*)"(?:[^"\\]|\\.)*"', re.IGNORECASE)
_AUTH_HEADER_RE = re.compile(r"\b((?:proxy-)?authorization\s*[:=]\s*)(?:(?:bearer|basic)\s+)?[^\s,;\"']+", re.IGNORECASE)
_BEARER_RE = re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE)
_USERINFO_RE = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@", re.IGNORECASE)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
_GOOGLE_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _secret_forms(secrets: Iterable[object]) -> list[str]:
    """Each secret plus the escaped forms it takes inside a URL or a JSON string."""
    forms: set[str] = set()
    for secret in secrets:
        text = secret if isinstance(secret, str) else ""
        text = text.strip()
        if not text:
            continue
        forms.add(text)
        forms.add(quote(text, safe=""))
        forms.add(quote_plus(text))
        forms.add(text.replace("\\", "\\\\").replace('"', '\\"'))
    return sorted(forms, key=len, reverse=True)  # longest first: no remnant of a longer secret


def scrub_text(value: object, secrets: Iterable[object] = (), limit: int = SNIPPET_LIMIT) -> str:
    """
    `value` (a body, an exception, anything) as one short line that is safe to log.

    Order matters: the known secret values go first (exact, plus their URL and
    JSON escaped forms), then anything shaped like a credential (key=..., JSON
    "client_secret": "...", Authorization / Bearer values, URL userinfo, JWTs,
    Google API keys), then whitespace is collapsed and the result is truncated.
    Truncating last means a cut can never leave half a secret visible.
    """
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    for form in _secret_forms(secrets):
        text = text.replace(form, REDACTED)
    text = _CONTROL_RE.sub(" ", text[:_SCAN_LIMIT])
    text = _AUTH_HEADER_RE.sub(rf"\1{REDACTED}", text)
    text = _BEARER_RE.sub(rf"\1 {REDACTED}", text)
    text = _JSON_PAIR_RE.sub(rf'\1"{REDACTED}"', text)
    text = _QUERY_PAIR_RE.sub(rf"\1={REDACTED}", text)
    text = _USERINFO_RE.sub(rf"\1{REDACTED}@", text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _GOOGLE_KEY_RE.sub(REDACTED, text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(limit - 3, 0)].rstrip() + "..."
    return text


def scrub_url(url: object) -> str:
    """A URL safe to log: no userinfo, no fragment, credential-looking query values redacted."""
    text = "" if url is None else str(url)
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return scrub_text(text, limit=300)
    netloc = host + (f":{port}" if port else "")
    pairs = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        secret_name = _SENSITIVE_NAME_RE.search(name) or field_key(name) in _CREDENTIAL_NAMES  # "sig" is not a substring match
        shown = REDACTED if secret_name else quote(value, safe="")
        pairs.append(f"{quote(name, safe='')}={shown}")
    return scrub_text(urlunsplit((parts.scheme, netloc, parts.path, "&".join(pairs), "")), limit=300)


# Query names that carry a credential. Matched on the END of the normalised name, so
# "api_key", "access-token" and "clientSecret" count while "keyword" and "author" do not.
_CREDENTIAL_NAME_ENDINGS = ("key", "token", "secret", "password", "passwd", "pwd", "signature")
_CREDENTIAL_NAMES = frozenset({"auth", "authorization", "sig"})


def url_has_credentials(url: object) -> bool:
    """
    True when a URL embeds userinfo (user:pass@host) or a credential-looking query value.
    Such a URL must not be stored in a sheet or shown on a map, whoever supplied it; an
    unparseable URL is treated the same way, because nothing about it can be vouched for.
    """
    try:
        parts = urlsplit("" if url is None else str(url))
        if parts.username or parts.password:
            return True
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return True
    for name, value in pairs:
        key = field_key(name)
        if value and (key in _CREDENTIAL_NAMES or key.endswith(_CREDENTIAL_NAME_ENDINGS)):
            return True
    return False


# ---------------------------------------------------------------------------
# Counting what happened to rows, without quoting them
# ---------------------------------------------------------------------------
class RowTally:
    """
    Why rows were dropped (gone) or adjusted (kept with a blank value).

    Reasons are fixed strings written in the adapters, never text taken from a
    row, so the notes that come out of here are safe for a public CI log.
    """

    def __init__(self) -> None:
        self._dropped: Counter[str] = Counter()
        self._adjusted: Counter[str] = Counter()

    def drop(self, reason: str, count: int = 1) -> None:
        self._dropped[reason] += count

    def adjust(self, reason: str, count: int = 1) -> None:
        self._adjusted[reason] += count

    @property
    def dropped(self) -> int:
        return sum(self._dropped.values())

    @property
    def adjusted(self) -> int:
        return sum(self._adjusted.values())

    def dropped_count(self, reason: str) -> int:
        return self._dropped[reason]

    def adjusted_count(self, reason: str) -> int:
        return self._adjusted[reason]

    @staticmethod
    def _describe(counter: Counter[str]) -> str:
        ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        return "; ".join(f"{reason} (x{count})" for reason, count in ordered)

    def dropped_summary(self) -> str:
        return self._describe(self._dropped)

    def notes(self) -> list[str]:
        lines = []
        if self._dropped:
            lines.append(f"dropped {self.dropped} row(s): {self._describe(self._dropped)}")
        if self._adjusted:
            lines.append(f"blanked a value on {self.adjusted} occasion(s): {self._describe(self._adjusted)}")
        return lines


# ---------------------------------------------------------------------------
# HTTP with bounded retries
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RetryPolicy:
    """Retries on timeouts, connection errors, 429 and 5xx. Waits double each time, capped."""

    max_attempts: int = 4
    backoff_seconds: float = 2.0
    max_backoff_seconds: float = 30.0
    max_retry_after_seconds: float = 60.0  # a server may ask for longer; a daily job will not wait for it

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")


DEFAULT_RETRY = RetryPolicy()
AUTH_FAILURE_STATUSES = frozenset({401, 403})


def user_agent(cfg: Config) -> str:
    """The identifying User-Agent (name plus optional contact). Never a browser string."""
    return cfg.geocode_user_agent


@contextmanager
def session_scope(session: Any) -> Iterator[Any]:
    """The caller's session as is, or a fresh one that is closed afterwards."""
    if session is not None:
        yield session
        return
    created = requests.Session()
    try:
        yield created
    finally:
        created.close()


def _status_of(response: Any) -> Optional[int]:
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _retry_after_seconds(response: Any, policy: RetryPolicy) -> Optional[float]:
    headers = getattr(response, "headers", None)
    try:
        raw = headers.get("Retry-After") if headers is not None else None
        seconds = float(str(raw).strip()) if raw is not None else None
    except (ValueError, AttributeError, TypeError):
        return None  # the HTTP-date form is not worth parsing: fall back to our own backoff
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, policy.max_retry_after_seconds)


def send_with_retries(
    send: Callable[[], Any],
    *,
    label: str,
    secrets: Iterable[object] = (),
    policy: RetryPolicy = DEFAULT_RETRY,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """
    Call `send()` (one HTTP request) until it returns a 2xx response.

    Timeouts, connection errors, 429 and 5xx are retried up to policy.max_attempts
    times (honouring a Retry-After of up to policy.max_retry_after_seconds).
    401 and 403 are NOT retried: the credentials are wrong or access is not
    approved, and repeating the call only risks a lock-out. Other statuses are not
    retried either. Failure is a SourceError carrying the status code, or the
    class name of the exception, and nothing else: never the response body (an
    error page can echo the query, and the log of a public repository is public),
    never an exception's text (it can carry the request URL). It is raised after
    the except block so no raw exception hangs off it. `secrets` is accepted so
    callers need not change; nothing that could hold one is formatted any more.
    """
    failure = "no attempt was made"
    attempts = 0
    for attempt in range(1, policy.max_attempts + 1):
        attempts = attempt
        retry_after: Optional[float] = None
        try:
            response = send()
        except requests.exceptions.Timeout as exc:
            failure, retryable = f"request timed out ({type(exc).__name__})", True
        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            failure = f"connection failed ({type(exc).__name__})"  # its text carries the request URL
            retryable = True
        except Exception as exc:  # noqa: BLE001 - not retryable, and its text may carry a secret or row content
            failure, retryable = f"request failed ({type(exc).__name__})", False
        else:
            status = _status_of(response)
            if status is None:
                failure, retryable = "the response had no status code", False
            elif 200 <= status < 300:
                return response
            else:
                note = " (credentials rejected or access not approved)" if status in AUTH_FAILURE_STATUSES else ""
                if 300 <= status < 400:
                    # Callers that carry credentials send with allow_redirects=False: a 307/308 would
                    # otherwise re-send a POST body (the client secret) to whatever host the answer names.
                    note = " (a redirect; redirects are not followed, so no credential can be sent to another host)"
                failure = f"HTTP {status}{note}"
                retryable = status == 429 or status >= 500
                if retryable:
                    retry_after = _retry_after_seconds(response, policy)
        if not retryable:
            break
        if attempt < policy.max_attempts:
            wait = (
                retry_after
                if retry_after is not None
                else min(policy.backoff_seconds * 2 ** (attempt - 1), policy.max_backoff_seconds)
            )
            logger.warning("%s: %s; retrying in %.1fs (attempt %d of %d)", label, failure, wait, attempt, policy.max_attempts)
            sleep(wait)
    suffix = f" (gave up after {attempts} attempts)" if attempts > 1 else ""
    raise SourceError(f"{label}: {failure}{suffix}")


# The Content-Type of an unexpected answer is the server's text too, so only these fixed names are ever shown.
_KNOWN_MEDIA_TYPES = frozenset({
    "text/html", "text/plain", "text/xml", "text/csv", "application/json", "application/problem+json",
    "application/xml", "application/xhtml+xml", "application/octet-stream", "application/pdf",
})


def parse_json_response(response: Any, *, label: str) -> Any:
    """
    The response body as JSON, or SourceError that states the status and size, never the body.

    The raw bytes are decoded as JSON is defined (UTF-8, BOM tolerated), NOT with the charset
    requests guessed from the headers: for a text/* answer without a charset requests assumes
    ISO-8859-1, which turns every Arabic name into mojibake that then looks like a valid name.
    A body that is not valid UTF-8 is an error, never a second guess in another encoding.
    """
    try:
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)):
            return json.loads(bytes(content))
        return response.json()
    except (ValueError, RecursionError):
        pass
    try:
        size = len(response.text)
    except Exception:  # noqa: BLE001
        size = -1
    headers = getattr(response, "headers", None) or {}
    try:
        # A header is the server's text too: only a well-known media type is shown, never its parameters.
        media_type = str(headers.get("Content-Type", "")).split(";")[0].strip().lower()
        content_type = media_type if media_type in _KNOWN_MEDIA_TYPES else ""
    except Exception:  # noqa: BLE001
        content_type = ""
    raise SourceError(
        f"{label}: the response was not valid JSON "
        f"(HTTP {_status_of(response)}, {size} chars, content-type {content_type or 'unknown'})"
    )


# ---------------------------------------------------------------------------
# Tolerant records: the live schemas are unverified, so names are matched loosely
# ---------------------------------------------------------------------------
MAX_TEXT_LEN = 500  # a longer cell is a runaway value, not a name: the row is refused

def field_key(name: object) -> str:
    """'PROJECT_EN', 'projectEn' and 'Project En' all become 'projecten'."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def normalise_record(raw: Mapping[Any, Any]) -> dict[str, Any]:
    """A record with normalised keys. When two raw keys collapse to one, the first wins."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        out.setdefault(field_key(key), value)
    return out


def first_match(record: Mapping[str, Any], aliases: Sequence[str], convert: Callable[[Any], Optional[T]]) -> Optional[T]:
    """The first alias, in priority order, whose value `convert` accepts (returns not None)."""
    for alias in aliases:
        if alias in record:
            converted = convert(record[alias])
            if converted is not None:
                return converted
    return None


# Characters that never belong in a project name or a claim: C0/C1 controls (a NUL or BEL in a cell),
# and the bidi overrides / isolates, zero-width space and BOM, which make a reader see a different text
# than the one stored. ZWNJ / ZWJ / LRM / RLM are left alone: Arabic text uses them.
_CONTROL_CHARS_RE = re.compile("[\x00-\x1f\x7f-\x9f]")
_HIDDEN_FORMAT_RE = re.compile("[\u200b\u202a-\u202e\u2066-\u2069\ufeff]")


def has_unsafe_chars(text: str) -> bool:
    """True when `text` holds a control character or a hidden bidi / zero-width character."""
    return bool(_CONTROL_CHARS_RE.search(text) or _HIDDEN_FORMAT_RE.search(text))


def tidy_text(text: str) -> str:
    """`text` with controls turned into spaces, hidden format characters removed, whitespace collapsed."""
    return " ".join(_HIDDEN_FORMAT_RE.sub("", _CONTROL_CHARS_RE.sub(" ", text)).split())


def as_text(value: object) -> Optional[str]:
    """A scalar as tidy text, or None for blanks, placeholders, booleans and containers."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = int(value) if value == int(value) else value
    if not isinstance(value, (str, int, float)):
        return None
    text = tidy_text(str(value))
    return None if is_placeholder(text) else text


def as_cell(value: object) -> Any:
    """A numeric-ish scalar passed through for the model to coerce; '0' stays a real value."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        return value.strip() or None
    return value if isinstance(value, (int, float)) else None


EPOCH_ZERO_DATE = "1970-01-01"  # what an unset timestamp becomes in many systems: not a project date


def as_date(value: object) -> Optional[str]:
    """YYYY-MM-DD via models.parse_source_date, with the epoch-zero null sentinel read as blank."""
    iso = parse_source_date(value)
    return None if iso is None or iso == EPOCH_ZERO_DATE else iso


def first_date(record: Mapping[str, Any], aliases: Sequence[str], tally: RowTally) -> Optional[str]:
    """
    The first alias, in priority order, holding a readable date. A date that is
    present but unreadable is counted: if the API ever changes its date format
    every date would silently go blank, and the count in the notes is how that
    gets noticed. The '0001' no-date sentinel is expected and not counted.
    """
    for alias in aliases:
        if alias not in record:
            continue
        raw = record[alias]
        iso = parse_source_date(raw)
        if iso == EPOCH_ZERO_DATE:
            tally.adjust("epoch-zero date read as blank")
        elif iso is not None:
            return iso
        elif raw is not None and not is_placeholder(raw) and "0001" not in str(raw):
            tally.adjust("date not readable")
    return None


_ARABIC_RE = re.compile("[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")  # Arabic, Supplement, Extended-A, Presentation Forms A and B
_LATIN_RE = re.compile(r"[A-Za-z]")


def is_arabic_only(text: str) -> bool:
    """True for text that has Arabic script and no Latin letter (a name for the AR column)."""
    return bool(_ARABIC_RE.search(text)) and not _LATIN_RE.search(text)


def describe_types(values: Iterable[object]) -> str:
    counts = Counter(type(v).__name__ for v in values)
    return ", ".join(f"{name} x{count}" for name, count in sorted(counts.items()))


LIST_KEYS_DEFAULT = ("data", "results", "result", "records", "items", "rows", "projects", "value", "response", "list", "d")
_TOTAL_KEYS = ("total", "totalcount", "totalrecords", "recordstotal", "odatacount")  # bare 'count' is ambiguous: omitted
_REFRESH_KEYS = ("lastupdated", "datemodified", "modifiedat", "lastmodified", "refreshedat", "updatedat", "dataasof")

# Every top-level name this code (or the registry answers seen so far) puts in an envelope, as field_key writes it.
# describe_keys may repeat a name from this set and no other: it is code-defined text, not the server's.
ENVELOPE_KEYS = tuple(
    dict.fromkeys(
        LIST_KEYS_DEFAULT
        + _TOTAL_KEYS
        + _REFRESH_KEYS
        + (
            "accesstoken", "token", "tokentype", "expiresin",
            "meta", "message", "error", "errors", "status", "success", "code",
            "count", "page", "pagesize", "offset", "limit", "next",
        )
    )
)


def describe_keys(keys: Iterable[object]) -> str:
    """
    The top-level names of a server answer, for an error message: how many there were and which of them
    are in ENVELOPE_KEYS. A name outside that set is never shown. When an answer is keyed by project name
    (or by anything else the registry supplies) the key IS data, and the log of a public repository is
    public. A known name is shown the way ENVELOPE_KEYS spells it, not the way the server did.
    """
    spelt = [field_key(k) for k in keys]
    if not spelt:
        return "none"
    known = [name for name in ENVELOPE_KEYS if name in spelt]
    return f"{len(spelt)} name(s); " + (f"known: {', '.join(known)}" if known else "none known")


def extract_records(payload: object, list_keys: Sequence[str] = LIST_KEYS_DEFAULT) -> Optional[list[Any]]:
    """
    The record list in a JSON payload: the payload itself when it is a list, else
    the list under the first matching envelope key, looking one level inside a
    nested envelope too ({"data": {"items": [...]}}). None when no list is found.
    An empty list is a valid answer; deciding whether it is acceptable is the caller's job.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return None
    by_key = {field_key(k): v for k, v in reversed(list(payload.items()))}
    for key in list_keys:
        if isinstance(by_key.get(key), list):
            return by_key[key]
    for key in list_keys:
        inner = by_key.get(key)
        if isinstance(inner, Mapping):
            inner_by_key = {field_key(k): v for k, v in reversed(list(inner.items()))}
            for inner_key in list_keys:
                if isinstance(inner_by_key.get(inner_key), list):
                    return inner_by_key[inner_key]
    return None


def _int_or_none(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def extract_total(payload: object) -> Optional[int]:
    """A total row count the envelope states under an unambiguous name, else None."""
    if not isinstance(payload, Mapping):
        return None
    scopes = [payload] + [v for v in payload.values() if isinstance(v, Mapping)]
    for scope in scopes:
        by_key = {field_key(k): v for k, v in reversed(list(scope.items()))}
        for key in _TOTAL_KEYS:
            total = _int_or_none(by_key.get(key))
            if total is not None:
                return total
    return None


def extract_refreshed(payload: object) -> Optional[str]:
    """The source's own refresh date when the envelope states one in a readable form."""
    if not isinstance(payload, Mapping):
        return None
    by_key = {field_key(k): v for k, v in reversed(list(payload.items()))}
    for key in _REFRESH_KEYS:
        iso = as_date(by_key.get(key))
        if iso:
            return iso
    return None


def resolve_today(today: Optional[date]) -> date:
    """The UTC date rows are stamped with; a datetime is reduced to its date."""
    if today is None:
        return datetime.now(timezone.utc).date()
    return today.date() if isinstance(today, datetime) else today
