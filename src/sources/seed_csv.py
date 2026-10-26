"""
Hand-researched registry rows (docs/DESIGN.md section 4): src/data/seed_registry.csv.

This is the only way a Sharjah project, or a Dubai project the DLD feed does not
list yet, gets into the registry, so it is read strictly:

  * Columns are found by NAME, never by position: Emirate, Project Name (EN),
    Developer, Master Developer, Master Project, Area (EN), Zone, Status,
    Completion Date, Source URL. Order is free, extra columns are ignored (the
    notes count them and name the layout columns the file lacks, so a misspelt header
    is noticed; the text of a header cell is never repeated, because a file saved
    without its header row has a data row there).
  * A row needs an in-scope Emirate, a Latin project name, a Developer (the only
    identity a SEED row has is developer + name) and an http(s) Source URL that
    carries no credentials. The URL is stored in Source Dataset. A row without
    them is dropped and counted, never repaired.
  * A SEED row never has a registry id (models.RegistryRow.registry_key): a hand
    typed number has no authority behind it.
  * The file may legitimately hold no rows (it ships header-only). It may not be
    missing, unreadable, or have a header we cannot use: those are SourceError.
  * Two rows for one project: the later line wins, as in the listing seed. A
    hand-maintained file is append-and-correct, so the later line is the correction.

The file is not a dated feed, so Source Refreshed is left blank. File timestamps
are not used: a git checkout rewrites them, which would make the seed look fresh.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date
from pathlib import Path
from typing import Optional, Sequence, Union

from src.areas import normalise_emirate
from src.config import DATA_DIR, Config
from src.models import RegistryRow, is_http_url, is_placeholder, parse_source_date, utc_now_iso
from src.sources.base import (
    MAX_TEXT_LEN,
    RowTally,
    SourceError,
    SourceResult,
    field_key,
    resolve_today,
    tidy_text,
    url_has_credentials,
)

logger = logging.getLogger(__name__)

SOURCE_NAME = "seed_csv"
SEED_REGISTRY_FILE = DATA_DIR / "seed_registry.csv"

# The file is hand edited; a few megabytes means something else was saved over it.
MAX_SEED_BYTES = 5 * 1024 * 1024
MAX_URL_LEN = 2000
MAX_DROPPED_SHARE = 0.5

# Logical column -> the header it is found under (compared with field_key, so case,
# spaces and punctuation do not matter).
HEADER_FOR_FIELD = {
    "emirate": "Emirate",
    "project_name_en": "Project Name (EN)",
    "developer": "Developer",
    "master_developer": "Master Developer",
    "master_project": "Master Project",
    "area_en": "Area (EN)",
    "zone": "Zone",
    "status": "Status",
    "completion_date": "Completion Date",
    "source_url": "Source URL",
}
# Without these four no row can be identified or sourced; the header is refused.
REQUIRED_FIELDS = ("emirate", "project_name_en", "developer", "source_url")
TEXT_FIELDS = ("project_name_en", "developer", "master_developer", "master_project", "area_en", "zone", "status")

R_TOO_MANY_CELLS = "row has more cells than the header (an unquoted comma?)"
R_EMIRATE = "emirate is not Dubai or Sharjah"
R_NO_NAME = "no project name"
R_NO_URL = "Source URL is missing or is not an http(s) URL"
R_URL_CREDENTIALS = "Source URL carries credentials"
R_OVERSIZE = f"a text value is longer than {MAX_TEXT_LEN} characters"
R_INVALID = "row failed model validation"
R_NO_IDENTITY = (
    "no usable identity (the Developer and the project name must each have a Latin letter, "
    "no letter or number from another script, and none of + # @)"
)
R_DUPLICATE = "same project listed twice (the later row kept)"
A_DATE = "completion date not readable; blanked"


def _clean(cell: object) -> str:
    return tidy_text(str(cell)) if cell is not None else ""


def _field_positions(header: Sequence[str]) -> dict[str, int]:
    """
    Column index per logical field. Two header cells that mean the same column are
    refused (which one holds the value?), as is a header missing a required column.
    """
    wanted = {field_key(name): logical for logical, name in HEADER_FOR_FIELD.items()}
    found: dict[str, int] = {}
    for index, cell in enumerate(header):
        logical = wanted.get(field_key(cell))
        if logical is None:
            continue
        if logical in found:
            raise SourceError(f"{SOURCE_NAME}: the header names the column {HEADER_FOR_FIELD[logical]!r} twice")
        found[logical] = index
    missing = [HEADER_FOR_FIELD[f] for f in REQUIRED_FIELDS if f not in found]
    if missing:
        # The first row is not repeated: a file saved without its header row has a DATA row there.
        # The missing names come from HEADER_FOR_FIELD (code), and the count says how wide the row was.
        raise SourceError(
            f"{SOURCE_NAME}: the header has no column for: {', '.join(missing)}. "
            f"The first row has {len(header)} column(s); its text is not shown"
        )
    return found


def _read_table(path: Path) -> list[list[str]]:
    """The non-blank rows of the CSV (header first). Anything unreadable is a SourceError."""
    try:
        size = path.stat().st_size
        if size > MAX_SEED_BYTES:
            raise SourceError(f"{SOURCE_NAME}: {path.name} is {size} bytes, over the {MAX_SEED_BYTES} byte limit")
        text = path.read_bytes().decode("utf-8-sig")
        return [row for row in csv.reader(io.StringIO(text, newline="")) if any(c.strip() for c in row)]
    except SourceError:
        raise
    except UnicodeDecodeError:
        error = f"{path.name} is not valid UTF-8"
    except csv.Error as exc:
        error = f"{path.name} is not valid CSV ({type(exc).__name__})"
    except OSError as exc:
        error = f"cannot read {path.name} ({type(exc).__name__})"
    raise SourceError(f"{SOURCE_NAME}: {error}")


def _date_cell(text: str, tally: RowTally) -> str:
    """A completion date as YYYY-MM-DD; text that is not a date is blanked and counted."""
    if not text or is_placeholder(text):
        return ""
    iso = parse_source_date(text)
    if iso is None:
        tally.adjust(A_DATE)
        return ""
    return iso


def _row_from_cells(
    cells: Sequence[str], positions: dict[str, int], *, width: int, today_iso: str, tally: RowTally
) -> Optional[RegistryRow]:
    """One validated SEED row, or None with the reason counted in `tally`."""
    if len(cells) > width and any(c.strip() for c in cells[width:]):
        tally.drop(R_TOO_MANY_CELLS)
        return None

    def cell(logical: str) -> str:
        index = positions.get(logical)
        return _clean(cells[index]) if index is not None and index < len(cells) else ""

    emirate = normalise_emirate(cell("emirate"))
    if emirate is None:
        tally.drop(R_EMIRATE)
        return None
    texts = {name: cell(name) for name in TEXT_FIELDS}
    if is_placeholder(texts["project_name_en"]):
        tally.drop(R_NO_NAME)
        return None
    url = cell("source_url")
    if not is_http_url(url):
        tally.drop(R_NO_URL)
        return None
    if url_has_credentials(url):
        tally.drop(R_URL_CREDENTIALS)
        return None
    if len(url) > MAX_URL_LEN or any(len(value) > MAX_TEXT_LEN for value in texts.values()):
        tally.drop(R_OVERSIZE)
        return None
    try:
        row = RegistryRow(
            emirate=emirate,
            registry="SEED",
            completion_date=_date_cell(cell("completion_date"), tally),
            source_dataset=url,
            first_seen=today_iso,
            last_seen=today_iso,
            **texts,
        )
    except ValueError:  # pydantic.ValidationError is a ValueError
        tally.drop(R_INVALID)
        return None
    if not row.has_identity:
        tally.drop(R_NO_IDENTITY)
        return None
    return row


class SeedCsvAdapter:
    """The hand-researched registry seed. Always on; an empty file is a normal state."""

    name = SOURCE_NAME
    expects_rows = False

    def __init__(self, *, path: Union[Path, str, None] = None, today: Optional[date] = None) -> None:
        self._path = Path(path) if path is not None else SEED_REGISTRY_FILE
        self._today = today

    def enabled(self, cfg: Config) -> bool:
        return True

    def skip_reason(self, cfg: Config) -> str:
        return ""

    def fetch(self) -> SourceResult:
        path = self._path
        if not path.is_file():
            raise SourceError(f"{self.name}: the seed file {path.name} is missing (it ships with the repository)")
        table = _read_table(path)
        if not table:
            raise SourceError(f"{self.name}: {path.name} is empty; it must at least hold the header row")
        header, data = table[0], table[1:]
        positions = _field_positions(header)
        known = set(positions.values())
        ignored = [c for i, c in enumerate(header) if i not in known and _clean(c)]
        today_iso = resolve_today(self._today).isoformat()

        tally = RowTally()
        kept: dict[str, RegistryRow] = {}
        for cells in data:
            row = _row_from_cells(cells, positions, width=len(header), today_iso=today_iso, tally=tally)
            if row is None:
                continue
            if row.project_key in kept:
                tally.drop(R_DUPLICATE)
            kept[row.project_key] = row  # an existing key keeps its place in the order
        rows = list(kept.values())

        unusable = tally.dropped - tally.dropped_count(R_DUPLICATE)
        if data and not rows:
            raise SourceError(f"{self.name}: none of the {len(data)} row(s) in {path.name} could be used: {tally.dropped_summary()}")
        if unusable * 2 > len(data):
            raise SourceError(
                f"{self.name}: {unusable} of {len(data)} row(s) in {path.name} were unusable, "
                f"more than {int(MAX_DROPPED_SHARE * 100)} percent: {tally.dropped_summary()}"
            )
        notes = [f"read {len(data)} data row(s) from {path.name}"] if data else [f"{path.name} has no rows (header only)"]
        notes += tally.notes()
        if ignored:
            # Counts and code-defined names only: the text of a header cell is the file's, not ours.
            lacking = [name for field, name in HEADER_FOR_FIELD.items() if field not in positions]
            notes.append(
                f"ignored {len(ignored)} column(s) not in the seed layout"
                + (f"; layout column(s) not found in the file: {', '.join(lacking)}" if lacking else "")
            )
        logger.info("%s: %d row(s) from %d data row(s)", self.name, len(rows), len(data))
        if tally.dropped:
            logger.warning("%s: %s", self.name, tally.dropped_summary())
        return SourceResult(source=self.name, rows=rows, fetched_at=utc_now_iso(), source_refreshed=None, notes=notes)
