"""
Source adapters (docs/DESIGN.md sections 4 and 12).

Every adapter returns rows it can stand behind or raises SourceError. build_adapters
returns all four, always in the same order, enabled or not: deciding what runs, what
is skipped and what a skip means (REQUIRE_SOURCES) belongs to src/main.py, which asks
each adapter enabled() and skip_reason().
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from src.config import Config
from src.sources.base import SourceAdapter, SourceError, SourceResult, check_row_count
from src.sources.dld_projects import DldProjectsAdapter
from src.sources.gemini_news import GeminiNewsAdapter
from src.sources.seed_csv import SeedCsvAdapter
from src.sources.srerd_projects import SrerdProjectsAdapter

__all__ = [
    "DldProjectsAdapter",
    "GeminiNewsAdapter",
    "SeedCsvAdapter",
    "SourceAdapter",
    "SourceError",
    "SourceResult",
    "SrerdProjectsAdapter",
    "build_adapters",
    "check_row_count",
]


def build_adapters(cfg: Config, *, session: Any = None, today: Optional[date] = None) -> list[SourceAdapter]:
    """
    The four adapters in the fixed order dld_projects, srerd_projects, seed_csv,
    gemini_news. `session` is the requests.Session-like object the two HTTP registry
    adapters use (tests pass a fake; None means each makes its own). gemini_news
    talks through the Gemini SDK and does not use it. `today` is the UTC date rows
    are stamped with.
    """
    return [
        DldProjectsAdapter(cfg, session=session, today=today),
        SrerdProjectsAdapter(cfg, session=session, today=today),
        SeedCsvAdapter(today=today),
        GeminiNewsAdapter(cfg, today=today),
    ]
