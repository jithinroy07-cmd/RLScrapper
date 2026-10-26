"""
Geocoder for Dubai and Sharjah registry rows (docs/DESIGN.md sections 5 and 12).

The one rule that shapes everything: an empty cell is better than a wrong pin.
Measured on the public OSM geocoder, a quarter of bare-name queries came back
as confident answers on another continent, and one qualified query landed on a
street 81 km from the real project while still inside the right emirate. So a
provider answer is never trusted; it is a CANDIDATE that must survive every
check below, and a candidate that fails any of them is a miss, not a pin.

Tiers (RegistryRow.geocode_tiers builds the queries; this module never rebuilds
variants). The first tier whose hit survives validation wins, and that tier, and
nothing else, sets Location Precision: an 'exact' hit is a project-name hit, a
'community' hit came from the Master Project query, an 'area' hit from the
Area (EN) query. A community or area centroid can therefore never be filed as
exact.

Validation of one candidate (every check must hold):
  1. the coordinates parse (providers send strings; plain ASCII decimals only);
  2. the provider's country, when it states one, is the UAE;
  3. it is not a street (OSM class highway, geoapify 'street', google 'route');
  4. it is not a whole emirate or a whole country (useless as a project location);
  5. the provider did not flag it as a partial match (google);
  6. the provider's state/emirate, when it states one, is the declared emirate
     (spelling variants tolerated, see emirate_from_state);
  7. the point is strictly inside the declared emirate's polygon
     (emirates.point_in_emirate). Bounding boxes never decide anything.
More apply to the 'exact' tier only, because a project-name query is the
one most likely to return a loosely related place:
  8. the hit is not a whole settlement or district (OSM city/town/suburb/
     district, an administrative boundary): that is an area centroid, not a
     project, and the community/area tiers exist to file it honestly;
  8b. NAME AGREEMENT (a design change made in the adversarial review; DESIGN
     section 5 lists it as the 'name-mismatch' check): the place the provider names must share
     its distinctive words with the row's own name (English, Arabic, or either
     with the Phase/developer suffix stripped); name_agrees() decides. Reason
     "name-mismatch". Without it a confident answer for a different place in
     the same community (a mosque, a car park, villa 12) became an exact pin.
     It is deliberately conservative: generic words (tower, residences, villa,
     park, burj, al, the, ...) and the developer brand never count, spelling
     variants are tolerated (Muwaileh/Muweilah, Mariam/Maryam), Arabic is folded
     before comparing, and whenever it cannot judge (the provider gave no name,
     or the only words left are generic, or title and row are in different
     scripts with nothing to compare) it AGREES. Only the exact tier is checked:
     the community and area tiers and the centroid lookups are not, because the
     registry's area names are often not the map's names.
  8c. Geoapify only: rank.match_type says only the street, postcode, city/district or
     country/state matched (the documented values match_by_street / _postcode /
     _city_or_disrict / _country_or_state): a locality answering for a project it
     does not know. Reason "coarse-match". Absent or any other value passes.
  9. the hit is within EXACT_MAX_CENTROID_KM (8 km) of the centroid of the
     community or area the row declares, when the row declares one;
  9b. and farther than SAME_POINT_KM (10 m) from it: a hit AT the centroid is the
     community itself (a neighbourhood called like the project, found by fuzzy
     search), and filing it as 'exact' would present a community centroid as a
     project pin. Reason "is-community-centroid"; the community tier then files it
     honestly.

How the centroid becomes known. It is NOT looked up for every row. It is
looked up lazily, only when an exact candidate has already passed checks 1 to 8
and is waiting for check 9. The declared places are tried in order: Master
Project, then Area (EN). Each is resolved with exactly the queries its own tier
would use (alias_variants(...)[:MAX_PLACE_VARIANTS] + the emirate qualifier)
and validated like any other hit (checks 1 to 7). The first that validates
gives (lat, lon). The result, including a genuine "could not be found", is
memoised per run by (emirate, canonical place), so the many projects of one
community share one lookup; and because the community tier of a failing row
issues the same queries, the provider-result cache makes that lookup free the
second time. A centroid lookup costs budget like any other lookup. Three cases:
  * the row declares neither a Master Project nor an Area (EN): there is no
    centroid to be known, check 9 does not apply, and the exact hit stands
    (checks 1 to 8 still did their job);
  * the row declares a place and its centroid is found: the 8 km gate applies;
  * the row declares a place but the centroid CANNOT be obtained (the provider
    does not know the place, or its answer failed validation): the exact hit is
    REJECTED ("no-centroid") rather than filed unverified. The 81 km miss above
    was inside the right emirate; only this gate catches that kind of error, and
    "blank beats invented". The cost is that a genuine exact hit in an
    OSM-unknown community is lost; the community/area tiers then run and will
    usually fail for the same reason, leaving the row unresolved. If the
    centroid lookup was merely INTERRUPTED (budget, time, quota, outage), the
    row is not judged at all: see "Rows are never falsely condemned".

Borders. A hit within BORDER_CHECK_KM (1.5 km) of the other emirate's polygon
gets the status suffix " border-check" and counts in border_checks: OSM draws
the Dubai/Sharjah border imperfectly (Al Nahda is split), so a human should
look at those pins.

Overrides (Geocode_Overrides) win over everything. They are matched on ANY of
a row's identity keys (registry key first; but a shared name key that several
DIFFERENT registry projects carry is ambiguous and pins none of them, because
DESIGN section 2 says rows with different registry keys are different projects), applied even when geocoding is
disabled and even if the backend later aborts, replace any coordinates the row
has, keep the human's precision (blank stays blank), set geocode_method
"override", and never touch the budget. A later lookup never overwrites one:
a row with coordinates is not queued. The pin is NOT validated against the
polygon (a human may mean it); an override outside the declared emirate is
applied and logged as a warning.

The queue. Rows with coordinates are skipped. Rows with a blank Geocode Status
(never tried) come first, in input order. Then rows whose status is
"unresolved <date>" with the date at least geocode_retry_cooldown_days old,
stalest first; a fresher failure is deferred. A status that is not "unresolved"
(e.g. "resolved ..." but the coordinates were lost), or has no usable date, is
eligible at once, and a date in the future (a typo) counts as no date, so it
can never defer a row forever. Ties keep input order, so a run is
deterministic. Rows with nothing to query (no name, no area) are left alone.

Rows are never falsely condemned. A row is stamped "unresolved <date>" only
when the provider actually answered every query it needed and none produced a
valid hit. If the run is cut short while a row is half done (lookup budget,
time budget, a quota, an outage), that row is left exactly as it was: a
never-tried row keeps its blank status and stays first in line next run; an
old failure keeps its old date.

Stopping.
  * geocode_max_per_run (0 = the backend default; public Nominatim 200) caps
    network lookups. A cached answer costs nothing. Retries of one lookup count
    once. When a lookup is needed and none is left the loop ends.
  * geocode_time_budget_seconds ends the loop too (checked before every row and
    before every request attempt, with the injected clock). The wait that paces a
    request is not cut short, so a run can end up to one pacing interval plus one
    request after the budget.
  * The first HTTP 429, any 401/402/403, or a quota status in a 200 body
    (google OVER_QUERY_LIMIT / OVER_DAILY_LIMIT / REQUEST_DENIED) ABORTS the
    whole run: GeocodeStats.aborted holds the reason, nothing is cached, the
    rest of the queue is untouched. (Retrying a "slow down" 429 with backoff
    is not done: the contract here is to stop at the first one, which is also
    what public Nominatim's policy wants.)
  * budget and time stops are normal and recorded in stop_reason, not aborted.

Transient failures (5xx, timeouts, connection errors, an unreadable 200 body):
each request is retried up to MAX_ATTEMPTS times with a backoff (through the
injected sleep). If every attempt fails the lookup is INCONCLUSIVE: it is not
cached, the row is left untouched (see above), and transient_failures counts
it. MAX_CONSECUTIVE_FAILURES inconclusive lookups (or HTTP 4xx rejections) in
a row abort the run, so an outage or a broken configuration cannot silently eat
the whole queue. Any other 4xx (the provider refuses this particular query)
is a miss for that query, is not cached, and counts toward the same breaker.
HTTP 404 is an ordinary "nothing matched" for LocationIQ only (its way of saying it).
For every other backend a 404 means a wrong URL or a broken proxy: it is treated
like a 5xx (retried, then inconclusive), so it is never cached as "no such place"
and no row is condemned for it.

Cache (config.GEOCODE_CACHE_FILE, git-ignored): an optimisation only. It is
keyed by backend + query and stores the provider's own (slimmed) result items,
never a decision: every check above re-runs on a cached item. Genuine "no
result" answers are cached; aborts, transient failures and bad requests never
are. Entries older than CACHE_TTL_DAYS are ignored, and a remembered MISS
(empty answer) is ignored once it is as old as geocode_retry_cooldown_days (a
same-day miss still answers for the rest of the day), so the cooldown that
schedules a retry is never answered by the stale local miss it was meant to
re-check. A missing, corrupt or odd-shaped file is ignored and rebuilt. The
file is written atomically through a per-process temp name. The cache grows
with every distinct query inside the TTL and is rewritten whole on each save.

Queries. Control and invisible format characters (NUL, zero-width and
left-to-right/right-to-left marks, the byte-order mark) are removed and white
space collapsed before a query is sent or used as a cache key (_clean_query):
they change nothing a human reads but make the provider answer a different
question and fragment the cache.

Public logs. This code runs in a public repository's Actions log, which may hold
only counts, backend names, HTTP status codes, a provider's fixed status token
(OVER_QUERY_LIMIT and the like) and fixed phrases written in this module. A
response body, a provider's error_message, a query, a project name, a URL or the
text of an exception is never logged and never put in an exception message: a
transport fault is named by its class, a refusal by its status code (and the
429 / quota decision is taken from the status code or token, never from text).
A debug record for a rejected candidate names the reason, not the query.

Secrets. The keyed backends send their key as a query-string parameter, and
requests puts the full URL, key included, into many exception messages. Those
messages are not logged (see above), and the configured secret values are removed
from the cache file before it is written. scrub_secrets (exported) is the text
scrubber for any caller that has to format provider or exception text anyway: it
removes the configured secret values (and their URL-encoded forms), any key=/
token=/secret=/password=/signature=/Authorization value (also in JSON and header
form), and anything shaped like a secret even when it is not configured (PEM
private-key blocks, Bearer/Basic credentials, AIza and ya29. tokens, JWTs). The
service-account setting is a JSON document: _secret_values takes it apart (plain
or base64) and registers the whole text, its JSON-escaped form, the private key's
body, each line of it, and the key id, client email (also its name and domain),
client id and project id (the member rule is src.log_policy.json_secret_members,
the one the log handler uses).

Rate limits. Backends are paced by BACKEND_SETTINGS using the injected clock
and sleep. Public Nominatim is paced at one request per 15 s with 200 lookups a
run: its usage policy (operations.osmfoundation.org/policies/nominatim, read
25 Oct 2026) allows an absolute maximum of 1 request per second and restricts
scripts that run at regular intervals to 4 requests per minute, and a daily
scheduled job is such a script. At that pace the default backend geocodes at most
200 projects a day, and fewer in practice, because one project can need several
lookups (up to three name variants, then the community, then the area, plus the
lookup of the centre of its community or area that an answer is checked against);
the real figure has not been measured. A registry of a few thousand projects
therefore takes weeks; a keyed backend (LocationIQ, Geoapify: see the setup guide)
is the way to go faster.
The User-Agent is cfg.geocode_user_agent plus cfg.geocode_contact when set; no
browser string is ever sent.

UNVERIFIED (no key and no network were available when this was written; the
request shapes follow the research digest and an earlier scraper's live
runs): every endpoint URL, the exact parameter names of locationiq,
geoapify and google, geoapify's result_type values, and the field names of all
response shapes (the Google result and component types were cross-checked with
the published Geocoding API documentation in the review; the others were not).
Geoapify's rank.match_type is used in one place only (check 8c above). Anything unrecognised is a miss or a loud failure, never a
guess. Run one real lookup per backend before trusting a new backend.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import quote, quote_plus

import requests

from src import emirates
from src.areas import (
    alias_variants,
    canonical_area,
    developer_brand,
    normalise_emirate,
    normalise_key,
    query_qualifier,
    strip_developer_suffix,
    strip_phase,
    tidy,
)
from src.config import DEFAULT_GEOCODE_USER_AGENT, GEOCODE_BACKENDS, GEOCODE_CACHE_FILE, Config
from src.log_policy import json_secret_members
from src.models import (
    MAX_PLACE_VARIANTS,
    PRECISIONS,
    TIER_EXACT,
    GeocodeOverride,
    RegistryRow,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BACKEND_SETTINGS",
    "GeoHit",
    "GeocodeStats",
    "Geocoder",
    "GeocoderConfigError",
    "LookupInterrupted",
    "LookupOutcome",
    "emirate_from_state",
    "geocode_pending",
    "haversine_km",
    "name_agrees",
    "resolve_backend",
    "scrub_secrets",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BACKEND_NOMINATIM = "nominatim"
BACKEND_LOCATIONIQ = "locationiq"
BACKEND_GEOAPIFY = "geoapify"
BACKEND_GOOGLE = "google"

# UNVERIFIED against the live services from this project (see module docstring).
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"
LOCATIONIQ_ENDPOINT = "https://us1.locationiq.com/v1/search"
GEOAPIFY_ENDPOINT = "https://api.geoapify.com/v1/geocode/search"
GOOGLE_ENDPOINT = "https://maps.googleapis.com/maps/api/geocode/json"

UAE_COUNTRY_CODE = "ae"
ACCEPT_LANGUAGE = "en"
RESULT_LIMIT = 1  # only the provider's best match is judged; a worse one is not a fallback

REQUEST_TIMEOUT_SECONDS = 20
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (2.0, 5.0)  # waits before attempt 2 and 3
MAX_CONSECUTIVE_FAILURES = 5
MAX_QUERY_CHARS = 300  # longer than any real name; a URL this long is refused by servers

EXACT_MAX_CENTROID_KM = 8.0  # DESIGN section 5
BORDER_CHECK_KM = 1.5  # DESIGN section 5
SAME_POINT_KM = 0.01  # an exact hit this close to the community centroid IS the centroid (10 m)
CACHE_TTL_DAYS = 30
CACHE_VERSION = 1

# Place ranks at or below these are a country / a state-level object in Nominatim's scheme.
_MAX_COUNTRY_RANK = 4
_MAX_EMIRATE_RANK = 9

_MIN_SECRET_LENGTH = 4
_REDACTED = "***"


@dataclass(frozen=True)
class BackendSettings:
    """Pacing and the per-run lookup ceiling of one backend."""

    delay: float  # minimum seconds between request starts
    budget: int  # lookups per run when GEOCODE_MAX_PER_RUN is 0
    needs_key: bool


# The budgets sit below each provider's published free allowance so one run cannot
# exhaust it. Public Nominatim: one request per 15 s, 200 a run (usage policy for
# scripts that run at regular intervals: 4 a minute; 200 x 15 s is 50 minutes of
# paced lookups, which fits inside the default time budget of 3300 s, 55 minutes).
# LocationIQ's 0.55 s pacing earned a 429 in the older project's live run, hence ~1 s. Google is
# billable and opt-in; 300 a run keeps a daily run inside its 10,000 free events a
# month (research digest; pricing UNVERIFIED, re-check before relying on it).
BACKEND_SETTINGS: dict[str, BackendSettings] = {
    BACKEND_NOMINATIM: BackendSettings(delay=15.0, budget=200, needs_key=False),
    BACKEND_LOCATIONIQ: BackendSettings(delay=1.05, budget=4500, needs_key=True),
    BACKEND_GEOAPIFY: BackendSettings(delay=0.25, budget=2800, needs_key=True),
    BACKEND_GOOGLE: BackendSettings(delay=0.05, budget=300, needs_key=True),
}

# Rejection reasons (stable strings: they appear in the run log).
REJECT_COORDINATES = "bad-coordinates"
REJECT_SHAPE = "bad-shape"
REJECT_COUNTRY = "wrong-country"
REJECT_STREET = "street"
REJECT_TOO_BROAD = "emirate-or-country"
REJECT_PARTIAL = "partial-match"
REJECT_STATE = "wrong-state"
REJECT_OUTSIDE = "outside-emirate"
REJECT_COARSE_EXACT = "coarse-for-exact"
REJECT_NAME = "name-mismatch"
REJECT_COARSE_MATCH = "coarse-match"
REJECT_NO_CENTROID = "no-centroid"
REJECT_IS_CENTROID = "is-community-centroid"
REJECT_FAR = "far-from-community"

# Result kinds, after provider-specific classification.
KIND_STREET = "street"
KIND_COUNTRY = "country"
KIND_EMIRATE = "emirate"
KIND_AREA = "area"  # a whole settlement / district / administrative area
KIND_PLACE = "place"  # anything else: a building, a development, a point of interest
_KINDS = frozenset({KIND_STREET, KIND_COUNTRY, KIND_EMIRATE, KIND_AREA, KIND_PLACE})

# OSM place=* values that are a whole settlement or district, not a development.
# neighbourhood / quarter / hamlet / locality are deliberately NOT here: named
# developments are often mapped as those, and rejecting them would lose real pins.
_AREA_PLACE_TYPES = frozenset(
    {"city", "town", "village", "suburb", "district", "borough", "county", "municipality",
     "city_district", "state_district", "postcode"}
)
_GEOAPIFY_AREA_TYPES = frozenset({"suburb", "district", "postcode", "city", "county"})
_GOOGLE_AREA_TYPES = frozenset(
    {"locality", "sublocality", "sublocality_level_1", "sublocality_level_2", "sublocality_level_3",
     "sublocality_level_4", "sublocality_level_5", "neighborhood", "colloquial_area", "postal_town",
     "administrative_area_level_2", "administrative_area_level_3", "administrative_area_level_4",
     "administrative_area_level_5", "administrative_area_level_6", "administrative_area_level_7",
     "postal_code"}
)
# Geoapify rank.match_type values that say only a PART of the query matched (the street, the
# postcode, the city or district, the country or state): for a project-name query that is a
# locality answering for a project it does not know. Other values (full_match, inner_part,
# match_by_building, anything new) pass.
_GEOAPIFY_COARSE_MATCHES = frozenset(
    {"match_by_street", "match_by_postcode", "match_by_city_or_disrict", "match_by_city_or_district", "match_by_country_or_state"}
)
# A Google result of these types is a junction or a road, not a project.
_GOOGLE_STREET_TYPES = frozenset({"route", "intersection"})
_GOOGLE_NAME_COMPONENTS = frozenset({"premise", "establishment", "point_of_interest", "natural_feature", "park", "airport"})
# Address parts that name no place finer than the emirate.
_COARSE_ADDRESS_KEYS = frozenset({"state", "region", "country", "country_code", "state_code"})


class GeocoderConfigError(ValueError):
    """The requested geocoding setup is unusable (e.g. google without a key). Messages name settings, never values."""


class LookupInterrupted(Exception):
    """
    A lookup could not give a verdict. `kind` is:
      'abort'        the run must stop (429, quota, rejected key, repeated failures);
      'budget'       no lookups left this run;
      'time'         the time budget is spent;
      'inconclusive' this one lookup failed after its retries.
    A row whose walk is interrupted is left untouched; it is never called a miss.
    """

    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------
_SECRET_PARAM_RE = re.compile(
    r"""(?ix)
    (
      (?:^|[?&;,\s"'(])
      [\w.-]*?
      (?:api[_-]?key|key|token|secret|signature|authorization|password|passwd|pwd|auth|sig)
      ["']?\s*[=:]\s*["']?
    )
    (?:bearer\s+|basic\s+)?
    [^&\s"',;#)]+
    """
)

# Secrets whose value is not known in advance but whose shape gives them away.
_SECRET_SHAPES: tuple[re.Pattern[str], ...] = (
    # A PEM private key, whether its line breaks are real or the two characters \n.
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),  # Google API key
    re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"),  # Google OAuth access token
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),  # a JWT
)


def scrub_secrets(text: object, secrets: Iterable[str] = ()) -> str:
    """
    `text` with every known secret value (and its URL-encoded forms) replaced, and
    any key=/token=/password=/Authorization value, PEM private key, bearer token,
    Google API key or JWT redacted whether or not it was known.

    WHY: the keyed geocoders take their key in the query string, and requests
    quotes the whole URL in connection errors. Anything that came from a URL, an
    exception or a response body goes through here before it is logged or raised.
    """
    try:
        out = str(text)
    except Exception:  # an exception whose own __str__ fails must not take the run down while it is being logged
        out = f"<{type(text).__name__}: text unavailable>"
    known = sorted({s for s in secrets if isinstance(s, str) and len(s) >= _MIN_SECRET_LENGTH}, key=len, reverse=True)
    for secret in known:
        for variant in (secret, quote(secret, safe=""), quote_plus(secret)):
            out = out.replace(variant, _REDACTED)
    for shape in _SECRET_SHAPES:
        out = shape.sub(_REDACTED, out)
    return _SECRET_PARAM_RE.sub(lambda m: m.group(1) + _REDACTED, out)


_SECRET_MEMBER_HINTS = ("key", "secret", "token", "password", "email", "client_id", "private")


def _json_secret_members(value: str) -> list[str]:
    """
    The secret parts of a service-account style JSON value (plain or base64): its
    private_key (whole, and every line of it, because an error may quote one
    line), private_key_id, client_email, client_id and any other member whose
    name says key / secret / token / password / email. [] when `value` is not
    such a JSON document. What counts as a secret member, and which identifiers of the
    account (client_email with its name and domain, client_id, project_id) are
    scrubbed too, is the rule of src.log_policy.json_secret_members, so this module
    scrubs what the log handler scrubs; the forms below are added on top of it.
    """
    text = value.strip()
    data: object = None
    document = ""
    for candidate in (text, _decode_base64(text)):
        if candidate.startswith("{"):
            try:
                data = json.loads(candidate)
            except (ValueError, RecursionError):
                continue
            document = candidate
            break
    if not isinstance(data, dict):
        return []
    out: list[str] = list(json_secret_members(document))
    for name, member in data.items():
        if not isinstance(member, str) or not member.strip():
            continue
        if not any(hint in str(name).lower() for hint in _SECRET_MEMBER_HINTS):
            continue
        out.append(member)
        out.append(json.dumps(member, ensure_ascii=False)[1:-1])  # the escaped form (\n as two characters)
        lines = [ln.strip() for ln in member.replace("\\n", "\n").splitlines()]
        out.extend(ln for ln in lines if len(ln) >= 8 and not ln.startswith("-----"))
        body = "".join(ln for ln in lines if ln and not ln.startswith("-----"))
        if len(lines) > 1 and len(body) >= 8:
            out.append(body)
    return out


def _decode_base64(text: str) -> str:
    try:
        return base64.b64decode(text, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def _secret_values(cfg: Any) -> tuple[str, ...]:
    names = (
        "gemini_api_key", "gcp_service_account_key", "spreadsheet_id", "data_dubai_api_key",
        "data_dubai_api_secret", "locationiq_api_key", "geoapify_api_key", "google_maps_api_key",
    )
    out: list[str] = []
    for name in names:
        value = getattr(cfg, name, "")
        if isinstance(value, str) and value:
            out.append(value)
            out.extend(_json_secret_members(value))
    return tuple(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# Backend choice
# ---------------------------------------------------------------------------
def _backend_key(cfg: Any, backend: str) -> str:
    return {
        BACKEND_LOCATIONIQ: getattr(cfg, "locationiq_api_key", ""),
        BACKEND_GEOAPIFY: getattr(cfg, "geoapify_api_key", ""),
        BACKEND_GOOGLE: getattr(cfg, "google_maps_api_key", ""),
    }.get(backend, "") or ""


def resolve_backend(cfg: Config) -> str:
    """
    nominatim | locationiq | geoapify | google.

    cfg.geocode_backend is honoured, with two safety rules. Google is only ever
    chosen when explicitly requested (its terms restrict storing results and it
    bills), and requesting it without a key raises GeocoderConfigError. A keyed
    free backend that is requested without its key is NOT selected: a warning is
    logged and the automatic choice is used. Automatic: locationiq if its key is
    set, else geoapify if its key is set, else public nominatim.
    """
    requested = (getattr(cfg, "geocode_backend", "") or "").strip().lower()
    if requested and requested not in GEOCODE_BACKENDS:
        # The setting's value is not echoed: it is typed by hand, and this text reaches the public Actions log.
        raise GeocoderConfigError(f"GEOCODE_BACKEND is not a supported backend; use one of {', '.join(GEOCODE_BACKENDS)}")
    if requested == BACKEND_GOOGLE:
        if not _backend_key(cfg, BACKEND_GOOGLE):
            raise GeocoderConfigError("GEOCODE_BACKEND=google needs GOOGLE_MAPS_API_KEY, which is not set")
        return BACKEND_GOOGLE
    if requested == BACKEND_NOMINATIM:
        return BACKEND_NOMINATIM
    if requested and _backend_key(cfg, requested):
        return requested
    if requested:
        logger.warning("GEOCODE_BACKEND=%s was requested but its API key is not set; not selecting it", requested)
    if _backend_key(cfg, BACKEND_LOCATIONIQ):
        return BACKEND_LOCATIONIQ
    if _backend_key(cfg, BACKEND_GEOAPIFY):
        return BACKEND_GEOAPIFY
    return BACKEND_NOMINATIM


def _identifying_user_agent(cfg: Any) -> str:
    """cfg.geocode_user_agent plus the contact, ASCII only (HTTP header values must be encodable)."""
    agent = " ".join((getattr(cfg, "geocode_user_agent", "") or "").split())
    contact = " ".join((getattr(cfg, "geocode_contact", "") or "").split())
    agent = agent.encode("ascii", "ignore").decode("ascii").strip() or DEFAULT_GEOCODE_USER_AGENT
    contact = contact.encode("ascii", "ignore").decode("ascii").strip()
    if contact and contact not in agent:
        agent = f"{agent} ({contact})"
    return agent


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GeoHit:
    """A provider answer that survived validation."""

    lat: float
    lon: float
    backend: str
    query: str
    kind: str  # KIND_PLACE or KIND_AREA (street / emirate / country never survive)
    state: str  # the emirate the provider reported, canonical, or "" when it reported none
    name: str  # provider display text, for debugging only
    from_cache: bool = False
    title: str = ""  # the matched object's own name, "" when the provider gave none (see name_agrees)
    match: str = ""  # geoapify rank.match_type, "" when the provider gave none


@dataclass(frozen=True)
class LookupOutcome:
    """
    What one lookup found. hit is None for a plain miss (rejection == "") and for a
    rejected candidate (rejection names the reason). `note` explains a lookup that
    sent no request ("empty-query", "query-too-long").
    """

    hit: Optional[GeoHit]
    rejection: str = ""
    from_cache: bool = False
    note: str = ""


@dataclass
class GeocodeStats:
    """
    What a geocode_pending run did. Counts are rows unless stated.

      attempted            queued rows the loop reached, including one a stop interrupted
      resolved/unresolved  rows given coordinates / stamped unresolved (overrides are separate)
      rejected             provider hits thrown away by validation (hits, not rows);
                           `rejections` breaks them down by reason
      skipped_over_budget  queued rows never started: lookup or time budget spent
      skipped_after_abort  queued rows never started: the run aborted
      aborted              why the run aborted ("HTTP 429", ...), "" when it did not
      stop_reason          a normal stop ("lookup budget", "time budget"), "" when none
    """

    backend: str
    attempted: int = 0
    resolved: int = 0
    unresolved: int = 0
    rejected: int = 0
    by_precision: dict[str, int] = field(default_factory=lambda: {p: 0 for p in PRECISIONS})
    overrides_applied: int = 0
    border_checks: int = 0
    skipped_over_budget: int = 0
    aborted: str = ""
    skipped_after_abort: int = 0
    deferred_cooldown: int = 0
    no_query: int = 0
    lookups: int = 0  # network lookups spent (retries count once)
    cache_hits: int = 0
    transient_failures: int = 0
    flush_failures: int = 0
    stop_reason: str = ""
    rejections: dict[str, int] = field(default_factory=dict)

    def as_log_dict(self) -> dict[str, int | str]:
        """Flat, JSON-friendly counters for Scrape_Run_Logs."""
        out: dict[str, int | str] = {
            "backend": self.backend,
            "attempted": self.attempted,
            "resolved": self.resolved,
            "unresolved": self.unresolved,
            "rejected": self.rejected,
        }
        for precision in PRECISIONS:
            out[f"resolved_{precision}"] = self.by_precision.get(precision, 0)
        out.update(
            overrides_applied=self.overrides_applied,
            border_checks=self.border_checks,
            skipped_over_budget=self.skipped_over_budget,
            skipped_after_abort=self.skipped_after_abort,
            deferred_cooldown=self.deferred_cooldown,
            no_query=self.no_query,
            lookups=self.lookups,
            cache_hits=self.cache_hits,
            transient_failures=self.transient_failures,
            flush_failures=self.flush_failures,
            aborted=self.aborted,
            stop_reason=self.stop_reason,
        )
        for reason, count in sorted(self.rejections.items()):
            out[f"rejected_{reason.replace('-', '_')}"] = count
        return out


# ---------------------------------------------------------------------------
# Geometry and state helpers
# ---------------------------------------------------------------------------
_EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((phi2 - phi1) / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


_ASCII_DECIMAL_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)", re.ASCII)


def _parse_coordinate(value: object, low: float, high: float) -> Optional[float]:
    """
    A finite float in [low, high], or None. Providers send strings, so strings are
    read, but only plain ASCII decimals: a decimal comma ("25,2"), underscores,
    exponents, non-ASCII digits and booleans are refused, not guessed at.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:  # an int too large for a float is not a coordinate
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not _ASCII_DECIMAL_RE.fullmatch(text):
            return None
        number = float(text)
    else:
        return None
    if not math.isfinite(number) or not low <= number <= high:
        return None
    return number


# Arabic names of the two emirates, hamza-folded, for providers that ignore accept-language.
_ARABIC_EMIRATES = {"دبي": "Dubai", "الشارقه": "Sharjah"}
_STATE_NOISE_WORDS = frozenset({"emirate", "emirates", "of", "the", "state", "united", "arab", "uae", "imarat", "imarah"})


def _fold_arabic(text: str) -> str:
    decomposed = unicodedata.normalize("NFKC", text)
    kept = "".join(ch for ch in decomposed if not unicodedata.combining(ch) and ch != "ـ")
    for source, target in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")):
        kept = kept.replace(source, target)
    return kept


def emirate_from_state(state: object) -> Optional[str]:
    """
    'Dubai' or 'Sharjah' from the state/emirate text a provider reports, else None.

    Tolerates the spellings seen in practice: 'Dubai Emirate', 'Emirate of Sharjah',
    'Dubayy', 'Sharjah - United Arab Emirates', mixed case, and the Arabic names.
    Anything else (Ajman, Abu Dhabi, gibberish) is None: a state that cannot be
    read as the declared emirate is a mismatch, never a pass.
    """
    if not isinstance(state, str):
        return None
    text = tidy(state)
    if not text:
        return None
    direct = normalise_emirate(text)
    if direct:
        return direct
    words = [w for w in normalise_key(text).split() if w not in _STATE_NOISE_WORDS]
    if words:
        stripped = normalise_emirate(" ".join(words))
        if stripped:
            return stripped
    arabic = [w for w in _fold_arabic(text).split() if w not in ("امارة", "اماره")]
    return _ARABIC_EMIRATES.get(" ".join(arabic))


# ---------------------------------------------------------------------------
# Provider responses -> a common candidate
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Candidate:
    lat: object
    lon: object
    kind: str
    state: str
    country: str  # lower-case alpha-2, or ""
    name: str
    partial: bool = False
    title: str = ""  # the matched object's OWN name (not its address chain); "" when the provider gave none
    match: str = ""  # geoapify rank.match_type ("" for every other provider)
    malformed: bool = False  # a field has the wrong type: the item cannot be classified, so it is not trusted


def _lower(value: object) -> str:
    return str(value).strip().lower() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _as_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _names_an_emirate_or_country(text: object) -> bool:
    key = normalise_key(text) if isinstance(text, str) else ""
    return bool(key) and (normalise_emirate(text) is not None or key in ("united arab emirates", "uae", "emirates"))  # type: ignore[arg-type]


def _osm_kind(raw: Mapping[str, Any], address: Mapping[str, Any]) -> str:
    """Classify a Nominatim / LocationIQ item. Order matters: a street is a street first."""
    cls = _lower(raw.get("class") or raw.get("category"))
    typ = _lower(raw.get("type"))
    addresstype = _lower(raw.get("addresstype"))
    rank = _as_int(raw.get("place_rank"))
    if cls == "highway":
        return KIND_STREET
    if typ == "country" or addresstype == "country" or (rank is not None and rank <= _MAX_COUNTRY_RANK):
        return KIND_COUNTRY
    if typ in ("state", "region") or addresstype in ("state", "region") or (rank is not None and rank <= _MAX_EMIRATE_RANK):
        return KIND_EMIRATE
    if cls in ("boundary", "place"):
        first_segment = str(raw.get("display_name") or raw.get("name") or "").split(",")[0]
        if _names_an_emirate_or_country(first_segment):
            return KIND_EMIRATE
        finer = [k for k in address if k not in _COARSE_ADDRESS_KEYS and not str(k).startswith("ISO3166")]
        if address and not finer:
            return KIND_EMIRATE
    if cls == "boundary" or (cls == "place" and typ in _AREA_PLACE_TYPES):
        return KIND_AREA
    return KIND_PLACE


def _first_segment(text: object) -> str:
    return str(text or "").split(",")[0].strip()[:200]


def _parse_osm_item(raw: Mapping[str, Any]) -> _Candidate:
    address = raw.get("address") if isinstance(raw.get("address"), dict) else {}
    own_name = raw.get("name")
    return _Candidate(
        lat=raw.get("lat"),
        lon=raw.get("lon"),
        kind=_osm_kind(raw, address),
        state=str(address.get("state") or ""),
        country=_lower(address.get("country_code")),
        name=str(raw.get("display_name") or raw.get("name") or "")[:200],
        title=str(own_name).strip()[:200] if isinstance(own_name, str) and own_name.strip() else _first_segment(raw.get("display_name")),
    )


def _parse_geoapify_item(raw: Mapping[str, Any]) -> _Candidate:
    props = raw.get("properties") if isinstance(raw.get("properties"), dict) else raw
    geometry = raw.get("geometry") if isinstance(raw.get("geometry"), dict) else {}
    coords = geometry.get("coordinates")
    lon_fallback, lat_fallback = (coords[0], coords[1]) if isinstance(coords, list) and len(coords) >= 2 else (None, None)
    result_type = _lower(props.get("result_type"))
    kind = {"street": KIND_STREET, "country": KIND_COUNTRY, "state": KIND_EMIRATE}.get(result_type)
    if kind is None:
        kind = KIND_AREA if result_type in _GEOAPIFY_AREA_TYPES else KIND_PLACE
    return _Candidate(
        lat=props.get("lat", lat_fallback),
        lon=props.get("lon", lon_fallback),
        kind=kind,
        state=str(props.get("state") or ""),
        country=_lower(props.get("country_code")),
        name=str(props.get("formatted") or props.get("name") or "")[:200],
        title=str(props["name"]).strip()[:200] if isinstance(props.get("name"), str) and props["name"].strip() else _first_segment(props.get("formatted")),
        match=_lower(props["rank"].get("match_type")) if isinstance(props.get("rank"), dict) else "",
    )


def _parse_google_item(raw: Mapping[str, Any]) -> _Candidate:
    geometry = raw.get("geometry") if isinstance(raw.get("geometry"), dict) else {}
    location = geometry.get("location") if isinstance(geometry.get("location"), dict) else {}
    raw_types = raw.get("types") or []  # missing / null is "none"; a non-list is malformed (below)
    components = raw.get("address_components") or []
    malformed = not isinstance(raw_types, list) or not isinstance(components, list)
    types = {t for t in raw_types if isinstance(t, str)} if isinstance(raw_types, list) else set()
    if types & _GOOGLE_STREET_TYPES:
        kind = KIND_STREET
    elif "country" in types:
        kind = KIND_COUNTRY
    elif "administrative_area_level_1" in types:
        kind = KIND_EMIRATE
    elif types & _GOOGLE_AREA_TYPES:
        kind = KIND_AREA
    else:
        kind = KIND_PLACE
    state = country = title = ""
    for component in components if isinstance(components, list) else []:
        if not isinstance(component, dict):
            continue
        component_types = component.get("types") or []
        if not isinstance(component_types, list):
            malformed = True
            component_types = []
        if "administrative_area_level_1" in component_types:
            state = str(component.get("long_name") or "")
        if "country" in component_types:
            country = _lower(component.get("short_name"))
        # The geocoding API does not return a place's name; only some results carry
        # it as a premise / establishment component. "" = unknown, never a guess.
        if not title and any(t in component_types for t in _GOOGLE_NAME_COMPONENTS):
            long_name = str(component.get("long_name") or "")[:200]
            title = long_name if any(ch.isalpha() for ch in long_name) else ""  # a bare number is no name
    return _Candidate(
        lat=location.get("lat"),
        lon=location.get("lng"),
        kind=kind,
        state=state,
        country=country,
        name=str(raw.get("formatted_address") or "")[:200],
        partial=raw.get("partial_match") is True,
        title=title,
        malformed=malformed,
    )


def _parse_item(backend: str, raw: Mapping[str, Any]) -> _Candidate:
    """One provider result item (already known to be a mapping) as a _Candidate."""
    if backend == BACKEND_GEOAPIFY:
        return _parse_geoapify_item(raw)
    if backend == BACKEND_GOOGLE:
        return _parse_google_item(raw)
    return _parse_osm_item(raw)


_BULKY_KEYS = frozenset(
    {"boundingbox", "geojson", "geokml", "svg", "polygonpoints", "licence", "extratags", "namedetails",
     "bbox", "datasource", "plus_code", "navigation_points", "viewport", "bounds", "photos", "icon"}
)


def _slim(value: Any, depth: int = 0) -> Any:
    """A JSON-safe, size-bounded copy of a provider item (drops bulky keys, caps text and lists)."""
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {str(k): _slim(v, depth + 1) for k, v in value.items() if k not in _BULKY_KEYS}
    if isinstance(value, list):
        return [_slim(v, depth + 1) for v in value[:10]]
    if isinstance(value, str):
        return value[:300]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (bool, int)):
        return value
    return str(value)[:300]


def _evaluate(
    candidate: _Candidate, declared: str, backend: str, query: str, from_cache: bool
) -> tuple[Optional[GeoHit], str]:
    """Checks 1 to 7 of the module docstring. Returns (hit, "") or (None, reason)."""
    if candidate.malformed:
        return None, REJECT_SHAPE
    lat = _parse_coordinate(candidate.lat, -90.0, 90.0)
    lon = _parse_coordinate(candidate.lon, -180.0, 180.0)
    if lat is None or lon is None:
        return None, REJECT_COORDINATES
    if candidate.country and candidate.country != UAE_COUNTRY_CODE:
        return None, REJECT_COUNTRY
    if candidate.kind == KIND_STREET:
        return None, REJECT_STREET
    if candidate.kind in (KIND_COUNTRY, KIND_EMIRATE):
        return None, REJECT_TOO_BROAD
    if candidate.partial:
        return None, REJECT_PARTIAL
    state = emirate_from_state(candidate.state) if candidate.state.strip() else None
    if candidate.state.strip() and state != declared:
        return None, REJECT_STATE
    if not emirates.point_in_emirate(lat, lon, declared):
        return None, REJECT_OUTSIDE
    hit = GeoHit(
        lat=lat, lon=lon, backend=backend, query=query,
        kind=candidate.kind if candidate.kind in _KINDS else KIND_PLACE,
        state=state or "", name=candidate.name, from_cache=from_cache, title=candidate.title,
        match=candidate.match,
    )
    return hit, ""


# ---------------------------------------------------------------------------
# Name agreement (check 8b, exact tier only)
# ---------------------------------------------------------------------------
# Words that name a KIND of building or an article, not THIS project: "Tower" is in
# half the names in Dubai. They never count as agreement on their own.
_GENERIC_NAME_WORDS = frozenset({
    "the", "by", "at", "of", "in", "on", "and", "for", "a", "an", "al", "el", "tower", "towers", "burj", "residence",
    "residences", "residency", "residential", "apartment", "apartments", "apt", "building", "buildings", "bldg",
    "block", "villa", "villas", "townhouse", "townhouses", "town", "homes", "home", "house", "heights", "park",
    "garden", "gardens", "court", "plaza", "centre", "center", "complex", "project", "phase", "stage", "hotel",
    "suites", "community", "development", "square", "view", "views", "dubai", "sharjah", "uae", "emirate",
    # Arabic: tower, towers, building, villa, villas, project, complex, apartments, residential
    "برج", "ابراج", "مبني", "عماره", "فيلا", "فلل", "مشروع", "مجمع", "شقق", "سكني",
})
_ROMAN_RE = re.compile(r"[ivx]{1,4}")
_APOSTROPHES_RE = re.compile("['‘’ʻʼʿʾ`´]")
_TOKEN_RE = re.compile(r"[^\W_]+")
_ARABIC_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f]")  # Arabic, Arabic Supplement
_PREFIX_MIN_CHARS = 5  # Garden / Gardens
_FUZZY_MIN_CHARS = 6  # Maryam / Mariam, Muwaileh / Muweilah
_FUZZY_RATIO = 0.75


def _name_tokens(text: object) -> list[str]:
    """
    Lower-case word tokens of a name, comparable across spelling noise: NFKC, case
    folded, diacritics and invisible marks removed, Arabic letters folded (hamza,
    ta marbuta, alef maksura), the Arabic article "ال" dropped from Arabic words,
    punctuation ignored. Digits stay (they are tokens; they just never decide).
    """
    if not isinstance(text, str):
        return []
    folded = _fold_arabic(text).casefold()
    folded = "".join(ch for ch in unicodedata.normalize("NFKD", folded)
                     if not unicodedata.combining(ch) and unicodedata.category(ch) not in ("Cf", "Cc"))
    folded = _APOSTROPHES_RE.sub("", folded)  # Za'abeel / Zabeel / Zaabeel are one word, not "za" + "abeel"
    tokens: list[str] = []
    for token in _TOKEN_RE.findall(folded):
        if _ARABIC_RE.search(token) and token.startswith("ال") and len(token) > 3:
            token = token[2:]
        tokens.append(token)
    return tokens


def _is_arabic(token: str) -> bool:
    return bool(_ARABIC_RE.search(token))


def _distinctive(tokens: Sequence[str], extra_generic: frozenset[str] = frozenset()) -> list[str]:
    """Tokens that say WHICH project this is: not generic, not a number / Roman numeral, not one letter."""
    return [
        t for t in tokens
        if len(t) > 1 and not t.isdigit() and t not in _GENERIC_NAME_WORDS and t not in extra_generic
        and not _ROMAN_RE.fullmatch(t)
    ]


def _tokens_match(a: str, b: str) -> bool:
    """Equal, or one a prefix/part of the other (Garden/Gardens, SobhaHartland), or near-identical spelling (Maryam/Mariam)."""
    if a == b:
        return True
    shortest = min(len(a), len(b))
    if shortest < _PREFIX_MIN_CHARS:
        return False
    if a.startswith(b) or b.startswith(a) or (shortest >= _FUZZY_MIN_CHARS and (a in b or b in a)):
        return True
    return shortest >= _FUZZY_MIN_CHARS and SequenceMatcher(None, a, b).ratio() >= _FUZZY_RATIO


def name_agrees(project_names: Iterable[str], title: object, extra_generic_words: Iterable[str] = ()) -> bool:
    """
    Does the object the provider returned (`title`: its own name, not its address)
    plausibly carry the name of the project that was asked for?

    A project-name query is the one most likely to return a loosely related place:
    the provider matches part of the query, or the address, and answers with a
    building that has nothing to do with the project, which then passes every
    geographic check when it happens to stand in the same community. This is the
    check that catches that. It is deliberately CONSERVATIVE in what it counts as
    disagreement:

      * Only DISTINCTIVE words count (not Tower / Residences / Al / numbers), and
        at least half of them (rounded up, minimum one) must appear in the title.
        A project made only of generic words falls back to its non-article words.
      * Spelling noise is tolerated: case, accents, punctuation, plurals, glued or
        split words, and near-identical spellings (Maryam / Mariam).
      * English and Arabic are compared like with like: an Arabic title is
        compared with the project's Arabic name. When no pair of names is
        comparable (the title is blank, or in a script the project has no name
        in), the check CANNOT judge and returns True: it never rejects on
        silence. The geographic gates still apply.
      * `extra_generic_words` (the developer's brand) are ignored unless nothing
        else is left, so "Acme Heights" does not agree with "Acme Head Office".

    True when the names agree or cannot be compared; False only on a real mismatch.
    """
    title_tokens = _name_tokens(title)
    if not title_tokens:
        return True
    extra = frozenset(w for e in extra_generic_words for w in _name_tokens(e))
    comparable = False
    for project in project_names:
        project_tokens = _name_tokens(project)
        for arabic in (False, True):
            wanted = [t for t in project_tokens if _is_arabic(t) == arabic]
            seen = [t for t in title_tokens if _is_arabic(t) == arabic]
            if not wanted or not seen:
                continue
            key_words = _distinctive(wanted, extra) or _distinctive(wanted)
            if not key_words:
                key_words = [t for t in wanted if len(t) > 1 and not t.isdigit() and t not in ("the", "of", "by", "at", "al", "el", "a", "an", "and")]
            if not key_words:
                return True  # nothing in the project name can be judged
            comparable = True
            matched = sum(1 for word in key_words if any(_tokens_match(word, t) for t in seen))
            if matched >= max(1, -(-len(key_words) // 2)):
                return True
    return not comparable


# ---------------------------------------------------------------------------
# The geocoder
# ---------------------------------------------------------------------------
_OK, _MISS, _BAD, _RETRY = "ok", "miss", "bad-request", "retry"
_GOOGLE_ABORT_STATUSES = frozenset({"OVER_QUERY_LIMIT", "OVER_DAILY_LIMIT", "REQUEST_DENIED"})
_GOOGLE_REFUSAL_STATUSES = frozenset({"INVALID_REQUEST"})  # the only other documented non-OK status
# Fixed phrases for the abort log line. A provider's own words (a 429 body, Google's error_message)
# can echo the query, so they are never logged; the status code or token is the evidence.
_DETAIL_KEY_REFUSED = "the key or plan was refused; check the API key"
_DETAIL_QUOTA_SPENT = "the provider's rate limit or quota is spent"
_QUOTA_ABORTS = frozenset({"HTTP 429", "Google OVER_QUERY_LIMIT", "Google OVER_DAILY_LIMIT"})


def abort_is_quota(reason: str) -> bool:
    """
    True when an abort reason (GeocodeStats.aborted) only means "out of quota or rate
    for today": the rows left over are retried tomorrow and the run is not a failure.
    Anything else (a refused or revoked key, a plan that must be paid, a backend that
    kept failing) needs a person, so the run must not stay green.
    """
    return reason in _QUOTA_ABORTS


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError(f"today must be a date, got {type(value).__name__}")


def _clean_query(query: object) -> str:
    """
    The query text as it is sent: whitespace collapsed, and control / invisible
    format characters (NUL, zero-width spaces, left-to-right and right-to-left marks,
    BOM) removed. Copy-pasted Arabic names carry such marks; a provider treats them
    as part of the word, so the name would not match and the row would be condemned.
    """
    if not isinstance(query, str):
        return ""
    visible = "".join(
        " " if ch.isspace() else ch for ch in query if ch.isspace() or unicodedata.category(ch) not in ("Cc", "Cf")
    )
    return tidy(visible)


class Geocoder:
    """
    One backend, paced and budgeted, with a local cache of provider results.

    lookup() is the whole contract for callers that want a single validated answer;
    lookup_detailed() also says WHY a candidate was thrown away, and raises
    LookupInterrupted when no verdict is possible. geocode_pending drives it.
    """

    def __init__(
        self,
        cfg: Config,
        *,
        session: Any = None,
        cache_file: Optional[Path | str] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        use_cache: bool = True,
    ):
        self.cfg = cfg
        self.backend = resolve_backend(cfg)
        settings = BACKEND_SETTINGS[self.backend]
        self.delay = settings.delay
        self.max_lookups = int(getattr(cfg, "geocode_max_per_run", 0) or 0) or settings.budget
        self.user_agent = _identifying_user_agent(cfg)
        self.session = session
        self.sleep = sleep
        self.clock = clock
        self.today: Optional[date] = None
        self.cache_file = Path(cache_file) if cache_file is not None else GEOCODE_CACHE_FILE
        self._use_cache = use_cache
        self._secrets = _secret_values(cfg)
        self._miss_ttl_days = max(0, int(getattr(cfg, "geocode_retry_cooldown_days", CACHE_TTL_DAYS) or 0))
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_dirty = False
        self._last_start: Optional[float] = None
        self._deadline: Optional[float] = None
        self._consecutive_failures = 0
        self._centroids: dict[tuple[str, str], Optional[tuple[float, float]]] = {}
        self.lookups_used = 0
        self.cache_hits = 0
        self.transient_failures = 0
        self.rejections: Counter[str] = Counter()
        self.aborted = ""
        self.stop_reason = ""
        if use_cache:
            self._load_cache()

    # -- run state -------------------------------------------------------------
    def begin_run(self, today: Optional[date] = None, time_budget_seconds: Optional[int] = None) -> None:
        """Reset the per-run counters, the centroid memo and the time budget. The cache is kept."""
        self.today = _as_date(today) if today is not None else None
        self.lookups_used = 0
        self.cache_hits = 0
        self.transient_failures = 0
        self.rejections = Counter()
        self.aborted = ""
        self.stop_reason = ""
        self._consecutive_failures = 0
        self._centroids = {}
        budget = time_budget_seconds if time_budget_seconds is not None else getattr(self.cfg, "geocode_time_budget_seconds", 0)
        self._deadline = self.clock() + budget if budget and budget > 0 else None

    @property
    def budget_remaining(self) -> int:
        return 0 if self.aborted else max(0, self.max_lookups - self.lookups_used)

    def out_of_time(self) -> bool:
        return self._deadline is not None and self.clock() >= self._deadline

    def _today(self) -> date:
        return self.today or datetime.now(timezone.utc).date()

    # -- cache -----------------------------------------------------------------
    @staticmethod
    def _valid_entry(entry: object) -> bool:
        if not isinstance(entry, dict) or not isinstance(entry.get("results"), list):
            return False
        if not isinstance(entry.get("fetched"), str):  # date.fromisoformat raises TypeError, not ValueError, on an int
            return False
        if not all(isinstance(item, dict) for item in entry["results"]):
            return False
        try:
            date.fromisoformat(str(entry.get("fetched")))
        except ValueError:
            return False
        return True

    def _load_cache(self) -> None:
        """Read the cache file; a missing, corrupt or odd-shaped file is simply ignored."""
        if not self.cache_file.is_file():
            return
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            logger.warning("geocode cache %s unreadable (%s); starting empty", self.cache_file.name, type(exc).__name__)
            return
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            logger.warning("geocode cache %s has an unexpected shape; starting empty", self.cache_file.name)
            return
        self._cache = {str(k): v for k, v in entries.items() if self._valid_entry(v)}
        dropped = len(entries) - len(self._cache)
        if dropped:
            logger.warning("geocode cache: ignored %d malformed entries", dropped)

    def _cache_key(self, query: str) -> str:
        return f"{self.backend}|{' '.join(query.lower().split())}"

    def _cache_get(self, key: str) -> Optional[list[dict]]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        age = (self._today() - date.fromisoformat(entry["fetched"])).days
        if age < 0 or age > CACHE_TTL_DAYS:
            return None
        if not entry["results"] and age > 0 and age >= self._miss_ttl_days:
            return None  # a remembered "no result" must not defeat the retry cooldown that scheduled this retry
        return entry["results"]

    def _cache_put(self, key: str, items: list[dict]) -> None:
        self._cache[key] = {"fetched": self._today().isoformat(), "results": items}
        self._cache_dirty = True

    def save_cache(self) -> None:
        """Write the cache atomically (best effort; a failure is logged, never raised)."""
        if not self._use_cache or not self._cache_dirty:
            return
        today = self._today()
        live = {
            key: entry for key, entry in self._cache.items()
            if 0 <= (today - date.fromisoformat(entry["fetched"])).days <= CACHE_TTL_DAYS
        }
        text = self._scrub_json(json.dumps({"version": CACHE_VERSION, "entries": live}, ensure_ascii=False, sort_keys=True))
        tmp = self.cache_file.with_name(f"{self.cache_file.name}.{os.getpid()}.tmp")  # per process: two writers never share a temp file
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.cache_file)
        except OSError as exc:
            logger.warning("could not write geocode cache %s (%s)", self.cache_file.name, type(exc).__name__)
            try:
                tmp.unlink()
            except OSError:
                pass
            return
        self._cache_dirty = False

    def _scrub_json(self, text: str) -> str:
        """Known secret values are removed from the serialised cache as a last line of defence."""
        for secret in self._secrets:
            if len(secret) >= _MIN_SECRET_LENGTH:
                text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], _REDACTED)
        return text

    # -- network ---------------------------------------------------------------
    def _http(self) -> Any:
        if self.session is None:
            self.session = requests.Session()
        return self.session

    def _pace(self) -> None:
        """Wait until `delay` seconds have passed since the previous request started, then mark this start."""
        if self._last_start is not None:
            wait = self.delay - (self.clock() - self._last_start)
            if wait > 0:
                self.sleep(wait)
        self._last_start = self.clock()

    def _build_request(self, query: str) -> tuple[str, dict[str, Any], dict[str, str]]:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        key = _backend_key(self.cfg, self.backend)
        if self.backend == BACKEND_LOCATIONIQ:
            params = {
                "key": key, "q": query, "format": "json", "limit": RESULT_LIMIT,
                "countrycodes": UAE_COUNTRY_CODE, "accept-language": ACCEPT_LANGUAGE, "addressdetails": 1,
            }
            return LOCATIONIQ_ENDPOINT, params, headers
        if self.backend == BACKEND_GEOAPIFY:
            params = {
                "text": query, "apiKey": key, "limit": RESULT_LIMIT, "filter": f"countrycode:{UAE_COUNTRY_CODE}",
                "lang": ACCEPT_LANGUAGE, "format": "geojson",
            }
            return GEOAPIFY_ENDPOINT, params, headers
        if self.backend == BACKEND_GOOGLE:
            params = {
                "address": query, "key": key, "components": f"country:{UAE_COUNTRY_CODE.upper()}",
                "language": ACCEPT_LANGUAGE,
            }
            return GOOGLE_ENDPOINT, params, headers
        params = {
            "q": query, "format": "jsonv2", "limit": RESULT_LIMIT, "countrycodes": UAE_COUNTRY_CODE,
            "accept-language": ACCEPT_LANGUAGE, "addressdetails": 1,
        }
        return NOMINATIM_ENDPOINT, params, headers

    def _abort(self, reason: str, detail: str = "") -> LookupInterrupted:
        """`reason` is a status code or token and `detail` a fixed phrase: neither may carry provider text (public log)."""
        if not self.aborted:
            self.aborted = reason
            # A quota stop is routine and retried tomorrow (main.py logs it as "Not an error");
            # anything else needs a person.
            level = logging.WARNING if abort_is_quota(reason) else logging.ERROR
            logger.log(level, "geocoder %s aborted the run: %s%s", self.backend, reason, f" ({detail})" if detail else "")
        return LookupInterrupted("abort", reason)

    def _note_failure(self, what: str) -> None:
        """Count a failed/refused lookup toward the circuit breaker; raises LookupInterrupted at the limit."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            raise self._abort(f"{self._consecutive_failures} consecutive backend failures", what)

    def _items_from_payload(self, payload: object) -> Optional[list[dict]]:
        """
        The result items of a decoded 200 body, or None when its shape is not the
        provider's (including a list with a non-object in it: a half-written
        answer must not be read as an empty one and cached as "no result").
        """
        if self.backend == BACKEND_GEOAPIFY:
            items = payload.get("features") if isinstance(payload, dict) else None
        elif self.backend == BACKEND_GOOGLE:
            items = payload.get("results") if isinstance(payload, dict) else None
        else:
            items = payload
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            return None
        return items

    def _attempt(self, url: str, params: dict[str, Any], headers: dict[str, str]) -> tuple[str, Any]:
        """One HTTP request, classified as (_OK, items) | (_MISS, []) | (_BAD, why) | (_RETRY, why). Aborts raise."""
        self._pace()
        try:
            response = self._http().get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:  # any transport fault is transient; its message carries the URL (and so the query): name the class only
            return _RETRY, f"{type(exc).__name__}"
        status = getattr(response, "status_code", None)
        if status in (401, 402, 403):
            raise self._abort(f"HTTP {status}", _DETAIL_KEY_REFUSED)
        if status == 429:
            raise self._abort("HTTP 429", _DETAIL_QUOTA_SPENT)  # never the body: a "slow down" page can echo the query
        if status == 404:
            if self.backend == BACKEND_LOCATIONIQ:
                return _MISS, []  # LocationIQ's way of saying "nothing matched"
            return _RETRY, "HTTP 404"  # elsewhere a wrong URL or a broken proxy: never a cached "no such place"
        if isinstance(status, int) and 500 <= status <= 599:
            return _RETRY, f"HTTP {status}"
        if status != 200:
            return _BAD, f"HTTP {status}"
        try:
            payload = response.json()
        except (ValueError, TypeError, RecursionError):
            return _RETRY, "response body was not JSON"
        if self.backend == BACKEND_GOOGLE and isinstance(payload, dict):
            verdict = payload.get("status")
            if not isinstance(verdict, str):
                return _RETRY, "response had an unexpected shape"
            if verdict in _GOOGLE_ABORT_STATUSES:  # the status token is fixed; error_message is Google's words and is not logged
                raise self._abort(f"Google {verdict}", _DETAIL_KEY_REFUSED if verdict == "REQUEST_DENIED" else _DETAIL_QUOTA_SPENT)
            if verdict == "ZERO_RESULTS":
                return _MISS, []
            if verdict == "UNKNOWN_ERROR":
                return _RETRY, "Google UNKNOWN_ERROR"
            if verdict != "OK":
                # Only a documented token is echoed; any other string is the provider's own text.
                return _BAD, f"Google {verdict}" if verdict in _GOOGLE_REFUSAL_STATUSES else "Google returned an unrecognised status"
        items = self._items_from_payload(payload)
        if items is None:
            return _RETRY, "response had an unexpected shape"
        return _OK, items

    def _fetch(self, query: str) -> tuple[list[dict], bool]:
        """
        Ask the provider. Returns (items, cacheable). Raises LookupInterrupted when no
        verdict is possible: out of budget or time, aborted, or inconclusive after retries.
        """
        if self.aborted:
            raise LookupInterrupted("abort", self.aborted)
        if self.lookups_used >= self.max_lookups:
            raise LookupInterrupted("budget", "lookup budget")
        if self.out_of_time():
            raise LookupInterrupted("time", "time budget")
        self.lookups_used += 1
        url, params, headers = self._build_request(query)
        problem = ""
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                self.sleep(RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)])
            if self.out_of_time():
                raise LookupInterrupted("time", "time budget")
            verdict, payload = self._attempt(url, params, headers)
            if verdict == _OK or verdict == _MISS:
                self._consecutive_failures = 0
                return [_slim(item) for item in payload[:RESULT_LIMIT]], True
            if verdict == _BAD:
                logger.warning("geocoder %s refused a query (%s)", self.backend, payload)
                self._note_failure(payload)
                return [], False
            problem = payload
            logger.warning("geocoder %s attempt %d/%d failed (%s)", self.backend, attempt + 1, MAX_ATTEMPTS, problem)
        self.transient_failures += 1
        self._note_failure(problem)
        raise LookupInterrupted("inconclusive", problem)

    def _results(self, query: str) -> tuple[list[dict], bool]:
        """Provider items for a query: the cache if it has them, else the network. (items, from_cache)."""
        key = self._cache_key(query)
        cached = self._cache_get(key) if self._use_cache else None
        if cached is not None:
            self.cache_hits += 1
            return cached, True
        items, cacheable = self._fetch(query)
        if cacheable and self._use_cache:
            self._cache_put(key, items)
        return items, False

    # -- public lookups --------------------------------------------------------
    def lookup_detailed(self, query: str, emirate: str) -> LookupOutcome:
        """
        One provider lookup for `query`, judged against the declared `emirate`
        (checks 1 to 7 of the module docstring; the exact-tier checks need the
        row and live in geocode_pending). Raises ValueError for an unsupported
        emirate and LookupInterrupted when no verdict is possible.
        """
        declared = normalise_emirate(emirate if isinstance(emirate, str) else None)
        if declared is None:
            raise ValueError(f"emirate must be Dubai or Sharjah, got a {type(emirate).__name__}")  # not the value: a row's cell
        text = _clean_query(query)
        if not text:
            return LookupOutcome(None, note="empty-query")
        if len(text) > MAX_QUERY_CHARS:
            return LookupOutcome(None, note="query-too-long")
        items, from_cache = self._results(text)
        if not items:
            return LookupOutcome(None, from_cache=from_cache)
        hit, reason = _evaluate(_parse_item(self.backend, items[0]), declared, self.backend, text, from_cache)
        if hit is None:
            self.note_rejection(reason)
        return LookupOutcome(hit, reason, from_cache)

    def lookup(self, query: str, emirate: str) -> Optional[GeoHit]:
        """
        One validated-or-rejected, rate-limited backend call. A rejected candidate,
        a miss and an interrupted lookup all give None (check .aborted / the log to
        tell an interruption from a miss); an unsupported emirate raises ValueError.
        """
        try:
            return self.lookup_detailed(query, emirate).hit
        except LookupInterrupted as stop:
            logger.info("geocoder lookup interrupted (%s)", stop.reason)
            return None

    def note_rejection(self, reason: str) -> None:
        self.rejections[reason] += 1
        logger.debug("geocoder %s rejected a candidate (%s)", self.backend, reason)  # never the query: it is a project name

    def centroid_for(self, emirate: str, places: Sequence[str]) -> Optional[tuple[float, float]]:
        """
        The (lat, lon) of the first of `places` (community first, then area) that the
        provider resolves to a VALID hit, or None when none does. Memoised per run by
        (emirate, canonical place); a genuine "not found" is memoised too, an
        interruption is not (it propagates). Costs budget like any lookup.
        """
        declared = normalise_emirate(emirate)
        if declared is None:
            raise ValueError(f"emirate must be Dubai or Sharjah, got a {type(emirate).__name__}")  # not the value: a row's cell
        qualifier = query_qualifier(declared)
        for place in places:
            key = (declared, normalise_key(canonical_area(place, declared)))
            if key not in self._centroids:
                found: Optional[tuple[float, float]] = None
                for variant in alias_variants(place, declared)[:MAX_PLACE_VARIANTS]:
                    hit = self.lookup_detailed(f"{variant}{qualifier}", declared).hit
                    if hit is not None:
                        found = (hit.lat, hit.lon)
                        break
                self._centroids[key] = found
            if self._centroids[key] is not None:
                return self._centroids[key]
        return None


# ---------------------------------------------------------------------------
# geocode_pending
# ---------------------------------------------------------------------------
_DATE_IN_STATUS_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _status_date(status: str, today: date) -> Optional[date]:
    """The last YYYY-MM-DD in a status, or None when absent, impossible, or in the future."""
    found = _DATE_IN_STATUS_RE.findall(status)
    if not found:
        return None
    try:
        moment = date.fromisoformat(found[-1])
    except ValueError:
        return None
    return None if moment > today else moment


def _apply_overrides(
    rows: list[RegistryRow], overrides: Mapping[str, GeocodeOverride], today: date, stats: GeocodeStats
) -> None:
    """Apply Geocode_Overrides in place on the working copy. Matches ANY identity key, registry key first."""
    matched: set[str] = set()
    chosen: dict[int, str] = {}
    registry_keys_of: dict[str, set[str]] = {}
    for index, row in enumerate(rows):
        key = next((k for k in row.identity_keys() if k in overrides), None)
        if key is not None:
            chosen[index] = key
            if row.registry_key():
                registry_keys_of.setdefault(key, set()).add(row.registry_key())
    ambiguous: set[str] = set()
    outside_boundary = 0
    for index, row in enumerate(rows):
        key = chosen.get(index)
        override = overrides.get(key) if key else None
        if not isinstance(override, GeocodeOverride):
            continue
        if len(registry_keys_of.get(key, ())) > 1:  # a registry-key override can never land here: it names exactly one row
            # One shared (name) key, several different registry projects: which did the human mean? DESIGN
            # section 2 says rows with different registry keys are different projects, so pin none of them.
            ambiguous.add(key)
            continue
        matched.add(key)
        stats.overrides_applied += 1
        if not emirates.point_in_emirate(override.latitude, override.longitude, row.emirate):
            outside_boundary += 1
        unchanged = (
            row.latitude == override.latitude and row.longitude == override.longitude
            and row.location_precision == override.precision and row.geocode_method == "override"
        )
        if unchanged:
            continue
        pinned = row.model_copy(update={
            "latitude": override.latitude,
            "longitude": override.longitude,
            "location_precision": override.precision,
            "geocode_method": "override",
            "geocode_status": f"resolved via override {today.isoformat()}",
        })
        rows[index] = pinned.model_copy(update={"map_pin_link": pinned.build_map_pin_link()})
    # Counts only: a key is a registry number or a developer and project name, and this log is public.
    if ambiguous:
        logger.warning("%d override key(s) are ambiguous: several different registry projects share them, "
                       "so none was pinned; key those overrides by the registry key instead", len(ambiguous))
    if outside_boundary:
        logger.warning("%d override pin(s) lie outside their emirate's boundary; applied anyway (a human pin wins)", outside_boundary)
    unmatched = len(overrides) - len(matched) - len(ambiguous)
    if unmatched:
        logger.info("%d geocode override(s) matched no registry row", unmatched)


def _build_queue(
    rows: Sequence[RegistryRow], cfg: Config, today: date, stats: GeocodeStats
) -> tuple[list[int], dict[int, list[tuple[str, list[str]]]]]:
    """
    Indices of the rows to geocode, in work order, with their tiers. Never-tried
    (blank status) first; then failures at least `cooldown` days old, stalest first;
    ties keep input order. See the module docstring for the status rules.
    """
    cooldown = int(getattr(cfg, "geocode_retry_cooldown_days", 30))
    keyed: list[tuple[tuple[int, str, int], int]] = []
    tiers_by_index: dict[int, list[tuple[str, list[str]]]] = {}
    for index, row in enumerate(rows):
        if row.has_coordinates():
            continue
        status = (row.geocode_status or "").strip()
        rank, stamp = 0, ""
        if status:
            moment = _status_date(status, today)
            if status.lower().startswith("unresolved") and moment is not None:
                if (today - moment).days < cooldown:
                    stats.deferred_cooldown += 1
                    continue
                rank, stamp = 1, moment.isoformat()
            else:
                rank = 1  # tried, but not a dated failure: eligible at once, ahead of dated ones
        tiers = row.geocode_tiers()
        if not tiers:
            stats.no_query += 1
            continue
        tiers_by_index[index] = tiers
        keyed.append(((rank, stamp, index), index))
    keyed.sort()
    return [index for _, index in keyed], tiers_by_index


def _hit_carries_row_name(row: RegistryRow, hit: GeoHit) -> bool:
    """Check 8b: the object the provider returned is called something like the project (see name_agrees)."""
    names: list[str] = []
    english = tidy(row.project_name_en)
    if english:
        stripped = strip_phase(strip_developer_suffix(english, row.developer))
        names.append(stripped or english)
    if tidy(row.project_name_ar):
        names.append(tidy(row.project_name_ar))
    brands = [developer_brand(d) for d in (row.developer, row.master_developer) if d and d.strip()]
    # Registry names often wear their community as a prefix ("MBR City District One"); those words say WHERE, not WHICH.
    places = [text for text in (row.master_project, row.area_en) if text and text.strip()]
    return name_agrees(names, hit.title, brands + places)


def _exact_hit_acceptable(geocoder: Geocoder, row: RegistryRow, hit: GeoHit) -> bool:
    """Checks 8 and 9 of the module docstring for a candidate from the exact tier."""
    if hit.kind == KIND_AREA:
        geocoder.note_rejection(REJECT_COARSE_EXACT)
        return False
    if hit.match in _GEOAPIFY_COARSE_MATCHES:
        geocoder.note_rejection(REJECT_COARSE_MATCH)
        return False
    if not _hit_carries_row_name(row, hit):
        geocoder.note_rejection(REJECT_NAME)
        return False
    places = [text for text in (row.master_project, row.area_en) if text and text.strip()]
    if not places:
        return True  # no declared place, so no centroid can be known and the gate does not apply
    centroid = geocoder.centroid_for(row.emirate, places)
    if centroid is None:
        geocoder.note_rejection(REJECT_NO_CENTROID)
        return False
    distance = haversine_km(hit.lat, hit.lon, centroid[0], centroid[1])
    if distance > EXACT_MAX_CENTROID_KM:
        geocoder.note_rejection(REJECT_FAR)
        return False
    if distance < SAME_POINT_KM:
        geocoder.note_rejection(REJECT_IS_CENTROID)  # the provider answered with the community itself
        return False
    return True


def _walk_tiers(
    geocoder: Geocoder, row: RegistryRow, tiers: Sequence[tuple[str, list[str]]]
) -> Optional[tuple[GeoHit, str]]:
    """The first (hit, tier) that passes every check, or None when the provider has no valid answer."""
    for tier, queries in tiers:
        for query in queries:
            hit = geocoder.lookup_detailed(query, row.emirate).hit
            if hit is None:
                continue
            if tier == TIER_EXACT and not _exact_hit_acceptable(geocoder, row, hit):
                continue
            return hit, tier
    return None


def _resolved_row(row: RegistryRow, hit: GeoHit, tier: str, today: date, stats: GeocodeStats) -> RegistryRow:
    status = f"resolved via {hit.backend} {tier} {today.isoformat()}"
    distance = emirates.distance_to_border_km(hit.lat, hit.lon, row.emirate)
    if not math.isnan(distance) and distance <= BORDER_CHECK_KM:
        status += " border-check"
        stats.border_checks += 1
    pinned = row.model_copy(update={
        "latitude": round(hit.lat, 6),
        "longitude": round(hit.lon, 6),
        "location_precision": tier,
        "geocode_method": hit.backend,
        "geocode_status": status,
    })
    return pinned.model_copy(update={"map_pin_link": pinned.build_map_pin_link()})


def _unresolved_row(row: RegistryRow, today: date) -> RegistryRow:
    stamped = row.model_copy(update={
        "latitude": None,
        "longitude": None,
        "location_precision": "",
        "geocode_method": "",
        "geocode_status": f"unresolved {today.isoformat()}",
    })
    # Always rebuilt: a link left over from coordinates a human cleared would still point at the old pin.
    return stamped.model_copy(update={"map_pin_link": stamped.build_map_pin_link()})


def geocode_pending(
    rows: Iterable[RegistryRow],
    overrides: Optional[Mapping[str, GeocodeOverride]],
    cfg: Config,
    *,
    today: date,
    geocoder: Optional[Geocoder] = None,
    flush: Optional[Callable[[list[RegistryRow]], None]] = None,
) -> tuple[list[RegistryRow], GeocodeStats]:
    """
    Pin the rows that need it. Returns (new rows in the input order, stats); the
    input rows are never mutated. Order of work and every rule: module docstring.

    `flush`, when given, is called with the FULL current row list (updated rows in
    their input positions, so a writer that replaces the sheet's data rows is
    safe) after every cfg.geocode_flush_every concluded rows (resolved or stamped
    unresolved: both are state worth keeping if a run is killed), and once more
    at the end of the run, clean, stopped or aborted. A flush that raises is
    logged and counted in stats.flush_failures; the run carries on and still
    returns its rows.
    """
    today = _as_date(today)
    if overrides is not None and not isinstance(overrides, Mapping):
        raise TypeError(f"overrides must be a mapping of project key to GeocodeOverride, got {type(overrides).__name__}")
    current: list[RegistryRow] = []
    for position, row in enumerate(rows):
        if not isinstance(row, RegistryRow):
            raise TypeError(f"row {position} is a {type(row).__name__}, expected RegistryRow")
        current.append(row.model_copy())
    stats = GeocodeStats(backend="disabled")
    _apply_overrides(current, overrides or {}, today, stats)

    def do_flush() -> None:
        if flush is None:
            return
        try:
            flush(list(current))
        except Exception as exc:  # durability is best effort; the caller still gets the rows
            stats.flush_failures += 1
            # The class only: the caller's writer talks to the Sheets API, whose error text can echo what was written.
            logger.error("geocode flush failed (%s)", type(exc).__name__)

    if not getattr(cfg, "geocode_enabled", True):
        logger.info("geocoding is disabled; only overrides were applied (%d)", stats.overrides_applied)
        do_flush()
        return current, stats

    try:
        geo = geocoder if geocoder is not None else Geocoder(cfg)
    except GeocoderConfigError:
        do_flush()  # keep the overrides already applied, then fail loudly
        raise
    stats.backend = geo.backend
    geo.begin_run(today, int(getattr(cfg, "geocode_time_budget_seconds", 0) or 0))
    queue, tiers_by_index = _build_queue(current, cfg, today, stats)
    flush_every = max(1, int(getattr(cfg, "geocode_flush_every", 250)))
    concluded = 0

    try:
        for position, index in enumerate(queue):
            left = len(queue) - position
            if geo.out_of_time():
                stats.stop_reason = geo.stop_reason = "time budget"
                stats.skipped_over_budget += left
                break
            stats.attempted += 1
            row = current[index]
            try:
                found = _walk_tiers(geo, row, tiers_by_index[index])
            except LookupInterrupted as stop:
                if stop.kind == "inconclusive":
                    continue  # this row stays as it was; the next one may still work
                if stop.kind == "abort":
                    stats.skipped_after_abort += left - 1  # stats.aborted is recorded from geo.aborted below
                else:
                    stats.stop_reason = geo.stop_reason = stop.reason
                    stats.skipped_over_budget += left - 1
                break
            if found is None:
                current[index] = _unresolved_row(row, today)
                stats.unresolved += 1
            else:
                hit, tier = found
                current[index] = _resolved_row(row, hit, tier, today, stats)
                stats.resolved += 1
                stats.by_precision[tier] = stats.by_precision.get(tier, 0) + 1
            concluded += 1
            if concluded % flush_every == 0:
                geo.save_cache()
                do_flush()
    finally:
        stats.lookups = geo.lookups_used
        stats.cache_hits = geo.cache_hits
        stats.transient_failures = geo.transient_failures
        stats.rejections = dict(geo.rejections)
        stats.rejected = sum(geo.rejections.values())
        if geo.aborted:
            stats.aborted = geo.aborted
        geo.save_cache()
        do_flush()
    logger.info("geocoding finished: %s", stats.as_log_dict())
    return current, stats
