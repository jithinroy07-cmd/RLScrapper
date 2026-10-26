"""
The daily run: fetch the registries, merge, geocode, write the sheet (docs/DESIGN.md
sections 4 and 12).

    python -m src.main [--dry-run] [--no-geocode] [--snapshot PATH] [--env-file PATH] [-v]

Flags
    --dry-run       Read and compute everything, write nothing: no registry, candidates,
                    Map_Pins, Listing_Data seed, snapshot file or run-log row, and no
                    geocoding lookups (human overrides are still applied in memory).
                    The spreadsheet is still opened the normal way (the read-only open is
                    for the clean-up scripts: on a blank, freshly shared sheet it would
                    have no tabs to read), so a dry run may add a missing tab or header
                    column to an existing spreadsheet; because that open could also
                    CREATE a spreadsheet, a dry run requires SPREADSHEET_ID.
    --no-geocode    Behave as GEOCODE_ENABLED=false: no lookups, overrides still apply.
    --snapshot PATH Where to write the JSON snapshot (default: scraped_projects_latest.json
                    in the repository root, the path the workflow uploads).
    --env-file PATH A .env file to layer under the real environment.
    -v              Debug lines from this project's own loggers (third-party loggers stay quiet).

Exit codes (the CI job fails on anything but 0)
    0  clean run: every enabled source delivered, nothing blocked, every write succeeded.
       A geocode run that stopped on a quota or a budget is clean, not a failure.
    1  the run did what it could and something is wrong. Any of: an enabled adapter
       failed (error, zero rows from a source that always has some, a drop below 80
       percent of its last good run); a source named in REQUIRE_SOURCES was skipped or
       disabled; the geocode stage crashed, was misconfigured, or gave up for a reason other
       than a quota (a refused key, five failed lookups in a row); Registry_Projects or
       Geocode_Overrides holds rows that could not be read; the Listing_Data seed has
       bad rows or could not be applied; a persistence step failed; Registry_Projects was
       edited by someone while the run was in progress (the write is refused so that
       edit is not overwritten, and the next run starts from it).
    2  fatal setup failure before any work: bad command line or configuration, an unknown
       name in REQUIRE_SOURCES, the spreadsheet cannot be opened (bad credentials, not
       shared with the service account), or a dry run without SPREADSHEET_ID. There is
       nothing to write a run log to, so none is written.

Rules this module keeps (the reasons are in docs/DESIGN.md section 0)
    * One stage failing never skips the persistence of data another stage already holds:
      a failed source merges nothing but the others are written; a geocode crash still
      writes the pre-geocode merge (or whatever its last flush saved).
    * Registry_Projects is written back only from a complete read. A row that failed
      validation is excluded from what we read, so writing the rest back would delete it:
      while any such row exists, or the read failed, NO registry write happens (not even
      a geocode flush) and the run fails loudly until a human fixes the row. Every write
      also re-reads the tab first and is refused if it no longer matches what this run
      last read or wrote (RegistryWriter), because a write replaces every data row.
    * Logs hold counts, source names and row numbers. Never a secret, never row contents,
      never a project key or name (the log of a public repository is public); the keys a
      person needs go into the private Scrape_Run_Logs notes. An exception reaches the log
      (and every failure message, which is public too) as its class name and where it
      happened (file:line in function); only this project's own error types, whose text is
      written to be shown, add their message, scrubbed first. Any other class can carry a
      cell, a name or a response body in its text (a KeyError's text is the missing key), so
      its text stays out of the log; a failed source's scrubbed text goes to its private
      run-log note only. Nothing here logs repr(cfg) or a traceback.
    * Importing this module has no side effects: no logging configuration, no file or
      network access. Only main() configures logging, and sys.exit is called only under
      `if __name__ == "__main__"`.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import reduce
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from src.config import (
    EMIRATES,
    ROOT_DIR,
    SOURCE_NAMES,
    TAB_CANDIDATES,
    TAB_OVERRIDES,
    TAB_REGISTRY,
    Config,
    load_config,
)
from src.geocoder import GeocoderConfigError, abort_is_quota, geocode_pending
from src.gsheet_manager import RunLogEntry, SheetError, SheetManager, SourceRunInfo
from src.listing_data import SeedFileError, check_seed_rows, read_seed
from src.log_policy import (
    LOG_HANDLER_MARK,
    LOG_LINE_LIMIT,
    SecretFilter,  # noqa: F401 - re-exported: the tests and callers use src.main.SecretFilter
    configure_logging,
    error_location,
    install_excepthook,
    scrub_for_log,
    secret_values,
    set_secrets,
)
from src.models import (
    GeocodeOverride,
    LaunchCandidate,
    RegistryRow,
    cluster_rows,
    dedupe_rows,
    merge_rows,
    utc_now_iso,
)
from src.sources import build_adapters
from src.sources.base import SourceError, check_row_count

# Not __name__: under `python -m src.main` that is "__main__", outside the "src" logger
# tree that the public log lets through (src/log_policy.py).
logger = logging.getLogger("src.main")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_SETUP = 2

SNAPSHOT_FILENAME = "scraped_projects_latest.json"  # the path .github/workflows/daily_uae_real_estate.yml uploads
GIT_SHA_CHARS = 12
MAX_PROBLEM_LINES_LOGGED = 20
MAX_UNMATCHED_KEYS_LOGGED = 10
FAILURE_LIMIT = 400
NOTE_LIMIT = 280
# A cell can hold 50,000 characters; one runaway value must not make the whole registry
# write fail, so a fresh row with a longer text is dropped (and counted) instead.
MAX_FIELD_CHARS = 10_000

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_DISABLED = "disabled"
NOT_CONFIGURED = "not configured"  # how every adapter's skip_reason starts when settings are missing

_LOG_HANDLER_MARK = LOG_HANDLER_MARK  # tests and callers look the mark up here; src/log_policy.py owns it


# ---------------------------------------------------------------------------
# Secrets and error text (the secret list, the scrubber and the log policy are in src/log_policy.py)
# ---------------------------------------------------------------------------
class SetupError(Exception):
    """A fatal setup failure (exit 2). The message has already been scrubbed."""


class RegistryChanged(Exception):
    """Registry_Projects was edited by someone else after this run read it. Nothing was written."""


# This project's own error types. Their messages are written to be shown (a count, a field name, a source name,
# never a cell or a response body), so a public log line may carry them once scrubbed.
_EXPECTED_ERRORS = (SourceError, SheetError, GeocoderConfigError, SetupError, RegistryChanged, SeedFileError)


def describe_error(
    exc: BaseException, secrets: Iterable[str] = (), limit: int = LOG_LINE_LIMIT, *, private: bool = False
) -> str:
    """
    For a public log line or failure message: 'ClassName: scrubbed message' for one of this
    project's own error types, and for any other class only 'ClassName (at file:line in
    function)'. The text of an error nobody planned for can carry anything (a KeyError's text
    is the missing key, an SDK error repeats the request), and the log of a public repository
    is public, so it is not shown. `private=True` is for the private Scrape_Run_Logs notes: it
    also keeps the scrubbed text of such an error. Never a traceback: a traceback can carry
    local values.
    """
    own = isinstance(exc, _EXPECTED_ERRORS)
    text = scrub_for_log(exc, secrets, limit) if own or private else ""
    out = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    if not own:
        out += f" (at {error_location(exc)})"
    return out


class Report:
    """What went wrong in this run. Every message is scrubbed once, here, then logged at ERROR."""

    def __init__(self, secrets: Iterable[str] = ()):
        self.secrets = tuple(secrets)
        self.failures: list[str] = []

    def scrub(self, value: object, limit: int = LOG_LINE_LIMIT) -> str:
        return scrub_for_log(value, self.secrets, limit)

    def error_text(self, exc: BaseException, *, private: bool = False) -> str:
        return describe_error(exc, self.secrets, private=private)

    def fail(self, message: str, *args: object) -> None:
        text = self.scrub(message % args if args else message, FAILURE_LIMIT)
        self.failures.append(text)
        logger.error("%s", text)


def log_problem_lines(label: str, problems: Sequence[str], report: Report) -> None:
    """The first few problem lines, scrubbed. The manager already put only field names in them."""
    for line in list(problems)[:MAX_PROBLEM_LINES_LOGGED]:
        logger.warning("%s problem: %s", label, report.scrub(line))
    if len(problems) > MAX_PROBLEM_LINES_LOGGED:
        logger.warning("%s problem: ... and %d more", label, len(problems) - MAX_PROBLEM_LINES_LOGGED)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def git_sha(env: Optional[Mapping[str, str]] = None) -> str:
    """GITHUB_SHA (set by Actions), shortened; '' on a local run."""
    source = os.environ if env is None else env
    return (source.get("GITHUB_SHA") or "").strip()[:GIT_SHA_CHARS]


def default_snapshot_path() -> Path:
    return ROOT_DIR / SNAPSHOT_FILENAME


def log_start(cfg: Config, report: Report, *, dry_run: bool) -> None:
    sha = git_sha()
    logger.info("UAE real-estate scraper run starting (git %s%s)", sha or "unknown", ", DRY RUN: nothing will be written" if dry_run else "")
    for name, value in cfg.describe().items():
        logger.info("config %s: %s", name, report.scrub(value))
    for warning in cfg.warnings:
        logger.warning("config warning: %s", report.scrub(warning))


def unknown_required_sources(cfg: Config, adapters: Sequence[Any]) -> list[str]:
    """Names in REQUIRE_SOURCES that no adapter answers to: a typo there would silently disable the check."""
    known = {getattr(a, "name", "") for a in adapters}
    return [name for name in cfg.require_sources if name not in known]


def prepare_run(
    cfg: Config, adapters: Optional[Sequence[Any]], sheets: Any, *, today: date, dry_run: bool, report: Report
) -> tuple[list[Any], Any]:
    """
    Everything that must be right before any work: the adapters exist, REQUIRE_SOURCES
    names real ones (checked BEFORE the sheet is touched), and the spreadsheet opens.
    Raises SetupError (exit 2).
    """
    try:
        adapter_list = list(adapters) if adapters is not None else list(build_adapters(cfg, today=today))
    except Exception as exc:  # noqa: BLE001 - any failure here is a setup failure
        raise SetupError(f"could not build the source adapters ({report.error_text(exc)})") from None
    unknown = unknown_required_sources(cfg, adapter_list)
    if unknown:
        known = ", ".join(sorted(getattr(a, "name", "?") for a in adapter_list))
        # The unknown names are what somebody typed into a setting: counted, never printed.
        raise SetupError(f"REQUIRE_SOURCES names {len(unknown)} source(s) this scraper does not have. Known sources: {known}")
    if dry_run and not cfg.spreadsheet_id:
        raise SetupError(
            "a dry run needs SPREADSHEET_ID: without it the sheet layer may CREATE a spreadsheet by name, "
            "and a dry run must not create anything"
        )
    try:
        manager = sheets if sheets is not None else SheetManager(cfg)
        manager.open()
    except Exception as exc:  # noqa: BLE001 - SheetError, bad credentials, anything else
        raise SetupError(f"cannot open the spreadsheet ({report.error_text(exc)})") from None
    return adapter_list, manager


# ---------------------------------------------------------------------------
# Reading the sheet
# ---------------------------------------------------------------------------
@dataclass
class SheetState:
    """What the sheet held at the start of the run, and whether each part could be trusted."""

    registry: list[RegistryRow] = field(default_factory=list)
    registry_readable: bool = True
    registry_problems: list[str] = field(default_factory=list)
    overrides: dict[str, GeocodeOverride] = field(default_factory=dict)
    overrides_readable: bool = True
    override_problems: list[str] = field(default_factory=list)
    listing_records: dict[str, dict] = field(default_factory=dict)
    previous_counts: dict[str, int] = field(default_factory=dict)


def read_sheet_state(sheets: Any, report: Report) -> SheetState:
    """Read every input tab. Each read is separate: one broken tab fails the run but not the others."""
    state = SheetState()
    try:
        rows, problems = sheets.read_registry()
        state.registry, state.registry_problems = list(rows), list(problems)
    except Exception as exc:  # noqa: BLE001
        state.registry_readable = False
        report.fail("could not read %s (%s); it will not be written this run", TAB_REGISTRY, report.error_text(exc))
    if state.registry_problems:
        log_problem_lines(TAB_REGISTRY, state.registry_problems, report)
        report.fail(
            "%d %s row(s) are unusable and were left out of what was read; writing the registry back would "
            "delete them, so it is NOT written this run. Fix or remove those rows",
            len(state.registry_problems), TAB_REGISTRY,
        )
    try:
        overrides, problems = sheets.read_overrides()
        state.overrides, state.override_problems = dict(overrides), list(problems)
    except Exception as exc:  # noqa: BLE001
        state.overrides_readable = False
        report.fail("could not read %s (%s); no geocoding lookups this run", TAB_OVERRIDES, report.error_text(exc))
    if state.override_problems:
        log_problem_lines(TAB_OVERRIDES, state.override_problems, report)
        report.fail(
            "%d %s row(s) are unusable, so those pins are not applied. Fix or remove them",
            len(state.override_problems), TAB_OVERRIDES,
        )
    try:
        state.listing_records = dict(sheets.read_listing_records())
    except Exception as exc:  # noqa: BLE001
        report.fail("could not read Listing_Data (%s)", report.error_text(exc))
    not_known = 0
    try:
        read_counts = dict(sheets.last_source_counts())
        # Source names come from a cell a person can edit, and this log is public: only the names of the
        # adapters (config.SOURCE_NAMES) are kept, here as well as in the sheet reader, and the rest are counted.
        state.previous_counts = {name: count for name, count in read_counts.items() if name in SOURCE_NAMES}
        not_known = len(read_counts) - len(state.previous_counts)
    except Exception as exc:  # noqa: BLE001
        report.fail(
            "could not read the previous source counts (%s); the row-count guard is OFF for this run",
            report.error_text(exc),
        )
    logger.info(
        "sheet state: %d registry row(s), %d override(s), %d listing record(s), previous counts %s%s",
        len(state.registry), len(state.overrides), len(state.listing_records), state.previous_counts,
        f" (+{not_known} for names this code does not know, not shown)" if not_known else "",
    )
    return state


def apply_seed(sheets: Any, report: Report, *, dry_run: bool) -> int:
    """
    Add the Listing_Data seed rows the tab does not have yet. Bad seed rows are
    logged and counted and fail the run, but never stop it. Returns how many were added.
    """
    try:
        seed = read_seed()
    except Exception as exc:  # noqa: BLE001 - SeedFileError (no Project Key column), OSError, a CSV error
        report.fail("could not read the Listing_Data seed file (%s)", report.error_text(exc))
        return 0
    problems = check_seed_rows(seed)
    if problems:
        log_problem_lines("seed", problems, report)
        report.fail("%d bad Listing_Data seed row(s); they were not added", len(problems))
    if dry_run:
        logger.info("dry run: the Listing_Data seed (%d row(s) read) was not applied", len(seed))
        return 0
    try:
        added = sheets.apply_listing_seed(seed)
    except Exception as exc:  # noqa: BLE001
        report.fail("could not apply the Listing_Data seed (%s)", report.error_text(exc))
        return 0
    logger.info("Listing_Data seed: %d new row(s) added", added)
    return added


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
@dataclass
class SourceOutcome:
    """What one adapter did. Only an `ok` outcome contributes rows and candidates."""

    name: str
    info: SourceRunInfo
    rows: list[RegistryRow] = field(default_factory=list)
    candidates: list[LaunchCandidate] = field(default_factory=list)


def row_problem(row: RegistryRow) -> Optional[str]:
    """Why a fresh row must not be merged, or None. A fixed phrase, never text from the row."""
    if row.emirate not in EMIRATES:
        return "emirate outside Dubai and Sharjah"
    if not row.has_identity:
        return "no identity key"
    if any(len(cell) > MAX_FIELD_CHARS for cell in row.to_sheet_dict().values()):
        return f"a text longer than {MAX_FIELD_CHARS} characters"
    return None


def accept_rows(name: str, rows: Iterable[Any]) -> tuple[list[RegistryRow], Counter]:
    """
    The rows main will merge, and why the others were dropped (counted by reason).
    Anything that is not a RegistryRow means the adapter is broken: SourceError.
    """
    kept: list[RegistryRow] = []
    dropped: Counter = Counter()
    for item in rows:
        if not isinstance(item, RegistryRow):
            raise SourceError(f"{name}: returned a {type(item).__name__} where a RegistryRow was expected")
        reason = row_problem(item)
        if reason:
            dropped[reason] += 1
        else:
            kept.append(item)
    return kept, dropped


def accept_candidates(name: str, candidates: Iterable[Any]) -> list[LaunchCandidate]:
    out = list(candidates)
    for item in out:
        if not isinstance(item, LaunchCandidate):
            raise SourceError(f"{name}: returned a {type(item).__name__} where a LaunchCandidate was expected")
    return out


def _adapter_name(adapter: Any) -> str:
    name = getattr(adapter, "name", "")
    return name if isinstance(name, str) and name else type(adapter).__name__


def run_source(adapter: Any, cfg: Config, previous: Mapping[str, int], report: Report) -> SourceOutcome:
    """
    Run one adapter in isolation. Never raises. A source that is off is `skipped` (settings
    missing) or `disabled` (switched off); a source that errors, returns nothing it should
    have, or shrinks past the guard is `failed` and contributes nothing at all.
    """
    name = _adapter_name(adapter)
    try:
        if not adapter.enabled(cfg):
            reason = report.scrub(adapter.skip_reason(cfg) or "not enabled", 200)
            if reason.lower().startswith(NOT_CONFIGURED):
                logger.info("%s: SKIPPED (%s)", name, reason)
                return SourceOutcome(name, SourceRunInfo(STATUS_SKIPPED, 0, reason))
            logger.info("%s: DISABLED (%s)", name, reason)
            return SourceOutcome(name, SourceRunInfo(STATUS_DISABLED, 0, reason))
        result = adapter.fetch()
        rows, dropped = accept_rows(name, result.rows)
        candidates = accept_candidates(name, result.candidates)
        expects_rows = bool(getattr(adapter, "expects_rows", False))
        # The shrink guard is for a remote registry whose partial answer means a broken fetch. A source that
        # may legitimately be empty (the hand-kept seed file, news discovery) is edited on purpose, so pruning
        # it must not turn every later run red until someone flips ALLOW_ROW_DROP. Nothing is deleted either
        # way: a project that no feed mentions is kept.
        check_row_count(
            name, len(rows), previous.get(name) if expects_rows else None,
            expects_rows=expects_rows, allow_drop=cfg.allow_row_drop,
        )
        notes = [str(n) for n in list(result.notes)[:4]]
    except Exception as exc:  # noqa: BLE001 - a source must never take the run down; the class name says what it was
        report.fail("%s: FAILED, nothing from it was merged (%s)", name, report.error_text(exc))
        # The log of a public repository shows the class and where; the private run-log note keeps the scrubbed text.
        note = report.scrub(report.error_text(exc, private=True), NOTE_LIMIT)
        return SourceOutcome(name, SourceRunInfo(STATUS_FAILED, 0, note))

    if dropped:
        summary = "; ".join(f"{reason} (x{count})" for reason, count in sorted(dropped.items()))
        notes.append(f"main dropped {sum(dropped.values())} row(s): {summary}")
        logger.warning("%s: dropped %d row(s) before merging: %s", name, sum(dropped.values()), summary)
    if candidates:
        notes.append(f"{len(candidates)} candidate(s)")
    note = report.scrub("; ".join(notes), NOTE_LIMIT)
    logger.info(
        "%s: ok, %d row(s), %d candidate(s) (previous good run: %s)%s",
        name, len(rows), len(candidates), previous.get(name, "none"), f" [{note}]" if note else "",
    )
    return SourceOutcome(name, SourceRunInfo(STATUS_OK, len(rows), note), rows, candidates)


def collect_sources(
    adapters: Sequence[Any], cfg: Config, previous: Mapping[str, int], report: Report
) -> list[SourceOutcome]:
    """Every adapter, in order. One failure does not stop the others."""
    return [run_source(adapter, cfg, previous, report) for adapter in adapters]


def check_required_sources(cfg: Config, outcomes: Sequence[SourceOutcome], report: Report) -> None:
    """A source named in REQUIRE_SOURCES that did not run is a failure (a failed one already is)."""
    by_name = {o.name: o for o in outcomes}
    for name in cfg.require_sources:
        outcome = by_name.get(name)
        if outcome is not None and outcome.info.status in (STATUS_SKIPPED, STATUS_DISABLED):
            report.fail(
                "required source %s was %s (%s)", name, outcome.info.status.upper(), outcome.info.note or "no reason given"
            )


def no_source_data(outcomes: Sequence[SourceOutcome]) -> bool:
    """True when nothing was fetched and nothing failed: every source skipped, disabled or empty."""
    return all(o.info.status != STATUS_FAILED and not o.rows for o in outcomes)


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------
@dataclass
class MergeStats:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    kept_not_seen: int = 0  # in the sheet, in no feed this run: kept, never deleted

    def describe(self) -> str:
        return (
            f"{self.new} new, {self.updated} updated, {self.unchanged} unchanged, "
            f"{self.kept_not_seen} kept (in no feed this run)"
        )


def _comparable(row: RegistryRow) -> dict[str, str]:
    cells = row.to_sheet_dict()
    cells.pop("Last Seen", None)  # re-confirming a project is not a change
    return cells


def merge_feeds(fresh: Sequence[RegistryRow], existing: Sequence[RegistryRow]) -> tuple[list[RegistryRow], MergeStats]:
    """
    dedupe_rows(fresh + existing): fresh rows FIRST so their values win a conflict, and a
    registry row that no feed mentioned this run is kept (a registry hiccup never deletes
    a project). The stats classify each merged project. fresh and existing must be
    distinct objects.
    """
    combined = list(fresh) + list(existing)
    clusters = cluster_rows(combined)
    merged = dedupe_rows(combined)
    if len(clusters) != len(merged):
        raise RuntimeError("dedupe_rows and cluster_rows disagree about the number of projects")
    fresh_ids = {id(row) for row in fresh}
    stats = MergeStats()
    for cluster, row in zip(clusters, merged):
        old = [member for member in cluster if id(member) not in fresh_ids]
        if len(old) == len(cluster):
            stats.kept_not_seen += 1
        elif not old:
            stats.new += 1
        elif len(old) > 1 or _comparable(row) != _comparable(reduce(merge_rows, old)):
            stats.updated += 1
        else:
            stats.unchanged += 1
    return merged, stats


def unmatched_listing_keys(listing_records: Mapping[str, Any], rows: Iterable[RegistryRow]) -> list[str]:
    """Listing_Data keys that match no registry row (yet): informational, a seed can lead the registry."""
    known = {key for row in rows for key in row.identity_keys()}
    return sorted(key for key in listing_records if key not in known)


# ---------------------------------------------------------------------------
# Writing the registry, through one guard
# ---------------------------------------------------------------------------
def _fingerprint(rows: Iterable[RegistryRow]) -> list[str]:
    """
    The rows as the sheet would hand them back (a round trip through the cell strings), order
    ignored, so "what we read" and "what we wrote" compare equal exactly when nobody touched the tab.
    """
    return sorted(
        json.dumps(RegistryRow.from_sheet_dict(row.to_sheet_dict()).to_sheet_dict(), sort_keys=True) for row in rows
    )


class RegistryWriter:
    """
    The only way this module writes Registry_Projects. Used by the geocoder's flush AND the
    final write, so a refusal applies to both: with `blocked_reason` set (dry run, rows that
    could not be read, an unreadable tab, a failed merge) NOTHING is written.

    `expected` (the rows as read at the start) switches on a lost-update guard. A write replaces
    every data row of the tab, and a run can take most of an hour, so a row a person typed in the
    meantime would be deleted without a trace. Before each write the tab is read again; if it no
    longer holds exactly what this run last read or wrote, the write is refused with
    RegistryChanged (the run fails, the person's edit stays, the next run starts from it).
    """

    def __init__(self, sheets: Any, *, blocked_reason: str = "", expected: Optional[Sequence[RegistryRow]] = None):
        self._sheets = sheets
        self.blocked_reason = blocked_reason
        self.rows_written: Optional[int] = None
        self.writes = 0
        self._last: Optional[list[RegistryRow]] = None
        self._refusal_logged = False
        self._expected: Optional[list[str]] = _fingerprint(expected) if expected is not None else None

    def _check_unchanged(self) -> None:
        current, problems = self._sheets.read_registry()
        if problems or _fingerprint(current) != self._expected:
            raise RegistryChanged(
                f"{TAB_REGISTRY} changed while the run was in progress ({len(current)} readable row(s), "
                f"{len(problems)} unreadable, now); writing would overwrite someone's edit, so nothing was "
                "written. Run again"
            )

    def write(self, rows: Iterable[RegistryRow]) -> bool:
        """True when the sheet now holds exactly `rows`. A refusal is False; a failed write raises."""
        batch = list(rows)
        if self.blocked_reason:
            if not self._refusal_logged:
                logger.warning("%s not written: %s", TAB_REGISTRY, self.blocked_reason)
                self._refusal_logged = True
            return False
        if not batch:
            return False
        if self._last is not None and batch == self._last:
            return True  # the last flush already stored exactly this
        if self._expected is not None:
            self._check_unchanged()
        self._sheets.write_registry(batch)
        self._last, self.rows_written, self.writes = batch, len(batch), self.writes + 1
        if self._expected is not None:
            self._expected = _fingerprint(batch)
        return True


def registry_block_reason(state: SheetState, *, merge_failed: bool, dry_run: bool) -> str:
    """Why the registry must not be written this run, or ''."""
    if dry_run:
        return "dry run"
    if not state.registry_readable:
        return f"{TAB_REGISTRY} could not be read"
    if state.registry_problems:
        return f"{len(state.registry_problems)} row(s) could not be read and would be lost"
    if merge_failed:
        return "the merge failed"
    return ""


@dataclass
class Progress:
    """The rows as last handed to the registry writer: the pre-geocode merge, then each flush."""

    rows: list[RegistryRow]


class FlushFailed(Exception):
    """A geocode flush that could not be written. The geocoder logs only this class's name, never its message (the writer talks to the Sheets API, whose error text can echo what was written)."""


def make_flush(writer: RegistryWriter, progress: Progress, report: Report) -> Callable[[list[RegistryRow]], None]:
    def flush(rows: list[RegistryRow]) -> None:
        progress.rows = list(rows)  # remembered BEFORE the write, so a failed write still leaves the newest state
        try:
            writer.write(progress.rows)
        except Exception as exc:  # noqa: BLE001
            raise FlushFailed(report.error_text(exc)) from None

    return flush


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------
@dataclass
class GeocodeResult:
    rows: Optional[list[RegistryRow]]  # None: the stage crashed, use Progress.rows
    stats: dict[str, Any]


def _stats_dict(stats: Any) -> dict[str, Any]:
    if hasattr(stats, "as_log_dict"):
        raw = stats.as_log_dict()
    elif isinstance(stats, Mapping):
        raw = stats
    else:
        return {}
    return {str(k): v if isinstance(v, (int, float, str, bool)) or v is None else str(v) for k, v in dict(raw).items()}


def run_geocode(
    rows: Sequence[RegistryRow],
    overrides: Mapping[str, GeocodeOverride],
    cfg: Config,
    *,
    today: date,
    geocode: Callable[..., Any],
    flush: Callable[[list[RegistryRow]], None],
    lookups: bool,
    report: Report,
) -> GeocodeResult:
    """
    Run the geocode stage. Overrides are always applied. With `lookups` False (dry run, or
    the registry cannot be written so a result could not be kept) geocoding is switched
    off for this call, which leaves a pure in-memory overrides pass.

    A crash or a GeocoderConfigError is a failure, but never costs data: the caller falls
    back to the rows last handed to the writer. A quota abort or a budget stop is not.
    """
    run_cfg = cfg if lookups else dataclasses.replace(cfg, geocode_enabled=False)
    try:
        out, stats = geocode(list(rows), dict(overrides), run_cfg, today=today, flush=flush)
        out = list(out)
        if len(out) != len(rows) or not all(isinstance(r, RegistryRow) for r in out):
            raise RuntimeError(f"the geocode stage returned {len(out)} item(s) for {len(rows)} row(s)")
    except GeocoderConfigError as exc:
        report.fail("geocoding is misconfigured (%s); the rest of the run continues", report.error_text(exc))
        return GeocodeResult(None, {"error": "GeocoderConfigError"})
    except Exception as exc:  # noqa: BLE001 - a geocoder bug must not cost the merge
        report.fail("the geocode stage crashed (%s); the merge without its results is kept", report.error_text(exc))
        return GeocodeResult(None, {"error": type(exc).__name__})
    summary = _stats_dict(stats)
    aborted = report.scrub(getattr(stats, "aborted", "") or summary.get("aborted", ""), 100)
    stopped = report.scrub(getattr(stats, "stop_reason", "") or summary.get("stop_reason", ""), 100)
    if aborted and abort_is_quota(aborted):
        logger.warning("geocoding aborted (%s); the rows left over are retried on the next run. Not an error.", aborted)
    elif aborted:
        # A refused or revoked key, a plan that needs paying, or a backend that kept failing:
        # nothing will fix itself tomorrow, so the run must not stay green.
        report.fail("geocoding aborted (%s); the key may be refused or the backend is failing, so the rows left over stay untried", aborted)
    if stopped:
        logger.info("geocoding stopped early (%s); the rows left over are retried on the next run.", stopped)
    if summary.get("flush_failures"):
        logger.warning("%s geocode flush(es) failed; the final registry write decides whether the data is safe", summary["flush_failures"])
    if not lookups:
        summary["lookups_skipped"] = 1
    return GeocodeResult(out, summary)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def persist_registry(writer: RegistryWriter, rows: Sequence[RegistryRow], report: Report) -> bool:
    try:
        return writer.write(rows)
    except Exception as exc:  # noqa: BLE001
        report.fail("writing %s failed (%s)", TAB_REGISTRY, report.error_text(exc))
        return False


def persist_candidates(sheets: Any, candidates: Sequence[LaunchCandidate], report: Report) -> int:
    try:
        added = sheets.add_candidates(candidates)
    except Exception as exc:  # noqa: BLE001
        report.fail("writing %s failed (%s)", TAB_CANDIDATES, report.error_text(exc))
        return 0
    logger.info("%s: %d new candidate(s) of %d seen", TAB_CANDIDATES, added, len(candidates))
    return added


def persist_pins(sheets: Any, rows: Sequence[RegistryRow], report: Report) -> int:
    try:
        return int(sheets.write_map_pins(rows))
    except Exception as exc:  # noqa: BLE001
        report.fail("writing Map_Pins failed (%s)", report.error_text(exc))
        return 0


def build_snapshot(
    rows: Sequence[RegistryRow], outcomes: Sequence[SourceOutcome], *, generated_at: str, sha: str
) -> dict[str, Any]:
    """
    The JSON snapshot: when it was made, per-source status and row counts, and the merged
    registry as sheet dicts (every value a string). It is data for people to read; nothing
    here is interpreted, so text that looks like a formula is just text.
    """
    return {
        "generated_at": generated_at,
        "git_sha": sha,
        "sources": {o.name: {"status": o.info.status, "rows": o.info.rows} for o in outcomes},
        "registry_rows": len(rows),
        "rows": [row.to_sheet_dict() for row in rows],
    }


def write_snapshot(path: Path | str, payload: Mapping[str, Any]) -> None:
    """Write JSON atomically (temp file in the same folder, then replace) so a crash never leaves half a file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=False, allow_nan=False) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=target.parent, prefix=target.name + ".", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def persist_snapshot(path: Path | str, payload: Mapping[str, Any], report: Report) -> bool:
    try:
        write_snapshot(path, payload)
    except Exception as exc:  # noqa: BLE001
        report.fail("writing the snapshot failed (%s)", report.error_text(exc))
        return False
    logger.info("snapshot written (%d registry row(s))", payload.get("registry_rows", 0))
    return True


def build_run_log(
    *,
    started_at: str,
    exit_code: int,
    outcomes: Sequence[SourceOutcome],
    geocode_stats: Mapping[str, Any],
    registry_rows: int,
    map_pins: int,
    merge_stats: Optional[MergeStats],
    report: Report,
    unmatched_listing: Sequence[str] = (),
    dropped_extras: Sequence[str] = (),
) -> RunLogEntry:
    notes = []
    if merge_stats is not None:
        notes.append(f"merge: {merge_stats.describe()}")

    def keyed_note(keys: Sequence[str], what: str) -> None:
        # The keys go here, in the private sheet, and not in the log: a public Actions log must hold counts, not rows.
        shown = ", ".join(report.scrub(key, 80) for key in keys[:MAX_UNMATCHED_KEYS_LOGGED])
        more = f" and {len(keys) - MAX_UNMATCHED_KEYS_LOGGED} more" if len(keys) > MAX_UNMATCHED_KEYS_LOGGED else ""
        notes.append(f"{len(keys)} {what}: {shown}{more}")

    if unmatched_listing:
        keyed_note(
            unmatched_listing,
            "Listing_Data key(s) match no registry row (not in the registry yet, a typo, or the feed renamed the project)",
        )
    if dropped_extras:
        keyed_note(
            dropped_extras,
            "Registry_Projects row(s) had analyst-added column values that were not carried over because their Project Key "
            "is no longer in the registry",
        )
    notes.extend(report.failures)
    return RunLogEntry(
        started_at=started_at,
        finished_at=utc_now_iso(),
        exit_code=exit_code,
        sources={o.name: o.info for o in outcomes},
        geocode=dict(geocode_stats),
        registry_rows=registry_rows,
        map_pins=map_pins,
        git_sha=git_sha(),
        notes=report.scrub(" | ".join(notes), 1800),
    )


def append_run_log(sheets: Any, entry: RunLogEntry, report: Report) -> bool:
    """Best effort: a run log that cannot be written is logged, and the exit code is left as it was."""
    try:
        sheets.append_run_log(entry)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not append the run log (%s); the exit code is unchanged", report.error_text(exc))
        return False
    logger.info("run log appended (exit code %d)", entry.exit_code)
    return True


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def run(
    cfg: Config,
    *,
    adapters: Optional[Sequence[Any]] = None,
    sheets: Any = None,
    today: Optional[date] = None,
    geocode: Callable[..., Any] = geocode_pending,
    snapshot_path: Path | str | None = None,
    dry_run: bool = False,
) -> int:
    """
    One scrape run; returns the exit code (module docstring). A thin orchestrator: every
    stage is a function above that catches its own failures and records them in the Report.
    """
    report = Report(secret_values(cfg))
    started_at = utc_now_iso()
    today = today if today is not None else utc_today()
    log_start(cfg, report, dry_run=dry_run)

    try:
        adapter_list, sheets = prepare_run(cfg, adapters, sheets, today=today, dry_run=dry_run, report=report)
    except SetupError as exc:
        logger.error("setup failed, so nothing was done and there is no sheet to write a run log to: %s", exc)
        return EXIT_SETUP

    state = read_sheet_state(sheets, report)
    apply_seed(sheets, report, dry_run=dry_run)
    outcomes = collect_sources(adapter_list, cfg, state.previous_counts, report)
    check_required_sources(cfg, outcomes, report)

    fresh = [row for o in outcomes if o.info.status == STATUS_OK for row in o.rows]
    candidates = [c for o in outcomes if o.info.status == STATUS_OK for c in o.candidates]
    try:
        merged, merge_stats = merge_feeds(fresh, state.registry)
    except Exception as exc:  # noqa: BLE001
        report.fail("merging the sources into the registry failed (%s); the registry is not written", report.error_text(exc))
        merged, merge_stats = None, None
    unmatched: list[str] = []
    if merge_stats is not None:
        logger.info("registry merge: %s; %d project(s) in total", merge_stats.describe(), len(merged or []))
        unmatched = unmatched_listing_keys(state.listing_records, merged or [])
        if unmatched:
            # A name-keyed row stops matching when a feed renames the project or its developer; its price then
            # disappears from the map. The count is logged; the keys (project and developer names) are written to
            # the private Scrape_Run_Logs row only, because the Actions log of a public repository is public.
            logger.info("%d Listing_Data key(s) match no registry row yet (the keys are in the run log's Notes)", len(unmatched))
    if no_source_data(outcomes):
        logger.warning(
            "NO SOURCE PRODUCED ANY REGISTRY ROW this run (the registry holds %d row(s)): nothing will change "
            "until a source is enabled (DATA_DUBAI_API_KEY and DATA_DUBAI_API_SECRET for Dubai, ENABLE_SRERD=true "
            "for Sharjah)",
            len(merged if merged is not None else state.registry),
        )

    blocked = registry_block_reason(state, merge_failed=merged is None, dry_run=dry_run)
    writer = RegistryWriter(sheets, blocked_reason=blocked, expected=state.registry if state.registry_readable else None)
    progress = Progress(rows=list(merged if merged is not None else state.registry))
    geocode_stats: dict[str, Any] = {"skipped": "no merged rows"}
    final_rows = progress.rows
    if merged is not None:
        lookups = not dry_run and not blocked and state.overrides_readable
        if not lookups:
            logger.info("geocoding lookups are off for this run (overrides are still applied)")
        outcome = run_geocode(
            merged, state.overrides, cfg, today=today, geocode=geocode,
            flush=make_flush(writer, progress, report), lookups=lookups, report=report,
        )
        geocode_stats = outcome.stats
        final_rows = outcome.rows if outcome.rows is not None else progress.rows

    pins = 0
    if dry_run:
        logger.info(
            "dry run: would write %d registry row(s) and %d candidate(s); nothing was written", len(final_rows), len(candidates)
        )
    else:
        persist_registry(writer, final_rows, report)
        persist_candidates(sheets, candidates, report)
        if state.registry_readable:
            pins = persist_pins(sheets, final_rows, report)
        else:
            # Map_Pins is derived from the whole registry. Without a read, final_rows is only this run's
            # fresh rows, and replacing a full pin set with that would erase most of the map.
            logger.warning("Map_Pins not written: %s could not be read, so the pin set would be incomplete", TAB_REGISTRY)
        persist_snapshot(
            snapshot_path if snapshot_path is not None else default_snapshot_path(),
            build_snapshot(final_rows, outcomes, generated_at=utc_now_iso(), sha=git_sha()),
            report,
        )

    exit_code = EXIT_FAILED if report.failures else EXIT_OK
    if not dry_run:
        dropped = getattr(sheets, "last_dropped_extras", None)
        append_run_log(
            sheets,
            build_run_log(
                started_at=started_at, exit_code=exit_code, outcomes=outcomes, geocode_stats=geocode_stats,
                registry_rows=writer.rows_written if writer.rows_written is not None else len(state.registry),
                map_pins=pins, merge_stats=merge_stats, report=report, unmatched_listing=unmatched,
                dropped_extras=[str(key) for key in dropped] if isinstance(dropped, (list, tuple)) else [],
            ),
            report,
        )
    if report.failures:
        logger.error("RUN FAILED (exit %d) with %d problem(s):", exit_code, len(report.failures))
        for number, line in enumerate(report.failures, start=1):
            logger.error("  %d. %s", number, line)
    else:
        logger.info("run finished cleanly (exit 0)%s", " (dry run)" if dry_run else "")
    return exit_code


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
class _ExitRequested(Exception):
    """Raised instead of argparse calling sys.exit, so main() can return the code."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


class _Parser(argparse.ArgumentParser):
    def exit(self, status: int = 0, message: Optional[str] = None) -> Any:  # noqa: A003
        if message:
            sys.stderr.write(message)
        raise _ExitRequested(status)

    def error(self, message: str) -> Any:
        self.print_usage(sys.stderr)
        self.exit(EXIT_SETUP, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m src.main", description="Run the UAE real-estate scraper once.")
    parser.add_argument("--dry-run", action="store_true", help="read and compute everything, write nothing")
    parser.add_argument("--no-geocode", action="store_true", help="no geocoding lookups (human overrides still apply)")
    parser.add_argument("--snapshot", type=Path, default=None, help=f"snapshot file (default: {SNAPSHOT_FILENAME} in the repository root)")
    parser.add_argument("--env-file", type=Path, default=None, help="a .env file to layer under the environment")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug lines from this project's own loggers")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse, configure, build the real objects and run. Returns the exit code; never calls sys.exit."""
    # First of all, so that nothing below can print a traceback with chained message text (a cancelled run
    # raises KeyboardInterrupt, which the handlers below do not catch). Not at import time: see log_policy.
    install_excepthook()
    try:
        args = build_parser().parse_args(argv)
    except _ExitRequested as stop:
        return stop.code
    handler = configure_logging(args.verbose)
    try:
        if args.env_file is not None and not Path(args.env_file).is_file():
            logger.error("--env-file %s does not exist", args.env_file)
            return EXIT_SETUP
        cfg = load_config(env_file=args.env_file)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not load the configuration (%s)", describe_error(exc))
        return EXIT_SETUP
    secrets = secret_values(cfg)
    set_secrets(handler, secrets)
    if args.no_geocode:
        cfg = dataclasses.replace(cfg, geocode_enabled=False)
    try:
        today = utc_today()
        sheets = SheetManager(cfg)
        adapters = build_adapters(cfg, today=today)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not set up the run (%s)", describe_error(exc, secrets))
        return EXIT_SETUP
    try:
        return run(
            cfg, adapters=adapters, sheets=sheets, today=today, snapshot_path=args.snapshot, dry_run=args.dry_run
        )
    except Exception as exc:  # noqa: BLE001 - belt and braces: every stage already guards itself
        logger.error("the run stopped on an unexpected error (%s)", describe_error(exc, secrets))
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
