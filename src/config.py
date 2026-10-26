"""
Configuration for the UAE real-estate scraper (docs/DESIGN.md sections 1, 3, 9).

Two rules shape this module:

  * No import-time side effects. Nothing here reads the environment or a .env
    file until load_config() is called, so importing src.config in a test or a
    script never picks up a developer's local secrets by accident.
  * No fabricated defaults. A setting nobody supplied is empty (or off), not a
    guess. The only non-empty defaults are operational knobs (budgets, cooldowns)
    and the two names the design itself fixes: the sheet name and the
    geocoder User-Agent.

Secret values are never printed or logged: Config hides them from repr(), and
Config.describe() reports only "set" / "not set" for them. The log of a public
repository is public, so describe() also never prints a free-text setting that
somebody typed (the contact, the sheet name, the User-Agent, a source name that
matched nothing): a boolean, a number or a fixed choice is printed because code
validated it, anything else only as "default" or "custom (not shown)". The
warnings name a setting and never the value it was given.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"

# --- Scope (DESIGN 1) --------------------------------------------------------
EMIRATES = ("Dubai", "Sharjah")
# Where a registry row came from. DLD means Dubai, SRERD means Sharjah, SEED is a
# hand-researched row whose emirate is stated, not implied.
REGISTRIES = ("DLD", "SRERD", "SEED")

# --- Sheet tabs (DESIGN 3) ---------------------------------------------------
TAB_REGISTRY = "Registry_Projects"
TAB_LISTINGS = "Listing_Data"
TAB_CANDIDATES = "Launch_Candidates"
TAB_PINS = "Map_Pins"
TAB_OVERRIDES = "Geocode_Overrides"
TAB_RUN_LOGS = "Scrape_Run_Logs"
ALL_TABS = (
    TAB_REGISTRY,
    TAB_LISTINGS,
    TAB_CANDIDATES,
    TAB_PINS,
    TAB_OVERRIDES,
    TAB_RUN_LOGS,
)

DEFAULT_SHEET_NAME = "UAE Real Estate Projects Tracker"
# Display only. Every stored timestamp is timezone-aware UTC (DESIGN 1).
DISPLAY_TZ = "Asia/Dubai"

DEFAULT_GEOCODE_USER_AGENT = "uae-real-estate-scraper/1.0"
# The geocoding time budget and its ceiling. The scrape job is stopped after 90 minutes
# (timeout-minutes in the workflow), so the ceiling keeps a typed value from outrunning it;
# tests/test_config.py reads the job timeout from the workflow and checks both against it.
DEFAULT_GEOCODE_TIME_BUDGET_SECONDS = 3300
MAX_GEOCODE_TIME_BUDGET_SECONDS = 4500
GEOCODE_BACKENDS = ("nominatim", "locationiq", "geoapify", "google")
SHARE_ROLES = ("reader", "commenter", "writer")
# The names REQUIRE_SOURCES can hold: each adapter's `name` (src/sources). Kept here so
# describe() can tell a real name from a typo without importing the adapters; a test
# keeps this list in step with build_adapters().
SOURCE_NAMES = ("dld_projects", "srerd_projects", "seed_csv", "gemini_news")
# The only GEMINI_MODEL text describe() prints: a model id shaped like gemini-2.5-flash.
_MODEL_NAME = re.compile(r"(?:models/)?gemini-[a-z0-9][a-z0-9.\-]{0,40}")

# Local geocode cache. Gitignored; the Google Sheet is the source of truth for
# coordinates, this only saves repeat lookups on one machine.
GEOCODE_CACHE_FILE = DATA_DIR / "geocode_cache.json"

_TRUE = frozenset({"1", "true", "yes", "y", "on"})
_FALSE = frozenset({"0", "false", "no", "n", "off"})


@dataclass(frozen=True)
class Config:
    """
    Every setting named in DESIGN section 9, parsed and typed.

    Secrets use repr=False so a stray print(cfg) or a traceback cannot leak them.
    `warnings` holds one line per setting that was present but unusable (a
    boolean that was not a boolean, a negative budget): the run proceeds on the
    default, and main.py is expected to log these.
    """

    # Secrets
    gemini_api_key: str = field(default="", repr=False)
    gcp_service_account_key: str = field(default="", repr=False)
    spreadsheet_id: str = field(default="", repr=False)
    data_dubai_api_key: str = field(default="", repr=False)
    data_dubai_api_secret: str = field(default="", repr=False)
    locationiq_api_key: str = field(default="", repr=False)
    geoapify_api_key: str = field(default="", repr=False)
    google_maps_api_key: str = field(default="", repr=False)
    share_with_emails: tuple[str, ...] = field(default=(), repr=False)

    # Local-only (a path, not a secret, but never meaningful in CI)
    gcp_service_account_file: str = "service_account.json"

    # Variables
    google_sheet_name: str = DEFAULT_SHEET_NAME
    gemini_model: str = ""  # no default: a model name nobody verified is worse than none
    enable_srerd: bool = False
    enable_gemini_news: bool = False
    require_sources: tuple[str, ...] = ()
    allow_row_drop: bool = False
    geocode_enabled: bool = True
    geocode_backend: str = ""  # "" = auto-pick a free backend; google is opt-in only
    geocode_max_per_run: int = 0  # 0 = the chosen backend's own ceiling
    geocode_user_agent: str = DEFAULT_GEOCODE_USER_AGENT
    geocode_contact: str = ""
    geocode_retry_cooldown_days: int = 30
    geocode_time_budget_seconds: int = DEFAULT_GEOCODE_TIME_BUDGET_SECONDS
    geocode_flush_every: int = 250
    share_role: str = "writer"
    share_notify: bool = False
    # Read by the workflow's deploy job, which is the only place that has it: the scrape step is
    # never given DEPLOY_MAP, so in CI this is always false. Parsed so a malformed local value
    # warns; describe() leaves it out because a "false" there would say nothing about the deploy.
    deploy_map: bool = False

    warnings: tuple[str, ...] = ()

    # -- convenience, so callers do not re-derive these inconsistently ----------
    @property
    def has_data_dubai_credentials(self) -> bool:
        """The Data.Dubai client-credentials flow needs both halves."""
        return bool(self.data_dubai_api_key and self.data_dubai_api_secret)

    @property
    def has_gcp_credentials_inline(self) -> bool:
        """True when the service-account JSON is supplied as an env value (CI)."""
        return bool(self.gcp_service_account_key)

    def describe(self) -> dict[str, str]:
        """
        A log-safe view for a public log. Secrets and the contact show only "set" /
        "not set". A boolean, a number or a fixed choice shows its value (code
        validated it). Free text somebody typed never shows: the sheet name and the
        User-Agent read "default" or "custom (not shown)", the model name shows only
        when it has the shape of a Gemini model id, and REQUIRE_SOURCES shows the
        source names this scraper has plus a count of any other name. Use this,
        never repr(), when printing the effective configuration.
        """

        def flag(value: object) -> str:
            return "set" if value else "not set"

        contact = self.geocode_contact
        default_agents = {DEFAULT_GEOCODE_USER_AGENT}
        if contact:
            default_agents.add(f"{DEFAULT_GEOCODE_USER_AGENT} ({contact})")
        required = [n for n in self.require_sources if n in SOURCE_NAMES]
        unrecognised = len(self.require_sources) - len(required)
        if unrecognised:
            required.append(f"{unrecognised} unrecognised name(s) (not shown)")
        if not self.gemini_model:
            model = "not set"
        else:
            model = self.gemini_model if _MODEL_NAME.fullmatch(self.gemini_model) else "custom (not shown)"

        return {
            "GEMINI_API_KEY": flag(self.gemini_api_key),
            "GCP_SERVICE_ACCOUNT_KEY": flag(self.gcp_service_account_key),
            "SPREADSHEET_ID": flag(self.spreadsheet_id),
            "DATA_DUBAI_API_KEY": flag(self.data_dubai_api_key),
            "DATA_DUBAI_API_SECRET": flag(self.data_dubai_api_secret),
            "LOCATIONIQ_API_KEY": flag(self.locationiq_api_key),
            "GEOAPIFY_API_KEY": flag(self.geoapify_api_key),
            "GOOGLE_MAPS_API_KEY": flag(self.google_maps_api_key),
            "SHARE_WITH_EMAILS": f"{len(self.share_with_emails)} address(es)",
            "GOOGLE_SHEET_NAME": "default" if self.google_sheet_name == DEFAULT_SHEET_NAME else "custom (not shown)",
            "GEMINI_MODEL": model,
            "ENABLE_SRERD": str(self.enable_srerd).lower(),
            "ENABLE_GEMINI_NEWS": str(self.enable_gemini_news).lower(),
            "REQUIRE_SOURCES": ", ".join(required) or "none",
            "ALLOW_ROW_DROP": str(self.allow_row_drop).lower(),
            "GEOCODE_ENABLED": str(self.geocode_enabled).lower(),
            "GEOCODE_BACKEND": self.geocode_backend or "auto",
            "GEOCODE_MAX_PER_RUN": str(self.geocode_max_per_run),
            "GEOCODE_USER_AGENT": "default" if self.geocode_user_agent in default_agents else "custom (not shown)",
            "GEOCODE_CONTACT": flag(self.geocode_contact),
            "GEOCODE_RETRY_COOLDOWN_DAYS": str(self.geocode_retry_cooldown_days),
            "GEOCODE_TIME_BUDGET_SECONDS": str(self.geocode_time_budget_seconds),
            "GEOCODE_FLUSH_EVERY": str(self.geocode_flush_every),
            "SHARE_ROLE": self.share_role,
            "SHARE_NOTIFY": str(self.share_notify).lower(),
        }


def _text(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _parse_bool(env: Mapping[str, str], name: str, default: bool, warnings: list[str]) -> bool:
    """
    Unset or empty means the default. GitHub Actions renders an unset repository
    variable as an empty string, so empty must never be an error.
    """
    raw = _text(env, name).lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    warnings.append(f"{name} is not a boolean; using {str(default).lower()}")
    return default


def _parse_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    warnings: list[str],
    minimum: int = 0,
    maximum: Optional[int] = None,
) -> int:
    raw = _text(env, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        warnings.append(f"{name} is not an integer; using {default}")
        return default
    if value < minimum:
        warnings.append(f"{name} is below the minimum {minimum}; using {default}")
        return default
    if maximum is not None and value > maximum:
        # Clamped, not reset: a too-large budget still means "as long as allowed".
        warnings.append(f"{name} is above the maximum {maximum}; using {maximum}")
        return maximum
    return value


def _parse_list(raw: str, lower: bool = False) -> tuple[str, ...]:
    """Comma / semicolon / whitespace separated, de-duplicated, order preserved."""
    seen: dict[str, None] = {}
    for part in re.split(r"[,;\s]+", raw or ""):
        part = part.strip()
        if not part:
            continue
        seen.setdefault(part.lower() if lower else part, None)
    return tuple(seen)


def _parse_choice(
    env: Mapping[str, str],
    name: str,
    allowed: tuple[str, ...],
    default: str,
    warnings: list[str],
) -> str:
    raw = _text(env, name).lower()
    if not raw:
        return default
    if raw in allowed:
        return raw
    warnings.append(f"{name} is not one of {', '.join(allowed)}; using {default or 'auto'}")
    return default


def _merged_environment(env_file: Optional[Path | str]) -> dict[str, str]:
    """
    The process environment layered over a .env file. The real environment wins
    (standard dotenv behaviour) and an empty exported value counts as unset, so
    an empty CI variable cannot shadow a local .env. The file is parsed with
    dotenv_values, so os.environ is never mutated.
    """
    merged = dict(os.environ)
    path = Path(env_file) if env_file is not None else ROOT_DIR / ".env"
    if path.is_file():
        from dotenv import dotenv_values  # imported here: only load_config touches .env

        for key, value in dotenv_values(path).items():
            if value is not None and not (merged.get(key) or "").strip():
                merged[key] = value
    return merged


def load_config(
    env: Optional[Mapping[str, str]] = None, *, env_file: Optional[Path | str] = None
) -> Config:
    """
    Build a Config.

    env=None (production / local runs): read os.environ, layered over ROOT/.env
    when that file exists. env=<mapping> (tests): use exactly that mapping and
    never touch the environment or any file, so a test is deterministic on any
    machine.
    """
    source: Mapping[str, str] = _merged_environment(env_file) if env is None else env
    warnings: list[str] = []

    contact = _text(source, "GEOCODE_CONTACT")
    user_agent = _text(source, "GEOCODE_USER_AGENT") or DEFAULT_GEOCODE_USER_AGENT
    if contact and contact not in user_agent:
        # Nominatim's usage policy asks for a way to reach the operator.
        user_agent = f"{user_agent} ({contact})"

    return Config(
        gemini_api_key=_text(source, "GEMINI_API_KEY"),
        gcp_service_account_key=_text(source, "GCP_SERVICE_ACCOUNT_KEY"),
        spreadsheet_id=_text(source, "SPREADSHEET_ID"),
        data_dubai_api_key=_text(source, "DATA_DUBAI_API_KEY"),
        data_dubai_api_secret=_text(source, "DATA_DUBAI_API_SECRET"),
        locationiq_api_key=_text(source, "LOCATIONIQ_API_KEY"),
        geoapify_api_key=_text(source, "GEOAPIFY_API_KEY"),
        google_maps_api_key=_text(source, "GOOGLE_MAPS_API_KEY"),
        share_with_emails=_parse_list(_text(source, "SHARE_WITH_EMAILS")),
        gcp_service_account_file=_text(source, "GCP_SERVICE_ACCOUNT_FILE") or "service_account.json",
        google_sheet_name=_text(source, "GOOGLE_SHEET_NAME") or DEFAULT_SHEET_NAME,
        gemini_model=_text(source, "GEMINI_MODEL"),
        enable_srerd=_parse_bool(source, "ENABLE_SRERD", False, warnings),
        enable_gemini_news=_parse_bool(source, "ENABLE_GEMINI_NEWS", False, warnings),
        require_sources=_parse_list(_text(source, "REQUIRE_SOURCES"), lower=True),
        allow_row_drop=_parse_bool(source, "ALLOW_ROW_DROP", False, warnings),
        geocode_enabled=_parse_bool(source, "GEOCODE_ENABLED", True, warnings),
        geocode_backend=_parse_choice(source, "GEOCODE_BACKEND", GEOCODE_BACKENDS, "", warnings),
        geocode_max_per_run=_parse_int(source, "GEOCODE_MAX_PER_RUN", 0, warnings),
        geocode_user_agent=user_agent,
        geocode_contact=contact,
        geocode_retry_cooldown_days=_parse_int(
            source, "GEOCODE_RETRY_COOLDOWN_DAYS", 30, warnings
        ),
        geocode_time_budget_seconds=_parse_int(
            source,
            "GEOCODE_TIME_BUDGET_SECONDS",
            DEFAULT_GEOCODE_TIME_BUDGET_SECONDS,
            warnings,
            minimum=1,
            maximum=MAX_GEOCODE_TIME_BUDGET_SECONDS,
        ),
        geocode_flush_every=_parse_int(source, "GEOCODE_FLUSH_EVERY", 250, warnings, minimum=1),
        share_role=_parse_choice(source, "SHARE_ROLE", SHARE_ROLES, "writer", warnings),
        share_notify=_parse_bool(source, "SHARE_NOTIFY", False, warnings),
        deploy_map=_parse_bool(source, "DEPLOY_MAP", False, warnings),
        warnings=tuple(warnings),
    )
