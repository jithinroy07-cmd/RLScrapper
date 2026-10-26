"""
Tests for the package surface: build_adapters, the SourceAdapter contract as the four
adapters meet it, the switches seen from main.py's side, and static guards over the
adapter source files and their SYNTHETIC test fixtures. No network.
"""

from __future__ import annotations

import ast
import itertools
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.sources as sources
from src.sources import (
    DldProjectsAdapter,
    GeminiNewsAdapter,
    SeedCsvAdapter,
    SourceAdapter,
    SourceError,
    SourceResult,
    SrerdProjectsAdapter,
    build_adapters,
    check_row_count,
)
from src.sources import seed_csv
from test_sources_support import (
    ALL_FAKE_SECRETS,
    FAKE_KEY,
    FAKE_SECRET,
    FAKE_TOKEN,
    FIXTURES,
    FakeResponse,
    FakeSession,
    Sleeps,
    dld_cfg,
    gemini_cfg,
    grounding_response,
    load_fixture,
    make_cfg,
    token_ok,
)

SOURCES_DIR = Path(sources.__file__).resolve().parent
TESTS_DIR = Path(__file__).resolve().parent
SOURCE_FILES = sorted(SOURCES_DIR.glob("*.py"))
MY_TEST_FILES = sorted(TESTS_DIR.glob("test_sources_*.py"))
OTHER_DAY = date(2020, 1, 2)

EXPECTED = [("dld_projects", True), ("srerd_projects", True), ("seed_csv", False), ("gemini_news", False)]


def everything_on():
    return make_cfg(
        DATA_DUBAI_API_KEY=FAKE_KEY, DATA_DUBAI_API_SECRET=FAKE_SECRET, ENABLE_SRERD="true",
        ENABLE_GEMINI_NEWS="true", GEMINI_API_KEY=FAKE_KEY, GEMINI_MODEL="fake-model-1",
    )


# ---------------------------------------------------------------------------
# build_adapters
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cfg", [make_cfg(), everything_on()], ids=["all off", "all on"])
def test_all_four_adapters_come_back_in_the_fixed_order_enabled_or_not(cfg):
    adapters = build_adapters(cfg)
    assert [(a.name, a.expects_rows) for a in adapters] == EXPECTED
    assert [type(a) for a in adapters] == [DldProjectsAdapter, SrerdProjectsAdapter, SeedCsvAdapter, GeminiNewsAdapter]


def test_every_adapter_meets_the_source_adapter_contract():
    for adapter in build_adapters(make_cfg()):
        assert isinstance(adapter, SourceAdapter)
        assert callable(adapter.fetch) and callable(adapter.enabled) and callable(adapter.skip_reason)


def test_each_call_builds_fresh_adapters():
    first, second = build_adapters(make_cfg()), build_adapters(make_cfg())
    assert all(a is not b for a, b in zip(first, second))


def test_the_package_exports_exactly_the_documented_surface():
    assert sorted(sources.__all__) == sorted(
        ["DldProjectsAdapter", "GeminiNewsAdapter", "SeedCsvAdapter", "SourceAdapter", "SourceError", "SourceResult",
         "SrerdProjectsAdapter", "build_adapters", "check_row_count"]
    )
    for name in sources.__all__:
        assert getattr(sources, name) is not None


def test_the_contract_types_behave_as_documented():
    assert issubclass(SourceError, Exception)
    result = SourceResult(source="x", rows=[], fetched_at="2026-10-25T00:00:00Z")
    assert result.source_refreshed is None and result.notes == [] and result.candidates == []
    other = SourceResult(source="y", rows=[], fetched_at="t")
    result.notes.append("n")
    assert other.notes == []  # the default lists are not shared
    check_row_count("x", 5, None, expects_rows=True, allow_drop=False)


# ---------------------------------------------------------------------------
# Switches, as main.py will ask
# ---------------------------------------------------------------------------
CASES = list(itertools.product([None, "both", "key", "secret"], [None, "true", "false"], [None, "true"], [None, FAKE_KEY], [None, "m"]))


@pytest.mark.parametrize("dld,srerd,gemini,gkey,gmodel", CASES)
def test_enabled_and_skip_reason_always_agree(dld, srerd, gemini, gkey, gmodel):
    env = {}
    if dld in ("both", "key"):
        env["DATA_DUBAI_API_KEY"] = FAKE_KEY
    if dld in ("both", "secret"):
        env["DATA_DUBAI_API_SECRET"] = FAKE_SECRET
    if srerd is not None:
        env["ENABLE_SRERD"] = srerd
    if gemini is not None:
        env["ENABLE_GEMINI_NEWS"] = gemini
    if gkey is not None:
        env["GEMINI_API_KEY"] = gkey
    if gmodel is not None:
        env["GEMINI_MODEL"] = gmodel
    cfg = make_cfg(**env)
    on = {
        "dld_projects": dld == "both",
        "srerd_projects": srerd == "true",
        "seed_csv": True,
        "gemini_news": gemini == "true" and gkey is not None and gmodel is not None,
    }
    for adapter in build_adapters(cfg):
        assert adapter.enabled(cfg) is on[adapter.name], adapter.name
        reason = adapter.skip_reason(cfg)
        assert (reason == "") is on[adapter.name], adapter.name
        for secret in ALL_FAKE_SECRETS:
            assert secret not in reason


def test_a_skip_reason_names_settings_not_values():
    cfg = make_cfg(DATA_DUBAI_API_KEY=FAKE_KEY)
    dld = build_adapters(cfg)[0]
    assert dld.skip_reason(cfg) == "not configured: DATA_DUBAI_API_SECRET is not set"
    assert "ENABLE_SRERD" in build_adapters(cfg)[1].skip_reason(cfg)


def test_the_three_switchable_adapters_refuse_to_fetch_when_off_and_say_why():
    cfg = make_cfg()
    for adapter in build_adapters(cfg):
        if adapter.name == "seed_csv":
            continue
        with pytest.raises(SourceError) as info:
            adapter.fetch()
        assert adapter.name in str(info.value) and adapter.skip_reason(cfg) in str(info.value)


def test_the_seed_always_runs_and_the_shipped_seed_is_a_quiet_zero():
    (seed,) = [a for a in build_adapters(make_cfg()) if a.name == "seed_csv"]
    result = seed.fetch()
    assert result.rows == [] and result.source == "seed_csv"
    check_row_count("seed_csv", len(result.rows), None, expects_rows=seed.expects_rows, allow_drop=False)


# ---------------------------------------------------------------------------
# What build_adapters passes down
# ---------------------------------------------------------------------------
def test_one_session_is_shared_by_the_registry_adapters_and_stays_open():
    session = FakeSession(
        [token_ok(), FakeResponse(200, load_fixture("dld_page1.json")), FakeResponse(200, load_fixture("dld_page2.json")),
         FakeResponse(200, {"data": []}),  # the confirming empty page that ends the register
         FakeResponse(200, load_fixture("srerd_projects.json"))]
    )
    dld, srerd, _, _ = build_adapters(everything_on(), session=session, today=OTHER_DAY)
    assert len(dld.fetch().rows) == 5
    assert len(srerd.fetch().rows) == 5
    assert session.closed is False and session.script == []


def test_today_reaches_every_row_stamp():
    session = FakeSession(
        [token_ok(), FakeResponse(200, load_fixture("dld_page1.json")), FakeResponse(200, load_fixture("dld_page2.json")),
         FakeResponse(200, {"data": []}),  # the confirming empty page that ends the register
         FakeResponse(200, load_fixture("srerd_projects.json"))]
    )
    dld, srerd, _, _ = build_adapters(everything_on(), session=session, today=OTHER_DAY)
    for row in dld.fetch().rows + srerd.fetch().rows:
        assert (row.first_seen, row.last_seen) == ("2020-01-02", "2020-01-02")


def test_today_reaches_the_seed_adapter(monkeypatch, tmp_path):
    path = tmp_path / "seed_registry.csv"
    header = "Emirate,Project Name (EN),Developer,Source URL"
    path.write_text(header + "\nDubai,Test Seed Tower,Example Developer LLC,https://seed.example.com/x\n", encoding="utf-8")
    monkeypatch.setattr(seed_csv, "SEED_REGISTRY_FILE", path)
    (seed,) = [a for a in build_adapters(make_cfg(), today=OTHER_DAY) if a.name == "seed_csv"]
    (row,) = seed.fetch().rows
    assert row.first_seen == row.last_seen == "2020-01-02"


def test_today_reaches_the_prompt_of_the_gemini_adapter(monkeypatch):
    from google import genai

    prompts = []

    class RecordingClient:
        def __init__(self, **kwargs):
            self.models = SimpleNamespace(generate_content=lambda **kw: prompts.append(kw["contents"]) or grounding_response("[]"))

    monkeypatch.setattr(genai, "Client", RecordingClient)
    cfg = gemini_cfg()
    gemini = build_adapters(cfg, today=OTHER_DAY)[3]
    assert gemini.fetch().candidates == []
    assert "2020-01-02" in prompts[0]


def test_http_requests_have_a_timeout_an_identifying_agent_and_keep_tls_verification():
    session = FakeSession(
        [token_ok(), FakeResponse(200, load_fixture("dld_page1.json")), FakeResponse(200, load_fixture("dld_page2.json")),
         FakeResponse(200, {"data": []}),  # the confirming empty page that ends the register
         FakeResponse(200, load_fixture("srerd_projects.json"))]
    )
    cfg = everything_on()
    dld, srerd, _, _ = build_adapters(cfg, session=session)
    dld.fetch(), srerd.fetch()
    assert len(session.calls) == 5
    for call in session.calls:
        timeout = call.kwargs["timeout"]
        assert (isinstance(timeout, (int, float)) and 0 < timeout <= 300) or (isinstance(timeout, tuple) and all(0 < t <= 300 for t in timeout))
        assert call.kwargs.get("verify", True) is not False
        agent = call.kwargs["headers"]["User-Agent"]
        assert agent == cfg.geocode_user_agent and "Mozilla" not in agent
        assert call.url.startswith("https://")


def test_the_two_registry_secrets_travel_only_to_the_token_endpoint():
    session = FakeSession(
        [token_ok(), FakeResponse(200, load_fixture("dld_page1.json")), FakeResponse(200, load_fixture("dld_page2.json")),
         FakeResponse(200, {"data": []})]
    )
    DldProjectsAdapter(dld_cfg(), session=session, today=OTHER_DAY, sleep=Sleeps()).fetch()
    token_call, *page_calls = session.calls
    assert token_call.method == "POST" and token_call.kwargs["data"]["client_secret"] == FAKE_SECRET
    for call in page_calls:
        assert FAKE_SECRET not in repr(call.kwargs) and FAKE_SECRET not in call.url and FAKE_KEY not in call.url
        assert call.kwargs["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"


# ---------------------------------------------------------------------------
# Static guards over the adapter code
# ---------------------------------------------------------------------------
def parsed(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_the_expected_adapter_files_exist():
    assert {p.name for p in SOURCE_FILES} == {"__init__.py", "base.py", "dld_projects.py", "srerd_projects.py", "seed_csv.py", "gemini_news.py"}


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_tls_verification_is_never_switched_off(path):
    for node in ast.walk(parsed(path)):
        if isinstance(node, ast.keyword) and node.arg == "verify":
            assert not (isinstance(node.value, ast.Constant) and node.value.value is False), f"verify=False in {path.name}"
    assert "verify=False" not in path.read_text(encoding="utf-8")
    assert "InsecureRequestWarning" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_every_http_call_through_a_session_sets_a_timeout(path):
    for node in ast.walk(parsed(path)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "post", "put", "delete", "head", "patch", "request"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"session", "requests", "created"}
        ):
            assert any(k.arg == "timeout" for k in node.keywords), f"{path.name}:{node.lineno} has no timeout"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_no_spoofed_browser_agent_no_print_no_direct_environment_reads(path):
    text = path.read_text(encoding="utf-8")
    assert "Mozilla" not in text and "AppleWebKit" not in text
    for node in ast.walk(parsed(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"print", "input", "eval", "exec"}, f"{node.func.id}() in {path.name}:{node.lineno}"
    assert "os.environ" not in text and "getenv" not in text  # settings come only through Config


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_adapters_do_not_import_other_builders_modules(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"(from|import)\s+src(\.|\s+import\s+)(gsheet_manager|geocoder|main|export_map_pins|dedupe_sheet)\b", text)


@pytest.mark.parametrize("path", SOURCE_FILES + MY_TEST_FILES, ids=lambda p: p.name)
def test_no_secret_shaped_literals_in_adapter_code_or_tests(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"AIza[0-9A-Za-z_-]{20,}", text)
    assert not re.search(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.", text)
    assert not re.search(r"\b(sk|ghp|xox[bp])[-_][A-Za-z0-9]{20,}", text)
    assert not re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text)


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_no_network_address_is_a_bare_ip_or_carries_a_key(path):
    for url in re.findall(r"https?://[^\s\"'`)>]+", path.read_text(encoding="utf-8")):
        assert not re.match(r"https?://\d+\.\d+\.\d+\.\d+", url), url
        assert not re.search(r"[?&](key|api_key|token|apikey)=", url, re.IGNORECASE), url


# ---------------------------------------------------------------------------
# Fixtures are labelled and synthetic
# ---------------------------------------------------------------------------
FIXTURE_FILES = sorted(p for p in FIXTURES.rglob("*") if p.is_file())


def test_there_are_fixtures_to_check():
    assert {p.name for p in FIXTURE_FILES} >= {"dld_page1.json", "dld_page2.json", "srerd_projects.json", "gemini_leads.json"}


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_every_fixture_is_labelled_synthetic(path):
    assert "SYNTHETIC" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", MY_TEST_FILES, ids=lambda p: p.name)
def test_every_test_module_says_its_data_is_synthetic(path):
    assert "SYNTHETIC" in path.read_text(encoding="utf-8")[:1500]


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixtures_use_only_invented_names_and_example_domains(path):
    text = path.read_text(encoding="utf-8")
    for url in re.findall(r"https?://[^\s\"']+", text):
        host = url.split("/")[2].lower()
        assert host.endswith((".example.com", ".example.org", ".example.net")) or host in {"example.com", "example.org", "example.net"}, url
