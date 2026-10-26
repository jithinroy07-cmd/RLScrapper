"""
Exports every located project as GeoJSON for the static map page in site/
(docs/DESIGN.md section 7).

The page is a view, not a source of truth, so this script applies three rules and
nothing cleverer:

  * Only rows with BOTH coordinates, inside the coarse UAE gate, become pins. A
    half-written pair, a swapped lat/lng, (0, 0) or a point in another country is
    counted in meta.dropped and never drawn: a guess is not a pin.
  * The point must also lie strictly inside the boundary polygon of the emirate the
    REGISTRY gave the row (src.emirates.point_in_emirate; the coarse gate is only a
    rectangle). A Dubai row whose point is in Ajman, the sea or Sharjah is counted as
    outside_emirate and never drawn, whoever typed the coordinate (the geocoder, a
    Geocode_Overrides row or a hand edit of the Registry tab): this script is the last
    stop before the public file. A row whose emirate is not exactly Dubai or Sharjah
    is counted as unknown_emirate and never drawn: the emirate is not guessed from
    the point. A point exactly on a boundary line is inside neither polygon.
  * Precision is data. A blank Location Precision with coordinates is exported as
    "unknown", which the page draws as approximate. Nothing here can turn a
    centroid into an exact pin.
  * Properties are an explicit allow-list. A new sheet column reaches the public
    page only when someone adds it to ALLOWED_PROPERTIES on purpose.

Left out on purpose (the page is meant to be shareable, and the registry licence is
still open, DESIGN section 10):
  * Entered By and Permit No.: people and advertisements, not project facts.
  * Escrow Agent: not needed to find or judge a project, and the field is a bank
    name that some sources fill with account details. Cheap to omit, hard to
    retract once published.
  * Payment Plan and Source Licence: free text that can carry contact details, and
    licence terms that are an internal matter.
  * Any contact data. Handover is the only analyst-typed text that is exported, and
    it is dropped if it looks like an email, a link or a phone number (checked after
    Unicode NFKC folding, so a full-width "@" or circled digits do not hide one).
    Names of people and digits spelled out in words cannot be recognised and are not
    filtered: free text is analyst-reviewed.

Listing facts (price, bedrooms, ...) are exported only together with an http(s)
source URL. A fact whose source cannot be shown is not shown. A source URL is
scrubbed first: embedded credentials and parameters named like secrets or people are
removed, as is any parameter VALUE (or fragment) that carries an address, a chat link
or an international number, because a pasted signed or personal link would otherwise
be published to everyone. A link is withheld whole when its host is not public
(localhost, single-label and .local names, private and loopback addresses), when its
host or path is a chat link, or when its path holds a phone-number-sized digit run.

build_geojson is pure: no clock, no input mutation, no writes, identical output for the
same rows in any order (the only I/O is src.emirates reading the bundled boundary
file once and caching it). The CLI reads through SheetManager (imported lazily, so
this module and its tests need neither the sheet code nor credentials) and writes
the file atomically, so a crash can never leave a half-written pins.geojson behind.

Usage:
    python scripts/export_map_pins.py                  # -> site/pins.geojson
    python scripts/export_map_pins.py --out /tmp/p.geojson --min-pins 1
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import numbers
import os
import re
import sys
import tempfile
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlencode, urlsplit, urlunsplit

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.config import EMIRATES, Config, load_config  # noqa: E402
from src.emirates import in_union_bbox, point_in_emirate  # noqa: E402
from src.listing_data import MAX_BEDROOMS, PROPERTY_TYPES, parse_property_types  # noqa: E402
from src.log_policy import (  # noqa: E402
    configure_logging,
    install_excepthook,
    json_secret_members,
    secret_values,
    set_secrets,
)
from src.models import PRECISIONS, RegistryRow, is_http_url, parse_source_date, utc_now_iso  # noqa: E402

# The literal name is what src/log_policy.py lets through (OWN_LOGGERS); __name__ would be "__main__".
logger = logging.getLogger("export_map_pins")

DEFAULT_OUT = ROOT_DIR / "site" / "pins.geojson"

# Bumped when a property is renamed or removed; site/index.html refuses a file whose
# schema it does not know instead of silently drawing half of it.
SCHEMA_VERSION = 1

PRECISION_UNKNOWN = "unknown"  # a pin with coordinates but no recorded precision
PRECISION_ORDER = (*PRECISIONS, PRECISION_UNKNOWN)  # best first; also the sort order

ATTRIBUTION = (
    "Map data (c) OpenStreetMap contributors",
    "Open Database Licence (ODbL) 1.0: https://opendatacommons.org/licenses/odbl/1-0/",
)

# The only property names a pin may carry, in output order.
ALLOWED_PROPERTIES = (
    "project_key",
    "name",
    "name_ar",
    "developer",
    "emirate",
    "area",
    "master_project",
    "registry",
    "registry_no",
    "status",
    "percent_completed",
    "completion_date",
    "units",
    "precision",
    "price_from",
    "price_to",
    "price_per_sqft",
    "bedrooms_min",
    "bedrooms_max",
    "property_types",
    "handover",
    "source_url",
    "as_of",
)

# Sanity ceilings, like MAX_BEDROOMS in listing_data: a figure above these is an
# extra digit, a pasted phone number or a TOTAL typed into the per-sq-ft column, and
# would stretch the page's price slider until it was useless. They are not claims about
# the market. The per-sq-ft one has to be tight to do its job: Dubai and Sharjah rates
# sit well under AED 50,000 per sq ft even for the dearest prime units, so 100,000 keeps
# a wide margin and still refuses "850,000" (a villa price pasted into the rate column).
MAX_PRICE_AED = 10**10
MAX_PRICE_PER_SQFT_AED = 10**5
MAX_TEXT_CHARS = 300
MAX_HANDOVER_CHARS = 60
MAX_URL_CHARS = 2000

DROP_REASONS = (
    "no_coordinates", "half_written_coordinates", "out_of_range", "unknown_emirate", "outside_emirate", "duplicate_key",
)

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ExportError(Exception):
    """The export could not be completed. The message is always safe to log."""


# ---------------------------------------------------------------------------
# Scrubbing: nothing secret may reach a log line, an exception or the output file
# ---------------------------------------------------------------------------
_SECRET_NAME = r"[a-z0-9_.\-]*(?:key|token|secret|password|passwd|pwd|signature|sig|auth|credential|credentials)"
_SCRUBBERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), "[redacted private key]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s,;'\"]+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(rf"(?i)((?:^|[?&;\s'\"]){_SECRET_NAME}\s*=\s*)[^&\s'\"<>]+"), r"\1[redacted]"),
    (re.compile(rf'(?i)("{_SECRET_NAME}"\s*:\s*)"[^"]*"'), r'\1"[redacted]"'),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[redacted]@"),
)
_MIN_SECRET_LEN = 6  # a shorter "secret" would mangle ordinary words when replaced


def scrub_secrets(text: object, secrets: Iterable[str] = ()) -> str:
    """
    `text` with credentials removed: the known secret values first (exact match,
    which also catches a value in an unexpected shape), then the usual shapes -
    Authorization / Bearer headers, key= / token= query parameters, JSON secret
    members, PEM private keys and user:password@ in URLs.
    """
    out = str(text)
    for secret in sorted({s for s in secrets if isinstance(s, str) and len(s) >= _MIN_SECRET_LEN}, key=len, reverse=True):
        # HTTP libraries quote a whole URL into their error text, so the percent-encoded
        # spellings of a key are as good as the key itself.
        for variant in (secret, quote(secret, safe=""), quote_plus(secret)):
            out = out.replace(variant, "[redacted]")
    for pattern, replacement in _SCRUBBERS:
        out = pattern.sub(replacement, out)
    return out


# The list of what must never be printed (hidden Config fields, the members of a service-account
# key, the account's e-mail and ids) is shared with src/main.py: one list, one place to extend.
_json_secret_members = json_secret_members


# ---------------------------------------------------------------------------
# Small cleaners
# ---------------------------------------------------------------------------
# C0/C1 controls, plus the bidi embedding / override / isolate characters: one of
# those in a project name can reorder the text around it on screen.
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
_SECRET_PARAM_RE = re.compile(
    r"(?:key|token|secret|password|passwd|pwd|signature|sig|credential|credentials|auth|authorization|session|sessionid|sid|jwt|bearer)$|^x-amz-",
    re.I,
)
# A parameter that carries a person: dropped by name, or by an email-shaped value. Digit-heavy
# values are NOT dropped by value (a project id in a link is ten digits too).
_PERSONAL_PARAM_RE = re.compile(r"e[-_]?mail|phone|mobile|msisdn|whatsapp|contact|^(?:mail|tel)$", re.I)
_EMAIL_SHAPE_RE = re.compile(r"[^\s/?#&=;:@]+@[^\s/?#&=;@]+\.[^\s/?#&=;@]{2,}")
# An email, a link with or without a scheme (wa.me/..., t.me/..., bit.ly/..., example.com),
# and the usual "(at)" / "dot com" spellings of an address. Anything dotted like a host name
# is refused: a handover note has no reason to contain one, and a false alarm only blanks it.
_CONTACT_WORD_RE = re.compile(
    r"@|https?:|www\.|[\[(]\s*(?:at|dot)\s*[\])]|\bdot\s+(?:com|net|org|ae)\b|\b[a-z0-9][a-z0-9-]*\.[a-z]{2,}\b",
    re.I,
)
# Chat and messenger links: a phone number or a personal handle is the path of the link itself.
_CONTACT_LINK_HOSTS = (
    "wa.me", "wa.link", "whatsapp.com", "t.me", "telegram.me", "telegram.dog", "m.me", "signal.me",
)
# A run of digits joined by any of the separators people put inside a phone number.
_PHONE_RUN_RE = re.compile("\\+?\\d[\\d\\s().\\-/,;_|·•–—]*\\d")

# The suffix list above catches api_key, access_token, X-Amz-...; the next three match whole WORDS of a
# parameter name (camelCase is split first: userPass -> user_Pass), so "pass" and "code" can be refused
# without killing bypass, compass, country_code or postcode. A bare "code" is an OAuth / invite /
# verification code, and so are the qualified spellings; any other "..._code" is left alone.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SECRET_WORD_RE = re.compile(r"(?<![a-z0-9])(?:tokens?|tok|tkn|pass|passcode|passphrase|otp|tickets?)(?![a-z0-9])", re.I)
_SECRET_CODE_RE = re.compile(
    r"^(?:code|(?:access|auth|authorization|invite|invitation|verification|verify|reset|login|security|confirm|confirmation"
    r"|activation|sms)[-_.]?code)$",
    re.I,
)
_PERSONAL_WORD_RE = re.compile(r"(?<![a-z0-9])(?:cell(?:ular)?(?:[-_.]?(?:no|num|number))?|agents?)(?![a-z0-9])", re.I)
# A chat host anywhere in a decoded value or path, with or without a scheme or subdomain: the same
# list as _CONTACT_LINK_HOSTS, which is matched against the HOST of the link itself.
_CHAT_HOST_RE = re.compile(
    r"(?<![a-z0-9-])(?:[a-z0-9-]+\.)*(?:" + "|".join(re.escape(name) for name in _CONTACT_LINK_HOSTS) + r")(?![a-z0-9-])",
    re.I,
)
_CONTACT_SCHEME_RE = re.compile(
    r"(?<![a-z0-9+.-])(?:tel|sms|smsto|mailto|callto|whatsapp|tg|viber|skype|facetime)\s*:", re.I
)
# A path segment that is only a number: a phone number typed with spaces, dashes or dots, with or without a leading plus.
_NUMBER_SEGMENT_RE = re.compile(r"\+?[\d\s().\-_]+")
_MIN_PATH_PHONE_DIGITS = 9  # as for handover text; 8-digit listing ids and dates ("2027-12-31") stay under it
# Reserved or conventionally private top-level names: nothing a public visitor can reach.
_NON_PUBLIC_TLDS = frozenset(
    {"local", "localhost", "localdomain", "internal", "intranet", "lan", "home", "corp", "private", "arpa"}
)
# Ideographic full stops, which a browser reads as "." in a host name.
_DOT_FOLD = {0x3002: ".", 0xFF61: "."}


def _clean_str(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    """
    Display text: controls and bidi overrides removed, whitespace collapsed, bounded.
    Only text and plain numbers are text; a list, dict or object would be published
    as its Python repr, which is neither the analyst's data nor anything to show.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    text = str(value).encode("utf-8", "replace").decode("utf-8")  # a lone surrogate cannot be written
    text = " ".join(_CONTROL_RE.sub(" ", text).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _whole_number(value: object, ceiling: int, floor: int = 1) -> Optional[int]:
    """A finite whole number in [floor, ceiling], or None. Strings and booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number) or number != int(number):
        return None
    result = int(number)
    return result if floor <= result <= ceiling else None


def _fold(text: str) -> str:
    """
    `text` as the contact checks should read it. Invisible format characters (left-to-right
    marks, zero-width spaces, soft hyphens ...) ride along when a number is copied from a chat
    app or a web page and would split its digit run in two. NFKC turns a full-width "@" or
    "." and circled or full-width digits into the characters the patterns look for, and an
    ideographic full stop is the "." a browser reads it as. Only for testing: never published.
    """
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return unicodedata.normalize("NFKC", text).translate(_DOT_FOLD)


def _looks_like_contact(text: str) -> bool:
    """
    An email, a link, or a phone-number-shaped run: 9 or more digits, or 7 or more
    after a leading "+". Years and ranges ("2027-2028", "Q4 2027", "12/2027") stay
    well under that, so ordinary handover wording is not caught.
    """
    text = _fold(text)
    if _CONTACT_WORD_RE.search(text):
        return True
    for run in _PHONE_RUN_RE.findall(text):
        digits = sum(ch.isdigit() for ch in run)
        if digits >= 9 or (run.startswith("+") and digits >= 7):
            return True
    return False


def _handover(value: object) -> str:
    """The analyst's handover text, or '' if it is long or looks like contact data."""
    text = _clean_str(value, MAX_HANDOVER_CHARS + 1)
    if len(text) > MAX_HANDOVER_CHARS or _looks_like_contact(text):
        return ""
    return text


def _unquoted(text: str) -> str:
    """`text` with percent-escapes decoded until none is left (https%253A... is a second layer), then folded."""
    for _ in range(4):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return _fold(text)


def _contact_in_value(text: str) -> bool:
    """
    Whether a query value or fragment carries a person: an address, a chat link (with or
    without a scheme), a tel: / mailto: / whatsapp: link, or an international number
    ("+" and 7 or more digits). A bare long number is NOT a contact here: it is as
    likely a project id (pid=202610251234), and the parameter NAME already covers phone=.
    """
    text = _unquoted(text)
    if _EMAIL_SHAPE_RE.search(text) or _CHAT_HOST_RE.search(text) or _CONTACT_SCHEME_RE.search(text):
        return True
    return any(run.startswith("+") and sum(ch.isdigit() for ch in run) >= 7 for run in _PHONE_RUN_RE.findall(text))


def _phone_in_path(text: str) -> bool:
    """
    Whether a path (or fragment) holds a phone-number-sized run: _MIN_PATH_PHONE_DIGITS or more
    digits in a row, or a segment that is only a number with separators ("971-50-123-4567").
    A number cannot be cut out of a path without breaking the link, so the caller withholds the link.
    """
    text = _unquoted(text)
    if re.search(rf"\d{{{_MIN_PATH_PHONE_DIGITS},}}", text):
        return True
    return any(
        _NUMBER_SEGMENT_RE.fullmatch(segment) and sum(ch.isdigit() for ch in segment) >= _MIN_PATH_PHONE_DIGITS
        for segment in text.split("/")
    )


def _is_public_host(host: str) -> bool:
    """
    Whether a visitor on the internet could reach `host` (urlsplit's hostname: lower case, no brackets).
    Not: an address that is private, loopback, link-local, multicast or otherwise not global; a name with
    no dot (localhost, intranet, wiki); a reserved private top-level name (printer.local); or a short
    IPv4 spelling (127.1, 2130706433, 0x7f.1), which a browser still resolves although no top-level
    name is numeric.
    """
    name = _fold(host).rstrip(".").lower()
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        address = None
    if address is not None:
        return address.is_global and not address.is_multicast
    labels = name.split(".")
    if len(labels) < 2 or labels[-1] in _NON_PUBLIC_TLDS:
        return False
    return not re.fullmatch(r"\d+|0x[0-9a-f]*", labels[-1])


def _private_param(name: str, value: str) -> bool:
    """Whether a query (or fragment) parameter must not be published: a secret or a person by NAME, or a contact by VALUE."""
    spaced = _CAMEL_RE.sub("_", name)
    return bool(
        _SECRET_PARAM_RE.search(name) or _SECRET_WORD_RE.search(spaced) or _SECRET_CODE_RE.match(spaced)
        or _PERSONAL_PARAM_RE.search(name) or _PERSONAL_WORD_RE.search(spaced)
        or _contact_in_value(value)
    )


def sanitize_public_url(value: object) -> str:
    """
    An http(s) URL that is safe to publish, or ''.

    Removed: user:password@ (a credential, and also a phishing trick); query and
    fragment parameters whose NAME looks like a secret (api_key, token, pass, code,
    otp, X-Amz-...) or a person (phone, cell, agent, email), or whose decoded VALUE is
    an address, a chat link or an international number; a fragment that is one.
    Everything else is kept so the link still goes where the analyst pointed it.
    Withheld whole, because the person or the machine is not in a part that can be cut
    out: a chat host (wa.me ...) as the host or in the path, a path holding a phone-number-
    sized digit run or an address, a host a public visitor cannot reach (localhost,
    single-label and .local names, private and loopback addresses).
    Not http(s), whitespace inside, unparsable or over MAX_URL_CHARS: ''.
    """
    if not isinstance(value, str) or len(value) > MAX_URL_CHARS or not is_http_url(value) or _CONTROL_RE.search(value):
        return ""
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return ""
    # The person is in the PATH of a chat link (wa.me/9715...), where no parameter filter looks.
    bare_host = _fold(host).rstrip(".")  # "wa.me." (fully qualified) reaches the same server, and so does a full-width spelling
    if any(bare_host == name or bare_host.endswith("." + name) for name in _CONTACT_LINK_HOSTS):
        return ""
    if not _is_public_host(bare_host) or _phone_in_path(parts.path) or _CHAT_HOST_RE.search(_unquoted(parts.path)):
        return ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host

    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        kept = [(k, v) for k, v in pairs if not _private_param(k, v)]
        if len(kept) != len(pairs):
            query = urlencode(kept)
    fragment = parts.fragment
    if fragment and (
        _contact_in_value(fragment)
        or (
            any(_private_param(k, v) for k, v in parse_qsl(fragment, keep_blank_values=True))
            if "=" in fragment
            else _phone_in_path(fragment)  # a router-style fragment (#/agents/9715...); "#pid=2026..." is a parameter, not a path
        )
    ):
        fragment = ""
    cleaned = urlunsplit((parts.scheme.lower(), netloc, parts.path, query, fragment))
    # An address left in the path (/unsubscribe/ahmed@example.com) cannot be cut out without
    # breaking the link, so the whole link is withheld: a fact without a shown source is not shown.
    if _EMAIL_SHAPE_RE.search(_unquoted(cleaned)):
        return ""
    try:
        cleaned.encode("utf-8")  # a lone surrogate would make the whole file unwritable
    except UnicodeEncodeError:
        return ""
    return cleaned if is_http_url(cleaned) else ""


def normalise_stamp(value: object) -> str:
    """
    `generated_at` as 2026-10-25T03:00:00Z. Accepts a timezone-aware datetime or an
    ISO-8601 string with an offset (Z or +hh:mm); anything else raises, because a
    timestamp without a zone is ambiguous and this file is read in another time zone.
    """
    if isinstance(value, datetime):
        return utc_now_iso(value)
    if not isinstance(value, str):
        raise TypeError(f"generated_at must be an ISO-8601 string or an aware datetime, got {type(value).__name__}")
    text = value.strip()
    candidate = text[:-1] + "+00:00" if text[-1:] in ("Z", "z") else text
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        raise ValueError(f"generated_at is not an ISO-8601 timestamp: {value!r}") from None
    if moment.tzinfo is None:
        raise ValueError(f"generated_at has no time zone: {value!r}")
    return utc_now_iso(moment)


# ---------------------------------------------------------------------------
# Listing facts
# ---------------------------------------------------------------------------
def _listing_facts(record: object) -> Optional[dict[str, Any]]:
    """
    The exportable facts of one Listing_Data record (the shape listing_data.
    load_listing_records returns), or None when it has nothing to show.

    A record without a publishable source URL yields nothing: DESIGN section 0
    requires every price / bedroom / handover value to carry its source. A range
    that runs backwards (price from above price to, bedrooms min above max) is an
    entry error and loses BOTH ends: we cannot know which end is wrong.
    """
    if not isinstance(record, Mapping):
        return None
    source = sanitize_public_url(record.get("source_url"))
    if not source:
        return None

    facts: dict[str, Any] = {}
    low = _whole_number(record.get("price_from"), MAX_PRICE_AED)
    high = _whole_number(record.get("price_to"), MAX_PRICE_AED)
    if low is not None and high is not None and low > high:
        low = high = None
    for name, value in (("price_from", low), ("price_to", high)):
        if value is not None:
            facts[name] = value
    per_sqft = _whole_number(record.get("price_per_sqft"), MAX_PRICE_PER_SQFT_AED)
    if per_sqft is not None:
        facts["price_per_sqft"] = per_sqft

    beds_min = _whole_number(record.get("bedrooms_min"), MAX_BEDROOMS, floor=0)
    beds_max = _whole_number(record.get("bedrooms_max"), MAX_BEDROOMS, floor=0)
    if beds_min is not None and beds_max is not None and beds_min > beds_max:
        beds_min = beds_max = None
    for name, value in (("bedrooms_min", beds_min), ("bedrooms_max", beds_max)):
        if value is not None:
            facts[name] = value

    raw_types = record.get("types")
    if isinstance(raw_types, str):
        raw_types = parse_property_types(raw_types)
    if isinstance(raw_types, (list, tuple)):
        named = [t for t in raw_types if isinstance(t, str)]
        types = [t for t in dict.fromkeys(named) if t in PROPERTY_TYPES and t != "Unknown"]
        if types:
            facts["property_types"] = types

    handover = _handover(record.get("handover"))
    if handover:
        facts["handover"] = handover

    if not facts:
        return None
    facts["source_url"] = source
    as_of = parse_source_date(record.get("as_of")) or ""
    if as_of:
        facts["as_of"] = as_of
    return facts


def _ambiguous_name_keys(rows: Sequence[RegistryRow]) -> frozenset[str]:
    """
    Name keys carried by rows with DIFFERENT registry keys. Those rows are different projects
    (DESIGN section 2), so a listing typed under such a name key cannot be said to describe either.
    """
    registry_keys: dict[str, set[str]] = {}
    for row in rows:
        name, registry = row.name_key(), row.registry_key()
        if name and registry:
            registry_keys.setdefault(name, set()).add(registry)
    return frozenset(name for name, found in registry_keys.items() if len(found) > 1)


def _best_listing(
    row: RegistryRow, listings: Mapping[str, Any], ambiguous: frozenset[str] = frozenset()
) -> Optional[dict[str, Any]]:
    """
    The facts to show for a row. A listing joins on ANY of the row's identity keys,
    except a name key that several different registry projects share (`ambiguous`).
    When several keys have a record (the analyst typed it under the name key before
    the registry id existed), the record with the latest As Of wins, and on a tie the
    earlier key in identity_keys() (registry key first). One record is used whole:
    price from one entry and a source URL from another would break the rule that a
    value carries its own source.
    """
    candidates = []
    for order, key in enumerate(row.identity_keys()):
        if key in ambiguous:
            continue
        facts = _listing_facts(listings.get(key))
        if facts is not None:
            candidates.append((facts.get("as_of", ""), -order, facts))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


# ---------------------------------------------------------------------------
# Rows -> features
# ---------------------------------------------------------------------------
def _coordinate_problem(row: RegistryRow) -> Optional[str]:
    """Why a row cannot be a pin (a DROP_REASONS entry), or None if it can."""
    lat, lon = row.latitude, row.longitude
    if lat is None and lon is None:
        return "no_coordinates"
    if lat is None or lon is None:
        return "half_written_coordinates"
    if not in_union_bbox(lat, lon):
        return "out_of_range"
    # The emirate is exactly what the registry said (DESIGN section 1); a spelling this script would have to
    # repair or guess is not drawn. Checked here, before point_in_emirate, which logs the name it is given.
    if row.emirate not in EMIRATES:
        return "unknown_emirate"
    if not point_in_emirate(lat, lon, row.emirate):
        return "outside_emirate"
    return None


def _properties(row: RegistryRow, facts: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    percent = row.percent_completed
    if percent is not None:
        percent = round(percent, 2)
        percent = int(percent) if percent == int(percent) else percent
    values: dict[str, Any] = {
        "project_key": row.project_key,
        "name": _clean_str(row.project_name_en),
        "name_ar": _clean_str(row.project_name_ar),
        "developer": _clean_str(row.developer),
        "emirate": row.emirate,
        "area": _clean_str(row.area_en),
        "master_project": _clean_str(row.master_project),
        "registry": row.registry,
        "registry_no": _clean_str(row.registry_project_no, 40),
        "status": _clean_str(row.status, 80),
        "percent_completed": percent,
        "completion_date": row.completion_date,
        # A registry count of 0 means "not recorded" far more often than "no units".
        "units": row.units or None,
        "precision": row.location_precision if row.location_precision in PRECISIONS else PRECISION_UNKNOWN,
    }
    values.update(facts or {})
    # Blank values are left out to keep the file small, except "name", which is
    # always present so the page never has to guess whether a missing key means
    # "unknown" or "an older file".
    return {
        name: values[name]
        for name in ALLOWED_PROPERTIES
        if name in values and (name == "name" or values[name] not in (None, "", []))
    }


def _sort_key(feature: Mapping[str, Any]) -> tuple:
    props = feature["properties"]
    lon, lat = feature["geometry"]["coordinates"]
    emirate = props["emirate"]
    return (
        PRECISION_ORDER.index(props["precision"]),
        EMIRATES.index(emirate) if emirate in EMIRATES else len(EMIRATES),
        props["name"].casefold(),
        props.get("project_key", ""),
        lat,
        lon,
    )


def build_geojson(
    rows: Iterable[RegistryRow],
    listings: Optional[Mapping[str, Mapping[str, Any]]],
    *,
    generated_at: object,
    unreadable_rows: int = 0,
) -> dict[str, Any]:
    """
    A FeatureCollection of the located projects, exact pins first, plus a meta block.

    `listings` is {Project Key: record} as listing_data.load_listing_records returns
    it (None means no listings). `unreadable_rows` is how many sheet rows the reader
    had to skip; it is reported in meta so the page can say the data is incomplete.

    Coordinates are GeoJSON order, [lng, lat]. The result is deterministic: the same
    rows in any order give the same output, byte for byte once serialised.
    Raises TypeError for a row that is not a RegistryRow, ValueError / TypeError for
    a `generated_at` that is not an aware timestamp.
    """
    stamp = normalise_stamp(generated_at)
    if listings is None:
        listings = {}
    if not isinstance(listings, Mapping):
        raise TypeError(f"listings must be a mapping of Project Key to record, got {type(listings).__name__}")
    if isinstance(unreadable_rows, bool) or not isinstance(unreadable_rows, int) or unreadable_rows < 0:
        raise ValueError(f"unreadable_rows must be a non-negative integer, got {unreadable_rows!r}")

    if rows is None or isinstance(rows, (str, bytes, Mapping)):
        raise TypeError(f"rows must be an iterable of RegistryRow, got {type(rows).__name__}")

    rows = list(rows)
    ambiguous = _ambiguous_name_keys([row for row in rows if isinstance(row, RegistryRow)])
    dropped = {reason: 0 for reason in DROP_REASONS}
    projects_per_emirate = {name: 0 for name in EMIRATES}
    total = 0
    without_identity = 0
    candidates: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, RegistryRow):
            raise TypeError(f"rows must be RegistryRow objects, got {type(row).__name__}")
        total += 1
        if row.emirate in EMIRATES:  # an unknown emirate is a cell value; it never becomes a key of the public file
            projects_per_emirate[row.emirate] += 1
        problem = _coordinate_problem(row)
        if problem:
            dropped[problem] += 1
            continue
        facts = _best_listing(row, listings, ambiguous)
        candidates.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(row.longitude, 6), round(row.latitude, 6)],
                },
                "properties": _properties(row, facts),
            }
        )

    candidates.sort(key=_sort_key)
    features: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for feature in candidates:
        key = feature["properties"].get("project_key", "")
        if not key:
            without_identity += 1
        elif key in seen_keys:
            dropped["duplicate_key"] += 1
            continue
        else:
            seen_keys.add(key)
        features.append(feature)
    if dropped["duplicate_key"]:
        # Counted, not named: a key carries a developer and project name, and this log may be public.
        logger.warning("%d row(s) share a project key with a better-placed row and are not exported", dropped["duplicate_key"])
    # Counts only, for the same reason: no name, key, coordinate or emirate text.
    if dropped["outside_emirate"]:
        logger.warning("%d row(s) have coordinates outside their registry emirate's boundary and are not exported", dropped["outside_emirate"])
    if dropped["unknown_emirate"]:
        logger.warning("%d row(s) have an emirate other than Dubai or Sharjah and are not exported", dropped["unknown_emirate"])

    by_precision = {name: 0 for name in PRECISION_ORDER}
    located_per_emirate = {name: 0 for name in projects_per_emirate}
    for feature in features:
        props = feature["properties"]
        by_precision[props["precision"]] += 1
        located_per_emirate[props["emirate"]] += 1

    meta = {
        "schema": SCHEMA_VERSION,
        "generated_at": stamp,
        "total_projects": total,
        "total_located": len(features),
        "total_unlocated": total - len(features),
        "by_emirate": {
            name: {"projects": projects_per_emirate[name], "located": located_per_emirate[name]}
            for name in projects_per_emirate
        },
        "by_precision": by_precision,
        "dropped": dropped,
        "unreadable_rows": unreadable_rows,
        "without_identity": without_identity,
        "with_listing": sum(1 for f in features if "source_url" in f["properties"]),
        "with_price": sum(1 for f in features if {"price_from", "price_to"} & f["properties"].keys()),
        "with_bedrooms": sum(1 for f in features if {"bedrooms_min", "bedrooms_max"} & f["properties"].keys()),
        "attribution": list(ATTRIBUTION),
    }
    return {"type": "FeatureCollection", "meta": meta, "features": features}


# ---------------------------------------------------------------------------
# Writing and the CLI
# ---------------------------------------------------------------------------
def serialise(collection: Mapping[str, Any]) -> bytes:
    """Compact UTF-8 JSON. NaN / Infinity raise instead of producing a file no parser accepts."""
    return json.dumps(collection, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def write_geojson(collection: Mapping[str, Any], out_path: Path | str) -> Path:
    """
    Write atomically: serialise first (so a failure leaves any existing file alone),
    then write a temporary file beside the target and rename it into place.
    """
    target = Path(out_path)
    payload = serialise(collection)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=target.parent, prefix=target.name + ".", suffix=".tmp", delete=False)
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return target


def run(
    manager: Any,
    *,
    out_path: Path | str = DEFAULT_OUT,
    generated_at: Optional[str] = None,
    min_pins: int = 0,
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
    """
    Read the sheet through `manager` (a SheetManager or anything with read_registry
    and read_listing_records), build the collection and write it. Returns the
    collection. Every failure is an ExportError whose message is safe for a public CI
    log: it names the exception CLASS and never the exception's own text (a Google or
    OS error can quote a row, a query or a path of the runner).

    With min_pins > 0 a map with fewer pins is an error and the existing file is left
    untouched: an empty map must not silently replace a good one.
    """
    try:
        rows, problems = manager.read_registry()
        listings = manager.read_listing_records()
    except Exception as exc:  # noqa: BLE001 - any reader failure must surface, by class name only
        raise ExportError(f"reading the sheet failed: {type(exc).__name__}") from None

    if problems:
        logger.warning("%d sheet row(s) could not be read and are not on the map", len(problems))
        for line in list(problems)[:5]:
            logger.warning("  %s", scrub_secrets(line, secrets))

    collection = build_geojson(
        rows,
        listings,
        generated_at=generated_at if generated_at is not None else utc_now_iso(),
        unreadable_rows=len(problems),
    )
    meta = collection["meta"]
    if meta["total_located"] < min_pins:
        raise ExportError(
            f"only {meta['total_located']} located pin(s), fewer than the required {min_pins}; "
            f"{meta['total_projects']} project(s) read, dropped: {meta['dropped']}. No file was written "
            "(an existing one is left as it was)."
        )
    try:
        write_geojson(collection, out_path)
    except OSError as exc:
        # out_path is the --out argument as typed, not text taken from the error
        raise ExportError(f"writing {out_path} failed: {type(exc).__name__}") from None

    logger.info(
        "Wrote %d pin(s) of %d project(s) to %s (precision %s; dropped %s)",
        meta["total_located"], meta["total_projects"], out_path, meta["by_precision"], meta["dropped"],
    )
    if meta["total_located"] == 0:
        logger.warning("The map has no located projects; the page will show its empty state.")
    return collection


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export located projects as GeoJSON for the map page.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output path (default: {DEFAULT_OUT})")
    parser.add_argument("--min-pins", type=int, default=0, help="fail (and keep the old file) below this many pins")
    parser.add_argument(
        "--allow-sheet-by-name",
        action="store_true",
        help="proceed without SPREADSHEET_ID (SheetManager.open() may then look up or create a sheet by name)",
    )
    return parser.parse_args(argv)


def _where(exc: BaseException) -> str:
    """file:line of the innermost frame - location only, never variable values."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "unknown"
    frame = frames[-1]
    return f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    cfg: Optional[Config] = None,
    manager_factory: Optional[Callable[[Config], Any]] = None,
) -> int:
    """
    Exit code 0 on success, 1 on any failure. `cfg` and `manager_factory` are for
    tests; in normal use the configuration comes from the environment / .env and the
    manager is src.gsheet_manager.SheetManager, imported only here.

    The log follows the public-log policy of src/log_policy.py, the same as the daily run:
    only this script's own logger speaks (a library's record, at any level, never reaches
    the console) and the configuration's secrets are scrubbed from what it says. An exception
    nobody catches (a cancelled run's KeyboardInterrupt) is printed as its class and place only.
    """
    install_excepthook()  # here and not at import time: see src/log_policy.py
    args = _parse_args(argv)
    handler = configure_logging()
    secrets: list[str] = []
    try:
        cfg = cfg if cfg is not None else load_config()
        secrets = secret_values(cfg)
        set_secrets(handler, secrets)
        for warning in cfg.warnings:
            logger.warning(warning)
        if not cfg.spreadsheet_id and not args.allow_sheet_by_name:
            logger.error(
                "SPREADSHEET_ID is not set. The exporter only reads an existing sheet; "
                "set it, or pass --allow-sheet-by-name to let SheetManager look the sheet up by name."
            )
            return 1
        if manager_factory is None:
            from src.gsheet_manager import SheetManager  # noqa: PLC0415 - lazy: written by another module

            manager_factory = SheetManager
        manager = manager_factory(cfg)
        manager.open()
        run(manager, out_path=args.out, min_pins=args.min_pins, secrets=secrets)
    except ExportError as exc:
        logger.error("%s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - never let a traceback or an error text reach the public log
        logger.error("export failed: %s (at %s)", type(exc).__name__, _where(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
