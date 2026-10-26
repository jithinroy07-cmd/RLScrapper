"""
Data models and identity rules (docs/DESIGN.md sections 0-3, 5, 6, 9).

The central question this module answers is "are these two rows the same
project?", and it answers it conservatively. Two wrong answers are not equally
bad: splitting one project into two rows costs a duplicate someone can merge by
hand, while merging two different projects silently destroys data. So:

  * An id is only an id when it passes its authority's pattern. Placeholders
    (n/a, tbc, -, 0 ...) are None, never a key; a permit number is never a key.
  * Keys are namespaced by authority and emirate (dld:dubai:..., srerd:sharjah:...,
    name:dubai:...), so a Dubai and a Sharjah project can never merge.
  * Tower / block letters are part of the name: "Tower A" and "Tower B" are
    different projects. Phase / stage suffixes are removed only to build geocode
    queries, never for identity.
  * Two rows that carry registry ids of the same authority which differ are
    different projects, even when their names agree.
  * Merging prefers non-empty values and never invents one.

Sheet access elsewhere is by header name; this module owns the header tuples and
the dict <-> model conversion so a column can be added without shifting others.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache, reduce
from typing import Annotated, Any, Mapping, Optional, Sequence
from urllib.parse import quote_plus, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

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
from src.config import DISPLAY_TZ, EMIRATES, REGISTRIES

# ---------------------------------------------------------------------------
# Sheet headers (DESIGN section 3). Listing_Data's live in listing_data.py.
# ---------------------------------------------------------------------------
REGISTRY_HEADERS = (
    "Project Key",
    "Emirate",
    "Registry",
    "Registry Project No.",
    "Project Name (EN)",
    "Project Name (AR)",
    "Developer",
    "Master Developer",
    "Master Project",
    "Area (EN)",
    "Area (AR)",
    "Zone",
    "Status",
    "Percent Completed",
    "Start Date",
    "Completion Date",
    "Cancellation Date",
    "Escrow Agent",
    "Units",
    "Buildings",
    "Villas",
    "Permit No.",
    "Source Dataset",
    "Source Refreshed",
    "First Seen",
    "Last Seen",
    "Latitude",
    "Longitude",
    "Location Precision",
    "Geocode Method",
    "Geocode Status",
    "Map Pin Link",
)

CANDIDATE_HEADERS = (
    "Candidate Key",
    "Emirate",
    "Project Name",
    "Developer",
    "Area",
    "Claim",
    "Source URL",
    "Found At",
    "Status",
)

OVERRIDE_HEADERS = ("Project Key", "Latitude", "Longitude", "Precision", "Note")

# Registry_Projects header -> RegistryRow attribute. "Project Key" is computed
# from the identity rules, never stored, so it is deliberately absent.
_REGISTRY_FIELD_FOR_HEADER = {
    "Emirate": "emirate",
    "Registry": "registry",
    "Registry Project No.": "registry_project_no",
    "Project Name (EN)": "project_name_en",
    "Project Name (AR)": "project_name_ar",
    "Developer": "developer",
    "Master Developer": "master_developer",
    "Master Project": "master_project",
    "Area (EN)": "area_en",
    "Area (AR)": "area_ar",
    "Zone": "zone",
    "Status": "status",
    "Percent Completed": "percent_completed",
    "Start Date": "start_date",
    "Completion Date": "completion_date",
    "Cancellation Date": "cancellation_date",
    "Escrow Agent": "escrow_agent",
    "Units": "units",
    "Buildings": "buildings",
    "Villas": "villas",
    "Permit No.": "permit_no",
    "Source Dataset": "source_dataset",
    "Source Refreshed": "source_refreshed",
    "First Seen": "first_seen",
    "Last Seen": "last_seen",
    "Latitude": "latitude",
    "Longitude": "longitude",
    "Location Precision": "location_precision",
    "Geocode Method": "geocode_method",
    "Geocode Status": "geocode_status",
    "Map Pin Link": "map_pin_link",
}

# Location precision of a pin, best first (DESIGN section 5). Blank means unknown
# and must be treated as the weakest: it can never be drawn as an exact pin.
PRECISIONS = ("exact", "community", "area")
TIER_EXACT, TIER_COMMUNITY, TIER_AREA = PRECISIONS
CANDIDATE_STATUSES = ("new", "confirmed", "rejected")

# Variants per geocode tier. Every variant is one lookup against a daily budget.
MAX_EXACT_VARIANTS = 3
MAX_PLACE_VARIANTS = 2

# ---------------------------------------------------------------------------
# Placeholders, ids and keys (DESIGN section 2)
# ---------------------------------------------------------------------------
# Values that stand in for "no value". Explicit, not heuristic, so a surprising
# string is kept (and visible) rather than silently dropped. "pending" is NOT
# here: it is a genuine DLD project status.
PLACEHOLDER_VALUES = frozenset(
    {
        "", "-", "--", "---", "?", "0", "n/a", "na", "n.a", "n.a.", "nil", "none", "null", "nan",
        "unknown", "undefined", "tbc", "tbd", "tba", "missing", "not available",
        "not applicable", "not specified",
    }
)

DLD_ID_RE = re.compile(r"^[0-9]{1,9}\Z")
SRERD_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_NAME_KEY_RE = re.compile(rf"^name:(dubai|sharjah):({_SLUG}):({_SLUG})$")
_DLD_KEY_RE = re.compile(r"^dld:dubai:([0-9]{1,9})\Z")
_SRERD_KEY_RE = re.compile(r"^srerd:sharjah:([A-Za-z0-9._-]{1,40})$")


def is_placeholder(value: object) -> bool:
    """True for None, NaN, anything on the blocklist, or text with no letter or digit."""
    if value is None:
        return True
    if isinstance(value, float) and not math.isfinite(value):
        return True
    text = " ".join(str(value).split()).lower()
    return text in PLACEHOLDER_VALUES or not any(ch.isalnum() for ch in text)


def clean_registry_id(value: object) -> Optional[str]:
    """
    A registry id as a string, or None when it is missing or placeholder-looking.
    Sheets and CSV tools turn 1234 into 1234.0; that artefact is removed. An id
    made only of zeros is a placeholder. No length rule is applied (DLD ids can
    be short); pattern validation happens when a key is built.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            return None
        text = str(int(value))
    else:
        text = " ".join(str(value).split())
    text = re.sub(r"^([0-9]+)\.0+$", r"\1", text)
    if is_placeholder(text):
        return None
    core = re.sub(r"[^A-Za-z0-9]", "", text)
    if not core or set(core) == {"0"}:
        return None
    return text


def _fold(text: object) -> str:
    """NFKD, diacritics removed, other scripts' decimal digits (Arabic-Indic, Devanagari, ...) folded to ASCII."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(str(unicodedata.decimal(ch)) if not ch.isascii() and ch.isdecimal() else ch for ch in stripped)


def slugify(text: object) -> str:
    """
    Identity slug: _fold, lower-case, "&" read as "and", ASCII letters and digits
    only, runs of anything else joined by "-". The digit folding matters because
    "Tower <Arabic-Indic 1>" and "Tower <Arabic-Indic 2>" must not both slug to
    "tower" and merge. Every other letter and symbol is DROPPED, which is why
    keys are built from identity_slug, not from this.
    """
    if text is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", _fold(text).lower().replace("&", " and ")).strip("-")


_ASCII_LETTER_RE = re.compile(r"[a-z]")
# Symbols that can be the only thing telling two names apart ("Tower A+" and "Tower A", "C#" and "C").
_MEANINGFUL_SYMBOLS = frozenset("+#@")


def _slug_drops_identity(text: object) -> bool:
    """True when slugify would throw away a letter, a number or a meaningful symbol of `text` (not mere punctuation)."""
    for ch in _fold(text):
        if ch in _MEANINGFUL_SYMBOLS:
            return True
        # After _fold every decimal digit is ASCII, so a non-ASCII alphanumeric left is a letter or number slugify drops.
        # Modifier letters (category Lm: the hamza / ayn / apostrophe marks of Arabic transliteration) are punctuation.
        if not ch.isascii() and ch.isalnum() and unicodedata.category(ch) != "Lm":
            return True
    return False


def identity_slug(text: object) -> str:
    """
    The slug a key may be built from, or "" when the name cannot safely become one.
    slugify keeps only ASCII letters and digits, so two names that differ only in
    what it discards would share a key and dedupe_rows would merge them without a
    trace. A name therefore yields no slug when
      * it holds no ASCII letter: a name written only in another script slugs to
        its digits alone, which would make every such project of a developer one
        key; or
      * a letter or number that is not ASCII survives NFKD and diacritic folding
        (another script such as Arabic, Devanagari or CJK, or a Latin letter that
        does not decompose, such as the o-stroke or the sharp s): two names that
        differ only by such a letter would slug alike; or
      * it holds "+", "#" or "@": "Tower A+" and "Tower A", "C#" and "C" would
        slug alike.
    Ordinary punctuation (hyphen, apostrophe, "&", comma, brackets, dot, slash)
    and diacritics that fold ("e" with an acute accent) never distinguish
    projects and still key.
    No key is invented in place of the missing one (no hash, no transliteration):
    the name is simply not usable, so the row is keyed by its registry id if it
    has one and otherwise has no identity. cluster_rows / dedupe_rows keep such a
    row on its own, never merged and never dropped; the adapters and main count
    and refuse it (has_identity is False).
    """
    slug = slugify(text)
    if not _ASCII_LETTER_RE.search(slug) or _slug_drops_identity(text):
        return ""
    return slug


def make_name_key(emirate: object, developer: object, project_name: object) -> Optional[str]:
    """
    name:<emirate>:<developer-slug>:<project-slug>, or None when the emirate is
    out of scope or either slug is empty (a name key from half the information
    would merge unrelated projects). A name identity_slug refuses leaves its slug
    empty, so it makes no key.
    """
    canonical = normalise_emirate(emirate if isinstance(emirate, str) else None)
    if canonical is None:
        return None
    dev = "" if is_placeholder(developer) else identity_slug(developer)
    proj = "" if is_placeholder(project_name) else identity_slug(project_name)
    if not dev or not proj:
        return None
    return f"name:{canonical.lower()}:{dev}:{proj}"


def is_valid_project_key(key: object) -> bool:
    """
    True for a well-formed, namespaced Project Key: dld:dubai:<digits>,
    srerd:sharjah:<id>, or name:<emirate>:<developer-slug>:<project-slug>.
    Bare ids, other prefixes, other emirates and placeholder ids are all False.
    """
    if not isinstance(key, str) or key != key.strip():
        return False
    if _NAME_KEY_RE.match(key):
        return True
    for pattern in (_DLD_KEY_RE, _SRERD_KEY_RE):
        match = pattern.match(key)
        if match:
            return clean_registry_id(match.group(1)) == match.group(1)
    return False


def key_authority(key: str) -> str:
    """'dld', 'srerd' or 'name': the namespace a key belongs to."""
    return key.split(":", 1)[0]


def is_http_url(value: object) -> bool:
    """True for an http(s) URL with a host and no whitespace; nothing else is linkable."""
    if not isinstance(value, str) or not value or value != value.strip() or re.search(r"\s", value):
        return False
    try:
        parsed = urlparse(value)
        # A host is required (netloc alone would pass "http://:80/" and "http://user@/"),
        # and a port, when present, must be a real one. Both are read lazily, so the
        # ValueError for "http://[bad" / "http://host:abc" has to be caught here: a
        # malformed cell must be refused, not crash the whole load.
        host, _port = parsed.hostname, parsed.port
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(host)


# ---------------------------------------------------------------------------
# Dates and time (DESIGN section 1)
# ---------------------------------------------------------------------------
_TIME_PART = r"(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:Z|[+-]\d{2}:?\d{2})?)?"
_ISO_DATE_RE = re.compile(rf"^(\d{{4}})[-/](\d{{1,2}})[-/](\d{{1,2}}){_TIME_PART}$", re.IGNORECASE)
_DMY_DATE_RE = re.compile(rf"^(\d{{1,2}})[-/.](\d{{1,2}})[-/.](\d{{4}}){_TIME_PART}$", re.IGNORECASE)


def parse_source_date(value: object) -> Optional[str]:
    """
    A source date as YYYY-MM-DD, or None when blank or not a date.

    Accepts ISO dates and timestamps ("2026-10-25", "2026-10-25T03:00:00Z"), the
    legacy registry's day-first dd-mm-yyyy (also with "/" or "."; always read
    day first), and date / datetime objects. The date as written is kept: a
    timestamp is not shifted between time zones. Impossible dates, years outside
    1900-2100 (so the 0001-01-01 "no date" sentinel) and placeholders give None.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parts: Optional[tuple[int, int, int]] = (value.year, value.month, value.day)
    elif isinstance(value, date):
        parts = (value.year, value.month, value.day)
    else:
        text = " ".join(str(value).split())
        if is_placeholder(text):
            return None
        iso, dmy = _ISO_DATE_RE.match(text), _DMY_DATE_RE.match(text)
        if iso:
            parts = (int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        elif dmy:
            parts = (int(dmy.group(3)), int(dmy.group(2)), int(dmy.group(1)))
        else:
            parts = None
    if parts is None or not 1900 <= parts[0] <= 2100:
        return None
    try:
        return date(*parts).isoformat()
    except ValueError:
        return None


def utc_now_iso(now: Optional[datetime] = None) -> str:
    """Timezone-aware UTC timestamp, e.g. 2026-10-25T03:00:00Z. `now` is for tests."""
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("utc_now_iso needs a timezone-aware datetime")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@lru_cache(maxsize=1)
def _dubai_tz():
    """
    Asia/Dubai from the tz database, or UTC+4 when none is installed (Windows
    ships none without the tzdata package). The fallback is exact, not a guess:
    the UAE has no daylight saving.
    """
    try:
        return ZoneInfo(DISPLAY_TZ)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=4), "GST")


def format_dubai_time(iso: str) -> str:
    """
    Display a stored UTC timestamp in Dubai time: '2026-10-25T03:00:00Z' ->
    '2026-10-25 07:00 GST'. Blank gives blank. Text that is not a
    timezone-aware ISO timestamp is returned unchanged: showing the original is
    honest, assuming a zone for it would be inventing one.
    """
    text = (iso or "").strip()
    if not text:
        return ""
    candidate = text[:-1] + "+00:00" if text[-1] in "Zz" else text
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        return text
    if moment.tzinfo is None:
        return text
    return moment.astimezone(_dubai_tz()).strftime("%Y-%m-%d %H:%M") + " GST"


# ---------------------------------------------------------------------------
# Cell coercion (sheets hand back strings; sources hand back anything)
# ---------------------------------------------------------------------------
_THOUSANDS_RE = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")


def _to_float(value: object) -> Optional[float]:
    """
    A finite float or None. A comma is a thousands separator, so '25,2048' (a
    decimal comma) is NOT read as 25.2048: it parses as nothing and is rejected
    by the range checks, which is the safe failure.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:  # an int too large for a float is not a usable cell value
            return None
    else:
        text = str(value).strip().replace(" ", "").replace(" ", "")
        # "0" is on the placeholder list (it is not an id), but as a NUMBER it is a
        # real value: 0 villas, 0 % complete. Only the other placeholders are blank.
        if not text or (text != "0" and is_placeholder(text)):
            return None
        if "," in text:
            if not _THOUSANDS_RE.match(text):
                return None
            text = text.replace(",", "")
        if "_" in text:  # float() reads "1_000" as 1000; a cell like that is not a number
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def _to_count(value: object) -> Optional[int]:
    """A non-negative whole number, or None. 12.0 and '1,234' pass; 12.5 and -3 do not."""
    number = _to_float(value)
    if number is None or number < 0 or number != int(number):
        return None
    return int(number)


def _to_percent(value: object) -> Optional[float]:
    text = value.strip().rstrip("%") if isinstance(value, str) else value
    number = _to_float(text)
    return number if number is not None and 0 <= number <= 100 else None


def _to_lat(value: object) -> Optional[float]:
    number = _to_float(value)
    return number if number is not None and -90 <= number <= 90 else None


def _to_lon(value: object) -> Optional[float]:
    number = _to_float(value)
    return number if number is not None and -180 <= number <= 180 else None


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    text = " ".join(str(value).split())
    return "" if is_placeholder(text) else text


def _clean_date(value: object) -> str:
    return parse_source_date(value) or ""


Text = Annotated[str, BeforeValidator(_clean_text)]
IsoDate = Annotated[str, BeforeValidator(_clean_date)]
RegistryId = Annotated[Optional[str], BeforeValidator(clean_registry_id)]
Count = Annotated[Optional[int], BeforeValidator(_to_count)]
Percent = Annotated[Optional[float], BeforeValidator(_to_percent)]
Latitude = Annotated[Optional[float], BeforeValidator(_to_lat)]
Longitude = Annotated[Optional[float], BeforeValidator(_to_lon)]


def _format_number(value: float, places: int) -> str:
    return f"{value:.{places}f}".rstrip("0").rstrip(".")


def _cell(value: object) -> str:
    """A model value as a sheet cell: always a string, None is blank."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return _format_number(value, 7)
    return str(value)


def _check_precision(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in PRECISIONS else ""


# ---------------------------------------------------------------------------
# RegistryRow
# ---------------------------------------------------------------------------
class RegistryRow(BaseModel):
    """
    One project as known from an official registry or a hand-researched seed.

    Construction is strict about what decides identity and lenient about what
    does not: an unsupported emirate or registry raises (so a bad row is loud),
    while a placeholder in a descriptive column simply becomes blank. Unknown
    keyword arguments raise too (extra="forbid"), so an adapter typo cannot lose
    a column silently.

    Blank precision with coordinates present means "unknown precision" and must
    be shown as approximate, never as exact.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    emirate: str
    registry: str
    registry_project_no: RegistryId = None
    project_name_en: Text = ""
    project_name_ar: Text = ""
    developer: Text = ""
    master_developer: Text = ""
    master_project: Text = ""
    area_en: Text = ""
    area_ar: Text = ""
    zone: Text = ""
    status: Text = ""
    percent_completed: Percent = None
    start_date: IsoDate = ""
    completion_date: IsoDate = ""
    cancellation_date: IsoDate = ""
    escrow_agent: Text = ""
    units: Count = None
    buildings: Count = None
    villas: Count = None
    permit_no: Text = ""  # identifies an advertisement, never a project: not a key
    source_dataset: Text = ""
    source_refreshed: Text = ""
    first_seen: Text = ""
    last_seen: Text = ""
    latitude: Latitude = None
    longitude: Longitude = None
    location_precision: Annotated[str, BeforeValidator(_check_precision)] = ""
    geocode_method: Text = ""
    geocode_status: Text = ""
    map_pin_link: Text = ""

    @field_validator("emirate", mode="before")
    @classmethod
    def _check_emirate(cls, value: object) -> str:
        canonical = normalise_emirate(value if isinstance(value, str) else None)
        if canonical is None:
            raise ValueError(f"emirate must be one of {EMIRATES}")  # the offending cell is never echoed: logs are public
        return canonical

    @field_validator("registry", mode="before")
    @classmethod
    def _check_registry(cls, value: object) -> str:
        text = str(value or "").strip().upper()
        if text not in REGISTRIES:
            raise ValueError(f"registry must be one of {REGISTRIES}")
        return text

    @model_validator(mode="after")
    def _registry_matches_emirate(self) -> "RegistryRow":
        # The emirate comes from the registry (DESIGN 1), never from coordinates.
        expected = {"DLD": "Dubai", "SRERD": "Sharjah"}.get(self.registry)
        if expected and self.emirate != expected:
            raise ValueError(f"a {self.registry} row must be {expected}, not {self.emirate}")
        return self

    # -- identity ------------------------------------------------------------
    def registry_key(self) -> Optional[str]:
        """
        dld:dubai:<n> or srerd:sharjah:<id> when this row's own authority issued a
        valid id, else None. SEED rows never produce one: a hand-typed number has
        no authority behind it, so it must not be able to conflict with, or merge
        into, a real registry row.
        """
        number = self.registry_project_no
        if not number:
            return None
        if self.registry == "DLD" and DLD_ID_RE.match(number):
            return f"dld:dubai:{number}"
        if self.registry == "SRERD" and SRERD_ID_RE.match(number):
            return f"srerd:sharjah:{number}"
        return None

    def name_key(self) -> Optional[str]:
        """name:<emirate>:<developer-slug>:<project-slug>, or None if either slug is empty."""
        return make_name_key(self.emirate, self.developer, self.project_name_en)

    def identity_keys(self) -> list[str]:
        """
        Every key this row can be recognised by, registry key first. The name key
        is listed even when a registry key exists so that a hand-researched seed
        row (name only) can still find its registry row; it is the primary key
        (project_key) only when there is no registry id.
        """
        keys = [self.registry_key(), self.name_key()]
        return [k for k in keys if k]

    @property
    def project_key(self) -> str:
        """The primary key: the first identity key, or "" if the row has none."""
        keys = self.identity_keys()
        return keys[0] if keys else ""

    @property
    def has_identity(self) -> bool:
        return bool(self.identity_keys())

    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    # -- geocoding -------------------------------------------------------------
    def _name_variants(self) -> list[str]:
        """
        Tier-1 name variants, at most MAX_EXACT_VARIANTS, best first:
          1. the name as given;
          2. a trailing "by <developer>" removed or, if there is none, appended
             once (OSM often stores 'Masaar by Arada');
          3. a trailing "Phase N" / "Stage N" removed.
        Tower / block letters are untouched. Project names are NOT passed through
        the spelling alias table: they are brand names, not place names.
        """
        name = tidy(self.project_name_en)
        if not name:
            return []
        stripped = strip_developer_suffix(name, self.developer)
        if stripped == name and self.master_developer:
            stripped = strip_developer_suffix(name, self.master_developer)

        variants = [name]
        if stripped != name:
            variants.append(stripped)
        else:
            brand = developer_brand(self.developer)
            if brand and f" {normalise_key(brand)} " not in f" {normalise_key(name)} ":
                variants.append(f"{name} by {brand}")
        unphased = strip_phase(stripped)
        if unphased != stripped:
            variants.append(unphased)

        unique: list[str] = []
        for variant in variants:
            if variant.lower() not in {u.lower() for u in unique}:
                unique.append(variant)
        return unique[:MAX_EXACT_VARIANTS]

    def geocode_tiers(self) -> list[tuple[str, list[str]]]:
        """
        Ordered (tier, queries) pairs: 'exact' (the project name), 'community'
        (Master Project), 'area' (Area (EN)). The first tier that passes
        validation sets Location Precision. Only tiers with at least one query
        appear. Every query ends in ", <Emirate>, UAE" (the caller also passes
        countrycodes=ae), because a bare name is the dangerous kind of query.

        Two honesty rules keep precision truthful:
          * A name that is really a community or area (it equals Master Project or
            Area (EN) after spelling normalisation) is not queried as 'exact';
            it is left to its own tier, so a community centroid is never filed as
            an exact pin.
          * A query already issued in an earlier tier is not repeated.
        When Master Project is blank there is no community tier: the area is the
        community.
        """
        emirate = self.emirate
        qualifier = query_qualifier(emirate)
        place_keys = {
            normalise_key(canonical_area(text, emirate))
            for text in (self.master_project, self.area_en)
            if text
        }

        tiers: list[tuple[str, list[str]]] = []
        issued: set[str] = set()

        def add_tier(tier: str, names: list[str]) -> None:
            queries: list[str] = []
            for place in names:
                query = f"{place}{qualifier}"
                if query.lower() not in issued:
                    issued.add(query.lower())
                    queries.append(query)
            if queries:
                tiers.append((tier, queries))

        exact = [
            v for v in self._name_variants()
            if normalise_key(canonical_area(v, emirate)) not in place_keys
        ]
        add_tier(TIER_EXACT, exact)
        if self.master_project:
            add_tier(TIER_COMMUNITY, alias_variants(self.master_project, emirate)[:MAX_PLACE_VARIANTS])
        if self.area_en:
            add_tier(TIER_AREA, alias_variants(self.area_en, emirate)[:MAX_PLACE_VARIANTS])
        return tiers

    def build_map_pin_link(self) -> str:
        """
        A map link for this row: the coordinates when it has them, otherwise a
        name search (which claims no location). Blank when there is nothing to
        search for.
        """
        if self.has_coordinates():
            lat, lon = _format_number(self.latitude, 7), _format_number(self.longitude, 7)
            return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
        name = tidy(self.project_name_en) or tidy(self.area_en)
        if not name:
            return ""
        query = quote_plus(f"{name}{query_qualifier(self.emirate)}")
        return f"https://www.google.com/maps/search/?api=1&query={query}"

    # -- sheet round trip ------------------------------------------------------
    def to_sheet_dict(self) -> dict[str, str]:
        """
        Every REGISTRY_HEADERS column as a string ('' for none). Half a coordinate
        pair is not written, and precision is blank without coordinates, so the
        sheet can never show a pin that is not fully there.
        """
        pinned = self.has_coordinates()
        out: dict[str, str] = {}
        for header in REGISTRY_HEADERS:
            if header == "Project Key":
                out[header] = self.project_key
                continue
            attr = _REGISTRY_FIELD_FOR_HEADER[header]
            value = getattr(self, attr)
            if attr in ("latitude", "longitude", "location_precision") and not pinned:
                value = None
            if attr == "percent_completed" and value is not None:
                out[header] = _format_number(value, 2)
            else:
                out[header] = _cell(value)
        return out

    def to_sheet_row(self) -> list[str]:
        """The to_sheet_dict values in REGISTRY_HEADERS order, for a bulk write."""
        row = self.to_sheet_dict()
        return [row[h] for h in REGISTRY_HEADERS]

    @classmethod
    def from_sheet_dict(cls, data: Mapping[str, Any]) -> "RegistryRow":
        """
        Rebuild a row from a header-keyed mapping (gspread get_all_records).
        Tolerant: blanks, numeric strings ('1,234', '67.5', '12.0') and unknown
        extra columns are fine, and the stored 'Project Key' is ignored and
        recomputed. Strict where it matters: a missing or unsupported Emirate or
        Registry raises ValueError (pydantic.ValidationError is one), so callers
        loading a whole sheet should catch it per row and log the row number.
        """
        kwargs = {
            attr: data[header]
            for header, attr in _REGISTRY_FIELD_FOR_HEADER.items()
            if header in data
        }
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Matching, merging, clustering
# ---------------------------------------------------------------------------
def same_project(a: RegistryRow, b: RegistryRow) -> bool:
    """
    Same project when the emirates agree and the rows share any identity key,
    except that two rows carrying registry ids of the same authority are the
    same project only if those ids are equal. Different ids mean different
    projects even when the names match (two phases, two towers of one name).
    """
    if a.emirate != b.emirate:
        return False
    key_a, key_b = a.registry_key(), b.registry_key()
    if key_a and key_b and key_authority(key_a) == key_authority(key_b):
        return key_a == key_b
    return bool(set(a.identity_keys()) & set(b.identity_keys()))


_PRECISION_RANK = {"exact": 3, "community": 2, "area": 1, "": 0}
_AUTHORITY_FIELDS = ("registry", "registry_project_no")
_GEO_FIELDS = (
    "latitude", "longitude", "location_precision", "geocode_method", "geocode_status", "map_pin_link",
)
_MERGE_SPECIAL = frozenset(_AUTHORITY_FIELDS) | frozenset(_GEO_FIELDS) | {"emirate", "first_seen", "last_seen"}


def _is_empty(value: object) -> bool:
    return value is None or value == ""


def _authority_score(row: RegistryRow) -> tuple[int, int]:
    """A registry row outranks a seed row; a valid id outranks a missing one."""
    return (1 if row.registry_key() else 0, 0 if row.registry == "SEED" else 1)


def _geo_score(row: RegistryRow) -> tuple[int, int, int]:
    return (
        1 if row.has_coordinates() else 0,
        _PRECISION_RANK.get(row.location_precision, 0),
        1 if row.geocode_status else 0,
    )


def merge_rows(a: RegistryRow, b: RegistryRow) -> RegistryRow:
    """
    One row from two that describe the same project. Never invents a value:

      * each field takes the preferred row's value when non-empty, else the other's.
        The preferred row is `a` unless `b` has stronger authority (a valid
        registry id, or DLD / SRERD against SEED); pass the fresher row first;
      * Registry and Registry Project No. travel together, never mixed;
      * the coordinates, precision, method, status and link travel together from
        the row with the better pin (coordinates, then higher precision), so a
        latitude can never be paired with another row's longitude;
      * First Seen is the earliest value, Last Seen the latest.

    Raises ValueError for rows from different emirates or with differing
    registry ids of one authority: merging those would destroy a project.
    """
    if a.emirate != b.emirate:
        raise ValueError(f"cannot merge rows from {a.emirate} and {b.emirate}")
    key_a, key_b = a.registry_key(), b.registry_key()
    if key_a and key_b and key_authority(key_a) == key_authority(key_b) and key_a != key_b:
        # The two ids are cells: they stay out of the message, which a caller may log.
        raise ValueError("cannot merge different registry ids of one authority")

    primary, secondary = (b, a) if _authority_score(b) > _authority_score(a) else (a, b)
    data: dict[str, Any] = {"emirate": primary.emirate}

    for name in RegistryRow.model_fields:
        if name in _MERGE_SPECIAL:
            continue
        value = getattr(primary, name)
        data[name] = getattr(secondary, name) if _is_empty(value) else value

    data["registry"] = primary.registry
    data["registry_project_no"] = primary.registry_project_no
    if primary.registry == secondary.registry and _is_empty(primary.registry_project_no):
        data["registry_project_no"] = secondary.registry_project_no

    best_pin = max((primary, secondary), key=_geo_score)  # ties keep the primary
    for name in _GEO_FIELDS:
        data[name] = getattr(best_pin, name)

    firsts = [v for v in (a.first_seen, b.first_seen) if v]
    lasts = [v for v in (a.last_seen, b.last_seen) if v]
    data["first_seen"] = min(firsts) if firsts else ""
    data["last_seen"] = max(lasts) if lasts else ""
    return RegistryRow(**data)


def cluster_rows(rows: Sequence[RegistryRow]) -> list[list[RegistryRow]]:
    """
    Group rows that are the same project. Deterministic: clusters are ordered by
    their first member's position in `rows`, and members keep input order.

    Union-find in two passes. First, rows with the same registry key join. Then
    rows sharing a name key join, unless that name is claimed by registry rows
    with more than one distinct id: then the id-carrying rows stay apart and only
    the id-less ones are grouped, because an id-less row cannot tell which of the
    two it belongs to and guessing would merge different projects. As a result no
    cluster ever contains two different registry ids of one authority, and no
    cluster spans two emirates (every key is emirate-namespaced).
    """
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        root_i, root_j = find(i), find(j)
        if root_i != root_j:
            parent[max(root_i, root_j)] = min(root_i, root_j)

    registry_keys = [row.registry_key() for row in rows]
    first_with_key: dict[str, int] = {}
    for index, key in enumerate(registry_keys):
        if key is None:
            continue
        if key in first_with_key:
            union(first_with_key[key], index)
        else:
            first_with_key[key] = index

    by_name: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        name_key = row.name_key()
        if name_key:
            by_name.setdefault(name_key, []).append(index)
    for members in by_name.values():
        distinct_ids = {registry_keys[i] for i in members if registry_keys[i]}
        joinable = [i for i in members if registry_keys[i] is None] if len(distinct_ids) > 1 else members
        for index in joinable[1:]:
            union(joinable[0], index)

    clusters: dict[int, list[int]] = {}
    for index in range(len(rows)):
        clusters.setdefault(find(index), []).append(index)
    return [[rows[i] for i in members] for _, members in sorted(clusters.items())]


def dedupe_rows(rows: Sequence[RegistryRow]) -> list[RegistryRow]:
    """
    cluster_rows, then merge_rows within each cluster. Earlier rows are preferred
    for conflicting values, so put fresh source rows before the rows already in
    the sheet. Output order follows each cluster's first member.
    """
    return [reduce(merge_rows, cluster) for cluster in cluster_rows(rows)]


# ---------------------------------------------------------------------------
# LaunchCandidate (Launch_Candidates)
# ---------------------------------------------------------------------------
class LaunchCandidate(BaseModel):
    """
    A lead from grounded AI search. Never promoted automatically; a human sets
    Status. The Source URL is required and must be http(s): a claim with nothing
    to check it against is discarded, not stored.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    emirate: str
    project_name: Text
    developer: Text = ""
    area: Text = ""
    claim: Text = ""
    source_url: str
    found_at: Text = Field(default_factory=utc_now_iso)
    status: str = "new"

    @field_validator("emirate", mode="before")
    @classmethod
    def _check_emirate(cls, value: object) -> str:
        canonical = normalise_emirate(value if isinstance(value, str) else None)
        if canonical is None:
            raise ValueError(f"emirate must be one of {EMIRATES}")  # the offending cell is never echoed: logs are public
        return canonical

    @field_validator("source_url", mode="before")
    @classmethod
    def _check_url(cls, value: object) -> str:
        text = str(value or "").strip()
        if not is_http_url(text):
            raise ValueError("source_url is required and must be an http(s) URL")
        return text

    @field_validator("status", mode="before")
    @classmethod
    def _check_status(cls, value: object) -> str:
        text = str(value or "new").strip().lower()
        if text not in CANDIDATE_STATUSES:
            raise ValueError(f"status must be one of {CANDIDATE_STATUSES}")
        return text

    @model_validator(mode="after")
    def _needs_a_latin_name(self) -> "LaunchCandidate":
        if not identity_slug(self.project_name):
            raise ValueError("project_name needs a Latin letter and no other-script letter or +, # or @ (see identity_slug)")
        return self

    @property
    def candidate_key(self) -> str:
        """cand:<emirate>:<developer-slug or 'unknown'>:<project-slug>. Never a registry key."""
        developer = identity_slug(self.developer) or "unknown"
        return f"cand:{self.emirate.lower()}:{developer}:{identity_slug(self.project_name)}"

    def to_sheet_dict(self) -> dict[str, str]:
        return {
            "Candidate Key": self.candidate_key,
            "Emirate": self.emirate,
            "Project Name": self.project_name,
            "Developer": self.developer,
            "Area": self.area,
            "Claim": self.claim,
            "Source URL": self.source_url,
            "Found At": self.found_at,
            "Status": self.status,
        }

    @classmethod
    def from_sheet_dict(cls, data: Mapping[str, Any]) -> "LaunchCandidate":
        """Header-keyed rebuild; the stored Candidate Key is ignored and recomputed. Raises ValueError on a bad row."""
        mapping = {
            "Emirate": "emirate", "Project Name": "project_name", "Developer": "developer",
            "Area": "area", "Claim": "claim", "Source URL": "source_url",
            "Found At": "found_at", "Status": "status",
        }
        return cls(**{attr: data[header] for header, attr in mapping.items() if header in data})


# ---------------------------------------------------------------------------
# GeocodeOverride (Geocode_Overrides)
# ---------------------------------------------------------------------------
class GeocodeOverride(BaseModel):
    """
    A human-placed pin. It always wins over any backend and is never overwritten
    automatically. Precision is whatever the human states; blank is kept as
    blank (unknown, shown as approximate) rather than assumed exact.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_key: str
    latitude: float
    longitude: float
    precision: str = ""
    note: Text = ""

    @field_validator("project_key", mode="before")
    @classmethod
    def _check_key(cls, value: object) -> str:
        text = str(value or "").strip()
        if not is_valid_project_key(text):
            raise ValueError("project_key is not a valid namespaced key; copy it from the Project Key column of Registry_Projects")
        return text

    @field_validator("latitude", mode="before")
    @classmethod
    def _check_lat(cls, value: object) -> float:
        number = _to_lat(value)
        if number is None:
            raise ValueError("latitude must be a number between -90 and 90")
        return number

    @field_validator("longitude", mode="before")
    @classmethod
    def _check_lon(cls, value: object) -> float:
        number = _to_lon(value)
        if number is None:
            raise ValueError("longitude must be a number between -180 and 180")
        return number

    @field_validator("precision", mode="before")
    @classmethod
    def _check_precision_value(cls, value: object) -> str:
        return _check_precision(value)

    def to_sheet_dict(self) -> dict[str, str]:
        return {
            "Project Key": self.project_key,
            "Latitude": _format_number(self.latitude, 7),
            "Longitude": _format_number(self.longitude, 7),
            "Precision": self.precision,
            "Note": self.note,
        }

    @classmethod
    def from_sheet_dict(cls, data: Mapping[str, Any]) -> "GeocodeOverride":
        """Header-keyed rebuild. Raises ValueError for a bad key or coordinate; catch per row and log."""
        mapping = {
            "Project Key": "project_key", "Latitude": "latitude", "Longitude": "longitude",
            "Precision": "precision", "Note": "note",
        }
        return cls(**{attr: data[header] for header, attr in mapping.items() if header in data})
