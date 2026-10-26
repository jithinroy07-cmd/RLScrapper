"""
Place-name helpers: emirate names, area / community spellings, and the text
tweaks that feed the geocoder (docs/DESIGN.md section 5).

Why this exists. Measured on the public OSM geocoder, a bare name is dangerous
(a quarter of bare queries returned a confident answer on another continent) and
spelling drift is fatal ("Jumeira Village Circle" returns nothing while
"Jumeirah Village Circle" resolves). So every place name that reaches a query is
(1) put into one canonical spelling and (2) qualified with ", <Emirate>, UAE".

The alias table (data/area_aliases.json) is deliberately small and applies to
PLACE names (areas, communities) only. Project names are brand names and are
queried as given: rewriting "Mariam Residences" to "Maryam Residences" would be
inventing a spelling. Nothing in this module guesses a coordinate.

This module must not import src.models (models imports it).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from src.config import DATA_DIR, EMIRATES

ALIAS_FILE = DATA_DIR / "area_aliases.json"

# What providers and registries call each emirate. Nominatim returns
# "Dubai Emirate" / "Sharjah Emirate" in its state field; ISO 3166-2 uses the
# transliterations Dubayy and Ash Shariqah.
_EMIRATE_NAMES = {
    "dubai": "Dubai",
    "dubayy": "Dubai",
    "dubai emirate": "Dubai",
    "emirate of dubai": "Dubai",
    "sharjah": "Sharjah",
    "ash shariqah": "Sharjah",
    "sharjah emirate": "Sharjah",
    "emirate of sharjah": "Sharjah",
}


def tidy(text: object) -> str:
    """Collapse all whitespace (including no-break spaces) to single spaces."""
    return " ".join(str(text).split()) if text is not None else ""


def normalise_key(text: object) -> str:
    """
    A comparison key: lower-case, diacritics removed, "&" read as "and",
    apostrophes dropped (Ma'arid -> maarid) and any other punctuation a space.
    Two spellings that differ only in case, accents or punctuation share a key.
    """
    if text is None:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = stripped.lower().replace("&", " and ")
    lowered = re.sub(r"['‘’ʻʼʿʾ`]", "", lowered)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", lowered).split())


def normalise_emirate(text: Optional[str]) -> Optional[str]:
    """
    "dubai", "Dubai Emirate", "Dubayy" -> "Dubai"; likewise Sharjah. Anything
    else (Abu Dhabi, empty, None) -> None, which callers read as "out of scope".
    """
    if not text:
        return None
    return _EMIRATE_NAMES.get(normalise_key(text))


def query_qualifier(emirate: str) -> str:
    """
    The suffix appended to every geocode query, ", Dubai, UAE" or ", Sharjah, UAE".
    An unknown emirate raises: an unqualified query is the dangerous kind.
    """
    canonical = normalise_emirate(emirate)
    if canonical is None:
        raise ValueError(f"emirate must be one of {EMIRATES}, got {emirate!r}")
    return f", {canonical}, UAE"


# ---------------------------------------------------------------------------
# Alias table
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _AliasTable:
    tokens: dict[str, str]  # variant word -> canonical word (both lower-case)
    token_variants: dict[str, tuple[str, ...]]  # canonical word -> variant words
    index: dict[str, dict[str, str]]  # emirate -> normalised key -> canonical name
    aliases: dict[str, dict[str, tuple[str, ...]]]  # emirate -> canonical -> aliases


@lru_cache(maxsize=1)
def _table() -> _AliasTable:
    """
    Load and validate the alias file. A malformed or self-contradictory file
    raises: a silently half-loaded table would corrupt queries without a trace.
    """
    with open(ALIAS_FILE, encoding="utf-8") as handle:
        raw = json.load(handle)

    tokens: dict[str, str] = {}
    variants: dict[str, list[str]] = {}
    for variant, canonical in (raw.get("tokens") or {}).items():
        if variant != variant.lower() or canonical != canonical.lower():
            raise ValueError(f"alias tokens must be lower-case: {variant!r} -> {canonical!r}")
        tokens[variant] = canonical
        if variant != canonical:
            variants.setdefault(canonical, []).append(variant)

    index: dict[str, dict[str, str]] = {}
    aliases: dict[str, dict[str, tuple[str, ...]]] = {}
    for emirate, areas in (raw.get("areas") or {}).items():
        if emirate not in EMIRATES:
            raise ValueError(f"alias table names an unsupported emirate: {emirate!r}")
        by_key: dict[str, str] = {}
        by_name: dict[str, tuple[str, ...]] = {}
        for canonical, alt_names in areas.items():
            by_name[canonical] = tuple(alt_names)
            for spelling in (canonical, *alt_names):
                key = normalise_key(spelling)
                if not key:
                    raise ValueError(f"empty alias under {canonical!r} in {emirate}")
                if by_key.setdefault(key, canonical) != canonical:
                    raise ValueError(
                        f"{spelling!r} maps to both {by_key[key]!r} and {canonical!r} in {emirate}"
                    )
        index[emirate] = by_key
        aliases[emirate] = by_name

    return _AliasTable(
        tokens=tokens,
        token_variants={k: tuple(v) for k, v in variants.items()},
        index=index,
        aliases=aliases,
    )


def _match_case(model: str, replacement: str) -> str:
    """Give `replacement` the capitalisation style of the word it replaces."""
    if len(model) > 1 and model.isupper():
        return replacement.upper()
    if model[:1].isupper():
        return replacement.capitalize()
    return replacement


def _swap_tokens(text: str) -> str:
    """Rewrite variant words (Jumeira, El, Mariam ...) to their canonical spelling."""
    table = _table()
    words = text.split(" ")
    for i, word in enumerate(words):
        if word.isalpha():
            canonical = table.tokens.get(normalise_key(word))
            if canonical and canonical != word.lower():
                words[i] = _match_case(word, canonical)
    return " ".join(words)


def _lookup(key: str, emirate: Optional[str]) -> Optional[str]:
    """Canonical name for a normalised key, or None when unknown or ambiguous."""
    table = _table()
    sections = [emirate] if emirate else list(table.index)
    found = {table.index[e][key] for e in sections if e in table.index and key in table.index[e]}
    return found.pop() if len(found) == 1 else None


def canonical_area(name: str, emirate: Optional[str] = None) -> str:
    """
    One spelling for a place. Tidied input when the place is unknown: an
    unrecognised name is returned as given, never rewritten on a guess.

        canonical_area("Jumeira Village Circle", "Dubai") -> "Jumeirah Village Circle"
        canonical_area("MUWEILAH", "Sharjah")             -> "Muwaileh"
        canonical_area("El Mamsha", "Sharjah")            -> "Al Mamsha"
        canonical_area("Tilal", "Sharjah")                -> "Tilal City"

    Pass the emirate whenever it is known: a name that exists in both emirates
    must resolve inside the declared one.
    """
    text = tidy(name)
    if not text:
        return ""
    scope = normalise_emirate(emirate) if emirate else None
    hit = _lookup(normalise_key(text), scope)
    if hit:
        return hit
    rewritten = _swap_tokens(text)
    if rewritten != text:
        return _lookup(normalise_key(rewritten), scope) or rewritten
    return text


def alias_variants(name: str, emirate: Optional[str] = None, limit: int = 6) -> list[str]:
    """
    Spellings worth trying for one place, best first, de-duplicated ignoring
    case: the canonical spelling, the name as given, the table's aliases, then
    single-word spelling swaps (Al/El, Muwaileh/Muweilah ...). An unknown place
    yields just its tidied name. The geocoder spends a lookup per variant, so
    callers take only the first one or two.
    """
    text = tidy(name)
    if not text:
        return []
    table = _table()
    scope = normalise_emirate(emirate) if emirate else None
    canonical = canonical_area(text, scope)

    out: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        candidate = tidy(candidate)
        if candidate and candidate.lower() not in seen:
            seen.add(candidate.lower())
            out.append(candidate)

    add(canonical)
    add(text)
    for section in [scope] if scope else list(table.index):
        known = table.index.get(section, {}).get(normalise_key(canonical))
        for alias in table.aliases.get(section, {}).get(known or "", ()):
            add(alias)
    words = canonical.split(" ")
    for i, word in enumerate(words):
        for variant in table.token_variants.get(normalise_key(word), ()):
            swapped = words[:i] + [_match_case(word, variant)] + words[i + 1 :]
            add(" ".join(swapped))
    return out[: max(1, limit)]


def known_areas(emirate: str) -> list[str]:
    """Canonical area / community names the alias table knows for an emirate."""
    canonical = normalise_emirate(emirate)
    if canonical is None:
        raise ValueError(f"emirate must be one of {EMIRATES}, got {emirate!r}")
    return sorted(_table().aliases.get(canonical, {}))


# ---------------------------------------------------------------------------
# Project-name tweaks for geocode queries (never used for identity)
# ---------------------------------------------------------------------------
# "Phase 2", "Stage III", "(Phase 2A)" at the END of a name. Tower / block / part
# letters are deliberately not here: Tower A and Tower B are different projects
# and a map does not know either.
_PHASE_RE = re.compile(
    r"""
    (?:
        [\s,\-–]*\(\s*(?:phase|stage)\s*[-.]?\s*(?:\d{1,2}[a-z]?|[ivx]{1,4})\s*\)
      | [\s,\-–]*\b(?:phase|stage)\s*[-.]?\s*(?:\d{1,2}[a-z]?|[ivx]{1,4})\b
    )\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def strip_phase(name: str) -> str:
    """
    Remove a trailing "Phase N" / "Stage N" (Arabic or Roman numerals allowed):
    'Masaar Phase 2' -> 'Masaar'. A name that is nothing but a phase marker is
    returned unchanged. Used for the geocode query only, never for identity.
    """
    text = tidy(name)
    for _ in range(3):
        stripped = _PHASE_RE.sub("", text).strip(" ,-–")
        if not stripped or stripped == text:
            break
        text = stripped
    return text


_LEGAL_WORDS = frozenset(
    {"llc", "fz", "fze", "fzco", "fzllc", "pjsc", "psc", "jsc", "ltd", "limited", "inc",
     "co", "company", "corp", "corporation", "est", "establishment", "llp", "plc"}
)
_GENERIC_WORDS = frozenset(
    {"properties", "property", "real", "estate", "development", "developments", "developer",
     "developers", "investment", "investments", "holding", "holdings", "group", "realty"}
)
# Stripping must never leave only one of these ("Dubai Properties" is not "Dubai").
_PLACE_WORDS = frozenset({"dubai", "sharjah", "emirates", "uae", "national", "gulf", "arab", "al", "the"})


def _core(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def developer_brand(developer: str) -> str:
    """
    The short brand OSM uses after "by": 'ARADA PROPERTIES DEVELOPMENT L.L.C' ->
    'Arada', 'DAMAC PROPERTIES CO. (L.L.C)' -> 'Damac'. Only trailing legal-form
    and generic words are removed, never leaving nothing and never leaving just a
    place word ('Dubai Holding' stays 'Dubai Holding'). A wrong brand costs one
    wasted lookup, never a wrong pin: hits are validated afterwards.
    """
    original = [w for w in tidy(re.sub(r"\([^)]*\)", " ", developer or "")).split(" ") if w]

    def trim(removable: frozenset[str]) -> list[str]:
        words = list(original)
        while len(words) > 1:
            core = _core(words[-1])
            if core and core not in removable:
                break
            words.pop()
        return words

    def only_a_place(words: list[str]) -> bool:
        return len(words) == 1 and _core(words[0]) in _PLACE_WORDS

    words = trim(_LEGAL_WORDS | _GENERIC_WORDS)
    if only_a_place(words):
        # 'Sharjah Real Estate' must not shrink to 'Sharjah' (or 'Sharjah Real'):
        # drop the legal form only, and if that still leaves a bare place word, nothing.
        words = trim(_LEGAL_WORDS)
        if only_a_place(words):
            words = original
    brand = " ".join(words).strip(" ,-&")
    return brand.title() if brand.isupper() else brand


_BY_RE = re.compile(r"^(?P<base>.*\S)\s+by\s+(?P<dev>[^,()]+?)\s*$", re.IGNORECASE)


def _same_developer(captured: str, developer: str) -> bool:
    captured_key = normalise_key(developer_brand(captured) or captured)
    developer_key = normalise_key(developer)
    brand_key = normalise_key(developer_brand(developer))
    if not captured_key:
        return False
    if f" {captured_key} " in f" {developer_key} ":
        return True
    return bool(brand_key) and f" {brand_key} " in f" {captured_key} "


def strip_developer_suffix(name: str, developer: str = "") -> str:
    """
    Remove a trailing "by <developer>": 'Masaar by Arada' -> 'Masaar'.

    With a developer supplied, the suffix is removed only when it names that
    developer, so 'Residences by the Sea' survives. With none supplied, a short
    suffix (up to three words, not starting with "the") is treated as a
    developer tag. Anything else is returned tidied but unchanged.
    """
    text = tidy(name)
    match = _BY_RE.match(text)
    if not match:
        return text
    base, captured = match.group("base").strip(), match.group("dev").strip()
    if tidy(developer):
        return base if _same_developer(captured, developer) else text
    words = captured.split()
    if words and len(words) <= 3 and words[0].lower() not in {"the", "a", "an"}:
        return base
    return text
