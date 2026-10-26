"""
Sharjah registry source: the Sharjah Real Estate Registration Department (SRERD)
"List of Projects" page (docs/DESIGN.md section 4).

Status: UNVERIFIED and off by default (ENABLE_SRERD=true). The research digest
found, in the department's own page script, a POST to the cms-proxy projects
endpoint and a column list of project name (AR/EN), area (AR/EN) and ownership
type. Nobody has seen a response, so the shape is parsed tolerantly and anything
unrecognised is a SourceError rather than a guess: an unfamiliar answer, zero rows,
fewer rows than the answer itself says it has, or rows that cannot be keyed.

Identity is the weak point, and the reason this adapter is stricter than it looks:

  * A Sharjah row is keyed by its SRERD id, or else by developer + name
    (DESIGN section 2). The list has no developer column, so a row with no id has
    NO key at all. Such rows are dropped and counted; if that is every row the
    adapter fails and says so, instead of writing blank Project Keys to the sheet.
  * Only id fields that clearly name a project id are used (proId, projectId ...).
    A bare "id" is not trusted: it may be a row position, and a position that
    shifts between runs would merge today's project with yesterday's neighbour.
    For the same reason ids that form exactly 1..N in payload order are ignored.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from typing import Any, Callable, Mapping, Optional, Sequence

from src.config import Config
from src.models import SRERD_ID_RE, RegistryRow, clean_registry_id, utc_now_iso
from src.sources.base import (
    DEFAULT_RETRY,
    MAX_TEXT_LEN,
    RetryPolicy,
    RowTally,
    SourceError,
    SourceResult,
    as_cell,
    as_text,
    describe_keys,
    describe_types,
    extract_records,
    extract_total,
    field_key,
    first_match,
    is_arabic_only,
    normalise_record,
    parse_json_response,
    resolve_today,
    send_with_retries,
    session_scope,
    user_agent,
)

logger = logging.getLogger(__name__)

SOURCE_NAME = "srerd_projects"

# From the research digest: found in the department's page script. Not called by anyone yet.
SRERD_PROJECTS_URL = "https://cms-proxy.shjrerd.gov.ae/api/Projects/GetProjects"
# UNVERIFIED: the request body the endpoint wants. An empty object is the smallest thing
# that can work; if the service needs paging or filter fields it will answer with an
# error or no rows, and either way the adapter fails loudly.
SRERD_REQUEST_BODY: Mapping[str, Any] = {}
SRERD_TIMEOUT = (30, 120)
MAX_DROPPED_SHARE = 0.5
MIN_POSITIONAL_ROWS = 5  # fewer rows than this and "1..N" proves nothing

NAME_EN_ALIASES = (
    "projectnameen", "projectnameenglish", "projecten", "projectenglishname",
    "nameen", "enname", "englishname", "titleen",
)
NAME_AR_ALIASES = (
    "projectnamear", "projectnamearabic", "projectar", "projectarabicname",
    "namear", "arname", "arabicname", "titlear",
)
NAME_GENERIC_ALIASES = ("projectname", "name", "title")
AREA_EN_ALIASES = ("areanameen", "areaen", "areaname", "locationen", "districten", "area")
AREA_AR_ALIASES = ("areanamear", "areaar", "locationar", "districtar")
DEVELOPER_ALIASES = ("developername", "developernameen", "developeren", "developer")
STATUS_ALIASES = ("projectstatus", "status")
# A bare "id" is deliberately absent (see the module docstring).
ID_ALIASES = ("proid", "projectid", "projectno", "projectnumber", "projectcode")

ALL_NAME_ALIASES = NAME_EN_ALIASES + NAME_AR_ALIASES + NAME_GENERIC_ALIASES

R_NOT_OBJECT = "row is not an object"
R_NO_NAME = "no project name"
# Static text (it is logged and written to the run notes): the rule, never a cell value. The rule is
# models.identity_slug's: the developer and the project name each need a Latin letter, no letter or
# number from another script, and none of + # @.
R_NO_IDENTITY = (
    "no usable identity (no id field, and no developer and project name that each have "
    "a Latin letter, no letter or number from another script, and none of + # @)"
)
R_OVERSIZE = f"a text value is longer than {MAX_TEXT_LEN} characters"
R_INVALID = "row failed model validation"
R_DUPLICATE = "duplicate id (first row kept)"
A_BAD_ID = "id has characters outside A-Z a-z 0-9 . _ - or is too long; blanked"
A_POSITIONAL = "ids are exactly 1..N in payload order, which looks like row positions, not stable ids; all ids ignored"

_NUMBER_ONLY_RE = re.compile(r"^[\d.,\s]+$")


def _place_text(value: object) -> Optional[str]:
    """Area text; a bare number is an area SIZE, not a place, and is refused."""
    text = as_text(value)
    return None if text is None or _NUMBER_ONLY_RE.match(text) else text


def check_shape(records: Sequence[Any]) -> None:
    """Refuse rows with no project-name column anywhere: the answer is not the list we expect."""
    objects = [r for r in records if isinstance(r, Mapping)]
    if not objects:
        raise SourceError(
            f"{SOURCE_NAME}: the answer has {len(records)} row(s) but none is an object "
            f"(types seen: {describe_types(records)})"
        )
    raw_names: dict[str, None] = {}
    for record in objects:
        for key in record:
            raw_names.setdefault(str(key), None)
    if not {field_key(k) for k in raw_names} & set(ALL_NAME_ALIASES):
        raise SourceError(
            f"{SOURCE_NAME}: the answer has {len(records)} row(s) but no recognised project name column. "
            f"Columns seen: {describe_keys(raw_names)}"
        )


def _ids_look_positional(ids: Sequence[Optional[str]]) -> bool:
    """
    True when the ids that are present are row POSITIONS in payload order: every one equals its
    row number counted from 1 (an HTML table index) or from 0 (an array index, where the 0 itself
    is cleaned to "no id"). A missing id does not break the run, and at least MIN_POSITIONAL_ROWS
    ids must be present for the match to mean anything. A non-numeric id means a real id column.
    The cost of a false positive is loud (rows without a developer are dropped and counted),
    the cost of a false negative is unstable keys, so the guard leans towards positional.
    """
    present = [(position, i) for position, i in enumerate(ids) if i is not None]
    if len(present) < MIN_POSITIONAL_ROWS or any(not i.isdigit() for _, i in present):
        return False
    return all(int(i) == position + 1 for position, i in present) or all(int(i) == position for position, i in present)


def parse_srerd_records(
    records: Sequence[Any],
    *,
    today: date,
    source_dataset: str = SRERD_PROJECTS_URL,
    tally: Optional[RowTally] = None,
) -> list[RegistryRow]:
    """
    Map raw list entries to Sharjah/SRERD registry rows. Anything that cannot be
    keyed, named or validated is dropped and counted in `tally`; nothing is guessed.
    """
    tally = tally if tally is not None else RowTally()
    today_iso = today.isoformat()
    recs: list[Optional[dict[str, Any]]] = []
    for raw in records:
        if isinstance(raw, Mapping):
            recs.append(normalise_record(raw))
        else:
            tally.drop(R_NOT_OBJECT)
    raw_ids = [clean_registry_id(first_match(rec, ID_ALIASES, as_cell)) for rec in recs if rec is not None]
    ignore_ids = _ids_look_positional(raw_ids)
    if ignore_ids:
        tally.adjust(A_POSITIONAL)

    rows: list[RegistryRow] = []
    seen: set[str] = set()
    for rec, raw_id in zip((r for r in recs if r is not None), raw_ids):
        number = None if ignore_ids else raw_id
        if number is not None and not SRERD_ID_RE.match(number):
            tally.adjust(A_BAD_ID)
            number = None
        row = _row_from_record(rec, number, today_iso=today_iso, source_dataset=source_dataset, tally=tally)
        if row is None:
            continue
        key = row.registry_key()
        if key:
            if key in seen:
                tally.drop(R_DUPLICATE)
                continue
            seen.add(key)
        rows.append(row)
    return rows


def _row_from_record(
    rec: Mapping[str, Any], number: Optional[str], *, today_iso: str, source_dataset: str, tally: RowTally
) -> Optional[RegistryRow]:
    name_en = first_match(rec, NAME_EN_ALIASES, as_text)
    name_ar = first_match(rec, NAME_AR_ALIASES, as_text)
    generic = first_match(rec, NAME_GENERIC_ALIASES, as_text)
    if generic:
        if is_arabic_only(generic):
            name_ar = name_ar or generic
        else:
            name_en = name_en or generic
    if not (name_en or name_ar):
        tally.drop(R_NO_NAME)
        return None
    texts = {
        "project_name_en": name_en,
        "project_name_ar": name_ar,
        "developer": first_match(rec, DEVELOPER_ALIASES, as_text),
        "area_en": first_match(rec, AREA_EN_ALIASES, _place_text),
        "area_ar": first_match(rec, AREA_AR_ALIASES, _place_text),
        "status": first_match(rec, STATUS_ALIASES, as_text),
    }
    if any(value is not None and len(value) > MAX_TEXT_LEN for value in texts.values()):
        tally.drop(R_OVERSIZE)
        return None
    try:
        row = RegistryRow(
            emirate="Sharjah",
            registry="SRERD",
            registry_project_no=number,
            source_dataset=source_dataset,
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


class SrerdProjectsAdapter:
    """Sharjah: the SRERD projects list (one POST; shape unverified, parsed tolerantly)."""

    name = SOURCE_NAME
    expects_rows = True

    def __init__(
        self,
        cfg: Config,
        *,
        session: Any = None,
        today: Optional[date] = None,
        sleep: Callable[[float], None] = time.sleep,
        retry: RetryPolicy = DEFAULT_RETRY,
        url: str = SRERD_PROJECTS_URL,
    ) -> None:
        self._cfg = cfg
        self._session = session
        self._today = today
        self._sleep = sleep
        self._retry = retry
        self._url = url

    def enabled(self, cfg: Config) -> bool:
        return bool(cfg.enable_srerd)

    def skip_reason(self, cfg: Config) -> str:
        return "" if cfg.enable_srerd else "ENABLE_SRERD is not enabled (this source is off by default)"

    def fetch(self) -> SourceResult:
        if not self.enabled(self._cfg):
            raise SourceError(f"{self.name}: {self.skip_reason(self._cfg)}")
        today = resolve_today(self._today)
        with session_scope(self._session) as session:

            def send() -> Any:
                return session.post(
                    self._url,
                    json=dict(SRERD_REQUEST_BODY),
                    headers={"Accept": "application/json", "User-Agent": user_agent(self._cfg)},
                    timeout=SRERD_TIMEOUT,
                )

            response = send_with_retries(send, label=self.name, policy=self._retry, sleep=self._sleep)
            payload = parse_json_response(response, label=self.name)

        records = extract_records(payload)
        if records is None:
            seen = describe_keys(payload) if isinstance(payload, Mapping) else f"a {type(payload).__name__}"
            raise SourceError(f"{self.name}: the answer has no list of projects (it contained: {seen})")
        if not records:
            raise SourceError(f"{self.name}: the answer has zero projects")
        stated = extract_total(payload)
        if stated is not None and stated > len(records):
            raise SourceError(
                f"{self.name}: the answer holds {len(records)} of the {stated} projects it states, "
                "and paging is not implemented; refusing a partial list"
            )
        check_shape(records)

        tally = RowTally()
        rows = parse_srerd_records(records, today=today, source_dataset=self._url, tally=tally)
        if not rows:
            raise SourceError(f"{self.name}: none of the {len(records)} row(s) could be used: {tally.dropped_summary()}")
        if tally.dropped * 2 > len(records):
            raise SourceError(
                f"{self.name}: {tally.dropped} of {len(records)} row(s) were unusable, "
                f"more than {int(MAX_DROPPED_SHARE * 100)} percent: {tally.dropped_summary()}"
            )
        notes = [f"fetched {len(records)} raw row(s)"] + tally.notes()
        logger.info("%s: %d row(s) kept from %d raw row(s)", self.name, len(rows), len(records))
        return SourceResult(source=self.name, rows=rows, fetched_at=utc_now_iso(), source_refreshed=None, notes=notes)
