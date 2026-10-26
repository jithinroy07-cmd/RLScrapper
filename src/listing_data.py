"""
Listing data: the commercial facts (price, bedrooms, area, handover) that no
registry publishes (docs/DESIGN.md sections 3 and 6).

This module owns the one place those facts live and the parsers that turn what
people type ("AED 1.2M", "Studio to 3BR", "110 sq m") into numbers the map can
filter on. The rule that governs every parser here is the one from DESIGN
section 0: blank beats invented. Each parser returns None for anything
ambiguous, and none of them ever scales a bare number.

  * A comma is a thousands separator and nothing else. '1,250,000' is one
    million two hundred and fifty thousand; '1,2' and '12,50,000' are not
    numbers we will guess at.
  * A bare number is taken as typed. '850' is 850, not 850 thousand.
  * Anything with two numbers (a range), an unknown unit, or extra words that
    change the meaning is None. Ranges belong in the From / To columns.
  * A comparison or bound symbol ('<', '>', '=', or the Unicode less-or-equal,
    greater-or-equal, not-equal and plus-minus signs) makes the whole cell None:
    '<3 BR' is not 3, and dropping the symbol would publish the limit as if it were
    the figure. For bedrooms and area the same goes for a '+', dash or slash left
    dangling beside the number ('3 BR /', '1,200 -', '1,200+ sq ft'). A price is
    the one exception: a trailing '+', 'onwards' or 'from' reads as the From
    price, because that is what the Price From column holds.
  * A total price and a price per sq ft are different columns with different
    parsers; one is never converted into, or read as, the other.

Every row carries the URL its numbers came from. A row without one is rejected,
so no price can reach the map without a source behind it.
"""

from __future__ import annotations

import csv
import logging
import math
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from src.config import DATA_DIR
from src.models import is_http_url, is_valid_project_key, parse_source_date

logger = logging.getLogger(__name__)

LISTING_HEADERS = (
    "Project Key",
    "Price From (AED)",
    "Price To (AED)",
    "Price per sq ft (AED)",
    "Area From (sq ft)",
    "Area To (sq ft)",
    "Bedrooms Min",
    "Bedrooms Max",
    "Property Types",
    "Handover",
    "Payment Plan",
    "Source URL",
    "Source Licence",
    "As Of",
    "Entered By",
)

PROPERTY_TYPES = (
    "Apartment",
    "Villa",
    "Townhouse",
    "Penthouse",
    "Duplex",
    "Hotel Apartment",
    "Plot",
    "Commercial",
    "Mixed Use",
    "Unknown",
)
SOURCE_LICENCES = ("licensed-api", "developer-permitted", "manual")

SEED_FILE = DATA_DIR / "seed_projects.csv"
SQFT_PER_SQM = Decimal("10.7639")
MAX_BEDROOMS = 20  # a larger number is a floor area or a price that slipped in

_KEY_COL = LISTING_HEADERS.index("Project Key")
_URL_COL = LISTING_HEADERS.index("Source URL")

# A number whose commas, if any, are strict thousands groups: 1,250,000 / 850 /
# 1.5 / .5. "1,2" and "12,50,000" do not match as a whole, so they stay ambiguous.
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+"
_NUM_RE = re.compile(_NUM)
# Three digits after a lone dot ("750.000", "1.200", "12.345"). A European writer means a
# thousands separator (750,000), an English one a decimal; nothing in the cell says which,
# and reading it as a decimal turns 750.000 into 750 and 1.200 sq ft into 1. Refused, not guessed.
_DOT_GROUPED_RE = re.compile(r"\d{1,3}\.\d{3}")
# A comparison or bound symbol says "less than", "at least" or "give or take", never "exactly".
# Skipping over it publishes the limit as the figure ('<3 BR' read as 3 is the opposite of what
# was typed), so a cell carrying one is refused. Full-width forms are folded to these by NFKC.
_BOUND_RE = re.compile(r"[<>=\u2260\u2264\u2265\u00b1]")
# Every dash variant (U+2010 to U+2015: hyphens, figure / en / em dash, bar; and the minus sign),
# folded to "-" so that none of them slips past the range and sign checks as an ignorable symbol.
_DASH_RE = re.compile(r"[\u2010-\u2015\u2212]")


def _normalise_text(text: object) -> str:
    """NFKC (so m2 / ft2 superscripts fold to digits), lower-case, single-spaced."""
    return " ".join(unicodedata.normalize("NFKC", str(text)).lower().split())


# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------
_MULTIPLIERS = {
    "m": 10**6, "mn": 10**6, "mil": 10**6, "million": 10**6, "millions": 10**6,
    "k": 10**3, "thousand": 10**3,
}
# The optional trailing dot of an abbreviation ("Dhs.", "approx.") is eaten only when no digit
# follows it: in "AED.5M" the dot belongs to the number (500,000), and eating it would make 5,000,000.
_CURRENCY_RE = re.compile(r"\b(?:aed|dhs?|dirhams?)\b(?:\.(?!\d))?")
_FILLER_RE = re.compile(
    r"\b(?:starting|starts?|from|price|prices|only|approx|approximately|about|around|asking|at"
    r"|onwards|upwards|and above|and up)\b(?:\.(?!\d))?"
)
# Wording that marks a price as per unit of area; it must not parse as a total.
_PER_AREA_RE = re.compile(
    r"(?:/|\bper\b|\bp\.?)\s*(?:sq|sf|sqft|sqm|square)|\bpsf\b|\bsq\.?\s*(?:ft|m)\b|\bsqft\b|\bsqm\b|\bsquare\b|\bft2\b|\bm2\b"
)
_PER_SQFT_RE = re.compile(
    r"\s*(?:/|\bper\b)\s*(?:sq\.?\s*ft\.?|sqft|sf|square\s*(?:feet|foot|ft))\b\.?|\bpsf\b"
)


def _amount(text: str) -> Optional[int]:
    """The shared number-with-optional-unit core of the two price parsers."""
    t = re.sub(r"[*~]", " ", text)
    t = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", t)  # 'aed1.2m' -> 'aed 1.2 m'
    t = _CURRENCY_RE.sub(" ", t)
    t = _FILLER_RE.sub(" ", t)
    # Leading punctuation is NOT stripped past a separator colon: a leading "." is
    # part of ".5M" (500,000, not 5,000,000) and a leading "-" is a minus sign.
    # A trailing "-" is NOT stripped: "850K -" is a range with its end missing, and a
    # range is never a total price ("1,250,000-" must not read as 1,250,000).
    t = " ".join(t.split()).lstrip(" :").rstrip(" :/+.,")
    match = re.fullmatch(rf"(?P<num>{_NUM})(?:\s*(?P<unit>[a-z]+))?", t)
    if not match:
        return None
    unit = match.group("unit")
    if unit is None:
        multiplier = 1
    elif unit in _MULTIPLIERS:
        multiplier = _MULTIPLIERS[unit]
    else:
        return None
    if multiplier == 1 and _DOT_GROUPED_RE.fullmatch(match.group("num")):
        return None  # a unit ("1.250 m") settles the dot as a decimal point; a bare "750.000" does not
    value = Decimal(match.group("num").replace(",", "")) * multiplier
    if value <= 0 or value != value.to_integral_value():
        return None
    return int(value)


def _numeric_input(value: object) -> Optional[int]:
    """Cells that arrive already numeric (a sheet's number format)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value > 0 and value == int(value) else None
    return None


def parse_price_aed(text: object) -> Optional[int]:
    """
    A TOTAL price in whole AED, or None.

        'AED 1.2M' / '1.2 million'        -> 1_200_000
        '850K'                            -> 850_000
        'Dhs 1,250,000' / '1,250,000'     -> 1_250_000
        'from AED 850,000'                -> 850_000
        'AED 850,000+' / '850K onwards'   -> 850_000    (open upwards = a From price, which is what the column holds)
        '< 1,250,000', '>= 1.2M'          -> None       (a bound is not the price)
        '850'                             -> 850        (a bare number is never scaled)
        '1,2 million', '12,50,000'        -> None       (comma is thousands only)
        '750.000'                         -> None       (dot before three digits: a thousands separator or a decimal?)
        'AED 850K - 1.2M', 'on request'   -> None       (a range or no number)
        'AED 1,800 per sq ft'             -> None       (a per-area price is not a total)

    Result is an exact integer; a value that is not a whole number of AED
    ('1.23456M' is fine, '1.5' is not) is ambiguous and gives None.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return _numeric_input(text)
    t = _normalise_text(text)
    if not t or _PER_AREA_RE.search(t):
        return None
    return _amount(t)


def parse_price_per_sqft_aed(text: object) -> Optional[int]:
    """
    A price per square foot in whole AED: '1,800', 'AED 1,800/sq ft', '1,800 psf'.
    A per-square-metre price is None (it is not converted: that would be
    inventing a number the source never stated).
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return _numeric_input(text)
    t = _normalise_text(text)
    if not t:
        return None
    t = _PER_SQFT_RE.sub(" ", t)
    if _PER_AREA_RE.search(t):
        return None
    return _amount(t)


# ---------------------------------------------------------------------------
# Bedrooms
# ---------------------------------------------------------------------------
_UNIT_WORDS = frozenset(
    {"br", "brs", "bhk", "bed", "beds", "bedroom", "bedrooms", "bd", "bds", "bdr", "bdrm", "bdrms", "bedrm"}
)
_MAID_RE = re.compile(r"[+/]?\s*(?:\d+\s*)?maids?(?:['’]s)?\b(?:\s*(?:rooms?|quarters?))?")
_AREA_PHRASE_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:sq\.?\s*(?:ft|feet|m|meters?|metres?)\b\.?|sqft|sqm|m2|ft2)"
)
_BED_TOKEN_RE = re.compile(r"studios?|\d+(?:,\d{3})*(?:\.\d+)?|[a-z]+|[-/&,+]")


def parse_bedrooms(text: object) -> Optional[tuple[int, int]]:
    """
    (min, max) bedrooms, or None. Studio is 0.

        'Studio' -> (0, 0)      '1BR' / '2 BR' / '3 Bedrooms' / '4 BHK' -> (n, n)
        '1-3 BR' -> (1, 3)      'Studio to 3BR' -> (0, 3)      '1, 2 & 3 BR' -> (1, 3)
        '3 BR + Maid' -> (3, 3)       maid rooms are ignored, not counted ('3BR/Maid' too)
        '2 BR 1,200 sq ft' -> (2, 2)   an area phrase is ignored, not read as bedrooms
        '2.5 BR', '3+ BR', 'up to 4 BR', 'from 2 BR', '3 floors' -> None
        '<3 BR', '>= 2', '3 BR /', '.5 BR' -> None   (a bound, or a symbol stranded beside the count)

    A bare number or range with no other words ('3', '1-3') is accepted: that is
    how the Bedrooms Min / Max cells are typed. A listing with several
    non-adjacent sizes becomes the spanning (min, max), as the two columns
    cannot hold a set.
    """
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        if isinstance(text, float) and not (math.isfinite(text) and text == int(text)):
            return None
        number = int(text)
        return (number, number) if 0 <= number <= MAX_BEDROOMS else None

    t = _normalise_text(text)
    if _BOUND_RE.search(t):
        return None  # "<3 BR", ">= 2", "<= 2 BR" state a limit, not the count
    t = _DASH_RE.sub("-", t).replace("b/r", "br")
    t = _MAID_RE.sub(" ", t)
    t = _AREA_PHRASE_RE.sub(" ", t)
    if re.search(r"(?<!\d)\.\d", t):
        return None  # ".5 BR": the bare token regex below would drop the dot and read 5
    tokens = _BED_TOKEN_RE.findall(t)

    # A range marker ("-", "/" or "to") needs a size on both sides: "-1", "1-", "/3", "3 /",
    # "1--3" and "up to 4" are not ranges, and a leading minus is not a bedroom count.
    shape = "".join(
        "V" if tok.startswith("studio") or tok[0].isdigit() else "S"
        for tok in tokens
        if tok.startswith("studio") or tok[0].isdigit() or tok in ("-", "/", "to")
    )
    if shape.startswith("S") or shape.endswith("S") or "SS" in shape:
        return None

    values: list[int] = []
    words: list[str] = []
    for token in tokens:
        if token.startswith("studio"):
            values.append(0)
        elif token[0].isdigit():
            if "," in token or "." in token:
                return None
            number = int(token)
            if number > MAX_BEDROOMS:
                return None
            values.append(number)
        elif token == "+":
            return None  # open-ended ("3+") or a leftover we cannot place
        elif token.isalpha():
            words.append(token)

    if not values:
        return None
    # Any word that is not a bedroom unit or a connector ("3 floors", "3 BR villa
    # with 5 car garage") means the numbers may not be bedrooms at all.
    benign = _UNIT_WORDS | {"to", "and", "or", "from", "up"}
    if any(w not in benign for w in words):
        return None
    if "up" in words:
        return None  # "up to 4 BR" states a maximum only
    if "from" in words and len(set(values)) == 1:
        return None  # "from 2 BR" states a minimum only
    return (min(values), max(values))


# ---------------------------------------------------------------------------
# Area
# ---------------------------------------------------------------------------
_SQM_RE = re.compile(
    r"\b(?:sq\.?\s*m(?:eters?|etres?)?|square\s*(?:meters?|metres?)|sqm|m2)\b\.?"
)
_SQFT_RE = re.compile(
    r"\b(?:sq\.?\s*ft|sq\.?\s*feet|sqft|square\s*(?:feet|foot|ft)|ft2|sf)\b\.?"
)
# A minus (or any dash) standing directly in front of a number, not inside a word
# such as "built-up": hyphen-minus, the dash variants and U+2212.
_NEGATIVE_RE = re.compile(r"(?<![\w])[-‐-―−]\s*[\d.]")
_AREA_FILLER = frozenset(
    {"from", "approx", "approximately", "about", "around", "area", "size", "bua", "built", "up",
     "total", "starting", "starts", "start", "at", "of"}
)
# What is left beside the number once the unit and the filler words are gone must not be a plus,
# slash, backslash, ampersand, bar or dash: "1,200+", "1,200 -", "1,200 /" are a bound or a range
# with the other half missing. A hyphen inside a word ("built-up") is removed before this runs.
_AREA_STRANDED_RE = re.compile(r"[-+/\\&|]")
_INNER_HYPHEN_RE = re.compile(r"(?<=[a-z])-(?=[a-z])")


def parse_area_sqft(text: object) -> Optional[int]:
    """
    An area in whole square feet, or None. Square metres are converted with
    10.7639 and rounded half up.

        '1,200 sq ft' / '1200sqft'  -> 1200       '110 sq m' / '110 sqm' -> 1184
        '1200'                      -> 1200       (the column is sq ft, so a bare number is sq ft)
        '1,200 - 1,500 sq ft'       -> None       (a range: enter From and To separately)
        '2 acres', '1,2 sq ft'      -> None
        '1.200 sq ft'               -> None       (dot before three digits: a thousands separator or a decimal?)
        '< 1200 sq ft', '>= 900'    -> None       (a bound is not the area)
        '1,200+ sq ft', '1,200 -'   -> None       (open ended or a range with its end missing)
    """
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        if isinstance(text, float) and math.isfinite(text):
            # Half up, like the text path: round() would give 1200 for 1200.5 (half to even).
            return _numeric_input(int(Decimal(repr(text)).to_integral_value(rounding=ROUND_HALF_UP)))
        return _numeric_input(text)
    t = _normalise_text(text)
    if _BOUND_RE.search(t):
        return None  # "< 1200 sq ft", ">= 900": a limit, not the area
    t = _DASH_RE.sub("-", t)
    # A minus sign makes the area negative, which is not an area: refuse it rather
    # than drop the sign and return the magnitude ("-1200 sq ft" is not 1200).
    if _NEGATIVE_RE.search(t):
        return None
    t = re.sub(r"(?<=\d)(?=[a-z])", " ", t)  # '1200sqft' -> '1200 sqft'
    has_sqm, has_sqft = bool(_SQM_RE.search(t)), bool(_SQFT_RE.search(t))
    if has_sqm and has_sqft:
        return None
    t = _SQM_RE.sub(" ", _SQFT_RE.sub(" ", t))
    numbers = _NUM_RE.findall(t)
    if len(numbers) != 1:
        return None
    rest = _NUM_RE.sub(" ", t, count=1)
    if any(word not in _AREA_FILLER for word in re.findall(r"[a-z]+", rest)):
        return None
    if _AREA_STRANDED_RE.search(_INNER_HYPHEN_RE.sub(" ", rest)):
        return None
    if _DOT_GROUPED_RE.fullmatch(numbers[0]):
        return None
    value = Decimal(numbers[0].replace(",", ""))
    if has_sqm:
        value *= SQFT_PER_SQM
    # to_integral_value, not quantize: quantize raises InvalidOperation once the
    # result needs more digits than the decimal context holds (a 40-digit cell).
    value = value.to_integral_value(rounding=ROUND_HALF_UP)
    return int(value) if value > 0 else None


# ---------------------------------------------------------------------------
# Property types
# ---------------------------------------------------------------------------
# Ordered most specific first; each match is removed from the name before the
# next rule runs, so "Hotel Apartments" is not also an "Apartment". Deliberately
# narrow: "Tower", "Residences", "Heights" and "Mall" are NOT here, because each
# is used for several kinds of building and "Tower" in particular is as often an
# office as a block of flats. Unknown beats a wrong guess.
_TYPE_RULES = (
    ("Hotel Apartment", re.compile(r"\bhotel\s+apartments?\b", re.I)),
    ("Penthouse", re.compile(r"\bpent\s*houses?\b", re.I)),
    ("Duplex", re.compile(r"\bduplex(?:es)?\b", re.I)),
    ("Townhouse", re.compile(r"\btown\s*houses?\b|\btownhomes?\b", re.I)),
    ("Villa", re.compile(r"\bvillas?\b", re.I)),
    ("Plot", re.compile(r"\bplots?\b|\bplotted\b", re.I)),
    ("Mixed Use", re.compile(r"\bmixed[\s-]*use\b", re.I)),
    ("Commercial", re.compile(r"\bcommercial\b|\boffices?\b|\bretail\b|\bshops?\b|\bshowrooms?\b|\bwarehouses?\b", re.I)),
    ("Apartment", re.compile(r"\bapartments?\b|\bflats?\b", re.I)),
)

_TYPE_ALIASES = {
    "apartment": "Apartment", "apartments": "Apartment", "apt": "Apartment", "apts": "Apartment",
    "flat": "Apartment", "flats": "Apartment", "condo": "Apartment", "condominium": "Apartment",
    "villa": "Villa", "villas": "Villa",
    "townhouse": "Townhouse", "townhouses": "Townhouse", "town house": "Townhouse",
    "town houses": "Townhouse", "townhome": "Townhouse", "townhomes": "Townhouse",
    "penthouse": "Penthouse", "penthouses": "Penthouse",
    "duplex": "Duplex", "duplexes": "Duplex",
    "hotel apartment": "Hotel Apartment", "hotel apartments": "Hotel Apartment",
    "plot": "Plot", "plots": "Plot", "land": "Plot",
    "commercial": "Commercial", "office": "Commercial", "offices": "Commercial", "retail": "Commercial",
    "mixed use": "Mixed Use", "mixed-use": "Mixed Use",
    "unknown": "Unknown",
}


def type_from_name(project_name: str) -> str:
    """
    A property type from a project NAME alone, or 'Unknown'. Strict by design.

    Only explicit type words count (villa, townhouse, penthouse, duplex, plot,
    apartment, hotel apartment, mixed use, commercial / office / retail).
    A name with two residential types ('Villas and Townhouses') is Unknown; one
    with a commercial word and a residential word is Mixed Use. 'Tower' alone
    never implies anything, least of all Commercial.
    """
    working = project_name or ""
    found: list[str] = []
    for label, pattern in _TYPE_RULES:
        if pattern.search(working):
            found.append(label)
            working = pattern.sub(" ", working)
    if not found:
        return "Unknown"
    if "Mixed Use" in found:
        return "Mixed Use"
    if "Commercial" in found:
        return "Commercial" if len(found) == 1 else "Mixed Use"
    return found[0] if len(found) == 1 else "Unknown"


def normalise_type(text: object) -> str:
    """
    One typed label to a canonical type: '' for blank, 'Unknown' for text that is
    not recognised. Exact labels and common synonyms first, then the same strict
    name rules as type_from_name.
    """
    cleaned = " ".join(str(text or "").lower().replace("_", " ").split())
    if not cleaned:
        return ""
    return _TYPE_ALIASES.get(cleaned) or type_from_name(cleaned)


def parse_property_types(text: object) -> list[str]:
    """
    The Property Types cell as a list of canonical types, in order, no repeats:
    'Apartments & Penthouses' -> ['Apartment', 'Penthouse']. Blank -> []. Text
    with nothing recognisable -> ['Unknown']; Unknown is dropped when something
    real is present.
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    labels: list[str] = []
    for piece in re.split(r"[,;/&+]|\band\b", raw, flags=re.I):
        label = normalise_type(piece)
        if label and label not in labels:
            labels.append(label)
    known = [label for label in labels if label != "Unknown"]
    return known or ["Unknown"]


# ---------------------------------------------------------------------------
# Listing_Data records (what the map reads)
# ---------------------------------------------------------------------------
def _cell_text(value: object) -> str:
    return "" if value is None else " ".join(str(value).split())


def load_listing_records(records: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    Listing_Data rows (header-keyed, e.g. gspread get_all_records) as
    {Project Key: parsed record}. The latest row for a key wins, so an analyst
    corrects an entry by appending a line. Rows with an invalid key, or with no
    http(s) Source URL, are skipped: a price without its source is not shown.
    Unparseable numbers become None, never a guess.
    """
    out: dict[str, dict[str, Any]] = {}
    skipped = 0
    bad_keys = 0
    refused = 0

    def parse_cell(parser, value):
        # A typed cell that comes back None was refused as ambiguous, not left blank: count it.
        nonlocal refused
        result = parser(value)
        if result is None and _cell_text(value):
            refused += 1
        return result

    for record in records:
        key = _cell_text(record.get("Project Key"))
        if not is_valid_project_key(key):
            if key:  # a typed key that is not namespaced is a mistake to surface; a blank line is not
                bad_keys += 1
            continue
        url = _cell_text(record.get("Source URL"))
        if not is_http_url(url):
            skipped += 1
            continue
        beds_min = parse_cell(parse_bedrooms, record.get("Bedrooms Min"))
        beds_max = parse_cell(parse_bedrooms, record.get("Bedrooms Max"))
        out[key] = {
            "project_key": key,
            "price_from": parse_cell(parse_price_aed, record.get("Price From (AED)")),
            "price_to": parse_cell(parse_price_aed, record.get("Price To (AED)")),
            "price_per_sqft": parse_cell(parse_price_per_sqft_aed, record.get("Price per sq ft (AED)")),
            "area_from": parse_cell(parse_area_sqft, record.get("Area From (sq ft)")),
            "area_to": parse_cell(parse_area_sqft, record.get("Area To (sq ft)")),
            "bedrooms_min": beds_min[0] if beds_min else None,
            "bedrooms_max": beds_max[1] if beds_max else None,
            "types": parse_property_types(record.get("Property Types")),
            "handover": _cell_text(record.get("Handover")),
            "payment_plan": _cell_text(record.get("Payment Plan")),
            "source_url": url,
            "licence": _cell_text(record.get("Source Licence")),
            "as_of": parse_source_date(record.get("As Of")) or "",
            "entered_by": _cell_text(record.get("Entered By")),
        }
    if skipped:
        logger.warning("Listing_Data: %d row(s) skipped for lack of a Source URL", skipped)
    if bad_keys:
        logger.warning("Listing_Data: %d row(s) skipped for an invalid Project Key", bad_keys)
    if refused:
        # A count only: a public log must not carry what an analyst typed.
        logger.warning("Listing_Data: %d typed number cell(s) refused as ambiguous and left blank on the map", refused)
    return out


# ---------------------------------------------------------------------------
# Seed file
# ---------------------------------------------------------------------------
class SeedFileError(ValueError):
    """
    The Listing_Data seed file cannot be used as a whole. This project's own error type
    (a ValueError, as it always was): main shows its message in a public log, so the
    message is a fixed phrase that names a column and never a cell or the file's text.
    """


def _pad(row: Sequence[str]) -> list[str]:
    # A None cell is blank, never the text "None" (which would be written to the sheet).
    cells = ["" if c is None else str(c).strip() for c in row[: len(LISTING_HEADERS)]]
    return cells + [""] * (len(LISTING_HEADERS) - len(cells))


def read_seed(path: Path | str = SEED_FILE) -> list[list[str]]:
    """
    The seed CSV as rows aligned to LISTING_HEADERS (header excluded). A missing
    file is an empty list: a project with no seed yet is normal, not an error.

    The first row is a header unless its first cell is already a valid Project
    Key, in which case the file has no header and columns are taken in
    LISTING_HEADERS order. With a header, columns are matched by NAME, so column
    order is free and extra columns are ignored; a header with no 'Project Key'
    column raises SeedFileError, a ValueError with a fixed message (every row
    would be unusable, and saying so beats dropping them one by one). Blank lines are skipped; every row is padded (or
    cut) to the header width.
    """
    seed_path = Path(path)
    if not seed_path.is_file():
        return []
    with open(seed_path, newline="", encoding="utf-8-sig") as handle:
        rows = [r for r in csv.reader(handle) if any(c.strip() for c in r)]
    if not rows:
        return []
    if is_valid_project_key(rows[0][0].strip()):
        return [_pad(r) for r in rows]
    header = [c.strip() for c in rows[0]]
    if "Project Key" not in header:
        raise SeedFileError("the Listing_Data seed file has no 'Project Key' column in its header row")
    position: dict[str, int] = {}
    for index, name in enumerate(header):
        position.setdefault(name, index)
    aligned = []
    for row in rows[1:]:
        aligned.append(
            [
                row[position[h]].strip() if h in position and position[h] < len(row) else ""
                for h in LISTING_HEADERS
            ]
        )
    return aligned


def seed_row_problem(row: Sequence[str]) -> Optional[str]:
    """
    Why a seed row cannot be added, or None if it can. The text is fixed: it names the
    field and never the cell, because the callers print it in a public log and a key
    is a developer and project name. The caller adds the row number.
    """
    cells = _pad(row)
    if not is_valid_project_key(cells[_KEY_COL]):
        return "Project Key is not a valid namespaced key"
    if not is_http_url(cells[_URL_COL]):
        return "Source URL is required and must be an http(s) URL"
    return None


def check_seed_rows(seed_rows: Sequence[Sequence[str]]) -> list[str]:
    """One human-readable line per rejectable seed row, so a bad seed fails loudly."""
    problems = []
    for number, row in enumerate(seed_rows, start=1):
        problem = seed_row_problem(row)
        if problem:
            problems.append(f"seed data row {number}: {problem}")
    return problems


def rows_to_add(existing_keys: Iterable[str], seed_rows: Sequence[Sequence[str]]) -> list[list[str]]:
    """
    Seed rows to append to Listing_Data.

      * Rows without a valid namespaced Project Key or an http(s) Source URL are
        dropped (and logged); a seed is never trusted over the rules.
      * A key already in the sheet is never touched: an analyst's entry always
        outranks the seed.
      * Within the seed, the latest row for a key wins.
      * Rows come back padded to the header width, in first-seen key order.
    """
    have = {k.strip() for k in existing_keys if k and k.strip()}
    latest: dict[str, list[str]] = {}
    for number, row in enumerate(seed_rows, start=1):
        problem = seed_row_problem(row)
        if problem:
            logger.warning("seed data row %d skipped: %s", number, problem)
            continue
        cells = _pad(row)
        latest[cells[_KEY_COL]] = cells
    return [cells for key, cells in latest.items() if key not in have]
