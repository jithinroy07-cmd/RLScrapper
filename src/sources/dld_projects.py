"""
Dubai registry source: the Dubai Land Department "Real Estate Projects" dataset
through the Data.Dubai open API (docs/DESIGN.md section 4).

Status: built against the documented schema and synthetic fixtures. It has NEVER
been run against the live API (no key exists yet, DESIGN section 10), so every
fact about the live service below is a named constant marked UNVERIFIED, and every
disagreement with reality ends in a loud SourceError, never in empty or partial
rows:

  * the payload has rows but no column we recognise  -> error naming the missing
    logical fields and the column NAMES seen (never a value from a row);
  * paging cannot finish, repeats a page, or ends short of the stated total
    -> error (a partial registry would delete projects on the next merge);
  * most rows unusable -> error with the reasons counted.

A second way in, for a person with no key yet: the portal lets a signed-in user download
the same dataset as a JSON file. With DATA_DUBAI_PROJECTS_FILE (local .env only; the
workflow never passes it) pointing at that file, fetch() reads it instead of calling the
API, through the same schema check, row mapping and unusable-row guards. The 28 October
2026 download (3481 rows) mapped with the aliases below unchanged, which is the only
live evidence of the column names so far. The path is never logged.

The emirate is Dubai because the registry is DLD (DESIGN section 1), never because
of anything in a row. The registry id is the DLD project number; when a row has no
valid one it is left None and the row is keyed by developer + name instead.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Any, Callable, Mapping, Optional, Sequence

from src.config import Config
from src.models import DLD_ID_RE, RegistryRow, clean_registry_id, utc_now_iso
from src.sources.base import (
    DEFAULT_RETRY,
    MAX_TEXT_LEN,
    as_date,
    RetryPolicy,
    RowTally,
    SourceError,
    SourceResult,
    as_cell,
    as_text,
    describe_keys,
    describe_types,
    extract_records,
    extract_refreshed,
    extract_total,
    field_key,
    first_date,
    first_match,
    is_arabic_only,
    normalise_record,
    parse_json_response,
    resolve_today,
    scrub_url,
    send_with_retries,
    session_scope,
    user_agent,
)

logger = logging.getLogger(__name__)

SOURCE_NAME = "dld_projects"

# The data route. Stated as "dataAPIEndpoints" on the catalogue record of the dataset
# (https://data.dubai/o/c/datasets/467654, read 25 Oct 2026); the route answers 401 without a key.
DLD_PROJECTS_URL = "https://apis.data.dubai/open/dld/dld_projects-open-api"
# The dataset's page on the portal, where a signed-in person downloads the file that
# DATA_DUBAI_PROJECTS_FILE points at. Rows read from such a file carry it as their Source Dataset.
DLD_DOWNLOAD_PAGE = "https://data.dubai/en/web/guest/l/467654"
# The per-row load stamp in the portal's download; its date is the source refresh date.
LOAD_STAMP_ALIASES = ("loadtimestamp", "loaddate", "loadtime")
# DOCUMENTED, NOT EXERCISED: the same catalogue record ("How to use" section) gives this
# authentication service: POST ...?grant_type=client_credentials with the body
# client_id={API Key}&client_secret={API Secret}, then "Authorization: Bearer {access_token}"
# on every data call; a token lives "for example 30 minutes". Note the token host is the legacy
# Dubai Pulse gateway while the data host is apis.data.dubai (the page's own examples are for
# another dataset), and nobody has run it with a real key: UNVERIFIED that this host still
# issues tokens for the data route. A wrong answer fails loudly at the token step. Redirects are
# never followed (see _request_token), so a moved endpoint is an error, not a leak.
DATA_DUBAI_TOKEN_URL = "https://api.dubaipulse.gov.ae/oauth/client_credential/accesstoken?grant_type=client_credentials"
# The documented grant type. It is also sent in the form body (harmless, and some gateways read
# it from there); the key and secret travel in the body only, never in a URL, so a log line that
# shows a URL cannot show them.
TOKEN_GRANT_TYPE = "client_credentials"

# UNVERIFIED: the largest page the API serves (the documentation only says the limit "must be
# below the allowed maximum" without a number). The loop advances by the number of rows actually
# received, and the register ends only at an EMPTY page, so a server that quietly serves fewer
# rows than asked, or states a total that is not the register size, cannot cut the register short.
DLD_PAGE_SIZE = 500
# The register is estimated at a few thousand rows (catalogue says 24865); 200 pages of 500
# is 100k rows. Reaching the cap means paging is broken, not that the register is huge.
DLD_MAX_PAGES = 200
# (connect, read) seconds. Data.Dubai calls are slow (the catalogue needed minutes).
DLD_TIMEOUT = (30, 180)
TOKEN_TIMEOUT = (30, 60)
# More than this share of rows unusable means the data is broken, not merely dirty.
MAX_DROPPED_SHARE = 0.5

# Column names, normalised with field_key (lower-case, letters and digits only), in
# priority order. One list covers snake_case (project_name), the upper-case portal
# names (PROJECT_EN) and the "Project Name" style, because they normalise alike.
NUMBER_ALIASES = ("projectnumber", "projectno", "projectnum", "projno")
NAME_EN_ALIASES = ("projectnameen", "projecten", "projectnameenglish")
NAME_AR_ALIASES = ("projectnamear", "projectar", "projectnamearabic")
# The legacy export's bare project_name is mostly Arabic, so it is routed by script.
NAME_GENERIC_ALIASES = ("projectname",)
DEVELOPER_ALIASES = ("developername", "developernameen", "developeren", "developer")
MASTER_DEVELOPER_ALIASES = ("masterdevelopername", "masterdevelopernameen", "masterdeveloperen", "masterdeveloper")
MASTER_PROJECT_ALIASES = ("masterprojecten", "masterprojectname", "masterproject")
AREA_EN_ALIASES = ("areanameen", "areaen", "areaname")
AREA_AR_ALIASES = ("areanamear", "areaar")
ZONE_ALIASES = ("zoneen", "zonenameen", "zone", "zoningauthorityen", "zoningauthority")
STATUS_ALIASES = ("projectstatus", "projectstatusen", "status")
PERCENT_ALIASES = ("percentcompleted", "percentcomplete", "completionpercentage", "completedpercentage")
START_DATE_ALIASES = ("projectstartdate", "startdate")
# Completion Date is the actual completion date when there is one, else the planned
# project end date: the registry model has one date column for both.
COMPLETION_DATE_ALIASES = ("completiondate", "projectenddate", "enddate")
CANCELLATION_DATE_ALIASES = ("cancellationdate",)
ESCROW_ALIASES = ("escrowagentname", "escrowagentnameen", "escrowagenten", "escrowagent")
UNITS_ALIASES = ("noofunits", "nounits", "totalunits", "units")
BUILDINGS_ALIASES = ("noofbuildings", "nobuildings", "totalbuildings", "buildings")
VILLAS_ALIASES = ("noofvillas", "novillas", "totalvillas", "villas")

ALL_NAME_ALIASES = NAME_EN_ALIASES + NAME_AR_ALIASES + NAME_GENERIC_ALIASES

R_NOT_OBJECT = "row is not an object"
R_NO_NAME = "no project name"
# Static text (it is logged and written to the run notes): the rule, never a cell value. The rule is
# models.identity_slug's: the developer and the project name each need a Latin letter, no letter or
# number from another script, and none of + # @.
R_NO_IDENTITY = (
    "no usable identity (no valid project number, and no developer and project name that each have "
    "a Latin letter, no letter or number from another script, and none of + # @)"
)
R_OVERSIZE = f"a text value is longer than {MAX_TEXT_LEN} characters"
R_INVALID = "row failed model validation"
R_DUPLICATE = "duplicate project number (first row kept)"
A_BAD_NUMBER = "project number is not 1 to 9 ASCII digits; blanked"
A_NAME_KEY = "no valid project number; keyed by developer and name"
UNCONFIRMED_END_NOTE = "the end of the register was not confirmed by an empty page"
FILE_NOTE = "read from the local download named by DATA_DUBAI_PROJECTS_FILE; the API was not called"
FILE_WINS_NOTE = "DATA_DUBAI_API_KEY and DATA_DUBAI_API_SECRET are set too, but the local download wins"


def check_schema(records: Sequence[Any]) -> None:
    """
    Refuse a payload whose rows we cannot map. The minimum is a project number
    column and a project name column somewhere in the rows; the error names the
    logical fields that are missing and the column names that were seen, so a
    renamed column is a one-line fix and no registry value reaches a log.
    """
    objects = [r for r in records if isinstance(r, Mapping)]
    if not objects:
        raise SourceError(
            f"{SOURCE_NAME}: the payload has {len(records)} row(s) but none is an object "
            f"(types seen: {describe_types(records)})"
        )
    raw_names: dict[str, None] = {}
    for record in objects:
        for key in record:
            raw_names.setdefault(str(key), None)
    present = {field_key(k) for k in raw_names}
    missing = []
    if not present & set(NUMBER_ALIASES):
        missing.append("project_number")
    if not present & set(ALL_NAME_ALIASES):
        missing.append("project_name")
    if missing:
        raise SourceError(
            f"{SOURCE_NAME}: the payload has {len(records)} row(s) but no recognised column for: "
            f"{', '.join(missing)}. Columns seen: {describe_keys(raw_names)}"
        )


def _row_from_record(
    rec: Mapping[str, Any], *, today_iso: str, source_dataset: str, source_refreshed: str, tally: RowTally
) -> Optional[RegistryRow]:
    """One mapped, validated row, or None with the reason counted in `tally`."""
    number = clean_registry_id(first_match(rec, NUMBER_ALIASES, as_cell))
    # models.DLD_ID_RE is ASCII-only since the review (it used \d, which also matches Arabic-Indic digits);
    # the isascii() test stays as a second guard: a key built from other digits never equals the ASCII one.
    if number is not None and not (number.isascii() and DLD_ID_RE.match(number)):
        tally.adjust(A_BAD_NUMBER)
        number = None

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
        "master_developer": first_match(rec, MASTER_DEVELOPER_ALIASES, as_text),
        "master_project": first_match(rec, MASTER_PROJECT_ALIASES, as_text),
        "area_en": first_match(rec, AREA_EN_ALIASES, as_text),
        "area_ar": first_match(rec, AREA_AR_ALIASES, as_text),
        "zone": first_match(rec, ZONE_ALIASES, as_text),
        "status": first_match(rec, STATUS_ALIASES, as_text),
        "escrow_agent": first_match(rec, ESCROW_ALIASES, as_text),
    }
    if any(value is not None and len(value) > MAX_TEXT_LEN for value in texts.values()):
        tally.drop(R_OVERSIZE)
        return None

    try:
        row = RegistryRow(
            emirate="Dubai",
            registry="DLD",
            registry_project_no=number,
            percent_completed=first_match(rec, PERCENT_ALIASES, as_cell),
            start_date=first_date(rec, START_DATE_ALIASES, tally),
            completion_date=first_date(rec, COMPLETION_DATE_ALIASES, tally),
            cancellation_date=first_date(rec, CANCELLATION_DATE_ALIASES, tally),
            units=first_match(rec, UNITS_ALIASES, as_cell),
            buildings=first_match(rec, BUILDINGS_ALIASES, as_cell),
            villas=first_match(rec, VILLAS_ALIASES, as_cell),
            source_dataset=source_dataset,
            source_refreshed=source_refreshed,
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
    if row.registry_key() is None:
        tally.adjust(A_NAME_KEY)
    return row


def parse_dld_records(
    records: Sequence[Any],
    *,
    today: date,
    source_dataset: str = DLD_PROJECTS_URL,
    source_refreshed: Optional[str] = None,
    tally: Optional[RowTally] = None,
) -> list[RegistryRow]:
    """
    Map raw API records to Dubai/DLD registry rows. A bad record is dropped and
    counted in `tally`, never guessed at; two records with the same project number
    keep the first. Row order follows the payload.
    """
    tally = tally if tally is not None else RowTally()
    today_iso = today.isoformat()
    rows: list[RegistryRow] = []
    seen: set[str] = set()
    for raw in records:
        if not isinstance(raw, Mapping):
            tally.drop(R_NOT_OBJECT)
            continue
        row = _row_from_record(
            normalise_record(raw),
            today_iso=today_iso,
            source_dataset=source_dataset,
            source_refreshed=source_refreshed or "",
            tally=tally,
        )
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


class DldProjectsAdapter:
    """Dubai: DLD Real Estate Projects via Data.Dubai (client-credentials token, limit/offset paging)."""

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
        base_url: str = DLD_PROJECTS_URL,
        token_url: str = DATA_DUBAI_TOKEN_URL,
        page_size: int = DLD_PAGE_SIZE,
        max_pages: int = DLD_MAX_PAGES,
    ) -> None:
        if page_size < 1 or max_pages < 1:
            raise ValueError("page_size and max_pages must be at least 1")
        self._cfg = cfg
        self._session = session
        self._today = today
        self._sleep = sleep
        self._retry = retry
        self._base_url = base_url
        self._token_url = token_url
        self._page_size = page_size
        self._max_pages = max_pages

    # -- switches ---------------------------------------------------------------
    def enabled(self, cfg: Config) -> bool:
        return cfg.has_data_dubai_credentials or bool(cfg.data_dubai_projects_file)

    def skip_reason(self, cfg: Config) -> str:
        if cfg.data_dubai_projects_file:
            return ""
        missing = [
            name
            for name, value in (
                ("DATA_DUBAI_API_KEY", cfg.data_dubai_api_key),
                ("DATA_DUBAI_API_SECRET", cfg.data_dubai_api_secret),
            )
            if not value
        ]
        return "" if not missing else "not configured: " + ", ".join(f"{n} is not set" for n in missing)

    # -- the fetch --------------------------------------------------------------
    def fetch(self) -> SourceResult:
        if not self.enabled(self._cfg):
            raise SourceError(f"{self.name}: {self.skip_reason(self._cfg)}")
        today = resolve_today(self._today)
        if self._cfg.data_dubai_projects_file:
            return self._fetch_from_file(today)
        with session_scope(self._session) as session:
            token = self._request_token(session)
            secrets = (self._cfg.data_dubai_api_key, self._cfg.data_dubai_api_secret, token)
            paging_notes: list[str] = []
            records, first_payload, pages = self._fetch_pages(session, token, secrets, paging_notes)

        tally = RowTally()
        refreshed = extract_refreshed(first_payload)
        rows = parse_dld_records(
            records, today=today, source_dataset=self._base_url, source_refreshed=refreshed, tally=tally
        )
        if not rows:
            raise SourceError(f"{self.name}: none of the {len(records)} row(s) could be used: {tally.dropped_summary()}")
        if tally.dropped * 2 > len(records):
            raise SourceError(
                f"{self.name}: {tally.dropped} of {len(records)} row(s) were unusable, "
                f"more than {int(MAX_DROPPED_SHARE * 100)} percent: {tally.dropped_summary()}"
            )
        notes = [f"fetched {len(records)} raw row(s) in {pages} page(s) of up to {self._page_size}"]
        # Right after the headline: main keeps only the first 280 characters of the notes in the run log, and an
        # unconfirmed end of the register must not be the part that is cut off. It is also a log warning.
        notes += paging_notes
        for paging_note in paging_notes:
            logger.warning("%s: %s", self.name, paging_note)
        notes += tally.notes()
        if pages > 1 and tally.dropped_count(R_DUPLICATE):
            notes.append("duplicate project numbers across pages: the API's paging order may not be stable")
        logger.info("%s: %d row(s) kept from %d raw row(s), %d page(s)", self.name, len(rows), len(records), pages)
        return SourceResult(
            source=self.name, rows=rows, fetched_at=utc_now_iso(), source_refreshed=refreshed, notes=notes
        )

    def _fetch_from_file(self, today: date) -> SourceResult:
        """The register from a downloaded copy of the dataset: same checks as the API path, no request."""
        label = f"{self.name} local download"
        try:
            with open(self._cfg.data_dubai_projects_file, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            # The path is a typed setting and never shown; the class says enough (FileNotFoundError, PermissionError).
            raise SourceError(f"{label}: the file cannot be read ({type(exc).__name__})") from None
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceError(f"{label}: the file is not UTF-8 JSON ({type(exc).__name__})") from None
        records = extract_records(payload)
        if records is None:
            shape = describe_keys(payload) if isinstance(payload, Mapping) else f"a {type(payload).__name__}"
            raise SourceError(f"{label}: the file has no list of rows (it contained: {shape})")
        if not records:
            raise SourceError(f"{label}: the file has no rows at all")
        check_schema(records)
        refreshed = extract_refreshed(payload)
        if refreshed is None:
            first = next((r for r in records if isinstance(r, Mapping)), None)
            refreshed = first_match(normalise_record(first), LOAD_STAMP_ALIASES, as_date) if first is not None else None

        tally = RowTally()
        rows = parse_dld_records(
            records, today=today, source_dataset=DLD_DOWNLOAD_PAGE, source_refreshed=refreshed, tally=tally
        )
        if not rows:
            raise SourceError(f"{self.name}: none of the {len(records)} row(s) could be used: {tally.dropped_summary()}")
        if tally.dropped * 2 > len(records):
            raise SourceError(
                f"{self.name}: {tally.dropped} of {len(records)} row(s) were unusable, "
                f"more than {int(MAX_DROPPED_SHARE * 100)} percent: {tally.dropped_summary()}"
            )
        notes = [f"{FILE_NOTE} ({len(records)} raw row(s))"]
        if self._cfg.has_data_dubai_credentials:
            notes.append(FILE_WINS_NOTE)
            logger.warning("%s: %s", self.name, FILE_WINS_NOTE)
        notes += tally.notes()
        logger.info("%s: %d row(s) kept from %d raw row(s) in the local download", self.name, len(rows), len(records))
        return SourceResult(
            source=self.name, rows=rows, fetched_at=utc_now_iso(), source_refreshed=refreshed, notes=notes
        )

    def _request_token(self, session: Any) -> str:
        key, secret = self._cfg.data_dubai_api_key, self._cfg.data_dubai_api_secret
        label = f"{self.name} token request"

        def send() -> Any:
            return session.post(
                self._token_url,
                data={"grant_type": TOKEN_GRANT_TYPE, "client_id": key, "client_secret": secret},
                headers={"Accept": "application/json", "User-Agent": user_agent(self._cfg)},
                timeout=TOKEN_TIMEOUT,
                # requests re-sends a POST body on a 307/308 to whatever host the answer names, and the
                # body holds the client secret: a redirect is an error here, never followed.
                allow_redirects=False,
            )

        response = send_with_retries(send, label=label, secrets=(key, secret), policy=self._retry, sleep=self._sleep)
        payload = parse_json_response(response, label=label)
        token = first_match(normalise_record(payload), ("accesstoken", "token"), as_text) if isinstance(payload, Mapping) else None
        if token is None:
            seen = describe_keys(payload) if isinstance(payload, Mapping) else f"a {type(payload).__name__}"
            raise SourceError(f"{label}: the answer has no access token (it contained: {seen})")
        return token

    def _fetch_pages(
        self, session: Any, token: str, secrets: tuple[str, ...], notes: list[str]
    ) -> tuple[list[Any], Any, int]:
        """
        Every row of the register. The register ends at an EMPTY page, never at a short page or a
        stated total: a server that serves fewer rows than asked, or whose "total" is only the
        size of one page, would otherwise hand back a partial register with no error (and the
        row-count guard has nothing to compare a first run with). A page that looks like the last
        one is therefore followed by one confirming request; if that request fails the rows
        already collected are kept and a note says the end was not confirmed. A stated total
        remains a check in the other direction: an empty page before it is reached is an error.
        """
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": user_agent(self._cfg),
        }
        collected: list[Any] = []
        first_payload: Any = None
        previous: Optional[list[Any]] = None
        total: Optional[int] = None
        trust_total = True
        confirming = ""  # why the last page looked final; set while the confirming request is due
        offset = 0
        for page_no in range(1, self._max_pages + 1):
            label = f"{self.name} page {page_no}"

            def send(at: int = offset) -> Any:
                return session.get(
                    self._base_url,
                    params={"limit": self._page_size, "offset": at},
                    headers=headers,
                    timeout=DLD_TIMEOUT,
                    allow_redirects=False,  # the Authorization header must never follow a redirect
                )

            logger.debug("%s: GET %s offset=%d", self.name, scrub_url(self._base_url), offset)
            try:
                response = send_with_retries(send, label=label, secrets=secrets, policy=self._retry, sleep=self._sleep)
                payload = parse_json_response(response, label=label)
            except SourceError:
                if not confirming:
                    raise
                notes.append(UNCONFIRMED_END_NOTE + " (the confirming request failed)")
                return collected, first_payload, page_no - 1
            records = extract_records(payload)
            if records is None:
                if confirming:
                    notes.append(UNCONFIRMED_END_NOTE + " (the confirming request returned no list of rows)")
                    return collected, first_payload, page_no - 1
                shape = describe_keys(payload) if isinstance(payload, Mapping) else f"a {type(payload).__name__}"
                raise SourceError(f"{label}: the answer has no list of rows (it contained: {shape})")
            if page_no == 1:
                first_payload = payload
                if not records:
                    raise SourceError(f"{label}: the answer has no rows at all")
                check_schema(records)
            stated = extract_total(payload)
            if trust_total and stated is not None:
                total = stated

            if records and previous is not None and records == previous:
                if confirming:
                    # Some servers answer an offset past the end with their last page again.
                    notes.append(UNCONFIRMED_END_NOTE + " (the server repeated its last page)")
                    return collected, first_payload, page_no - 1
                raise SourceError(f"{label}: the server returned the same page twice, so it is ignoring offset")
            if not records:
                if total is not None and len(collected) < total:
                    raise SourceError(f"{label}: the register stopped at {len(collected)} row(s) of the {total} it states")
                return collected, first_payload, page_no - 1
            if confirming:
                notes.append(
                    f"more rows followed a page that looked like the last ({confirming}); paging went on to an empty page"
                )
                if total is not None and len(collected) >= total:
                    trust_total = False  # the stated total was below the register: stop believing it
                    total = None
                confirming = ""
            collected.extend(records)
            previous = records
            offset += len(records)
            if total is not None and len(collected) >= total:
                confirming = f"the stated total of {total} was reached"
            elif total is None and len(records) < self._page_size:
                confirming = f"the page held fewer than the {self._page_size} rows asked for"
        if confirming:
            notes.append(UNCONFIRMED_END_NOTE + " (the page limit was reached first)")
            return collected, first_payload, self._max_pages
        raise SourceError(
            f"{self.name}: paging did not finish within {self._max_pages} page(s) of {self._page_size} "
            f"({len(collected)} row(s) so far); refusing to return a partial register"
        )
