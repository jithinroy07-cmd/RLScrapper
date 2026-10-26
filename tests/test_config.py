"""
What the configuration layer may put in a PUBLIC log (docs/DESIGN.md section 8).

The log of a public repository is public. Config.describe() feeds the 'config NAME: value'
startup lines and Config.warnings feeds the 'config warning:' lines, so neither may carry
a value somebody typed: not the contact, not the sheet name, not the User-Agent, not a
source name that matched nothing, not the text of a setting that failed to parse.

Every value below is SYNTHETIC. The marker is a made-up token, so any appearance of it in
an output is a leak of a typed value.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
import yaml

import src.main as app
from src.config import (
    DEFAULT_GEOCODE_TIME_BUDGET_SECONDS,
    DEFAULT_GEOCODE_USER_AGENT,
    DEFAULT_SHEET_NAME,
    GEOCODE_BACKENDS,
    MAX_GEOCODE_TIME_BUDGET_SECONDS,
    SHARE_ROLES,
    SOURCE_NAMES,
    Config,
    load_config,
)
from src.sources import build_adapters

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "daily_uae_real_estate.yml"

MARK = "zq7mark"
CONTACT_MARK = f"{MARK}-contact@example.invalid"
SHARE_MARK = f"{MARK}-share@example.invalid"
FAKE_SECRET = f"{MARK}-fake-secret-0000"


def hostile_env() -> dict[str, str]:
    """Every free-text setting and every unusable value carries the marker."""
    return {
        "GEMINI_API_KEY": FAKE_SECRET,
        "GCP_SERVICE_ACCOUNT_KEY": FAKE_SECRET,
        "SPREADSHEET_ID": FAKE_SECRET,
        "DATA_DUBAI_API_KEY": FAKE_SECRET,
        "DATA_DUBAI_API_SECRET": FAKE_SECRET,
        "LOCATIONIQ_API_KEY": FAKE_SECRET,
        "GEOAPIFY_API_KEY": FAKE_SECRET,
        "GOOGLE_MAPS_API_KEY": FAKE_SECRET,
        "SHARE_WITH_EMAILS": f"{SHARE_MARK}, {SHARE_MARK.upper()}",
        "GOOGLE_SHEET_NAME": f"{MARK} Sheet Name",
        "GEMINI_MODEL": f"{MARK}-model",
        "REQUIRE_SOURCES": f"{MARK}source, dld_projects, {MARK.upper()}other",
        "GEOCODE_USER_AGENT": f"{MARK}-agent/9 ({MARK}-agent@example.invalid)",
        "GEOCODE_CONTACT": CONTACT_MARK,
        # unusable values: each falls back to its default and must only be named
        "ENABLE_SRERD": f"{MARK}-bool",
        "ENABLE_GEMINI_NEWS": f"{MARK}-bool",
        "ALLOW_ROW_DROP": f"{MARK}-bool",
        "GEOCODE_ENABLED": f"{MARK}-bool",
        "SHARE_NOTIFY": f"{MARK}-bool",
        "DEPLOY_MAP": f"{MARK}-bool",
        "GEOCODE_BACKEND": f"{MARK}-backend",
        "SHARE_ROLE": f"{MARK}-role",
        "GEOCODE_MAX_PER_RUN": f"{MARK}-int",
        "GEOCODE_RETRY_COOLDOWN_DAYS": f"{MARK}-int",
        "GEOCODE_TIME_BUDGET_SECONDS": "-4242424",
        "GEOCODE_FLUSH_EVERY": "-5353535",
    }


def everything_printed(cfg: Config) -> str:
    """Every byte of config text the startup log can carry: the described values and the warnings."""
    return "\n".join([*(f"{k}: {v}" for k, v in cfg.describe().items()), *cfg.warnings])


# ---------------------------------------------------------------------------
# The warnings name the setting, never the value
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name, expected",
    [
        ("ENABLE_SRERD", "ENABLE_SRERD is not a boolean; using false"),
        ("GEOCODE_ENABLED", "GEOCODE_ENABLED is not a boolean; using true"),
        ("GEOCODE_MAX_PER_RUN", "GEOCODE_MAX_PER_RUN is not an integer; using 0"),
        ("GEOCODE_RETRY_COOLDOWN_DAYS", "GEOCODE_RETRY_COOLDOWN_DAYS is not an integer; using 30"),
        ("GEOCODE_BACKEND", "GEOCODE_BACKEND is not one of nominatim, locationiq, geoapify, google; using auto"),
        ("SHARE_ROLE", "SHARE_ROLE is not one of reader, commenter, writer; using writer"),
    ],
)
def test_an_unusable_value_is_named_but_never_echoed(name, expected):
    cfg = load_config({name: f"{MARK}-Typed-Value"})
    assert cfg.warnings == (expected,)
    assert MARK not in cfg.warnings[0].lower()


@pytest.mark.parametrize(
    "name, expected",
    [
        ("GEOCODE_TIME_BUDGET_SECONDS", "GEOCODE_TIME_BUDGET_SECONDS is below the minimum 1; using 3300"),
        ("GEOCODE_FLUSH_EVERY", "GEOCODE_FLUSH_EVERY is below the minimum 1; using 250"),
        ("GEOCODE_MAX_PER_RUN", "GEOCODE_MAX_PER_RUN is below the minimum 0; using 0"),
    ],
)
def test_a_number_below_its_minimum_is_named_but_not_echoed(name, expected):
    cfg = load_config({name: "-7391735"})
    assert cfg.warnings == (expected,)
    assert "7391735" not in cfg.warnings[0]


@pytest.mark.parametrize("typed", ["4501", "987654321", "99999999999999999999"])
def test_a_time_budget_above_the_ceiling_is_clamped_and_named_but_not_echoed(typed):
    cfg = load_config({"GEOCODE_TIME_BUDGET_SECONDS": typed})
    assert cfg.geocode_time_budget_seconds == MAX_GEOCODE_TIME_BUDGET_SECONDS == 4500
    assert cfg.warnings == ("GEOCODE_TIME_BUDGET_SECONDS is above the maximum 4500; using 4500",)
    assert typed not in "\n".join(cfg.warnings) and typed not in everything_printed(cfg)


@pytest.mark.parametrize("typed, expected", [("1", 1), ("600", 600), ("3300", 3300), ("4499", 4499), ("4500", 4500)])
def test_a_time_budget_up_to_the_ceiling_is_kept_without_a_warning(typed, expected):
    cfg = load_config({"GEOCODE_TIME_BUDGET_SECONDS": typed})
    assert cfg.geocode_time_budget_seconds == expected
    assert cfg.warnings == ()


def test_the_time_budget_default_is_unchanged_by_the_ceiling():
    assert load_config({}).geocode_time_budget_seconds == DEFAULT_GEOCODE_TIME_BUDGET_SECONDS == 3300
    assert Config().geocode_time_budget_seconds == DEFAULT_GEOCODE_TIME_BUDGET_SECONDS


def test_the_default_and_the_ceiling_of_the_time_budget_fit_inside_the_scrape_job_timeout():
    # The job is killed at timeout-minutes. The budget only stops the geocoding loop, so it needs room for
    # the sources, the sheet writes and the pin export on either side of it: a 10-minute margin here.
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    timeout_minutes = workflow["jobs"]["scrape-and-sync"]["timeout-minutes"]
    assert isinstance(timeout_minutes, int) and not isinstance(timeout_minutes, bool)
    limit = timeout_minutes * 60 - 10 * 60
    assert load_config({}).geocode_time_budget_seconds < limit
    ceiling = load_config({"GEOCODE_TIME_BUDGET_SECONDS": str(10 * timeout_minutes * 60)}).geocode_time_budget_seconds
    assert ceiling == MAX_GEOCODE_TIME_BUDGET_SECONDS
    assert ceiling < limit


def test_the_choice_warning_keeps_the_allowed_choices_and_the_default():
    # the allowed choices are fixed code, not typed text, so they stay useful to the operator
    for allowed in GEOCODE_BACKENDS:
        assert allowed in load_config({"GEOCODE_BACKEND": "bing"}).warnings[0]
    for allowed in SHARE_ROLES:
        assert allowed in load_config({"SHARE_ROLE": "owner"}).warnings[0]


# ---------------------------------------------------------------------------
# describe(): nothing typed reaches the log
# ---------------------------------------------------------------------------
def test_a_hostile_config_prints_no_typed_value_anywhere():
    cfg = load_config(hostile_env())
    assert len(cfg.warnings) == 12  # every unusable value was noticed, so the check below is not vacuous
    printed = everything_printed(cfg)
    assert MARK not in printed.lower()
    assert "4242424" not in printed and "5353535" not in printed


def test_the_contact_never_reaches_describe():
    cfg = load_config({"GEOCODE_CONTACT": CONTACT_MARK})
    assert cfg.geocode_contact == CONTACT_MARK  # it is still used: the geocoder sends it in the User-Agent
    assert CONTACT_MARK in cfg.geocode_user_agent
    assert CONTACT_MARK not in "\n".join(cfg.describe().values())


def test_the_contact_and_the_share_list_are_shown_only_as_set_or_a_count():
    off = load_config({}).describe()
    assert off["GEOCODE_CONTACT"] == "not set" and off["SHARE_WITH_EMAILS"] == "0 address(es)"
    on = load_config({"GEOCODE_CONTACT": CONTACT_MARK, "SHARE_WITH_EMAILS": f"{SHARE_MARK} b@example.invalid"}).describe()
    assert on["GEOCODE_CONTACT"] == "set" and on["SHARE_WITH_EMAILS"] == "2 address(es)"
    assert SHARE_MARK not in "\n".join(on.values())


def test_the_user_agent_reads_default_or_custom_and_never_its_text():
    assert load_config({}).describe()["GEOCODE_USER_AGENT"] == "default"
    with_contact = load_config({"GEOCODE_CONTACT": CONTACT_MARK})
    assert with_contact.geocode_user_agent == f"{DEFAULT_GEOCODE_USER_AGENT} ({CONTACT_MARK})"
    assert with_contact.describe()["GEOCODE_USER_AGENT"] == "default"  # the default agent plus the contact is still the default
    # a typed agent is custom, whether or not it holds the contact or an address of its own
    for env in (
        {"GEOCODE_USER_AGENT": f"{MARK}-app/2.0"},
        {"GEOCODE_USER_AGENT": f"{MARK}-app/2.0 ({CONTACT_MARK})", "GEOCODE_CONTACT": CONTACT_MARK},
        {"GEOCODE_USER_AGENT": f"{MARK}-app/2.0 ({SHARE_MARK})"},
    ):
        described = load_config(env).describe()["GEOCODE_USER_AGENT"]
        assert described == "custom (not shown)"


def test_the_sheet_name_reads_default_or_custom():
    assert load_config({}).describe()["GOOGLE_SHEET_NAME"] == "default"
    assert load_config({"GOOGLE_SHEET_NAME": DEFAULT_SHEET_NAME}).describe()["GOOGLE_SHEET_NAME"] == "default"
    assert load_config({"GOOGLE_SHEET_NAME": f"{MARK} Sheet"}).describe()["GOOGLE_SHEET_NAME"] == "custom (not shown)"


@pytest.mark.parametrize("model", ["gemini-2.5-flash", "gemini-3-pro-preview", "models/gemini-2.5-flash-lite"])
def test_a_model_id_shows_when_it_has_the_shape_of_one(model):
    assert load_config({"GEMINI_MODEL": model}).describe()["GEMINI_MODEL"] == model


@pytest.mark.parametrize("typed", [f"{MARK}-model", "AIzaSyFAKE-not-a-model", "gemini-2.5-flash (my key is abc)", "gemini-", "gemini-" + "a" * 60])
def test_any_other_model_text_is_not_shown(typed):
    assert load_config({"GEMINI_MODEL": typed}).describe()["GEMINI_MODEL"] == "custom (not shown)"
    assert load_config({}).describe()["GEMINI_MODEL"] == "not set"


def test_require_sources_shows_real_source_names_and_only_counts_the_rest():
    assert load_config({}).describe()["REQUIRE_SOURCES"] == "none"
    assert load_config({"REQUIRE_SOURCES": "DLD_projects, seed_csv"}).describe()["REQUIRE_SOURCES"] == "dld_projects, seed_csv"
    mixed = load_config({"REQUIRE_SOURCES": f"dld_projects {MARK}source {MARK}other"}).describe()["REQUIRE_SOURCES"]
    assert mixed == "dld_projects, 2 unrecognised name(s) (not shown)"
    only_bad = load_config({"REQUIRE_SOURCES": f"{MARK}source"}).describe()["REQUIRE_SOURCES"]
    assert only_bad == "1 unrecognised name(s) (not shown)"


def test_source_names_match_the_adapters():
    # describe() tells a real name from a typo with this list; it must not drift from the adapters
    assert tuple(a.name for a in build_adapters(Config())) == SOURCE_NAMES


# Every setting describe() prints, and the only forms its value may take. A setting added to
# describe() without a row here fails test_every_described_setting_has_a_safe_form: a deliberate
# decision is needed that the new value is code-validated (a flag, a number, a fixed choice) or
# fixed text, never typed text.
SET = r"set|not set"
BOOL = r"true|false"
NUMBER = r"\d+"
SAFE_FORMS = {
    "GEMINI_API_KEY": SET,
    "GCP_SERVICE_ACCOUNT_KEY": SET,
    "SPREADSHEET_ID": SET,
    "DATA_DUBAI_API_KEY": SET,
    "DATA_DUBAI_API_SECRET": SET,
    "LOCATIONIQ_API_KEY": SET,
    "GEOAPIFY_API_KEY": SET,
    "GOOGLE_MAPS_API_KEY": SET,
    "SHARE_WITH_EMAILS": r"\d+ address\(es\)",
    "GOOGLE_SHEET_NAME": r"default|custom \(not shown\)",
    "GEMINI_MODEL": r"not set|custom \(not shown\)|(?:models/)?gemini-[a-z0-9][a-z0-9.\-]{0,40}",
    "ENABLE_SRERD": BOOL,
    "ENABLE_GEMINI_NEWS": BOOL,
    "REQUIRE_SOURCES": r"none|(?:(?:" + "|".join(SOURCE_NAMES) + r"|\d+ unrecognised name\(s\) \(not shown\))(?:, )?)+",
    "ALLOW_ROW_DROP": BOOL,
    "GEOCODE_ENABLED": BOOL,
    "GEOCODE_BACKEND": "auto|" + "|".join(GEOCODE_BACKENDS),
    "GEOCODE_MAX_PER_RUN": NUMBER,
    "GEOCODE_USER_AGENT": r"default|custom \(not shown\)",
    "GEOCODE_CONTACT": SET,
    "GEOCODE_RETRY_COOLDOWN_DAYS": NUMBER,
    "GEOCODE_TIME_BUDGET_SECONDS": NUMBER,
    "GEOCODE_FLUSH_EVERY": NUMBER,
    "SHARE_ROLE": "|".join(SHARE_ROLES),
    "SHARE_NOTIFY": BOOL,
}


def test_every_described_setting_has_a_safe_form():
    assert set(load_config({}).describe()) == set(SAFE_FORMS)


@pytest.mark.parametrize("typed", ["", "true", "false", "maybe"])
def test_deploy_map_is_not_reported_because_the_scrape_step_never_receives_it(typed):
    # Only the workflow's deploy job reads DEPLOY_MAP, so a "DEPLOY_MAP: false" line in the scrape log
    # would be wrong whenever the owner had switched the map on.
    assert "DEPLOY_MAP" not in load_config({"DEPLOY_MAP": typed}).describe()


@pytest.mark.parametrize(
    "env",
    [
        {},
        hostile_env(),
        {"GEMINI_MODEL": "gemini-2.5-flash", "REQUIRE_SOURCES": "dld_projects,srerd_projects", "GEOCODE_BACKEND": "google"},
        {"GOOGLE_SHEET_NAME": "Some Sheet", "GEOCODE_USER_AGENT": "my-app/2.0", "SHARE_ROLE": "reader", "SHARE_NOTIFY": "yes"},
    ],
    ids=["defaults", "hostile", "valid-choices", "custom-text"],
)
def test_every_printed_value_has_one_of_its_safe_forms(env):
    for name, value in load_config(env).describe().items():
        assert re.fullmatch(SAFE_FORMS[name], value), f"{name} printed a value outside its safe form"


# ---------------------------------------------------------------------------
# main.log_start: the lines that actually reach the public Actions log
# ---------------------------------------------------------------------------
def test_the_startup_log_carries_no_typed_value(caplog):
    cfg = load_config(hostile_env())
    # an empty secret list on purpose: the scrubber (a backstop) must have nothing to remove
    with caplog.at_level(logging.DEBUG):
        app.log_start(cfg, app.Report(), dry_run=False)
    assert "config GEMINI_API_KEY: set" in caplog.text and "config GEOCODE_CONTACT: set" in caplog.text
    assert "config warning: ENABLE_SRERD is not a boolean; using false" in caplog.text
    assert MARK not in caplog.text.lower()
    assert "4242424" not in caplog.text and "5353535" not in caplog.text
