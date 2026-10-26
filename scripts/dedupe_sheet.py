"""
Collapse duplicate rows already sitting in the Registry_Projects tab.

WHY: the daily run merges fresh rows with the sheet through models.dedupe_rows,
so a clean pipeline does not create duplicates. Rows can still pile up when an
analyst pastes rows in by hand, when a seed row and a registry row for one project
were written before the registry id was known, or after a bad manual edit. This
tool finds them with the pipeline's own rules (models.cluster_rows / merge_rows),
so the clean-up can never disagree with what the daily run would do.

It is deliberately conservative:

  * Dry run by default. Nothing is written without --apply.
  * A timestamped JSON backup of the rows is written under backups/ (git-ignored,
    because it holds live sheet data) BEFORE anything is written, and its path is
    printed. If the backup cannot be written, nothing is written.
  * It never merges rows of different emirates, or rows carrying different
    registry ids of one authority. cluster_rows already guarantees that; this
    module checks it again and, if the guarantee is ever broken, REFUSES to write
    anything at all (a broken guarantee means no cluster can be trusted).
  * It only touches Registry_Projects, and only through SheetManager.
  * It refuses --apply while the tab holds rows that could not be read: the
    rewrite would otherwise drop them silently.
  * Output is counts and project keys, never full rows, and never a spreadsheet
    id, URL or credential. Error text is scrubbed before it is printed, and only
    this tool's own errors (and the Sheets client's SheetError) show any: every
    other error is printed as its class and where it happened, never its text.

How the write works: the surviving row of each cluster takes the merged values at
the position of the cluster's first (earliest) row; the other members go. The
whole tab is then written back in one SheetManager.write_registry call and read
again to confirm the keys match. (SheetManager has no in-place row update, and
read_registry returns no sheet row numbers, so deleting by row number would mean
guessing them.) "Position N" in the report means the Nth data row as read; it is
the sheet row N + 1 only when the tab has no blank rows.

Usage:

    python scripts/dedupe_sheet.py                      # dry run: report only
    python scripts/dedupe_sheet.py --apply              # back up, merge, write, verify
    python scripts/dedupe_sheet.py --restore backups/Registry_Projects-<stamp>.json
                                                        # dry run of a restore
    python scripts/dedupe_sheet.py --restore <file> --apply

Exit codes: 0 done (or nothing to do, or a dry run), 1 something failed (setup,
read, backup, write, verification), 2 refused (bad arguments, a broken safety
check, unreadable rows, a removal that looks too large).

Do not run it while the daily workflow is running: the workflow rewrites the tab
and would put the duplicates back.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import traceback
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import reduce
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from pydantic import ValidationError  # noqa: E402

from src.config import TAB_REGISTRY, Config, load_config  # noqa: E402
from src.models import (  # noqa: E402
    REGISTRY_HEADERS,
    RegistryRow,
    cluster_rows,
    key_authority,
    merge_rows,
    utc_now_iso,
)

logger = logging.getLogger("dedupe_sheet")

BACKUP_DIR = ROOT_DIR / "backups"

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2

# Removing more than this share of the tab needs --allow-large-removal. A real
# clean-up can be big (a tab written twice is exactly half duplicates), but a
# fault in the clustering would look the same, so a human has to say so.
MAX_REMOVAL_FRACTION = 0.5

MAX_CLUSTERS_LISTED = 200
MAX_PROBLEM_LINES = 20
MAX_MESSAGE_CHARS = 300
NO_KEY = "(no key)"
HAND_COLUMNS_NOTE = (
    "Note: columns added by hand to this tab follow their project by Project Key, but the local backup "
    "holds only the standard columns. Make a copy of the spreadsheet first if you rely on them."
)


# ---------------------------------------------------------------------------
# Scrubbing: nothing secret may reach a log line, the console or an exception
# ---------------------------------------------------------------------------
_REDACTED = "[redacted]"
_SECRET_ATTRS = (
    "gemini_api_key",
    "gcp_service_account_key",
    "spreadsheet_id",
    "data_dubai_api_key",
    "data_dubai_api_secret",
    "locationiq_api_key",
    "geoapify_api_key",
    "google_maps_api_key",
)
_MIN_SECRET_CHARS = 4  # a shorter "secret" would redact ordinary words everywhere

# (pattern, replacement) pairs for secrets whose value we do not know in advance.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
        "[redacted private key]",
    ),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {_REDACTED}"),
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+"), rf"\1{_REDACTED}"),
    (
        re.compile(r"(?i)\b(api[_-]?key|key|token|secret|password|access_token|client_secret)=[^&\s'\"]+"),
        rf"\1={_REDACTED}",
    ),
    (
        re.compile(
            r"(?i)(\"(?:private_key|private_key_id|client_secret|client_email|client_id|api_key|access_token)\""
            r"\s*:\s*\")[^\"]+"
        ),
        rf"\1{_REDACTED}",
    ),
    (re.compile(r"(?i)\b(private_key_id)=[^&\s'\"]+"), rf"\1={_REDACTED}"),
    # A dict printed by Python uses single quotes, and header or log dumps use "name: value".
    (
        re.compile(
            r"(?i)('(?:private_key(?:_id)?|client_secret|x-goog-api-key|x-api-key|api[_-]?key|access_token"
            r"|refresh_token|id_token|token|password|secret)'\s*:\s*')[^']+"
        ),
        rf"\1{_REDACTED}",
    ),
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9_-])((?:x-goog-api-key|x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token"
            r"|id[_-]?token|client[_-]?secret|password|passwd|token|secret)\s*:\s*)(?!\[redacted\])[^\s\"',;<>}\]]+"
        ),
        rf"\1{_REDACTED}",
    ),
    # A spreadsheet id is an access handle, so it is never printed, whether it
    # arrives in a browser URL or an API URL.
    (re.compile(r"(?i)(/spreadsheets/(?:d/)?)[A-Za-z0-9_-]{20,}"), rf"\1{_REDACTED}"),
)


def secret_values(cfg: Config) -> list[str]:
    """
    Every configured secret value worth redacting, longest first. The service
    account JSON is also taken apart, because an error may quote one field of it.
    """
    values: list[str] = [str(getattr(cfg, attr, "") or "") for attr in _SECRET_ATTRS]
    values.extend(str(email) for email in getattr(cfg, "share_with_emails", ()) or ())
    raw_key = str(getattr(cfg, "gcp_service_account_key", "") or "")
    if raw_key:
        try:
            info = json.loads(raw_key)
        except ValueError:
            info = None
        if isinstance(info, dict):
            for field in ("private_key", "private_key_id", "client_email", "client_id"):
                if info.get(field):
                    values.append(str(info[field]))
    unique = {v for v in values if len(v) >= _MIN_SECRET_CHARS}
    return sorted(unique, key=len, reverse=True)


def scrub(text: object, secrets: Sequence[str] = ()) -> str:
    """
    `text` with known secret values and secret-shaped fragments removed, on one
    line and capped in length. Applied to every error before it is shown.
    """
    out = str(text)
    for secret in sorted((s for s in secrets if s and len(s) >= _MIN_SECRET_CHARS), key=len, reverse=True):
        out = out.replace(secret, _REDACTED)
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    out = " ".join(out.split())
    if len(out) > MAX_MESSAGE_CHARS:
        out = out[:MAX_MESSAGE_CHARS] + "..."
    return out


def _own_error_types() -> tuple[type, ...]:
    """
    The error types whose message is written to be shown (a count, a column or tab name, a
    fixed phrase; never a cell or a response body). SheetError belongs to the Sheets client,
    which this module must stay importable without (see open_sheet_manager), so it counts
    only once something has imported that module.
    """
    own: list[type] = [SetupError, BackupError]
    sheet_error = getattr(sys.modules.get("src.gsheet_manager"), "SheetError", None)
    if isinstance(sheet_error, type):
        own.append(sheet_error)
    return tuple(own)


def _error_location(exc: BaseException) -> str:
    """file:line in function of the innermost frame: where it happened, never any variable value."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "unknown"
    frame = frames[-1]
    return f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"


def describe_error(exc: BaseException, secrets: Sequence[str] = ()) -> str:
    """
    Safe to print: 'ClassName: scrubbed message' for one of this tool's own error types, and
    for any other class only 'ClassName (at file:line in function)', as src/main.py does. The
    text of an error nobody planned for can carry anything (a cell, a request URL, a response
    body) and scrubbing only removes what it can recognise, so it is not shown. Never a
    traceback: a traceback can carry local values.
    """
    name = type(exc).__name__
    if not isinstance(exc, _own_error_types()):
        return scrub(f"{name} (at {_error_location(exc)})", secrets)
    try:
        detail = str(exc)
    except Exception:  # an exception whose __str__ itself fails is still reportable
        detail = ""
    return scrub(f"{name}: {detail}" if detail else name, secrets)


# ---------------------------------------------------------------------------
# The plan (pure: no sheet, no files, no clock)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MergePlan:
    """One duplicate cluster that can be collapsed safely."""

    project_key: str  # primary key of the merged row
    keep_position: int  # 1-based data-row position of the survivor (the earliest member)
    remove_positions: tuple[int, ...]
    member_keys: tuple[str, ...]  # project key of each member, in position order
    gained: tuple[str, ...]  # sheet headers whose value differs from the survivor's own row
    merged: RegistryRow


@dataclass(frozen=True)
class Refusal:
    """Why a plan cannot be trusted. positions may be empty for whole-plan checks."""

    positions: tuple[int, ...]
    project_keys: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class DedupePlan:
    """
    What to do about duplicates. When `refusals` is non-empty the plan is blocked:
    `merges` and `result_rows` are empty and nothing may be written.
    """

    total_rows: int
    merges: tuple[MergePlan, ...]
    refusals: tuple[Refusal, ...]
    result_rows: tuple[RegistryRow, ...]  # the whole tab as it should be afterwards

    @property
    def blocked(self) -> bool:
        return bool(self.refusals)

    @property
    def rows_to_delete(self) -> tuple[int, ...]:
        """1-based data-row positions that disappear, ascending."""
        return tuple(sorted(p for merge in self.merges for p in merge.remove_positions))

    @property
    def rows_after(self) -> int:
        return self.total_rows if self.blocked else len(self.result_rows)

    @property
    def removal_fraction(self) -> float:
        return len(self.rows_to_delete) / self.total_rows if self.total_rows else 0.0


class _ClusterContractError(RuntimeError):
    """cluster_rows returned something that is not a partition of its input."""


def _positions_of(
    rows: Sequence[RegistryRow], clusters: Sequence[Sequence[RegistryRow]]
) -> list[tuple[int, ...]]:
    """
    1-based input positions of each cluster's members. Matching is by object
    identity, not equality, because two identical rows are still two rows; a queue
    per object handles the same instance appearing twice.
    """
    pending: dict[int, deque[int]] = defaultdict(deque)
    for position, row in enumerate(rows, start=1):
        pending[id(row)].append(position)
    out: list[tuple[int, ...]] = []
    for cluster in clusters:
        group: list[int] = []
        for member in cluster:
            queue = pending.get(id(member))
            if not queue:
                raise _ClusterContractError("cluster_rows returned a row that is not in its input")
            group.append(queue.popleft())
        out.append(tuple(group))
    if any(queue for queue in pending.values()):
        raise _ClusterContractError("cluster_rows dropped a row from its input")
    return out


def _cluster_violation(cluster: Sequence[RegistryRow]) -> str:
    """
    Why this cluster must not be merged, or "". cluster_rows promises these cannot
    happen; they are checked anyway because merging them destroys data.
    """
    emirates = sorted({row.emirate for row in cluster})
    if len(emirates) > 1:
        return f"rows from different emirates ({', '.join(emirates)})"
    by_authority: dict[str, set[str]] = defaultdict(set)
    for row in cluster:
        key = row.registry_key()
        if key:
            by_authority[key_authority(key)].add(key)
    for authority, keys in sorted(by_authority.items()):
        if len(keys) > 1:
            return f"different {authority.upper()} registry ids ({', '.join(sorted(keys))})"
    return ""


def _merge_violation(cluster: Sequence[RegistryRow], merged: RegistryRow) -> str:
    """Why the merged row is not a faithful merge of the cluster, or ""."""
    if merged.emirate != cluster[0].emirate:
        return "the merged row changed emirate"
    cluster_keys = {row.registry_key() for row in cluster if row.registry_key()}
    expected = next(iter(cluster_keys)) if len(cluster_keys) == 1 else None
    if merged.registry_key() != expected:
        return f"the merged row lost or changed the registry id (expected {expected or 'none'})"
    return ""


def _gained_headers(before: RegistryRow, after: RegistryRow) -> tuple[str, ...]:
    old, new = before.to_sheet_dict(), after.to_sheet_dict()
    return tuple(header for header in REGISTRY_HEADERS if old[header] != new[header])


def _member_key(row: RegistryRow) -> str:
    return row.project_key or NO_KEY


def plan_dedupe(rows: Sequence[RegistryRow]) -> DedupePlan:
    """
    Group `rows` into projects with models.cluster_rows and collapse each group
    with models.merge_rows. Pure: the input is not changed and nothing is read or
    written. Earlier rows are preferred for conflicting values and their position
    is kept, exactly as models.dedupe_rows does (the result equals
    dedupe_rows(rows) whenever the plan is not blocked).

    Raises TypeError for anything that is not a sequence of RegistryRow. Returns a
    blocked plan (see DedupePlan) instead of raising when a safety check fails.
    """
    if rows is None or isinstance(rows, (str, bytes)):
        raise TypeError("plan_dedupe needs a sequence of RegistryRow")
    rows = list(rows)
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, RegistryRow):
            raise TypeError(f"row {position} is a {type(row).__name__}, not a RegistryRow")
    if not rows:
        return DedupePlan(0, (), (), ())

    def blocked(refusals: list[Refusal]) -> DedupePlan:
        return DedupePlan(len(rows), (), tuple(refusals), ())

    clusters = cluster_rows(rows)
    try:
        positions = _positions_of(rows, clusters)
    except _ClusterContractError as exc:
        return blocked([Refusal((), (), str(exc))])

    merges: list[MergePlan] = []
    refusals: list[Refusal] = []
    result: list[RegistryRow] = []
    for cluster, where in zip(clusters, positions):
        if len(cluster) == 1:
            result.append(cluster[0])
            continue
        keys = tuple(_member_key(row) for row in cluster)
        problem = _cluster_violation(cluster)
        merged: Optional[RegistryRow] = None
        if not problem:
            try:
                merged = reduce(merge_rows, cluster)
            except ValueError as exc:  # merge_rows' own refusal
                problem = f"merge_rows refused: {scrub(exc)}"
        if merged is not None and not problem:
            problem = _merge_violation(cluster, merged)
        if problem or merged is None:
            refusals.append(Refusal(where, keys, problem or "the cluster could not be merged"))
            continue
        merges.append(
            MergePlan(
                project_key=merged.project_key,
                keep_position=where[0],
                remove_positions=where[1:],
                member_keys=keys,
                gained=_gained_headers(cluster[0], merged),
                merged=merged,
            )
        )
        result.append(merged)

    removed = sum(len(m.remove_positions) for m in merges)
    if len(result) != len(rows) - removed:
        refusals.append(Refusal((), (), "row counts do not add up (rows kept + rows removed != rows read)"))
    seen: dict[str, int] = {}
    for row in result:
        if row.project_key:
            seen[row.project_key] = seen.get(row.project_key, 0) + 1
    for key, count in sorted(seen.items()):
        if count > 1:
            refusals.append(Refusal((), (key,), f"{count} result rows would share the key {key}"))
    if refusals:
        return blocked(refusals)
    return DedupePlan(len(rows), tuple(merges), (), tuple(result))


# ---------------------------------------------------------------------------
# Report (counts and keys only)
# ---------------------------------------------------------------------------
def _numbers(positions: Sequence[int]) -> str:
    return ", ".join(str(p) for p in positions)


def format_plan(plan: DedupePlan) -> str:
    """A human-readable report: counts, then one line per cluster with project keys."""
    if plan.blocked:
        lines = [
            f"{TAB_REGISTRY}: {plan.total_rows} row(s) read. "
            f"REFUSED: {len(plan.refusals)} safety check(s) failed; nothing will be written."
        ]
        for refusal in plan.refusals:
            where = f"positions {_numbers(refusal.positions)}: " if refusal.positions else ""
            keys = f" [{', '.join(refusal.project_keys)}]" if refusal.project_keys else ""
            lines.append(f"  {where}{refusal.reason}{keys}")
        return "\n".join(lines)

    lines = [
        f"{TAB_REGISTRY}: {plan.total_rows} row(s) read, {len(plan.merges)} duplicate cluster(s), "
        f"{len(plan.rows_to_delete)} row(s) to remove, {plan.rows_after} row(s) afterwards."
    ]
    for merge in plan.merges[:MAX_CLUSTERS_LISTED]:
        gains = f"; survivor gains: {', '.join(merge.gained)}" if merge.gained else "; no field changes"
        lines.append(
            f"  [{merge.project_key or NO_KEY}] keep position {merge.keep_position}, "
            f"remove {_numbers(merge.remove_positions)}{gains}"
        )
    hidden = len(plan.merges) - MAX_CLUSTERS_LISTED
    if hidden > 0:
        lines.append(f"  ... and {hidden} more cluster(s)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
class BackupError(OSError, ValueError):
    """
    A backup that cannot be written or read back, or a file that is not a usable backup. Both
    bases on purpose: writing handles OSError ("the backup could not be made") and restoring
    handles ValueError ("not a backup of this tab"). The message is a fixed phrase that names
    a row number, a field or an error kind, never a cell value, so describe_error shows it.
    """


def write_backup(
    rows: Sequence[RegistryRow],
    *,
    backup_dir: Path | str = BACKUP_DIR,
    now: Optional[datetime] = None,
    tab: str = TAB_REGISTRY,
) -> Path:
    """
    Write the rows as JSON under `backup_dir` and return the path. The file name
    carries a UTC timestamp and never overwrites an existing file. The content is
    flushed to disk and read back before this returns, so a caller that gets a path
    can rely on it. Raises OSError (BackupError for a failed read-back) if any of
    that fails; a half-written file is removed.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    stamp = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "tab": tab,
        "captured_at": utc_now_iso(now),
        "headers": list(REGISTRY_HEADERS),
        "row_count": len(rows),
        "rows": [row.to_sheet_dict() for row in rows],
    }
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for attempt in range(100):
        path = directory / f"{tab}-{stamp}{'' if attempt == 0 else f'-{attempt}'}.json"
        try:
            handle = open(path, "x", encoding="utf-8")
        except FileExistsError:
            continue
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                saved = load_backup(path, tab=tab)
            except ValueError as exc:
                # Callers handle OSError ("the backup could not be made"); a bad
                # read-back is the same situation, so it must not escape as a traceback.
                raise BackupError(f"backup read-back failed: {exc}") from None
            if [r.project_key for r in saved] != [r.project_key for r in rows]:
                raise BackupError("backup read-back did not match what was written")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path
    raise BackupError("could not find a free backup file name")


def load_backup(path: Path | str, *, tab: str = TAB_REGISTRY) -> list[RegistryRow]:
    """
    Rows from a backup written by write_backup. Raises BackupError (a ValueError;
    never a traceback full of row values) for anything that is not a backup of
    `tab`, and OSError if the file cannot be read. The messages say what is wrong
    with the file and never repeat what it holds.
    """
    with open(path, encoding="utf-8") as handle:
        try:
            payload = json.load(handle)
        except ValueError as exc:
            raise BackupError(f"not valid JSON ({type(exc).__name__})") from None
    if not isinstance(payload, dict):
        raise BackupError("not a backup file (expected a JSON object)")
    if payload.get("tab") != tab:
        # What the file says its tab is stays out: it is the file's text, not ours.
        raise BackupError(f"this backup is for another tab, not {tab!r}")
    items = payload.get("rows")
    if not isinstance(items, list):
        raise BackupError("the backup has no 'rows' list")
    rows: list[RegistryRow] = []
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise BackupError(f"backup row {number} is not an object")
        try:
            rows.append(RegistryRow.from_sheet_dict(item))
        except ValidationError as exc:
            # Field names and error kinds only: pydantic's own messages quote the
            # offending cell value, and a restore must never print row contents.
            details = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'row'}: {e['type']}" for e in exc.errors())
            raise BackupError(f"backup row {number} is invalid ({details})") from None
        except ValueError as exc:
            raise BackupError(f"backup row {number} is invalid ({type(exc).__name__})") from None
    return rows


# ---------------------------------------------------------------------------
# Sheet access (SheetManager is imported lazily: this module and its tests must
# work before, and without, the Sheets client)
# ---------------------------------------------------------------------------
class SetupError(Exception):
    """The spreadsheet cannot be reached for a reason the owner can fix."""


def open_sheet_manager(cfg: Config) -> Any:
    """
    A SheetManager on the EXISTING spreadsheet named by SPREADSHEET_ID. Without
    that id SheetManager.open() would create a new spreadsheet, which a clean-up
    tool must never do as a side effect, so the id is required here.
    """
    if not cfg.spreadsheet_id:
        raise SetupError("SPREADSHEET_ID is not set; this tool edits an existing spreadsheet and never creates one")
    try:
        from src.gsheet_manager import SheetManager
    except ImportError as exc:
        raise SetupError(f"the Sheets client could not be loaded ({describe_error(exc)})") from None
    manager = SheetManager(cfg)
    # Read-only: a dry run says "nothing was changed", so opening must not add tabs or
    # columns, and --apply only needs the registry tab (write_registry completes its header).
    manager.open(read_only=True)
    return manager


def _say(text: str = "") -> None:
    """Write a report line to stdout; a console that cannot show a character gets '?'."""
    stream = sys.stdout
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        stream.write(text.encode(encoding, "replace").decode(encoding) + "\n")


def _keys_match(expected: Sequence[RegistryRow], actual: Sequence[RegistryRow]) -> bool:
    return sorted(r.project_key for r in expected) == sorted(r.project_key for r in actual)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"Collapse duplicate rows in the {TAB_REGISTRY} tab. Dry run unless --apply is given.",
    )
    parser.add_argument("--apply", action="store_true", help="write the changes (default: report only)")
    parser.add_argument(
        "--restore",
        metavar="BACKUP",
        help=f"replace {TAB_REGISTRY} with the rows of a backup file (dry run unless --apply)",
    )
    parser.add_argument(
        "--allow-large-removal",
        action="store_true",
        help=f"allow removing more than {int(MAX_REMOVAL_FRACTION * 100)} percent of the rows",
    )
    parser.add_argument("--backup-dir", help=f"where backups go (default: {BACKUP_DIR.name}/ in the repo)")
    return parser.parse_args(argv)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    config_loader: Callable[[], Config] = load_config,
    manager_factory: Callable[[Config], Any] = open_sheet_manager,
    backup_dir: Path | str | None = None,
    now: Optional[datetime] = None,
) -> int:
    """
    Run the tool and return the exit code. The three keyword arguments exist so
    tests can supply a fake configuration, a fake manager, a scratch backup
    directory and a fixed clock; the command line never needs them.
    """
    args = _parse_args(argv)
    cfg = config_loader()
    secrets = secret_values(cfg)
    target_dir = Path(args.backup_dir or backup_dir or BACKUP_DIR)

    def fail(message: str) -> int:
        logger.error("%s", scrub(message, secrets))
        return EXIT_FAILED

    def refuse(message: str) -> int:
        logger.error("REFUSED: %s", scrub(message, secrets))
        return EXIT_REFUSED

    restore_rows: Optional[list[RegistryRow]] = None
    if args.restore:
        try:
            restore_rows = load_backup(args.restore)
        except (OSError, ValueError) as exc:
            return refuse(f"cannot use the backup file: {describe_error(exc, secrets)}")

    try:
        manager = manager_factory(cfg)
    except Exception as exc:  # any failure here is a setup problem to report, not a crash
        return fail(f"could not open the spreadsheet: {describe_error(exc, secrets)}")
    try:
        rows, problems = manager.read_registry()
    except Exception as exc:
        return fail(f"could not read {TAB_REGISTRY}: {describe_error(exc, secrets)}")
    rows, problems = list(rows), [str(p) for p in (problems or [])]
    stray = next((n for n, row in enumerate(rows, start=1) if not isinstance(row, RegistryRow)), None)
    if stray is not None:
        # Backing up or planning over something that is not a row could save a
        # partial backup and then rewrite the tab from it; stop before either.
        return fail(f"{TAB_REGISTRY} row {stray} came back as something other than a registry row; nothing was changed")

    _say(f"{TAB_REGISTRY}: {len(rows)} row(s) read.")
    if problems:
        _say(
            f"{len(problems)} row(s) could not be read. They are not in the backup or the plan, "
            f"and --apply is refused until they are fixed or removed by hand:"
        )
        for line in problems[:MAX_PROBLEM_LINES]:
            _say(f"  {scrub(line, secrets)}")
        if len(problems) > MAX_PROBLEM_LINES:
            _say(f"  ... and {len(problems) - MAX_PROBLEM_LINES} more")

    backup_path: Optional[Path] = None
    if rows:
        try:
            backup_path = write_backup(rows, backup_dir=target_dir, now=now)
        except OSError as exc:
            return fail(f"the backup could not be written, so nothing was changed: {describe_error(exc, secrets)}")
        _say(f"Backup written: {backup_path}")

    if restore_rows is not None:
        return _run_restore(manager, restore_rows, rows, problems, args, backup_path, secrets)
    return _run_dedupe(manager, rows, problems, args, backup_path, secrets)


def _run_dedupe(
    manager: Any,
    rows: list[RegistryRow],
    problems: list[str],
    args: argparse.Namespace,
    backup_path: Optional[Path],
    secrets: Sequence[str],
) -> int:
    if not rows:
        _say(f"{TAB_REGISTRY} has no readable data rows. Nothing to do.")
        return EXIT_OK
    plan = plan_dedupe(rows)
    _say(format_plan(plan))
    if plan.blocked:
        logger.error("REFUSED: a safety check failed; nothing was written.")
        return EXIT_REFUSED
    if not plan.merges:
        _say("No duplicates found. Nothing to do.")
        return EXIT_OK
    _say(HAND_COLUMNS_NOTE)
    if not args.apply:
        _say("DRY RUN: nothing was changed. Re-run with --apply to write it.")
        return EXIT_OK
    if problems:
        logger.error("REFUSED: %d unreadable row(s) would be lost by the rewrite.", len(problems))
        return EXIT_REFUSED
    if plan.removal_fraction > MAX_REMOVAL_FRACTION and not args.allow_large_removal:
        logger.error(
            "REFUSED: this would remove %d of %d rows (more than %d percent). "
            "Check the report; if it is right, add --allow-large-removal.",
            len(plan.rows_to_delete),
            plan.total_rows,
            int(MAX_REMOVAL_FRACTION * 100),
        )
        return EXIT_REFUSED
    code = _write_and_verify(manager, list(plan.result_rows), backup_path, secrets, read=(rows, problems))
    if code == EXIT_OK:
        _say(
            f"Done: removed {len(plan.rows_to_delete)} duplicate row(s) in {len(plan.merges)} cluster(s); "
            f"{TAB_REGISTRY} now has {plan.rows_after} row(s)."
        )
    return code


def _run_restore(
    manager: Any,
    restore_rows: list[RegistryRow],
    rows: list[RegistryRow],
    problems: list[str],
    args: argparse.Namespace,
    backup_path: Optional[Path],
    secrets: Sequence[str],
) -> int:
    _say(
        f"Restore: {len(restore_rows)} row(s) from the backup would replace the "
        f"{len(rows)} row(s) now in {TAB_REGISTRY}."
    )
    if not restore_rows:
        logger.error("REFUSED: the backup holds no rows; restoring it would empty the tab.")
        return EXIT_REFUSED
    if not args.apply:
        _say("DRY RUN: nothing was changed. Re-run with --apply to restore it.")
        return EXIT_OK
    if problems:
        logger.error("REFUSED: %d unreadable row(s) would be lost by the rewrite.", len(problems))
        return EXIT_REFUSED
    code = _write_and_verify(manager, restore_rows, backup_path, secrets, read=(rows, problems))
    if code == EXIT_OK:
        _say(f"Done: {TAB_REGISTRY} now has {len(restore_rows)} row(s) from the backup.")
    return code


def _write_and_verify(
    manager: Any,
    new_rows: list[RegistryRow],
    backup_path: Optional[Path],
    secrets: Sequence[str],
    *,
    read: tuple[list[RegistryRow], list[str]],
) -> int:
    """
    Re-read the tab and write only if it still holds what `read` (rows, problems) saw,
    since write_registry replaces every data row and a person or a scrape run may have
    added one since (the backup alone can take seconds). Then write, read back, compare
    keys. Returns an exit code.
    """
    undo = f" The pre-write backup is {backup_path}." if backup_path else ""
    try:
        current, current_problems = manager.read_registry()
    except Exception as exc:
        logger.error(
            "%s",
            scrub(f"could not re-read {TAB_REGISTRY} just before the write, so nothing was written: "
                  f"{describe_error(exc, secrets)}.{undo}", secrets),
        )
        return EXIT_FAILED
    if list(current) != read[0] or [str(p) for p in (current_problems or [])] != read[1]:
        logger.error(
            "REFUSED: %s changed since it was read (someone edited it, or a scrape run wrote it), so nothing was "
            "written. Run again.%s", TAB_REGISTRY, scrub(undo, secrets),
        )
        return EXIT_REFUSED
    try:
        manager.write_registry(new_rows)
    except Exception as exc:
        logger.error(
            "%s",
            scrub(
                f"the write failed and the tab may be partly written: {describe_error(exc, secrets)}.{undo} "
                f"Run again without --apply to see the current state, or restore with --restore.",
                secrets,
            ),
        )
        return EXIT_FAILED
    try:
        after, after_problems = manager.read_registry()
    except Exception as exc:
        logger.error(
            "%s",
            scrub(f"written, but the read-back to verify failed: {describe_error(exc, secrets)}.{undo}", secrets),
        )
        return EXIT_FAILED
    if after_problems or len(after) != len(new_rows) or not _keys_match(new_rows, after):
        logger.error(
            "VERIFICATION FAILED: the tab does not hold what was written (%d row(s) expected, %d read back).%s",
            len(new_rows),
            len(after),
            scrub(undo, secrets),
        )
        return EXIT_FAILED
    _report_dropped_extras(manager, secrets)
    return EXIT_OK


def _report_dropped_extras(manager: Any, secrets: Sequence[str]) -> None:
    """
    SheetManager carries columns added by hand across a rewrite by Project Key and
    lists the rows whose values could not follow (`last_dropped_extras`). The local
    backup holds only the standard columns, so say so loudly rather than let a
    person find out later.
    """
    dropped = getattr(manager, "last_dropped_extras", None)
    if not isinstance(dropped, (list, tuple)) or not dropped:
        return
    shown = ", ".join(scrub(item, secrets) for item in dropped[:MAX_PROBLEM_LINES])
    more = f" (and {len(dropped) - MAX_PROBLEM_LINES} more)" if len(dropped) > MAX_PROBLEM_LINES else ""
    message = (
        f"WARNING: values in columns added by hand could not be carried over for {len(dropped)} row(s): "
        f"{shown}{more}. The local backup holds only the standard columns."
    )
    logger.warning("%s", message)
    _say(message)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    sys.exit(main())
