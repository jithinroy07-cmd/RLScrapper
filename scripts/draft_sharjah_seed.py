"""
Write a DRAFT of Sharjah seed rows from the SRERD "List of Projects" data call.

WHY: the list behind the Sharjah Real Estate Registration Department's public page
(src/sources/srerd_projects.py) answers project names and areas but no id and no
developer (seen on 14 November 2026: 104 rows, owner and every code column empty, proId
a sequence inside each developer's list), so it cannot feed the registry by itself. A
seed row needs the developer, because developer plus name is a SEED row's only identity
(docs/DESIGN.md sections 2 and 4). This tool does the typing: it fetches the list once
and writes a CSV in the seed layout with Emirate, Area (EN) and Source URL filled in,
the English name where the list has one, and two reference-only columns (the Arabic
name and the ownership type) beside them, so that a person fills in Developer and any
missing English name and then pastes the complete rows into src/data/seed_registry.csv.
The loader ignores the two extra columns, but it is tidier to drop them when pasting.

A local tool: it prints counts, never a row, and writes OUTSIDE the repository by
default (sharjah_seed_draft.csv in the home folder), because a CSV at the repository
root is a data file the hygiene tests refuse and the draft is not something to commit.

Usage:
    python -m scripts.draft_sharjah_seed             # writes ~/sharjah_seed_draft.csv
    python -m scripts.draft_sharjah_seed --out PATH  # somewhere else

Exit codes: 0 written; 1 the list could not be fetched or has no usable rows; 2 bad
arguments, or the output file could not be written.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from src.config import Config, load_config
from src.sources import srerd_projects as srerd
from src.sources.base import (
    DEFAULT_RETRY,
    SourceError,
    as_text,
    extract_records,
    first_match,
    is_arabic_only,
    normalise_record,
    parse_json_response,
    send_with_retries,
    session_scope,
    user_agent,
)

# The page a person reads to confirm a row; it is what the seed row cites as its Source URL.
SRERD_LIST_PAGE = "https://shjrerd.gov.ae/projects"
DEFAULT_OUT = Path.home() / "sharjah_seed_draft.csv"
# The seed layout (src/sources/seed_csv.py), then two columns the loader ignores.
DRAFT_HEADER = (
    "Emirate", "Project Name (EN)", "Developer", "Master Developer", "Master Project", "Area (EN)", "Zone", "Status",
    "Completion Date", "Source URL", "Arabic Name (reference only)", "Ownership (reference only)",
)
OWNERSHIP_ALIASES = ("usflagdesc", "ownershiptype", "ownership")


def fetch_list(cfg: Config, session: Any = None) -> list[Any]:
    """The raw list entries, by the same request the adapter makes. Raises SourceError."""
    with session_scope(session) as live:

        def send() -> Any:
            return live.post(
                srerd.SRERD_PROJECTS_URL,
                json=dict(srerd.SRERD_REQUEST_BODY),
                headers={"Accept": "application/json", "User-Agent": user_agent(cfg)},
                timeout=srerd.SRERD_TIMEOUT,
            )

        response = send_with_retries(send, label="srerd list", policy=DEFAULT_RETRY, sleep=lambda s: None)
        payload = parse_json_response(response, label="srerd list")
    records = extract_records(payload)
    if not records:
        raise SourceError("srerd list: the answer has no list of projects")
    return records


def _names(rec: Mapping[str, Any]) -> tuple[str, str]:
    """(English name, Arabic name), with a generic name routed by script as the adapter does."""
    name_en = first_match(rec, srerd.NAME_EN_ALIASES, as_text) or ""
    name_ar = first_match(rec, srerd.NAME_AR_ALIASES, as_text) or ""
    generic = first_match(rec, srerd.NAME_GENERIC_ALIASES, as_text)
    if generic:
        if is_arabic_only(generic):
            name_ar = name_ar or generic
        else:
            name_en = name_en or generic
    return name_en, name_ar


def _area(rec: Mapping[str, Any]) -> str:
    area = first_match(rec, srerd.AREA_EN_ALIASES, as_text)
    if not area:
        generic = first_match(rec, srerd.AREA_GENERIC_ALIASES, as_text)
        area = generic if generic and not is_arabic_only(generic) else ""
    # The list shouts (AL SAJAA INDUSTRIAL); the sheet does not.
    return area.title() if area.isupper() else area


def draft_rows(records: Sequence[Any]) -> tuple[list[list[str]], dict[str, int]]:
    """
    One draft row per distinct (English name, area), in list order. Rows with no name at all
    are skipped and counted; repeated (name, area) pairs are skipped and counted. A row whose
    English name is empty is kept (the person adds the name from the Arabic beside it).
    """
    counts = {"raw": len(records), "no_name": 0, "repeated": 0, "no_english_name": 0, "written": 0}
    seen: set[tuple[str, str]] = set()
    out: list[list[str]] = []
    for raw in records:
        if not isinstance(raw, Mapping):
            counts["no_name"] += 1
            continue
        rec = normalise_record(raw)
        name_en, name_ar = _names(rec)
        if not (name_en or name_ar):
            counts["no_name"] += 1
            continue
        area = _area(rec)
        key = ((name_en or name_ar).casefold(), area.casefold())
        if key in seen:
            counts["repeated"] += 1
            continue
        seen.add(key)
        if not name_en:
            counts["no_english_name"] += 1
        ownership = first_match(rec, OWNERSHIP_ALIASES, as_text) or ""
        out.append(["Sharjah", name_en, "", "", "", area, "", "", "", SRERD_LIST_PAGE, name_ar, ownership])
    counts["written"] = len(out)
    return out, counts


def write_draft(rows: Sequence[Sequence[str]], path: Path) -> None:
    """utf-8 with a byte-order mark, so a spreadsheet program opens the Arabic correctly."""
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(DRAFT_HEADER)
        writer.writerows(rows)


def main(argv: Optional[Sequence[str]] = None, *, session: Any = None, cfg: Optional[Config] = None) -> int:
    parser = argparse.ArgumentParser(description="Write a draft of Sharjah seed rows from the SRERD project list.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write the CSV (default: sharjah_seed_draft.csv in your home folder)")
    args = parser.parse_args(argv)
    cfg = cfg if cfg is not None else load_config()
    try:
        records = fetch_list(cfg, session=session)
    except SourceError as exc:
        print(f"could not read the SRERD list: {exc}", file=sys.stderr)
        return 1
    rows, counts = draft_rows(records)
    if not rows:
        print(f"the SRERD list answered {counts['raw']} row(s) but none had a project name; nothing written", file=sys.stderr)
        return 1
    try:
        write_draft(rows, args.out)
    except OSError as exc:
        print(f"could not write the draft ({type(exc).__name__})", file=sys.stderr)
        return 2
    print(
        f"fetched {counts['raw']} raw row(s); wrote {counts['written']} draft row(s) to {args.out} "
        f"({counts['no_english_name']} without an English name, {counts['repeated']} repeated name(s) skipped, "
        f"{counts['no_name']} without any name skipped). Fill in Developer, then paste the complete rows into "
        "src/data/seed_registry.csv."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
