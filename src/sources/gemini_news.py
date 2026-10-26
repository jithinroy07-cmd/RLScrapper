"""
Launch leads from Gemini with Google Search grounding (docs/DESIGN.md section 4).

Writes Launch_Candidates only, never the registry, and never promotes anything: a
person sets the Status. Because a language model will happily invent a plausible
project, the adapter keeps a lead only when it can point at where it came from:

  * the lead names a Source URL (http(s), no credentials in it), and
  * that URL belongs to a page the search tool actually retrieved: its address is
    one of the grounding sources the response lists, or its host is the same site
    as one of them.

Everything else is discarded and counted. The check proves the SITE was retrieved,
not that the page says what the lead claims; that is why candidates stay "new"
until a person confirms them.

Off by default (ENABLE_GEMINI_NEWS=true). The model name comes only from
GEMINI_MODEL: there is no default, because a model name nobody verified is worse
than none. The answer must be a JSON list; anything else is a SourceError, and so
is a response that proposes leads but carries no grounding at all (the search tool
was not used, which is a configuration fault, not a quiet day).

The API key is held by the SDK client. It never appears in a prompt, a log line or
an error. No exception text is shown at all: the SDK's error text can echo the
prompt (and so the scraped news snippets) as well as the key, and the log of a
public repository is public. A failed call is reported by the exception's class
name, its integer HTTP status and a fixed hint, nothing else.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from src.areas import normalise_emirate
from src.config import Config
from src.models import LaunchCandidate, is_http_url, utc_now_iso
from src.sources.base import (
    DEFAULT_RETRY,
    MAX_TEXT_LEN,
    RetryPolicy,
    RowTally,
    SourceError,
    SourceResult,
    as_text,
    extract_records,
    first_match,
    has_unsafe_chars,
    normalise_record,
    resolve_today,
    scrub_text,
    url_has_credentials,
)

logger = logging.getLogger(__name__)

SOURCE_NAME = "gemini_news"

GEMINI_TIMEOUT_MS = 120_000
LOOKBACK_DAYS = 14
MAX_ANSWER_CHARS = 200_000  # a list of leads is a few kilobytes; more is not the answer we asked for
MAX_CANDIDATES = 50
MAX_CLAIM_LEN = MAX_TEXT_LEN
MAX_URL_LEN = 2000  # a source link longer than this is not a page address; Sheets refuses a cell over 50,000 characters
# UNVERIFIED (observed shape, not documented): grounding chunks usually carry a redirect
# address on this host instead of the page's own address. It says nothing about the page,
# so its host is never used to corroborate a lead; only an exact match of the whole address is.
REDIRECT_HOSTS = frozenset({"vertexaisearch.cloud.google.com"})

PROMPT = (
    "Today is {today}. Use Google Search to find real-estate project launches announced in the "
    "last {days} days in Dubai or Sharjah, United Arab Emirates: new off-plan or newly released "
    "residential or mixed-use projects.\n"
    "Rules: report only what the search results you retrieved actually say. Do not guess and do "
    "not fill gaps from memory. Use an empty string for any field the sources do not state. "
    "Never include a project outside Dubai or Sharjah.\n"
    "Answer with ONLY a JSON array and no other text. Each element is an object with exactly "
    'these keys: "project_name", "developer", "emirate" (Dubai or Sharjah), "area", "claim" '
    '(one sentence on what the source says) and "source_url" (the full address of the page that '
    "says it). If nothing qualifies, answer []."
)

CANDIDATE_LIST_KEYS = ("candidates", "projects", "launches", "leads", "results", "items", "data")
NAME_ALIASES = ("projectname", "name", "project", "title")
DEVELOPER_ALIASES = ("developer", "developername", "developedby")
EMIRATE_ALIASES = ("emirate",)
AREA_ALIASES = ("area", "community", "location", "district")
CLAIM_ALIASES = ("claim", "summary", "description", "headline", "note")
URL_ALIASES = ("sourceurl", "url", "link", "source")

R_NOT_OBJECT = "lead is not an object"
R_NO_EMIRATE = "no emirate stated"
R_EMIRATE = "emirate is not Dubai or Sharjah"
R_NO_NAME = "no project name"
R_NO_URL = "no usable http(s) source URL"
R_URL_CREDENTIALS = "source URL carries credentials"
R_URL_TOO_LONG = f"source URL is longer than {MAX_URL_LEN} characters"
R_UNGROUNDED = "source URL is not one of the pages the search retrieved"
R_OVERSIZE = f"a text value is longer than {MAX_TEXT_LEN} characters"
R_INVALID = "lead failed model validation"
R_DUPLICATE = "same lead listed twice (first kept)"
A_CLAIM_CUT = f"claim cut to {MAX_CLAIM_LEN} characters"

# Exception class names (from httpx / requests / the SDK) that mean "try again".
_TRANSIENT_NAMES = frozenset(
    {
        "Timeout", "TimeoutException", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout",
        "ConnectError", "ReadError", "RemoteProtocolError", "ProtocolError", "ChunkedEncodingError",
    }
)
_HOST_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")
_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*[ \t]*\r?\n?(.*?)\r?\n?```\s*$", re.DOTALL)
_MISSING = object()


# ---------------------------------------------------------------------------
# Reading the answer
# ---------------------------------------------------------------------------
def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return _MISSING


def parse_answer(text: str) -> list[Any]:
    """
    The JSON list of leads in the model's answer. A ``` fence around it is removed and,
    failing that, the outermost [ ... ] span is tried. Anything else is a SourceError
    that states the length of the answer, never its text.
    """
    body = text.strip()
    fenced = _FENCE_RE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    payload = _loads(body)
    if payload is _MISSING:
        start, end = body.find("["), body.rfind("]")
        if 0 <= start < end:
            payload = _loads(body[start : end + 1])
    records = None if payload is _MISSING else extract_records(payload, CANDIDATE_LIST_KEYS)
    if records is None:
        raise SourceError(f"{SOURCE_NAME}: the answer ({len(text)} chars) is not the JSON list of leads that was asked for")
    return records


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _host(url: str) -> str:
    """Lower-case host without 'www.', or '' when there is none."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _chunk_domains(web: Any) -> set[str]:
    """The site(s) a grounding chunk stands for: its domain field, a domain-shaped title, or its own host."""
    found: set[str] = set()
    for value in (getattr(web, "domain", None), getattr(web, "title", None)):
        text = value.strip().lower() if isinstance(value, str) else ""
        text = text[4:] if text.startswith("www.") else text
        if _HOST_RE.match(text):
            found.add(text)
    uri = getattr(web, "uri", None)
    if isinstance(uri, str) and uri.strip():
        host = _host(uri.strip())
        if host and host not in REDIRECT_HOSTS and _HOST_RE.match(host):
            found.add(host)
    return found


def grounding_sources(response: Any) -> tuple[set[str], set[str], int]:
    """(domains, exact addresses, number of search queries) the response says it retrieved."""
    domains: set[str] = set()
    uris: set[str] = set()
    queries = 0
    for candidate in _as_list(getattr(response, "candidates", None)):
        meta = getattr(candidate, "grounding_metadata", None)
        if meta is None:
            continue
        queries += len(_as_list(getattr(meta, "web_search_queries", None)))
        for chunk in _as_list(getattr(meta, "grounding_chunks", None)):
            web = getattr(chunk, "web", None)
            if web is None:
                continue
            uri = getattr(web, "uri", None)
            if isinstance(uri, str) and uri.strip():
                uris.add(uri.strip())
            domains |= _chunk_domains(web)
    return domains, uris, queries


def is_grounded(url: str, domains: Iterable[str], uris: Iterable[str]) -> bool:
    """True when `url` is exactly a retrieved address, or is on the same site as a retrieved page."""
    if url in set(uris):
        return True
    host = _host(url)
    if not host or host in REDIRECT_HOSTS:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _url_text(value: object) -> Optional[str]:
    """A link as text, but a link holding a control or hidden bidi character is refused, not repaired."""
    if isinstance(value, str) and has_unsafe_chars(value):
        return None
    return as_text(value)


def parse_candidates(
    records: Iterable[Any],
    *,
    domains: set[str],
    uris: set[str],
    found_at: str,
    tally: RowTally,
) -> list[LaunchCandidate]:
    """Validated, grounded, de-duplicated leads. Every discard is counted in `tally`, none is guessed at."""
    kept: dict[str, LaunchCandidate] = {}
    for raw in records:
        if not isinstance(raw, Mapping):
            tally.drop(R_NOT_OBJECT)
            continue
        rec = normalise_record(raw)
        emirate_text = first_match(rec, EMIRATE_ALIASES, as_text)
        if emirate_text is None:
            tally.drop(R_NO_EMIRATE)
            continue
        emirate = normalise_emirate(emirate_text)
        if emirate is None:
            tally.drop(R_EMIRATE)
            continue
        name = first_match(rec, NAME_ALIASES, as_text)
        if name is None:
            tally.drop(R_NO_NAME)
            continue
        url = first_match(rec, URL_ALIASES, _url_text)
        if url is None or not is_http_url(url):
            tally.drop(R_NO_URL)
            continue
        if len(url) > MAX_URL_LEN:
            tally.drop(R_URL_TOO_LONG)
            continue
        if url_has_credentials(url):
            tally.drop(R_URL_CREDENTIALS)
            continue
        if not is_grounded(url, domains, uris):
            tally.drop(R_UNGROUNDED)
            continue
        developer = first_match(rec, DEVELOPER_ALIASES, as_text) or ""
        area = first_match(rec, AREA_ALIASES, as_text) or ""
        claim = first_match(rec, CLAIM_ALIASES, as_text) or ""
        if any(len(value) > MAX_TEXT_LEN for value in (name, developer, area)):
            tally.drop(R_OVERSIZE)
            continue
        if len(claim) > MAX_CLAIM_LEN:
            claim = claim[: MAX_CLAIM_LEN - 3].rstrip() + "..."
            tally.adjust(A_CLAIM_CUT)
        try:
            candidate = LaunchCandidate(
                emirate=emirate,
                project_name=name,
                developer=developer,
                area=area,
                claim=claim,
                source_url=url,
                found_at=found_at,
                status="new",
            )
        except ValueError:  # pydantic.ValidationError is a ValueError
            tally.drop(R_INVALID)
            continue
        if candidate.candidate_key in kept:
            tally.drop(R_DUPLICATE)
            continue
        kept[candidate.candidate_key] = candidate
    return list(kept.values())


# ---------------------------------------------------------------------------
# Calling the model
# ---------------------------------------------------------------------------
def _is_retryable(exc: BaseException) -> bool:
    """429, 408 and 5xx (the SDK's APIError carries .code), timeouts and dropped connections."""
    code = getattr(exc, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return code in (408, 429) or code >= 500
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return bool({cls.__name__ for cls in type(exc).__mro__} & _TRANSIENT_NAMES)


def _describe_failure(exc: BaseException) -> str:
    """The class name and the integer HTTP status, never the exception's text (it can echo the prompt)."""
    code = getattr(exc, "code", None)
    shown = f" HTTP {code}" if isinstance(code, int) and not isinstance(code, bool) else ""
    hint = " (the key, the model name or the request was rejected)" if code in (400, 401, 403, 404) else ""
    return f"{type(exc).__name__}{shown}{hint}"


_ENUM_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,39}")
UNRECOGNISED = "unrecognised value"


def _enum_text(value: Any) -> str:
    """
    An enum's name (SAFETY, PROHIBITED_CONTENT) only when it has the shape of one. The server
    chooses that text, and the SDK passes a value it does not know through as it was sent, so
    anything else (lower case, spaces, a long string) is a fixed phrase: it could be a note, a
    name or a prompt echoed back, and the log of a public repository is public.
    """
    name = getattr(value, "name", value)
    return name if isinstance(name, str) and _ENUM_NAME_RE.fullmatch(name) else UNRECOGNISED


def _finish_note(response: Any) -> str:
    """Why an answer has no text, from enum-valued fields only (never model text)."""
    reasons = []
    feedback = getattr(response, "prompt_feedback", None)
    block = getattr(feedback, "block_reason", None)
    if block is not None:
        reasons.append(f"prompt blocked: {_enum_text(block)}")
    for candidate in _as_list(getattr(response, "candidates", None)):
        finish = getattr(candidate, "finish_reason", None)
        if finish is not None:
            reasons.append(f"finish reason: {_enum_text(finish)}")
    return scrub_text("; ".join(reasons), limit=120)


class GeminiNewsAdapter:
    """Launch leads from grounded Gemini search. Candidates only, off by default."""

    name = SOURCE_NAME
    expects_rows = False

    def __init__(
        self,
        cfg: Config,
        *,
        client: Any = None,
        today: Optional[date] = None,
        sleep: Callable[[float], None] = time.sleep,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._today = today
        self._sleep = sleep
        self._retry = retry

    # -- switches ---------------------------------------------------------------
    def enabled(self, cfg: Config) -> bool:
        return bool(cfg.enable_gemini_news and cfg.gemini_api_key and cfg.gemini_model)

    def skip_reason(self, cfg: Config) -> str:
        if not cfg.enable_gemini_news:
            return "disabled: ENABLE_GEMINI_NEWS is not true (this source is off by default)"
        missing = [
            name
            for name, value in (("GEMINI_API_KEY", cfg.gemini_api_key), ("GEMINI_MODEL", cfg.gemini_model))
            if not value
        ]
        return "" if not missing else "not configured: " + ", ".join(f"{n} is not set" for n in missing)

    # -- the fetch --------------------------------------------------------------
    def fetch(self) -> SourceResult:
        if not self.enabled(self._cfg):
            raise SourceError(f"{self.name}: {self.skip_reason(self._cfg)}")
        today = resolve_today(self._today)
        response = self._generate(today)
        text = self._answer_text(response)
        records = parse_answer(text)
        domains, uris, queries = grounding_sources(response)
        if records and not domains and not uris:
            raise SourceError(
                f"{self.name}: the model proposed {len(records)} lead(s) but the response lists no grounding "
                "sources, so the search tool was not used; refusing ungrounded leads"
            )

        tally = RowTally()
        candidates = parse_candidates(
            records, domains=domains, uris=uris, found_at=utc_now_iso(), tally=tally
        )
        notes = [f"the model proposed {len(records)} lead(s); {len(candidates)} kept after the grounding checks"]
        notes.append(f"grounding: {queries} search quer(ies), {len(domains)} source site(s)")
        notes += tally.notes()
        if len(candidates) > MAX_CANDIDATES:
            notes.append(f"kept the first {MAX_CANDIDATES} of {len(candidates)} leads")
            candidates = candidates[:MAX_CANDIDATES]
        logger.info("%s: %d lead(s) kept from %d proposed", self.name, len(candidates), len(records))
        return SourceResult(
            source=self.name, rows=[], fetched_at=utc_now_iso(), source_refreshed=None, notes=notes, candidates=candidates
        )

    # -- internals ----------------------------------------------------------------
    def _load_sdk(self) -> tuple[Any, Any]:
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise SourceError(f"{self.name}: the google-genai package is not installed") from None
        return genai, types

    def _generate(self, today: date) -> Any:
        genai, types = self._load_sdk()
        client = self._client
        if client is None:
            client = self._new_client(genai, types)
        config = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])
        prompt = PROMPT.format(today=today.isoformat(), days=LOOKBACK_DAYS)

        failure = "no attempt was made"
        attempts = 0
        for attempt in range(1, self._retry.max_attempts + 1):
            attempts = attempt
            try:
                return client.models.generate_content(model=self._cfg.gemini_model, contents=prompt, config=config)
            except Exception as exc:  # noqa: BLE001 - classified below; its text (the key, the prompt) is never shown
                failure, retryable = _describe_failure(exc), _is_retryable(exc)
            if not retryable:
                break
            if attempt < self._retry.max_attempts:
                wait = min(self._retry.backoff_seconds * 2 ** (attempt - 1), self._retry.max_backoff_seconds)
                logger.warning("%s: %s; retrying in %.1fs (attempt %d of %d)", self.name, failure, wait, attempt, self._retry.max_attempts)
                self._sleep(wait)
        suffix = f" (gave up after {attempts} attempts)" if attempts > 1 else ""
        raise SourceError(f"{self.name}: {failure}{suffix}")

    def _new_client(self, genai: Any, types: Any) -> Any:
        try:
            return genai.Client(
                api_key=self._cfg.gemini_api_key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS)
            )
        except Exception as exc:  # noqa: BLE001 - its text may carry the key
            failure = _describe_failure(exc)
        raise SourceError(f"{self.name}: could not create the Gemini client: {failure}")

    def _answer_text(self, response: Any) -> str:
        try:
            text = response.text
        except Exception:  # noqa: BLE001 - the SDK raises on some blocked answers; the reason is read separately
            text = None
        if not isinstance(text, str) or not text.strip():
            note = _finish_note(response)
            raise SourceError(f"{self.name}: the model returned no text" + (f" ({note})" if note else ""))
        if len(text) > MAX_ANSWER_CHARS:
            raise SourceError(f"{self.name}: the answer is {len(text)} chars, over the {MAX_ANSWER_CHARS} limit")
        return text
