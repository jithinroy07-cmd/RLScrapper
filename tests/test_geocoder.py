"""
Tests for src/geocoder.py.

No network, no sleeping, no secrets: HTTP goes through FakeSession, time through
FakeClock, and the cache lives in tmp_path. Every provider response below is
SYNTHETIC (made-up names such as "Test Residences A"; the coordinates are real
points chosen so the real emirate polygons classify them, and the first test
asserts that, so a polygon update fails loudly instead of passing by accident).
Fake keys are obviously fake.
"""

from __future__ import annotations

import json
import logging
import re
import time
import zlib
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import pytest
import requests

from src import emirates
from src import geocoder as gc
from src.config import Config
from src.geocoder import (
    GeocodeStats,
    Geocoder,
    GeocoderConfigError,
    LookupInterrupted,
    emirate_from_state,
    geocode_pending,
    haversine_km,
    name_agrees,
    resolve_backend,
    scrub_secrets,
)
from src.models import GeocodeOverride, RegistryRow

TODAY = date(2026, 10, 25)
_REAL_SLEEP = time.sleep  # captured before the autouse fixture replaces it

# SYNTHETIC: obviously fake keys. Distinct, so a leak of one is attributable.
KEY_LIQ = "fake-key-liq-7f3a"
KEY_GEO = "fake-key-geo-2b9c"
KEY_GOOGLE = "fake-key-google-55d1"
KEYS = {"locationiq": KEY_LIQ, "geoapify": KEY_GEO, "google": KEY_GOOGLE}
ALL_KEYS = tuple(KEYS.values())
BACKENDS = ("nominatim", "locationiq", "geoapify", "google")
KEYED_BACKENDS = ("locationiq", "geoapify", "google")

# Real points, classified by the real polygons (asserted in the first test).
DOWNTOWN = (25.1972, 55.2744)  # Dubai
BUSINESS_BAY = (25.1850, 55.2650)  # Dubai, about 1.6 km from DOWNTOWN
MARINA = (25.0805, 55.1403)  # Dubai, about 19 km from DOWNTOWN
NEAR_8KM = (25.1300, 55.2744)  # Dubai, 7.5 km from DOWNTOWN (just inside the gate)
FAR_8KM = (25.1200, 55.2744)  # Dubai, 8.6 km from DOWNTOWN (just outside the gate)
AL_NAHDA_DUBAI = (25.2935, 55.3700)  # Dubai, 0.41 km from Sharjah
MIRDIF = (25.2217, 55.4190)  # Dubai, 7 km from Sharjah
SHARJAH_ALJADA = (25.3010, 55.4540)  # Sharjah, 2.5 km from Dubai
SHARJAH_MUWAILEH = (25.2900, 55.4500)  # Sharjah, 1.2 km from Dubai
SHARJAH_CITY_CENTRE = (25.3463, 55.4209)  # Sharjah
KALBA = (25.0486, 56.35)  # Sharjah exclave, about 90 km from the city
ABU_DHABI = (24.4539, 54.3773)  # neither
AJMAN = (25.4052, 55.5136)  # neither
MURCIA = (37.98, -1.13)  # another continent
UAE_CENTRE = (23.4241, 53.8478)  # whole-country centroid
NULL_ISLAND = (0.0, 0.0)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class UnexpectedRequest(BaseException):
    """Raised by FakeSession when a test did not expect a request (BaseException: nothing swallows it)."""


class FakeClock:
    """A clock whose sleep moves it, so pacing is observable and instant."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None, *, text: Optional[str] = None, bad_json: bool = False):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json
        self.text = text if text is not None else ("" if payload is None else json.dumps(payload, ensure_ascii=False))

    def json(self) -> Any:
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeSession:
    """Stand-in for requests.Session: replies come from a handler or a queue, every call is recorded."""

    def __init__(self, handler=None, replies=None):
        self.calls: list[dict[str, Any]] = []
        self._handler = handler
        self._replies = list(replies or [])

    def get(self, url, params=None, headers=None, timeout=None, **extra):
        call = {"url": url, "params": dict(params or {}), "headers": dict(headers or {}), "timeout": timeout, "extra": extra}
        self.calls.append(call)
        if self._handler is not None:
            reply = self._handler(call)
        elif self._replies:
            reply = self._replies.pop(0)
        else:
            raise UnexpectedRequest(f"no reply scripted for {call['params']}")
        if isinstance(reply, BaseException):
            raise reply
        return reply


def query_of(call: dict[str, Any]) -> str:
    params = call["params"]
    return params.get("q") or params.get("text") or params.get("address") or ""


def router(table: dict[str, Any], *, empty: Any = None, clock: Optional[FakeClock] = None, step: float = 0.0):
    """Reply by query text: the table maps a query to a payload, a FakeResponse or an exception."""

    def handle(call: dict[str, Any]):
        if clock is not None:
            clock.now += step
        reply = table.get(query_of(call), [] if empty is None else empty)
        return reply if isinstance(reply, (FakeResponse, BaseException)) else FakeResponse(200, reply)

    return handle


class Rig:
    def __init__(self, geo: Geocoder, session: FakeSession, clock: FakeClock, cache_file: Path):
        self.geo, self.session, self.clock, self.cache_file = geo, session, clock, cache_file

    @property
    def queries(self) -> list[str]:
        return [query_of(c) for c in self.session.calls]

    def cache_entries(self) -> dict[str, Any]:
        if not self.cache_file.exists():
            return {}
        return json.loads(self.cache_file.read_text(encoding="utf-8")).get("entries", {})


def cfg_for(backend: str = "nominatim", **overrides: Any) -> Config:
    keys = {
        "nominatim": {},
        "locationiq": {"locationiq_api_key": KEY_LIQ, "geocode_backend": "locationiq"},
        "geoapify": {"geoapify_api_key": KEY_GEO, "geocode_backend": "geoapify"},
        "google": {"google_maps_api_key": KEY_GOOGLE, "geocode_backend": "google"},
    }[backend]
    return Config(**{**keys, **overrides})


def make_rig(tmp_path: Path, cfg: Optional[Config] = None, *, handler=None, replies=None, use_cache: bool = True,
             cache_name: str = "geocode_cache.json") -> Rig:
    clock = FakeClock()
    session = FakeSession(handler=handler, replies=replies)
    cache_file = tmp_path / cache_name
    geo = Geocoder(cfg or Config(), session=session, cache_file=cache_file, sleep=clock.sleep, clock=clock.time, use_cache=use_cache)
    return Rig(geo, session, clock, cache_file)


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    def refuse(seconds):
        raise AssertionError(f"a test really slept for {seconds}s; inject a fake sleep")

    monkeypatch.setattr(time, "sleep", refuse)


# ---------------------------------------------------------------------------
# SYNTHETIC provider responses
# ---------------------------------------------------------------------------
def osm_place(lat: float, lon: float, *, name: str = "Test Residences A", state: str = "Dubai", cls: str = "building",
              typ: str = "apartments", rank: int = 30, country: str = "ae", addresstype: str = "building",
              display: Optional[str] = None) -> dict[str, Any]:
    """# SYNTHETIC Nominatim / LocationIQ item for a building-like place."""
    address: dict[str, Any] = {"building": name, "suburb": "Example Community", "country": "United Arab Emirates", "country_code": country}
    if state:
        address["state"] = state
    return {
        "place_id": 1, "lat": f"{lat:.7f}", "lon": f"{lon:.7f}", "category": cls, "type": typ, "place_rank": rank,
        "addresstype": addresstype, "display_name": display or f"{name}, Example Community, {state}, United Arab Emirates",
        "address": address, "boundingbox": ["25.0", "25.1", "55.0", "55.1"],
    }


def osm_area(lat: float, lon: float, *, name: str = "Example Community", state: str = "Dubai") -> dict[str, Any]:
    """# SYNTHETIC Nominatim item for a whole district (an area centroid)."""
    item = osm_place(lat, lon, name=name, state=state, cls="place", typ="suburb", rank=20, addresstype="suburb")
    item["address"] = {"suburb": name, "state": state, "country": "United Arab Emirates", "country_code": "ae"}
    return item


def osm_street(lat: float, lon: float, *, state: str = "Dubai") -> dict[str, Any]:
    """# SYNTHETIC Nominatim item for a street."""
    item = osm_place(lat, lon, name="Example Street", state=state, cls="highway", typ="residential", rank=26, addresstype="road")
    item["address"] = {"road": "Example Street", "state": state, "country": "United Arab Emirates", "country_code": "ae"}
    return item


def geoapify_payload(lat: float, lon: float, *, result_type: str = "building", state: str = "Dubai", country: str = "ae",
                     omit_latlon: bool = False) -> dict[str, Any]:
    """# SYNTHETIC Geoapify FeatureCollection with one feature."""
    props: dict[str, Any] = {"result_type": result_type, "state": state, "country_code": country,
                             "formatted": "Test Residences A, Dubai, United Arab Emirates"}
    if not omit_latlon:
        props.update(lat=lat, lon=lon)
    feature = {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [lon, lat]}}
    return {"type": "FeatureCollection", "features": [feature]}


def google_payload(lat: float, lon: float, *, types=("establishment", "point_of_interest"), state: str = "Dubai",
                   country: str = "AE", partial: bool = False) -> dict[str, Any]:
    """# SYNTHETIC Google geocoding response with one result."""
    result: dict[str, Any] = {
        "formatted_address": "Test Residences A, Dubai, United Arab Emirates",
        "geometry": {"location": {"lat": lat, "lng": lon}},
        "types": list(types),
        "address_components": [
            {"long_name": state, "short_name": state, "types": ["administrative_area_level_1", "political"]},
            {"long_name": "United Arab Emirates", "short_name": country, "types": ["country", "political"]},
        ],
    }
    if partial:
        result["partial_match"] = True
    return {"status": "OK", "results": [result]}


def hit_payload(backend: str, lat: float, lon: float) -> Any:
    if backend == "geoapify":
        return geoapify_payload(lat, lon)
    if backend == "google":
        return google_payload(lat, lon)
    return [osm_place(lat, lon)]


def empty_payload(backend: str) -> Any:
    if backend == "geoapify":
        return {"type": "FeatureCollection", "features": []}
    if backend == "google":
        return {"status": "ZERO_RESULTS", "results": []}
    return []


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
def q(place: str, emirate: str = "Dubai") -> str:
    return f"{place}, {emirate}, UAE"


def mk_row(name: str = "Test Residences A", *, emirate: str = "Dubai", number: Optional[str] = None,
           community: str = "Example Community", area: str = "Example Area", developer: str = "",
           geo_status: str = "", **extra: Any) -> RegistryRow:
    registry = "DLD" if emirate == "Dubai" else "SRERD"
    geo_status = extra.pop("geocode_status", geo_status)
    if number is None:
        number = str(zlib.crc32(name.encode("utf-8")) % 10**8 + 1)
    return RegistryRow(emirate=emirate, registry=registry, registry_project_no=number, project_name_en=name,
                       developer=developer, master_project=community, area_en=area, geocode_status=geo_status, **extra)


def lone_row(i: int, **kwargs: Any) -> RegistryRow:
    """A row with exactly one query ("Test Residences i, Dubai, UAE"): no community, no area, no developer."""
    return mk_row(f"Test Residences {i}", community="", area="", **kwargs)


def lone_q(i: int) -> str:
    return q(f"Test Residences {i}")


def run(rows, rig: Rig, cfg: Optional[Config] = None, *, overrides=None, flush=None, today: date = TODAY):
    return geocode_pending(rows, overrides or {}, cfg or Config(), today=today, geocoder=rig.geo, flush=flush)


def log_text(caplog) -> str:
    return "\n".join(f"{r.getMessage()} {r.exc_text or ''} {r.args}" for r in caplog.records)


EXACT_Q, COMM_Q, AREA_Q = q("Test Residences A"), q("Example Community"), q("Example Area")


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
def test_fixture_points_sit_where_the_tests_assume():
    assert emirates.emirate_for_point(*DOWNTOWN) == "Dubai"
    assert emirates.emirate_for_point(*BUSINESS_BAY) == "Dubai"
    assert emirates.emirate_for_point(*MARINA) == "Dubai"
    assert emirates.emirate_for_point(*AL_NAHDA_DUBAI) == "Dubai"
    assert emirates.emirate_for_point(*MIRDIF) == "Dubai"
    for point in (SHARJAH_ALJADA, SHARJAH_MUWAILEH, SHARJAH_CITY_CENTRE, KALBA):
        assert emirates.emirate_for_point(*point) == "Sharjah"
    for point in (ABU_DHABI, AJMAN, MURCIA, NULL_ISLAND):
        assert emirates.emirate_for_point(*point) is None
    assert emirates.distance_to_border_km(*AL_NAHDA_DUBAI, "Dubai") < 1.5
    assert emirates.distance_to_border_km(*SHARJAH_MUWAILEH, "Sharjah") < 1.5
    assert emirates.distance_to_border_km(*SHARJAH_ALJADA, "Sharjah") > 2.0
    assert emirates.distance_to_border_km(*MIRDIF, "Dubai") > 2.0
    assert haversine_km(*DOWNTOWN, *BUSINESS_BAY) < 4
    assert haversine_km(*DOWNTOWN, *MARINA) > 12
    assert emirates.emirate_for_point(*NEAR_8KM) == emirates.emirate_for_point(*FAR_8KM) == "Dubai"
    assert 7.0 < haversine_km(*DOWNTOWN, *NEAR_8KM) < 8.0 < haversine_km(*DOWNTOWN, *FAR_8KM) < 9.0
    assert haversine_km(*SHARJAH_ALJADA, *KALBA) > 60


def test_default_row_produces_the_tiers_these_tests_route_on():
    assert mk_row().geocode_tiers() == [("exact", [EXACT_Q]), ("community", [COMM_Q]), ("area", [AREA_Q])]


def test_haversine_known_values():
    assert haversine_km(0, 0, 0, 0) == 0
    assert haversine_km(0, 0, 1, 0) == pytest.approx(111.19, abs=0.1)
    assert haversine_km(*DOWNTOWN, *MARINA) == pytest.approx(haversine_km(*MARINA, *DOWNTOWN))


# ---------------------------------------------------------------------------
# resolve_backend
# ---------------------------------------------------------------------------
def test_backend_defaults_to_public_nominatim():
    assert resolve_backend(Config()) == "nominatim"


def test_auto_picks_a_keyed_free_backend_only_when_its_key_is_set():
    assert resolve_backend(Config(locationiq_api_key=KEY_LIQ)) == "locationiq"
    assert resolve_backend(Config(geoapify_api_key=KEY_GEO)) == "geoapify"
    assert resolve_backend(Config(locationiq_api_key=KEY_LIQ, geoapify_api_key=KEY_GEO)) == "locationiq"


def test_google_is_never_auto_selected_even_with_a_key():
    assert resolve_backend(Config(google_maps_api_key=KEY_GOOGLE)) == "nominatim"
    assert resolve_backend(Config(google_maps_api_key=KEY_GOOGLE, locationiq_api_key=KEY_LIQ)) == "locationiq"


def test_google_is_selected_when_explicitly_requested_with_a_key():
    assert resolve_backend(Config(geocode_backend="google", google_maps_api_key=KEY_GOOGLE)) == "google"
    assert resolve_backend(Config(geocode_backend=" Google ", google_maps_api_key=KEY_GOOGLE)) == "google"


def test_google_without_a_key_fails_loudly_and_names_the_setting_not_a_value():
    with pytest.raises(GeocoderConfigError, match="GOOGLE_MAPS_API_KEY"):
        resolve_backend(Config(geocode_backend="google"))
    with pytest.raises(GeocoderConfigError):
        Geocoder(Config(geocode_backend="google", locationiq_api_key=KEY_LIQ))


@pytest.mark.parametrize("backend", ["locationiq", "geoapify"])
def test_keyed_backend_requested_without_its_key_is_not_selected(backend, caplog):
    with caplog.at_level(logging.WARNING, logger="src.geocoder"):
        assert resolve_backend(Config(geocode_backend=backend)) == "nominatim"
    assert backend in caplog.text


def test_requested_backend_falls_back_to_the_automatic_choice_that_has_a_key():
    cfg = Config(geocode_backend="locationiq", geoapify_api_key=KEY_GEO)
    assert resolve_backend(cfg) == "geoapify"


def test_explicit_nominatim_wins_over_available_keys():
    cfg = Config(geocode_backend="nominatim", locationiq_api_key=KEY_LIQ, geoapify_api_key=KEY_GEO)
    assert resolve_backend(cfg) == "nominatim"


def test_unsupported_backend_name_raises_without_echoing_keys():
    with pytest.raises(GeocoderConfigError) as err:
        resolve_backend(Config(geocode_backend="mapquest", locationiq_api_key=KEY_LIQ))
    assert KEY_LIQ not in str(err.value)
    assert isinstance(err.value, ValueError)


def test_unsupported_backend_name_is_not_echoed_but_the_supported_ones_are_listed():
    with pytest.raises(GeocoderConfigError) as err:
        resolve_backend(Config(geocode_backend="MARKER-mapquest-secret-looking"))
    message = str(err.value)
    assert "MARKER" not in message and "mapquest" not in message  # a hand-typed setting; the text reaches a public log
    assert "GEOCODE_BACKEND" in message and all(name in message for name in gc.GEOCODE_BACKENDS)


# (code point, is it one of the two Arabic blocks the Arabic-name logic looks for); built with chr() because U+0600 is invisible
ARABIC_EDGES = [
    (0x05FF, False), (0x0600, True), (0x06FF, True), (0x0700, False),
    (0x074F, False), (0x0750, True), (0x077F, True), (0x0780, False),
]


@pytest.mark.parametrize("code_point,inside", ARABIC_EDGES, ids=[f"U+{cp:04X}" for cp, _ in ARABIC_EDGES])
def test_the_arabic_check_matches_exactly_its_two_blocks(code_point, inside):
    assert gc._is_arabic(chr(code_point)) is inside
    assert gc._is_arabic("Tower " + chr(code_point)) is inside


def test_the_arabic_pattern_is_written_with_escapes_not_invisible_characters():
    from pathlib import Path

    line = next(l for l in Path(gc.__file__).read_text(encoding="utf-8").splitlines() if l.startswith("_ARABIC_RE = "))
    assert line.isascii(), "a literal U+0600 in a regex class is invisible to a reviewer; use \\uXXXX escapes"
    for escape in ("\\u0600", "\\u06ff", "\\u0750", "\\u077f"):
        assert escape in line


def test_pacing_and_budget_constants_match_the_design():
    # Public Nominatim policy: scripts run at regular intervals get 4 requests a minute.
    assert gc.BACKEND_SETTINGS["nominatim"].delay >= 15.0
    assert gc.BACKEND_SETTINGS["nominatim"].budget == 200
    assert gc.EXACT_MAX_CENTROID_KM == 8.0
    assert gc.BORDER_CHECK_KM == 1.5
    assert all(gc.BACKEND_SETTINGS[b].needs_key for b in KEYED_BACKENDS)
    assert not gc.BACKEND_SETTINGS["nominatim"].needs_key


def test_lookup_ceiling_is_the_config_value_or_the_backend_default(tmp_path):
    assert make_rig(tmp_path, Config()).geo.max_lookups == 200
    assert make_rig(tmp_path, Config(geocode_max_per_run=7)).geo.max_lookups == 7
    assert make_rig(tmp_path, cfg_for("locationiq")).geo.max_lookups == gc.BACKEND_SETTINGS["locationiq"].budget


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend", BACKENDS)
def test_every_request_is_restricted_to_the_uae_in_english(tmp_path, backend):
    rig = make_rig(tmp_path, cfg_for(backend), handler=router({}, empty=empty_payload(backend)))
    rig.geo.lookup(EXACT_Q, "Dubai")
    call = rig.session.calls[0]
    params = call["params"]
    if backend in ("nominatim", "locationiq"):
        assert params["countrycodes"] == "ae"
        assert params["accept-language"] == "en"
        assert params["addressdetails"] == 1
        assert params["q"] == EXACT_Q
    elif backend == "geoapify":
        assert params["filter"] == "countrycode:ae"
        assert params["lang"] == "en"
        assert params["text"] == EXACT_Q
    else:
        assert params["components"] == "country:AE"
        assert params["language"] == "en"
        assert params["address"] == EXACT_Q
    assert call["url"].startswith("https://")
    assert call["extra"] == {}, "no extra transport options such as verify=False"
    assert 0 < call["timeout"] <= 60


def test_keyed_backends_send_the_key_in_the_expected_parameter_and_never_in_the_url(tmp_path):
    for backend, name in (("locationiq", "key"), ("geoapify", "apiKey"), ("google", "key")):
        rig = make_rig(tmp_path, cfg_for(backend), handler=router({}, empty=empty_payload(backend)), cache_name=f"{backend}.json")
        rig.geo.lookup(EXACT_Q, "Dubai")
        call = rig.session.calls[0]
        assert call["params"][name] == KEYS[backend]
        assert KEYS[backend] not in call["url"]


def test_nominatim_sends_no_key_parameter(tmp_path):
    rig = make_rig(tmp_path, Config(locationiq_api_key=KEY_LIQ, geocode_backend="nominatim"))
    rig.session._handler = router({})
    rig.geo.lookup(EXACT_Q, "Dubai")
    assert not {"key", "apiKey"} & set(rig.session.calls[0]["params"])


def test_user_agent_is_the_configured_identifying_one_with_contact(tmp_path):
    cfg = Config(geocode_user_agent="example-agent/2.0", geocode_contact="ops@example.invalid")
    rig = make_rig(tmp_path, cfg, handler=router({}))
    rig.geo.lookup(EXACT_Q, "Dubai")
    agent = rig.session.calls[0]["headers"]["User-Agent"]
    assert agent == "example-agent/2.0 (ops@example.invalid)"
    assert "mozilla" not in agent.lower()


def test_user_agent_does_not_repeat_a_contact_the_config_already_added(tmp_path):
    cfg = Config(geocode_user_agent="example-agent/2.0 (ops@example.invalid)", geocode_contact="ops@example.invalid")
    rig = make_rig(tmp_path, cfg, handler=router({}))
    rig.geo.lookup(EXACT_Q, "Dubai")
    assert rig.session.calls[0]["headers"]["User-Agent"].count("ops@example.invalid") == 1


def test_default_user_agent_names_the_project_and_has_no_contact_brackets(tmp_path):
    rig = make_rig(tmp_path, Config(), handler=router({}))
    rig.geo.lookup(EXACT_Q, "Dubai")
    assert rig.session.calls[0]["headers"]["User-Agent"] == "uae-real-estate-scraper/1.0"


def test_blank_or_non_ascii_user_agent_never_breaks_the_header(tmp_path):
    rig = make_rig(tmp_path, Config(geocode_user_agent="   ", geocode_contact="مشغل"), handler=router({}))
    rig.geo.lookup(EXACT_Q, "Dubai")
    agent = rig.session.calls[0]["headers"]["User-Agent"]
    assert agent == "uae-real-estate-scraper/1.0"
    agent.encode("ascii")


def test_source_has_none_of_the_transport_hazards():
    source = Path(gc.__file__).read_text(encoding="utf-8")
    assert "verify=" not in source
    assert "mozilla" not in source.lower()
    assert not re.search(r"(?m)^\s*print\(", source)


# ---------------------------------------------------------------------------
# Pacing (fake clock)
# ---------------------------------------------------------------------------
def test_nominatim_is_paced_at_four_requests_a_minute(tmp_path):
    rig = make_rig(tmp_path, Config(), handler=router({}))
    for i in range(3):
        rig.geo.lookup(q(f"Pace {i}"), "Dubai")
    assert rig.clock.sleeps == [pytest.approx(15.0), pytest.approx(15.0)]


def test_pacing_counts_from_the_request_start_not_from_the_previous_sleep(tmp_path):
    clock = FakeClock()
    rig = _rig_with_clock(tmp_path, clock, Config(), router({}, clock=clock, step=0.4))
    rig.geo.lookup(q("Pace A"), "Dubai")
    rig.geo.lookup(q("Pace B"), "Dubai")
    assert clock.sleeps == [pytest.approx(gc.BACKEND_SETTINGS["nominatim"].delay - 0.4)]


def _rig_with_clock(tmp_path: Path, clock: FakeClock, cfg: Config, handler) -> Rig:
    session = FakeSession(handler=handler)
    cache_file = tmp_path / "clocked.json"
    geo = Geocoder(cfg, session=session, cache_file=cache_file, sleep=clock.sleep, clock=clock.time)
    return Rig(geo, session, clock, cache_file)


def test_no_wait_when_enough_time_has_already_passed(tmp_path):
    clock = FakeClock()
    slow = gc.BACKEND_SETTINGS["nominatim"].delay + 0.5  # each request already took longer than the delay
    rig = _rig_with_clock(tmp_path, clock, Config(), router({}, clock=clock, step=slow))
    rig.geo.lookup(q("Pace A"), "Dubai")
    rig.geo.lookup(q("Pace B"), "Dubai")
    assert clock.sleeps == []


@pytest.mark.parametrize("backend", KEYED_BACKENDS)
def test_each_backend_is_paced_at_its_own_delay(tmp_path, backend):
    rig = make_rig(tmp_path, cfg_for(backend), handler=router({}, empty=empty_payload(backend)))
    rig.geo.lookup(q("Pace A"), "Dubai")
    rig.geo.lookup(q("Pace B"), "Dubai")
    assert rig.clock.sleeps == [pytest.approx(gc.BACKEND_SETTINGS[backend].delay)]


def test_a_cached_answer_neither_waits_nor_requests(tmp_path):
    rig = make_rig(tmp_path, Config(), handler=router({}))
    rig.geo.lookup(EXACT_Q, "Dubai")
    rig.geo.lookup(EXACT_Q, "Dubai")
    assert len(rig.session.calls) == 1
    assert rig.clock.sleeps == []


def test_default_sleep_and_clock_are_the_real_ones():
    geo = Geocoder(Config(), cache_file=Path("unused.json"), use_cache=False)
    assert geo.sleep is _REAL_SLEEP
    assert geo.clock is time.monotonic


# ---------------------------------------------------------------------------
# Validation: one synthetic response per rejection reason
# ---------------------------------------------------------------------------
def _with(item: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**item, **changes}


def _without(item: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k not in keys}


OSM_REJECTIONS = [
    ("lat-not-a-number", _with(osm_place(*DOWNTOWN), lat="north"), "Dubai", "bad-coordinates"),
    ("lon-missing", _without(osm_place(*DOWNTOWN), "lon"), "Dubai", "bad-coordinates"),
    ("lat-missing", _without(osm_place(*DOWNTOWN), "lat"), "Dubai", "bad-coordinates"),
    ("lat-none", _with(osm_place(*DOWNTOWN), lat=None), "Dubai", "bad-coordinates"),
    ("lat-empty", _with(osm_place(*DOWNTOWN), lat=""), "Dubai", "bad-coordinates"),
    ("decimal-comma", _with(osm_place(*DOWNTOWN), lat="25,1972"), "Dubai", "bad-coordinates"),
    ("exponent-notation", _with(osm_place(*DOWNTOWN), lat="2.51972e1"), "Dubai", "bad-coordinates"),
    ("underscore-digits", _with(osm_place(*DOWNTOWN), lat="2_5.19"), "Dubai", "bad-coordinates"),
    ("arabic-indic-digits", _with(osm_place(*DOWNTOWN), lat="٢٥.١٩٧٢"), "Dubai", "bad-coordinates"),
    ("nan", _with(osm_place(*DOWNTOWN), lat="nan"), "Dubai", "bad-coordinates"),
    ("infinity", _with(osm_place(*DOWNTOWN), lon="inf"), "Dubai", "bad-coordinates"),
    ("latitude-out-of-range", _with(osm_place(*DOWNTOWN), lat="125.0"), "Dubai", "bad-coordinates"),
    ("longitude-out-of-range", _with(osm_place(*DOWNTOWN), lon="-181"), "Dubai", "bad-coordinates"),
    ("boolean-latitude", _with(osm_place(*DOWNTOWN), lat=True), "Dubai", "bad-coordinates"),
    ("list-latitude", _with(osm_place(*DOWNTOWN), lat=[25.1]), "Dubai", "bad-coordinates"),
    ("integer-too-big-for-a-float", _with(osm_place(*DOWNTOWN), lat=10**400), "Dubai", "bad-coordinates"),
    ("five-thousand-digit-text", _with(osm_place(*DOWNTOWN), lat="9" * 5000), "Dubai", "bad-coordinates"),
    ("wrong-country", osm_place(*MURCIA, state="Murcia", country="es"), "Dubai", "wrong-country"),
    ("wrong-country-upper-case", osm_place(*DOWNTOWN, country="ES"), "Dubai", "wrong-country"),
    ("street-in-dubai", osm_street(*DOWNTOWN), "Dubai", "street"),
    ("street-far-away-in-the-right-emirate", osm_street(*KALBA, state="Sharjah Emirate"), "Sharjah", "street"),
    ("emirate-boundary-by-rank", osm_place(*DOWNTOWN, cls="boundary", typ="administrative", rank=8, addresstype="state",
                                           display="Dubai, United Arab Emirates"), "Dubai", "emirate-or-country"),
    ("emirate-boundary-without-rank", _without(osm_place(*DOWNTOWN, cls="boundary", typ="administrative", addresstype="county",
                                                         display="Dubai Emirate, United Arab Emirates"), "place_rank"),
     "Dubai", "emirate-or-country"),
    ("emirate-named-by-state-type", _with(osm_place(*DOWNTOWN), type="state", place_rank=30), "Dubai", "emirate-or-country"),
    ("address-with-only-state-and-country", _with(_without(osm_place(*DOWNTOWN, cls="place", typ="yes"), "place_rank"),
                                                  address={"state": "Dubai", "country": "United Arab Emirates", "country_code": "ae"},
                                                  display_name="Somewhere, United Arab Emirates"), "Dubai", "emirate-or-country"),
    ("whole-country", osm_place(*UAE_CENTRE, cls="boundary", typ="administrative", rank=4, addresstype="country",
                                display="United Arab Emirates"), "Dubai", "emirate-or-country"),
    ("country-by-type", _with(osm_place(*UAE_CENTRE), type="country"), "Dubai", "emirate-or-country"),
    ("provider-state-is-the-other-emirate", osm_place(*DOWNTOWN, state="Sharjah Emirate"), "Dubai", "wrong-state"),
    ("provider-state-dubai-for-a-sharjah-project", osm_place(*SHARJAH_ALJADA, state="Dubai"), "Sharjah", "wrong-state"),
    ("provider-state-unrecognised-emirate", osm_place(*DOWNTOWN, state="Abu Dhabi Emirate"), "Dubai", "wrong-state"),
    ("provider-state-gibberish", osm_place(*DOWNTOWN, state="???"), "Dubai", "wrong-state"),
    ("outside-the-polygon-though-the-state-agrees", osm_place(*ABU_DHABI, state="Dubai"), "Dubai", "outside-emirate"),
    ("ajman-point-no-state", osm_place(*AJMAN, state=""), "Sharjah", "outside-emirate"),
    ("sharjah-point-for-a-dubai-project-no-state", osm_place(*SHARJAH_ALJADA, state=""), "Dubai", "outside-emirate"),
    ("dubai-point-for-a-sharjah-project-no-state", osm_place(*DOWNTOWN, state=""), "Sharjah", "outside-emirate"),
    ("open-water-at-null-island", osm_place(*NULL_ISLAND, state=""), "Dubai", "outside-emirate"),
]


@pytest.mark.parametrize("backend", ["nominatim", "locationiq"])
@pytest.mark.parametrize("label,item,declared,reason", OSM_REJECTIONS, ids=[c[0] for c in OSM_REJECTIONS])
def test_osm_shaped_hit_is_rejected_for_each_reason(tmp_path, backend, label, item, declared, reason):
    rig = make_rig(tmp_path, cfg_for(backend), replies=[FakeResponse(200, [item])])
    outcome = rig.geo.lookup_detailed(q("Test Residences A", declared), declared)
    assert outcome.hit is None
    assert outcome.rejection == reason
    assert rig.geo.rejections == {reason: 1}
    assert rig.geo.lookup(q("Test Residences A", declared), declared) is None  # and lookup() says miss


OSM_ACCEPTED = [
    ("state-dubai", osm_place(*DOWNTOWN), "Dubai"),
    ("state-dubai-emirate", osm_place(*DOWNTOWN, state="Dubai Emirate"), "Dubai"),
    ("state-dubayy", osm_place(*DOWNTOWN, state="Dubayy"), "Dubai"),
    ("state-with-country-suffix", osm_place(*DOWNTOWN, state="Dubai - United Arab Emirates"), "Dubai"),
    ("state-in-arabic", osm_place(*DOWNTOWN, state="دبي"), "Dubai"),
    ("no-state-at-all", osm_place(*DOWNTOWN, state=""), "Dubai"),
    ("sharjah-emirate", osm_place(*SHARJAH_ALJADA, state="Sharjah Emirate"), "Sharjah"),
    ("sharjah-in-arabic", osm_place(*SHARJAH_ALJADA, state="الشارقة"), "Sharjah"),
    ("sharjah-exclave", osm_place(*KALBA, state="Sharjah"), "Sharjah"),
    ("coordinates-as-floats", _with(osm_place(*DOWNTOWN), lat=DOWNTOWN[0], lon=DOWNTOWN[1]), "Dubai"),
    ("coordinates-padded-with-spaces", _with(osm_place(*DOWNTOWN), lat=" 25.1972 ", lon="55.2744\n"), "Dubai"),
    ("coordinates-with-a-plus-sign", _with(osm_place(*DOWNTOWN), lat="+25.1972"), "Dubai"),
    ("jsonv2-category-key-and-no-class", osm_place(*DOWNTOWN), "Dubai"),
    ("legacy-class-key", _with(_without(osm_place(*DOWNTOWN), "category"), **{"class": "building"}), "Dubai"),
]


@pytest.mark.parametrize("label,item,declared", OSM_ACCEPTED, ids=[c[0] for c in OSM_ACCEPTED])
def test_osm_shaped_hit_that_passes_every_check_is_accepted(tmp_path, label, item, declared):
    rig = make_rig(tmp_path, replies=[FakeResponse(200, [item])])
    outcome = rig.geo.lookup_detailed(q("Test Residences A", declared), declared)
    assert outcome.rejection == ""
    assert outcome.hit is not None
    assert emirates.point_in_emirate(outcome.hit.lat, outcome.hit.lon, declared)
    assert outcome.hit.backend == "nominatim"
    assert outcome.hit.kind == "place"


def test_accepted_hit_carries_float_coordinates_state_and_query(tmp_path):
    rig = make_rig(tmp_path, replies=[FakeResponse(200, [osm_place(*DOWNTOWN, state="Dubai Emirate")])])
    hit = rig.geo.lookup(EXACT_Q, "Dubai")
    assert isinstance(hit.lat, float) and isinstance(hit.lon, float)
    assert (hit.lat, hit.lon) == pytest.approx(DOWNTOWN)
    assert hit.state == "Dubai"
    assert hit.query == EXACT_Q
    assert hit.from_cache is False


def test_a_district_is_returned_as_an_area_kind_not_a_place(tmp_path):
    rig = make_rig(tmp_path, replies=[FakeResponse(200, [osm_area(*DOWNTOWN)])])
    assert rig.geo.lookup(COMM_Q, "Dubai").kind == "area"


def test_only_the_providers_best_result_is_judged(tmp_path):
    rig = make_rig(tmp_path, replies=[FakeResponse(200, [osm_street(*DOWNTOWN), osm_place(*DOWNTOWN)])])
    assert rig.geo.lookup(EXACT_Q, "Dubai") is None
    assert rig.geo.rejections == {"street": 1}


GEOAPIFY_CASES = [
    ("street", geoapify_payload(*DOWNTOWN, result_type="street"), "Dubai", "street"),
    ("country", geoapify_payload(*UAE_CENTRE, result_type="country"), "Dubai", "emirate-or-country"),
    ("state", geoapify_payload(*DOWNTOWN, result_type="state"), "Dubai", "emirate-or-country"),
    ("wrong-state", geoapify_payload(*DOWNTOWN, state="Sharjah"), "Dubai", "wrong-state"),
    ("wrong-country", geoapify_payload(*MURCIA, state="Murcia", country="es"), "Dubai", "wrong-country"),
    ("outside", geoapify_payload(*ABU_DHABI, state=""), "Dubai", "outside-emirate"),
    ("bad-coordinates", geoapify_payload(25.1, 55.2) | {"features": [{"properties": {"lat": "x", "lon": "y"}, "geometry": {}}]},
     "Dubai", "bad-coordinates"),
    ("no-properties-at-all", {"features": [{"geometry": {}}]}, "Dubai", "bad-coordinates"),
]


@pytest.mark.parametrize("label,payload,declared,reason", GEOAPIFY_CASES, ids=[c[0] for c in GEOAPIFY_CASES])
def test_geoapify_hit_is_rejected_for_each_reason(tmp_path, label, payload, declared, reason):
    rig = make_rig(tmp_path, cfg_for("geoapify"), replies=[FakeResponse(200, payload)])
    outcome = rig.geo.lookup_detailed(q("Test Residences A", declared), declared)
    assert (outcome.hit, outcome.rejection) == (None, reason)


def test_geoapify_accepts_a_building_and_falls_back_to_the_geometry_for_coordinates(tmp_path):
    rig = make_rig(tmp_path, cfg_for("geoapify"), replies=[FakeResponse(200, geoapify_payload(*DOWNTOWN)),
                                                           FakeResponse(200, geoapify_payload(*BUSINESS_BAY, omit_latlon=True))])
    first = rig.geo.lookup(q("A"), "Dubai")
    second = rig.geo.lookup(q("B"), "Dubai")
    assert (first.lat, first.lon) == pytest.approx(DOWNTOWN)
    assert (second.lat, second.lon) == pytest.approx(BUSINESS_BAY)
    assert first.backend == "geoapify"


def test_geoapify_district_is_an_area_kind(tmp_path):
    rig = make_rig(tmp_path, cfg_for("geoapify"), replies=[FakeResponse(200, geoapify_payload(*DOWNTOWN, result_type="suburb"))])
    assert rig.geo.lookup(COMM_Q, "Dubai").kind == "area"


GOOGLE_CASES = [
    ("route", google_payload(*DOWNTOWN, types=("route",)), "Dubai", "street"),
    ("country", google_payload(*UAE_CENTRE, types=("country", "political")), "Dubai", "emirate-or-country"),
    ("emirate", google_payload(*DOWNTOWN, types=("administrative_area_level_1", "political")), "Dubai", "emirate-or-country"),
    ("partial-match", google_payload(*DOWNTOWN, partial=True), "Dubai", "partial-match"),
    ("wrong-state", google_payload(*DOWNTOWN, state="Sharjah"), "Dubai", "wrong-state"),
    ("wrong-country", google_payload(*MURCIA, state="Murcia", country="ES"), "Dubai", "wrong-country"),
    ("outside", google_payload(*AJMAN, state=""), "Sharjah", "outside-emirate"),
    ("bad-coordinates", {"status": "OK", "results": [{"geometry": {"location": {"lat": "x", "lng": None}}, "types": []}]},
     "Dubai", "bad-coordinates"),
]


@pytest.mark.parametrize("label,payload,declared,reason", GOOGLE_CASES, ids=[c[0] for c in GOOGLE_CASES])
def test_google_hit_is_rejected_for_each_reason(tmp_path, label, payload, declared, reason):
    rig = make_rig(tmp_path, cfg_for("google"), replies=[FakeResponse(200, payload)])
    outcome = rig.geo.lookup_detailed(q("Test Residences A", declared), declared)
    assert (outcome.hit, outcome.rejection) == (None, reason)


def test_google_accepts_an_establishment_and_marks_a_locality_as_an_area(tmp_path):
    rig = make_rig(tmp_path, cfg_for("google"), replies=[FakeResponse(200, google_payload(*DOWNTOWN)),
                                                         FakeResponse(200, google_payload(*DOWNTOWN, types=("locality", "political")))])
    assert rig.geo.lookup(q("A"), "Dubai").kind == "place"
    assert rig.geo.lookup(q("B"), "Dubai").kind == "area"


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_backend_turns_a_good_response_into_a_hit(tmp_path, backend):
    rig = make_rig(tmp_path, cfg_for(backend), replies=[FakeResponse(200, hit_payload(backend, *BUSINESS_BAY))])
    hit = rig.geo.lookup(EXACT_Q, "Dubai")
    assert (hit.lat, hit.lon) == pytest.approx(BUSINESS_BAY)
    assert hit.backend == backend


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_backend_treats_an_empty_answer_as_a_plain_miss(tmp_path, backend):
    rig = make_rig(tmp_path, cfg_for(backend), replies=[FakeResponse(200, empty_payload(backend))])
    outcome = rig.geo.lookup_detailed(EXACT_Q, "Dubai")
    assert (outcome.hit, outcome.rejection) == (None, "")
    assert rig.geo.rejections == {}


@pytest.mark.parametrize("state,expected", [
    ("Dubai", "Dubai"), ("dubai", "Dubai"), ("  DUBAI  ", "Dubai"), ("Dubai Emirate", "Dubai"), ("Emirate of Dubai", "Dubai"),
    ("Dubayy", "Dubai"), ("Dubai - United Arab Emirates", "Dubai"), ("Dubai, UAE", "Dubai"), ("دبي", "Dubai"), ("إمارة دبي", "Dubai"),
    ("Sharjah", "Sharjah"), ("Sharjah Emirate", "Sharjah"), ("Emirate of Sharjah", "Sharjah"), ("Ash Shariqah", "Sharjah"),
    ("الشارقة", "Sharjah"), ("إمارة الشارقة", "Sharjah"),
    ("Ajman", None), ("Abu Dhabi", None), ("Abu Dhabi Emirate", None), ("United Arab Emirates", None), ("", None), ("   ", None),
    ("Dubai Sharjah", None), (None, None), (12, None), (["Dubai"], None), ("???", None),
])
def test_emirate_from_state(state, expected):
    assert emirate_from_state(state) == expected


# ---------------------------------------------------------------------------
# lookup / lookup_detailed contract
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [None, "", "   ", "\n\t", 5, 2.5, [], {}, b"bytes"])
def test_an_empty_or_non_text_query_sends_nothing(tmp_path, bad):
    rig = make_rig(tmp_path)
    outcome = rig.geo.lookup_detailed(bad, "Dubai")
    assert outcome.hit is None and outcome.note == "empty-query"
    assert rig.geo.lookup(bad, "Dubai") is None
    assert rig.session.calls == []


def test_a_huge_query_is_refused_locally(tmp_path):
    rig = make_rig(tmp_path)
    outcome = rig.geo.lookup_detailed("Tower " * 5000, "Dubai")
    assert outcome.hit is None and outcome.note == "query-too-long"
    assert rig.session.calls == []


@pytest.mark.parametrize("emirate", ["Abu Dhabi", "", None, 7, "Ajman"])
def test_an_unsupported_emirate_is_a_programming_error(tmp_path, emirate):
    rig = make_rig(tmp_path)
    with pytest.raises(ValueError):
        rig.geo.lookup(EXACT_Q, emirate)
    assert rig.session.calls == []


def test_a_bad_emirate_error_names_the_type_never_the_value(tmp_path):
    rig = make_rig(tmp_path)
    with pytest.raises(ValueError) as lookup_err:
        rig.geo.lookup(EXACT_Q, "MARKER-Atlantis Residences")
    with pytest.raises(ValueError) as centroid_err:
        rig.geo.centroid_for("MARKER-Atlantis Residences", ["Example Community"])
    for err in (lookup_err, centroid_err):
        assert "MARKER" not in str(err.value) and "Atlantis" not in str(err.value)
        assert "Dubai or Sharjah" in str(err.value) and "str" in str(err.value)


def test_emirate_spelling_is_normalised(tmp_path):
    rig = make_rig(tmp_path, replies=[FakeResponse(200, [osm_place(*DOWNTOWN)])])
    assert rig.geo.lookup(EXACT_Q, " dubai ") is not None


def test_unicode_and_arabic_queries_are_sent_unchanged(tmp_path):
    arabic = "الخليج التجاري, Dubai, UAE"
    accented = "Café Résidences,  Dubai, UAE"
    rig = make_rig(tmp_path, handler=router({}))
    rig.geo.lookup(arabic, "Dubai")
    rig.geo.lookup(accented, "Dubai")
    assert rig.queries == [arabic, "Café Résidences, Dubai, UAE"]


def test_arabic_provider_text_survives_the_cache_round_trip(tmp_path):
    item = osm_place(*DOWNTOWN, display="برج الاختبار, دبي, الإمارات العربية المتحدة")
    first = make_rig(tmp_path, replies=[FakeResponse(200, [item])])
    first.geo.lookup(EXACT_Q, "Dubai")
    first.geo.save_cache()
    second = make_rig(tmp_path)
    hit = second.geo.lookup(EXACT_Q, "Dubai")
    assert hit.name.startswith("برج الاختبار")
    assert second.session.calls == []


# ---------------------------------------------------------------------------
# Tier walking, precision honesty and the centroid rule
# ---------------------------------------------------------------------------
def test_exact_hit_near_the_community_centroid_is_filed_as_exact(tmp_path):
    rig = make_rig(tmp_path, handler=router({EXACT_Q: [osm_place(*BUSINESS_BAY)], COMM_Q: [osm_area(*DOWNTOWN)]}))
    [out], stats = run([mk_row()], rig)
    assert out.location_precision == "exact"
    assert out.geocode_status == "resolved via nominatim exact 2026-10-25"
    assert out.geocode_method == "nominatim"
    assert (out.latitude, out.longitude) == pytest.approx(BUSINESS_BAY)
    assert rig.queries == [EXACT_Q, COMM_Q]
    assert stats.lookups == 2 and stats.by_precision == {"exact": 1, "community": 0, "area": 0}


def test_exact_hit_beyond_eight_km_of_the_centroid_is_a_miss_and_the_row_falls_to_the_community_tier(tmp_path):
    rig = make_rig(tmp_path, handler=router({EXACT_Q: [osm_place(*MARINA)], COMM_Q: [osm_area(*DOWNTOWN)]}))
    [out], stats = run([mk_row()], rig)
    assert out.location_precision == "community"
    assert (out.latitude, out.longitude) == pytest.approx(DOWNTOWN)
    assert stats.rejections == {"far-from-community": 1}
    assert rig.queries == [EXACT_Q, COMM_Q], "the community query was already answered for the centroid and is reused from the cache"
    assert stats.cache_hits == 1


@pytest.mark.parametrize("point,precision", [(NEAR_8KM, "exact"), (FAR_8KM, "community")])
def test_the_centroid_gate_sits_at_eight_kilometres(tmp_path, point, precision):
    rig = make_rig(tmp_path, handler=router({EXACT_Q: [osm_place(*point)], COMM_Q: [osm_area(*DOWNTOWN)]}))
    [out], _ = run([mk_row()], rig)
    assert out.location_precision == precision


def test_the_81_km_street_story_a_right_emirate_hit_far_from_its_community_is_rejected(tmp_path):
    row = mk_row("Test Residences A", emirate="Sharjah", community="Example Community", area="")
    table = {q("Test Residences A", "Sharjah"): [osm_place(*KALBA, state="Sharjah Emirate")],
             q("Example Community", "Sharjah"): [osm_area(*SHARJAH_ALJADA, state="Sharjah Emirate")]}
    rig = make_rig(tmp_path, handler=router(table))
    [out], stats = run([row], rig)
    assert stats.rejections == {"far-from-community": 1}
    assert out.location_precision == "community"
    assert (out.latitude, out.longitude) == pytest.approx(SHARJAH_ALJADA)


def test_exact_hit_with_a_declared_but_unobtainable_centroid_is_rejected(tmp_path):
    rig = make_rig(tmp_path, handler=router({EXACT_Q: [osm_place(*BUSINESS_BAY)]}))
    [out], stats = run([mk_row()], rig)
    assert stats.rejections == {"no-centroid": 1}
    assert out.latitude is None and out.longitude is None and out.location_precision == ""
    assert out.geocode_status == "unresolved 2026-10-25"
    assert rig.queries == [EXACT_Q, COMM_Q, AREA_Q], "both declared places were tried for a centroid, nothing was asked twice"


def test_a_centroid_that_fails_validation_does_not_count_as_known(tmp_path):
    table = {EXACT_Q: [osm_place(*BUSINESS_BAY)], COMM_Q: [osm_area(*ABU_DHABI, state="Dubai")], AREA_Q: [osm_street(*DOWNTOWN)]}
    rig = make_rig(tmp_path, handler=router(table))
    [out], stats = run([mk_row()], rig)
    assert stats.rejections["no-centroid"] == 1
    assert out.latitude is None


def test_the_area_supplies_the_centroid_when_there_is_no_master_project(tmp_path):
    row = mk_row(community="")
    assert [t for t, _ in row.geocode_tiers()] == ["exact", "area"]
    rig = make_rig(tmp_path, handler=router({EXACT_Q: [osm_place(*BUSINESS_BAY)], AREA_Q: [osm_area(*DOWNTOWN)]}))
    [out], _ = run([row], rig)
    assert out.location_precision == "exact"
    assert rig.queries == [EXACT_Q, AREA_Q]


def test_the_area_supplies_the_centroid_when_the_master_project_is_unknown_to_the_provider(tmp_path):
    table = {EXACT_Q: [osm_place(*BUSINESS_BAY)], AREA_Q: [osm_area(*DOWNTOWN)]}
    rig = make_rig(tmp_path, handler=router(table))
    [out], _ = run([mk_row()], rig)
    assert out.location_precision == "exact"
    assert rig.queries == [EXACT_Q, COMM_Q, AREA_Q]


def test_the_master_project_is_preferred_over_the_area_for_the_centroid(tmp_path):
    table = {EXACT_Q: [osm_place(*BUSINESS_BAY)], COMM_Q: [osm_area(*DOWNTOWN)], AREA_Q: [osm_area(*MARINA)]}
    rig = make_rig(tmp_path, handler=router(table))
    [out], _ = run([mk_row()], rig)
    assert out.location_precision == "exact"
    assert AREA_Q not in rig.queries


def test_no_declared_community_or_area_means_no_gate_and_the_exact_hit_stands(tmp_path):
    row = mk_row(community="", area="")
    rig = make_rig(tmp_path, handler=router({EXACT_Q: [osm_place(*MARINA)]}))
    [out], stats = run([row], rig)
    assert out.location_precision == "exact"
    assert rig.queries == [EXACT_Q]
    assert stats.rejected == 0


def test_the_centroid_is_looked_up_once_per_community_per_run_without_any_cache(tmp_path):
    rows = [mk_row("Test Residences A"), mk_row("Test Residences B")]
    table = {q("Test Residences A"): [osm_place(*BUSINESS_BAY)], q("Test Residences B"): [osm_place(*BUSINESS_BAY)],
             COMM_Q: [osm_area(*DOWNTOWN)]}  # B is not ON the community centroid: that is the is-community-centroid rejection
    rig = make_rig(tmp_path, handler=router(table), use_cache=False)
    out, stats = run(rows, rig)
    assert [r.location_precision for r in out] == ["exact", "exact"]
    assert rig.queries == [q("Test Residences A"), COMM_Q, q("Test Residences B")]
    assert stats.cache_hits == 0


def test_a_not_found_centroid_is_remembered_for_the_run_too(tmp_path):
    rows = [mk_row("Test Residences A"), mk_row("Test Residences B")]
    table = {q("Test Residences A"): [osm_place(*BUSINESS_BAY)], q("Test Residences B"): [osm_place(*BUSINESS_BAY)]}
    rig = make_rig(tmp_path, handler=router(table), use_cache=False)
    out, stats = run(rows, rig)
    assert stats.rejections == {"no-centroid": 2}
    assert rig.queries.count(COMM_Q) == 3 and rig.queries.count(AREA_Q) == 3  # community and area tiers still run per row
    assert rig.queries.count(q("Test Residences B")) == 1


def test_a_hit_that_is_a_whole_district_is_never_an_exact_pin(tmp_path):
    row = mk_row(community="", area="")
    rig = make_rig(tmp_path, handler=router({EXACT_Q: [osm_area(*DOWNTOWN)]}))
    [out], stats = run([row], rig)
    assert stats.rejections == {"coarse-for-exact": 1}
    assert out.latitude is None and out.location_precision == ""


def test_a_district_hit_for_the_project_name_falls_to_the_community_tier_and_is_filed_as_community(tmp_path):
    table = {EXACT_Q: [osm_area(*DOWNTOWN)], COMM_Q: [osm_area(*DOWNTOWN)]}
    rig = make_rig(tmp_path, handler=router(table))
    [out], stats = run([mk_row()], rig)
    assert out.location_precision == "community"
    assert stats.rejections == {"coarse-for-exact": 1}


def test_a_project_named_like_its_community_is_filed_as_community_never_exact(tmp_path):
    row = mk_row("Example Community", community="Example Community")
    assert "exact" not in [t for t, _ in row.geocode_tiers()]
    rig = make_rig(tmp_path, handler=router({COMM_Q: [osm_place(*DOWNTOWN)]}))
    [out], _ = run([row], rig)
    assert out.location_precision == "community"


def test_area_precision_comes_from_the_area_tier(tmp_path):
    rig = make_rig(tmp_path, handler=router({AREA_Q: [osm_area(*DOWNTOWN)]}))
    [out], stats = run([mk_row()], rig)
    assert out.location_precision == "area"
    assert out.geocode_status == "resolved via nominatim area 2026-10-25"
    assert stats.by_precision == {"exact": 0, "community": 0, "area": 1}


def test_a_rejected_exact_hit_falls_to_the_next_tier(tmp_path):
    table = {EXACT_Q: [osm_street(*DOWNTOWN)], COMM_Q: [osm_area(*DOWNTOWN)]}
    rig = make_rig(tmp_path, handler=router(table))
    [out], stats = run([mk_row()], rig)
    assert out.location_precision == "community"
    assert stats.rejections == {"street": 1}


def test_a_second_query_variant_in_a_tier_is_tried_when_the_first_misses(tmp_path):
    row = mk_row("Test Residences A", developer="Example Developer LLC", community="", area="")
    queries = row.geocode_tiers()[0][1]
    assert len(queries) >= 2
    rig = make_rig(tmp_path, handler=router({queries[1]: [osm_place(*DOWNTOWN)]}))
    [out], _ = run([row], rig)
    assert out.location_precision == "exact"
    assert rig.queries == queries[:2]


def test_sharjah_rows_are_queried_with_the_sharjah_qualifier_and_checked_against_sharjah(tmp_path):
    row = mk_row("Test Tower S", emirate="Sharjah", community="", area="")
    wrong = make_rig(tmp_path, handler=router({q("Test Tower S", "Sharjah"): [osm_place(*DOWNTOWN, state="Dubai")]}), cache_name="a.json")
    [out], stats = run([row], wrong)
    assert stats.rejections == {"wrong-state": 1} and out.latitude is None
    right = make_rig(tmp_path, handler=router({q("Test Tower S", "Sharjah"): [osm_place(*SHARJAH_ALJADA, state="Sharjah Emirate")]}),
                     cache_name="b.json")
    [out], _ = run([row], right)
    assert out.location_precision == "exact" and right.queries == [q("Test Tower S", "Sharjah")]


# ---------------------------------------------------------------------------
# Border check
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("point,emirate,state", [(AL_NAHDA_DUBAI, "Dubai", "Dubai"), (SHARJAH_MUWAILEH, "Sharjah", "Sharjah Emirate")])
def test_a_hit_within_1_5_km_of_the_other_emirate_is_flagged(tmp_path, point, emirate, state):
    row = mk_row("Test Residences B", emirate=emirate, community="", area="")
    rig = make_rig(tmp_path, handler=router({q("Test Residences B", emirate): [osm_place(*point, state=state)]}))
    [out], stats = run([row], rig)
    assert out.geocode_status == "resolved via nominatim exact 2026-10-25 border-check"
    assert stats.border_checks == 1
    assert out.latitude is not None, "a border-check pin is still a pin"


@pytest.mark.parametrize("point,emirate,state", [(MIRDIF, "Dubai", "Dubai"), (SHARJAH_ALJADA, "Sharjah", "Sharjah Emirate"),
                                                 (DOWNTOWN, "Dubai", "Dubai"), (KALBA, "Sharjah", "Sharjah Emirate")])
def test_a_hit_further_than_1_5_km_from_the_border_is_not_flagged(tmp_path, point, emirate, state):
    row = mk_row("Test Residences B", emirate=emirate, community="", area="")
    rig = make_rig(tmp_path, handler=router({q("Test Residences B", emirate): [osm_place(*point, state=state)]}))
    [out], stats = run([row], rig)
    assert out.geocode_status == "resolved via nominatim exact 2026-10-25"
    assert stats.border_checks == 0


def test_border_checks_are_counted_per_row(tmp_path):
    rows = [lone_row(1), lone_row(2), lone_row(3)]
    table = {lone_q(1): [osm_place(*AL_NAHDA_DUBAI)], lone_q(2): [osm_place(*AL_NAHDA_DUBAI)], lone_q(3): [osm_place(*MIRDIF)]}
    rig = make_rig(tmp_path, handler=router(table))
    out, stats = run(rows, rig)
    assert stats.border_checks == 2
    assert [r.geocode_status.endswith("border-check") for r in out] == [True, True, False]


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------
def override_for(row: RegistryRow, key: Optional[str] = None, **kw: Any) -> dict[str, GeocodeOverride]:
    key = key or row.project_key
    return {key: GeocodeOverride(project_key=key, latitude=kw.get("latitude", 25.2048), longitude=kw.get("longitude", 55.2708),
                                 precision=kw.get("precision", "exact"), note="SYNTHETIC example pin")}


def test_override_matched_on_the_registry_key(tmp_path):
    row = mk_row(number="1001")
    rig = make_rig(tmp_path, handler=router({}))
    [out], stats = run([row], rig, overrides=override_for(row, "dld:dubai:1001"))
    assert (out.latitude, out.longitude) == (25.2048, 55.2708)
    assert out.geocode_method == "override" and out.location_precision == "exact"
    assert out.geocode_status == "resolved via override 2026-10-25"
    assert stats.overrides_applied == 1
    assert rig.session.calls == []


def test_override_matched_on_the_name_key_when_the_row_also_has_a_registry_key(tmp_path):
    row = mk_row(number="1001", developer="Example Developer LLC")
    name_key = row.name_key()
    assert name_key and row.registry_key() and name_key != row.registry_key()
    [out], stats = run([row], make_rig(tmp_path, handler=router({})), overrides=override_for(row, name_key))
    assert out.geocode_method == "override" and stats.overrides_applied == 1


def test_override_matched_on_a_name_key_for_a_row_without_any_registry_id(tmp_path):
    row = RegistryRow(emirate="Dubai", registry="SEED", project_name_en="Test Residences A", developer="Example Developer LLC")
    assert row.registry_key() is None
    [out], _ = run([row], make_rig(tmp_path, handler=router({})), overrides=override_for(row))
    assert out.geocode_method == "override"


def test_override_keeps_a_blank_precision_blank(tmp_path):
    row = mk_row()
    [out], _ = run([row], make_rig(tmp_path), overrides=override_for(row, precision=""))
    assert out.location_precision == ""
    assert out.latitude is not None


@pytest.mark.parametrize("precision", ["exact", "community", "area"])
def test_override_precision_is_the_humans_own(tmp_path, precision):
    row = mk_row()
    [out], _ = run([row], make_rig(tmp_path), overrides=override_for(row, precision=precision))
    assert out.location_precision == precision


def test_override_map_pin_link_points_at_the_override(tmp_path):
    row = mk_row()
    [out], _ = run([row], make_rig(tmp_path), overrides=override_for(row))
    assert out.map_pin_link == "https://www.google.com/maps/search/?api=1&query=25.2048,55.2708"


def test_override_applies_even_when_geocoding_is_disabled(tmp_path):
    row = mk_row()
    rig = make_rig(tmp_path)
    out, stats = run([row, lone_row(2)], rig, Config(geocode_enabled=False), overrides=override_for(row))
    assert out[0].geocode_method == "override"
    assert out[1].geocode_status == "" and out[1].latitude is None
    assert stats.overrides_applied == 1 and stats.backend == "disabled"
    assert rig.session.calls == []


def test_disabled_run_never_even_builds_a_geocoder(tmp_path):
    out, stats = geocode_pending([mk_row()], {}, Config(geocode_enabled=False, geocode_backend="google"), today=TODAY)
    assert out[0].latitude is None and stats.attempted == 0


def test_override_applies_even_after_the_backend_aborts_and_is_never_overwritten(tmp_path):
    pinned = mk_row("Pinned Tower", community="", area="")
    other = lone_row(2)
    rig = make_rig(tmp_path, handler=router({lone_q(2): FakeResponse(429, text="slow down")}))
    out, stats = run([other, pinned], rig, overrides=override_for(pinned))
    assert stats.aborted == "HTTP 429"
    assert out[1].geocode_method == "override" and (out[1].latitude, out[1].longitude) == (25.2048, 55.2708)
    assert q("Pinned Tower") not in rig.queries


def test_override_wins_over_what_the_backend_would_say_and_replaces_older_coordinates(tmp_path):
    row = mk_row(latitude=25.1, longitude=55.2, location_precision="community", geocode_method="nominatim",
                 geocode_status="resolved via nominatim community 2026-09-01")
    rig = make_rig(tmp_path, handler=router({EXACT_Q: [osm_place(*MARINA)]}))
    [out], _ = run([row], rig, overrides=override_for(row))
    assert (out.latitude, out.longitude) == (25.2048, 55.2708)
    assert out.geocode_method == "override" and out.location_precision == "exact"
    assert rig.session.calls == []


def test_a_later_run_never_overwrites_an_override(tmp_path):
    row = mk_row()
    [pinned], _ = run([row], make_rig(tmp_path), overrides=override_for(row))
    rig = make_rig(tmp_path, cache_name="second.json", handler=lambda call: (_ for _ in ()).throw(UnexpectedRequest("queried")))
    [again], stats = run([pinned], rig, today=date(2026, 12, 1))
    assert again == pinned
    assert stats.attempted == 0


def test_applying_the_same_override_again_keeps_the_original_status_date(tmp_path):
    row = mk_row()
    overrides = override_for(row)
    [first], _ = run([row], make_rig(tmp_path), overrides=overrides)
    [second], stats = run([first], make_rig(tmp_path, cache_name="b.json"), overrides=overrides, today=date(2026, 12, 1))
    assert second.geocode_status == "resolved via override 2026-10-25"
    assert stats.overrides_applied == 1


def test_overrides_do_not_count_against_the_lookup_budget(tmp_path):
    pinned, plain = mk_row("Pinned Tower", community="", area=""), lone_row(2)
    rig = make_rig(tmp_path, Config(geocode_max_per_run=1), handler=router({lone_q(2): [osm_place(*DOWNTOWN)]}))
    out, stats = run([pinned, plain], rig, Config(geocode_max_per_run=1), overrides=override_for(pinned))
    assert out[0].geocode_method == "override" and out[1].geocode_method == "nominatim"
    assert stats.lookups == 1 and stats.skipped_over_budget == 0


def test_an_override_with_no_matching_row_is_ignored_and_reported(tmp_path, caplog):
    stray = {"dld:dubai:999999": GeocodeOverride(project_key="dld:dubai:999999", latitude=25.2, longitude=55.2)}
    with caplog.at_level(logging.INFO, logger="src.geocoder"):
        out, stats = run([lone_row(1)], make_rig(tmp_path, handler=router({})), overrides=stray)
    assert stats.overrides_applied == 0 and out[0].geocode_method != "override"
    assert "matched no registry row" in caplog.text


def test_a_mapping_value_that_is_not_an_override_is_ignored_not_fatal(tmp_path):
    row = mk_row()
    out, stats = run([row], make_rig(tmp_path, handler=router({})), overrides={row.project_key: {"latitude": 1, "longitude": 2}})
    assert stats.overrides_applied == 0 and out[0].geocode_method != "override"


def test_an_override_outside_its_emirate_is_applied_with_a_warning(tmp_path, caplog):
    row = mk_row()
    with caplog.at_level(logging.WARNING, logger="src.geocoder"):
        [out], _ = run([row], make_rig(tmp_path), overrides=override_for(row, latitude=24.4539, longitude=54.3773))
    assert (out.latitude, out.longitude) == (24.4539, 54.3773)
    assert "outside" in caplog.text and "1 override pin(s)" in caplog.text
    assert "dld:" not in caplog.text  # a key is row content; the public log gets the count


def test_two_rows_sharing_a_key_both_get_the_pin(tmp_path):
    a, b = mk_row(number="1001"), mk_row(number="1001", name="Test Residences A")
    out, stats = run([a, b], make_rig(tmp_path), overrides=override_for(a))
    assert [r.geocode_method for r in out] == ["override", "override"] and stats.overrides_applied == 2


def test_google_without_a_key_still_applies_overrides_before_failing_loudly(tmp_path):
    row = mk_row()
    flushed: list[list[RegistryRow]] = []
    with pytest.raises(GeocoderConfigError):
        geocode_pending([row], override_for(row), Config(geocode_backend="google"), today=TODAY, flush=flushed.append)
    assert len(flushed) == 1 and flushed[0][0].geocode_method == "override"


# ---------------------------------------------------------------------------
# geocode_pending: rows, purity, bookkeeping
# ---------------------------------------------------------------------------
def test_input_rows_are_never_mutated_and_the_order_is_kept(tmp_path):
    rows = [lone_row(i) for i in range(4)] + [mk_row("Has Pin", latitude=25.2, longitude=55.3, location_precision="exact")]
    before = [r.model_dump() for r in rows]
    table = {lone_q(0): [osm_place(*DOWNTOWN)], lone_q(2): [osm_place(*BUSINESS_BAY)]}
    out, _ = run(rows, make_rig(tmp_path, handler=router(table)), overrides=override_for(rows[1]))
    assert [r.model_dump() for r in rows] == before
    assert all(o is not r for o, r in zip(out, rows))
    assert [r.project_name_en for r in out] == [r.project_name_en for r in rows]
    assert len(out) == len(rows)


def test_a_run_is_deterministic_given_the_same_inputs_and_responses(tmp_path):
    rows = [lone_row(i) for i in range(6)]
    table = {lone_q(i): [osm_place(*(DOWNTOWN if i % 2 else BUSINESS_BAY))] for i in range(5)}
    a, stats_a = run(rows, make_rig(tmp_path, handler=router(table), cache_name="a.json"))
    b, stats_b = run(rows, make_rig(tmp_path, handler=router(table), cache_name="b.json"))
    assert [r.model_dump() for r in a] == [r.model_dump() for r in b]
    assert stats_a.as_log_dict() == stats_b.as_log_dict()


def test_rows_may_be_any_iterable(tmp_path):
    out, stats = run((lone_row(i) for i in range(2)), make_rig(tmp_path, handler=router({})))
    assert len(out) == 2 and stats.attempted == 2


def test_empty_input_is_fine_and_flushes_once(tmp_path):
    flushed = []
    out, stats = run([], make_rig(tmp_path), flush=flushed.append)
    assert out == [] and stats.attempted == 0 and flushed == [[]]


def test_wrong_types_fail_loudly(tmp_path):
    rig = make_rig(tmp_path)
    with pytest.raises(TypeError, match="row 1"):
        run([lone_row(1), {"Project Name (EN)": "x"}], rig)
    with pytest.raises(TypeError):
        run([lone_row(1)], rig, overrides=["not", "a", "mapping"])
    with pytest.raises(TypeError):
        geocode_pending([lone_row(1)], {}, Config(), today="2026-10-25", geocoder=rig.geo)
    assert rig.session.calls == []


def test_a_datetime_today_is_accepted_as_its_date(tmp_path):
    [out], _ = run([lone_row(1)], make_rig(tmp_path, handler=router({lone_q(1): [osm_place(*DOWNTOWN)]})), today=datetime(2026, 10, 25, 23, 59))
    assert out.geocode_status == "resolved via nominatim exact 2026-10-25"


def test_rows_that_already_have_coordinates_are_left_alone(tmp_path):
    pinned = lone_row(1, latitude=25.2, longitude=55.3, location_precision="area", geocode_method="nominatim",
                      geocode_status="resolved via nominatim area 2026-01-01")
    rig = make_rig(tmp_path, handler=router({}))
    [out], stats = run([pinned], rig)
    assert out == pinned and rig.session.calls == [] and stats.attempted == 0


def test_a_row_with_nothing_to_query_is_left_untouched_and_counted(tmp_path):
    empty = RegistryRow(emirate="Dubai", registry="SEED")
    rig = make_rig(tmp_path)
    [out], stats = run([empty], rig)
    assert out == empty and stats.no_query == 1 and rig.session.calls == []


def test_an_unresolved_row_gets_a_dated_status_and_no_coordinates(tmp_path):
    [out], stats = run([lone_row(1)], make_rig(tmp_path, handler=router({})))
    assert out.geocode_status == "unresolved 2026-10-25"
    assert out.latitude is None and out.longitude is None
    assert out.location_precision == "" and out.geocode_method == ""
    assert "query=" in out.map_pin_link and "25." not in out.map_pin_link.split("query=")[1][:3]
    assert stats.unresolved == 1 and stats.resolved == 0


def test_a_resolved_row_gets_rounded_coordinates_method_status_and_pin_link(tmp_path):
    item = osm_place(25.18501234567, 55.26504567891)
    [out], _ = run([lone_row(1)], make_rig(tmp_path, handler=router({lone_q(1): [_with(item, lat="25.18501234567", lon="55.26504567891")]})))
    assert (out.latitude, out.longitude) == (25.185012, 55.265046)
    assert out.geocode_method == "nominatim"
    assert out.map_pin_link == out.build_map_pin_link()
    assert "25.185012,55.265046" in out.map_pin_link


def test_a_half_written_row_is_requeued_and_completed(tmp_path):
    half = lone_row(1, latitude=25.2, geocode_method="nominatim", geocode_status="resolved via nominatim exact 2026-10-01")
    assert not half.has_coordinates()
    [out], _ = run([half], make_rig(tmp_path, handler=router({lone_q(1): [osm_place(*DOWNTOWN)]})))
    assert out.has_coordinates() and out.latitude != 25.2


def test_a_half_written_row_that_cannot_be_resolved_drops_the_stray_value(tmp_path):
    half = lone_row(1, latitude=25.2, location_precision="exact")
    [out], _ = run([half], make_rig(tmp_path, handler=router({})))
    assert out.latitude is None and out.longitude is None and out.location_precision == ""


def test_duplicate_projects_each_get_a_pin_and_the_second_is_served_from_the_cache(tmp_path):
    rows = [lone_row(1), lone_row(1)]
    rig = make_rig(tmp_path, handler=router({lone_q(1): [osm_place(*DOWNTOWN)]}))
    out, stats = run(rows, rig)
    assert all(r.has_coordinates() for r in out)
    assert len(rig.session.calls) == 1 and stats.cache_hits == 1


def test_stats_log_dict_is_flat_json_and_complete(tmp_path):
    table = {lone_q(1): [osm_place(*DOWNTOWN)], lone_q(2): [osm_street(*DOWNTOWN)]}
    _, stats = run([lone_row(1), lone_row(2)], make_rig(tmp_path, handler=router(table)))
    flat = stats.as_log_dict()
    json.dumps(flat)
    assert all(isinstance(v, (int, str)) for v in flat.values())
    assert flat["backend"] == "nominatim" and flat["resolved"] == 1 and flat["unresolved"] == 1
    assert flat["resolved_exact"] == 1 and flat["resolved_community"] == 0 and flat["resolved_area"] == 0
    assert flat["rejected"] == 1 and flat["rejected_street"] == 1 and flat["aborted"] == ""
    assert {"attempted", "overrides_applied", "border_checks", "skipped_over_budget", "lookups", "cache_hits"} <= set(flat)


def test_stats_instances_do_not_share_their_precision_counters():
    a, b = GeocodeStats(backend="x"), GeocodeStats(backend="x")
    a.by_precision["exact"] += 1
    assert b.by_precision["exact"] == 0
    assert set(a.by_precision) == {"exact", "community", "area"}


# ---------------------------------------------------------------------------
# Queue order and cooldown
# ---------------------------------------------------------------------------
def test_never_tried_rows_go_first_then_the_stalest_failures_and_fresh_failures_wait(tmp_path):
    rows = [
        lone_row(0, geo_status="unresolved 2026-08-01"),
        lone_row(1),
        lone_row(2, geo_status="unresolved 2026-07-01"),
        lone_row(3, geo_status="unresolved 2026-10-20"),
        lone_row(4),
        lone_row(5, geo_status="unresolved 2026-09-25"),
        lone_row(6, geo_status="unresolved 2026-09-26"),
    ]
    rig = make_rig(tmp_path, handler=router({}))
    out, stats = run(rows, rig)
    assert rig.queries == [lone_q(1), lone_q(4), lone_q(2), lone_q(0), lone_q(5)]
    assert stats.deferred_cooldown == 2
    assert out[3].geocode_status == "unresolved 2026-10-20" and out[6].geocode_status == "unresolved 2026-09-26"
    assert out[5].geocode_status == "unresolved 2026-10-25"


def test_the_cooldown_boundary_is_inclusive_at_the_configured_days(tmp_path):
    rows = [lone_row(1, geo_status="unresolved 2026-09-25"), lone_row(2, geo_status="unresolved 2026-09-26")]
    rig = make_rig(tmp_path, handler=router({}))
    run(rows, rig, Config(geocode_retry_cooldown_days=30))
    assert rig.queries == [lone_q(1)]
    rig = make_rig(tmp_path, handler=router({}), cache_name="b.json")
    run(rows, rig, Config(geocode_retry_cooldown_days=7))
    assert rig.queries == [lone_q(1), lone_q(2)]


def test_a_zero_day_cooldown_retries_failures_from_today(tmp_path):
    rig = make_rig(tmp_path, handler=router({}))
    run([lone_row(1, geo_status="unresolved 2026-10-25")], rig, Config(geocode_retry_cooldown_days=0))
    assert rig.queries == [lone_q(1)]


@pytest.mark.parametrize("status", [
    "resolved via nominatim exact 2026-10-24", "unresolved", "unresolved yesterday", "unresolved 2026-13-45",
    "unresolved 2099-01-01", "something odd 2026-10-24", "UNRESOLVED",
])
def test_a_status_that_is_not_a_fresh_dated_failure_does_not_defer_a_row_without_coordinates(tmp_path, status):
    rig = make_rig(tmp_path, handler=router({}))
    run([lone_row(1, geo_status=status)], rig)
    assert rig.queries == [lone_q(1)]


def test_an_odd_status_goes_before_dated_failures_but_after_never_tried(tmp_path):
    rows = [lone_row(0, geo_status="unresolved 2026-01-01"), lone_row(1, geo_status="resolved via nominatim exact 2026-10-24"),
            lone_row(2)]
    rig = make_rig(tmp_path, handler=router({}))
    run(rows, rig)
    assert rig.queries == [lone_q(2), lone_q(1), lone_q(0)]


def test_priority_survives_a_tight_budget(tmp_path):
    rows = [lone_row(0, geo_status="unresolved 2026-01-01"), lone_row(1)]
    rig = make_rig(tmp_path, Config(geocode_max_per_run=1), handler=router({}))
    out, stats = run(rows, rig, Config(geocode_max_per_run=1))
    assert rig.queries == [lone_q(1)]
    assert out[0].geocode_status == "unresolved 2026-01-01"
    assert stats.attempted == 2, "the stale row was reached and interrupted, not skipped silently"
    assert stats.stop_reason == "lookup budget" and stats.aborted == "" and stats.unresolved == 1


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------
def test_the_lookup_budget_stops_the_run_and_leaves_the_rest_untried(tmp_path):
    rows = [lone_row(i) for i in range(5)]
    rig = make_rig(tmp_path, Config(geocode_max_per_run=2), handler=router({}))
    out, stats = run(rows, rig, Config(geocode_max_per_run=2))
    assert len(rig.session.calls) == 2 and stats.lookups == 2
    assert [r.geocode_status for r in out] == ["unresolved 2026-10-25"] * 2 + [""] * 3
    assert stats.attempted == 3, "attempted counts the row the budget interrupted"
    assert stats.skipped_over_budget == 2, "and skipped counts only rows never started"
    assert stats.stop_reason == "lookup budget"


def test_cached_answers_do_not_spend_the_budget(tmp_path):
    rows = [lone_row(i) for i in range(3)]
    warm = make_rig(tmp_path, handler=router({}))
    run(rows, warm)
    cold_rows = [lone_row(i) for i in range(3)]
    rig = make_rig(tmp_path, Config(geocode_max_per_run=1), handler=lambda call: (_ for _ in ()).throw(UnexpectedRequest("net")))
    out, stats = run(cold_rows, rig, Config(geocode_max_per_run=1))
    assert stats.lookups == 0 and stats.cache_hits == 3 and stats.skipped_over_budget == 0
    assert all(r.geocode_status == "unresolved 2026-10-25" for r in out)


def test_retries_of_one_lookup_count_once_against_the_budget(tmp_path):
    replies = [FakeResponse(503), FakeResponse(503), FakeResponse(200, [])]
    rig = make_rig(tmp_path, Config(geocode_max_per_run=1), replies=replies)
    rig.geo.begin_run(TODAY)
    assert rig.geo.lookup_detailed(lone_q(1), "Dubai").hit is None
    assert rig.geo.lookups_used == 1 and len(rig.session.calls) == 3


def test_the_time_budget_stops_the_run_between_rows(tmp_path):
    clock = FakeClock()
    step = gc.BACKEND_SETTINGS["nominatim"].delay + 1.0  # every request outlasts the pacing delay, so only the requests take time
    cfg = Config(geocode_time_budget_seconds=int(step * 2.5))  # two requests fit, the third overruns
    rig = _rig_with_clock(tmp_path, clock, cfg, router({}, clock=clock, step=step))
    out, stats = run([lone_row(i) for i in range(6)], rig, cfg)
    assert len(rig.session.calls) == 3
    assert stats.stop_reason == "time budget" and stats.aborted == ""
    assert stats.skipped_over_budget == 3
    assert [r.geocode_status for r in out] == ["unresolved 2026-10-25"] * 3 + [""] * 3


def test_a_clock_exactly_at_the_deadline_has_no_time_left(tmp_path):
    rig = make_rig(tmp_path, Config(), handler=router({}))
    rig.geo.begin_run(TODAY, time_budget_seconds=60)
    start = rig.clock.now
    rig.clock.now = start + 59.0
    assert not rig.geo.out_of_time()
    rig.clock.now = start + 60.0  # the boundary belongs to "out of time": >=, not >
    assert rig.geo.out_of_time()
    with pytest.raises(LookupInterrupted) as stop:
        rig.geo.lookup_detailed(EXACT_Q, "Dubai")
    assert stop.value.kind == "time" and rig.session.calls == [] and rig.geo.lookups_used == 0


def test_the_budget_running_out_during_a_retry_leaves_the_row_untried(tmp_path):
    # attempt 1 fails with a 503; the 2 s back-off carries the clock past the 1 s budget, so attempt 2 is not made
    rig = make_rig(tmp_path, Config(geocode_time_budget_seconds=1), replies=[FakeResponse(503), FakeResponse(200, [osm_place(*DOWNTOWN)])])
    rig.geo.delay = 0.0
    [out], stats = run([lone_row(1)], rig, Config(geocode_time_budget_seconds=1))
    assert len(rig.session.calls) == 1
    assert stats.stop_reason == "time budget" and stats.aborted == ""
    assert out.geocode_status == "" and not out.has_coordinates()  # never tried, not "unresolved": it keeps its place in the queue


def test_an_abort_after_the_exact_lookup_missed_leaves_the_row_blank_not_unresolved(tmp_path):
    rig = make_rig(tmp_path, handler=router({EXACT_Q: []}, empty=FakeResponse(429)))
    [out], stats = run([mk_row("Test Residences A")], rig)
    assert rig.queries == [EXACT_Q, COMM_Q]  # the exact query missed, then the community fallback was refused
    assert stats.aborted == "HTTP 429"
    assert out.geocode_status == "" and not out.has_coordinates()


def test_a_zero_time_budget_means_no_time_limit(tmp_path):
    cfg = Config(geocode_time_budget_seconds=0)
    clock = FakeClock()
    rig = _rig_with_clock(tmp_path, clock, cfg, router({}, clock=clock, step=10_000.0))
    _, stats = run([lone_row(i) for i in range(3)], rig, cfg)
    assert stats.attempted == 3 and stats.stop_reason == ""


def test_running_out_of_budget_mid_row_leaves_that_row_untried_not_unresolved(tmp_path):
    rows = [mk_row("Test Residences A"), mk_row("Test Residences B")]
    cfg = Config(geocode_max_per_run=1)
    rig = make_rig(tmp_path, cfg, handler=router({q("Test Residences A"): [osm_place(*BUSINESS_BAY)]}))
    out, stats = run(rows, rig, cfg)
    assert out[0].geocode_status == "" and out[0].latitude is None, "its centroid lookup was cut off, so the row is undecided"
    assert out[1].geocode_status == ""
    assert stats.stop_reason == "lookup budget" and stats.unresolved == 0 and stats.skipped_over_budget == 1


# ---------------------------------------------------------------------------
# 429 / quota: abort
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend", BACKENDS)
def test_a_429_aborts_the_run_without_retry_and_leaves_every_untried_row_blank(tmp_path, backend):
    table = {lone_q(0): hit_payload(backend, *DOWNTOWN), lone_q(1): hit_payload(backend, *BUSINESS_BAY),
             lone_q(2): FakeResponse(429, text="Too Many Requests")}
    rows = [lone_row(i) for i in range(5)]
    rig = make_rig(tmp_path, cfg_for(backend), handler=router(table, empty=empty_payload(backend)))
    out, stats = run(rows, rig, cfg_for(backend))
    assert stats.aborted == "HTTP 429"
    assert len(rig.session.calls) == 3, "no retry after a 429"
    assert [bool(r.latitude) for r in out] == [True, True, False, False, False]
    assert [r.geocode_status for r in out[2:]] == ["", "", ""], "the 429 row and the untried rows keep a blank status"
    assert stats.resolved == 2 and stats.unresolved == 0 and stats.skipped_after_abort == 2 and stats.attempted == 3
    assert lone_q(2) not in " ".join(rig.cache_entries()).lower(), "an abort is never cached as a miss"
    assert len(rig.cache_entries()) == 2


def test_a_429_on_the_first_row_caches_nothing_and_writes_no_cache_file(tmp_path):
    rig = make_rig(tmp_path, handler=router({}, empty=FakeResponse(429)))
    out, stats = run([lone_row(1), lone_row(2)], rig)
    assert stats.aborted == "HTTP 429" and out[0].geocode_status == "" and out[1].geocode_status == ""
    assert not rig.cache_file.exists()
    assert len(rig.session.calls) == 1


@pytest.mark.parametrize("status", [401, 402, 403])
def test_a_refused_key_or_plan_aborts_the_run(tmp_path, status):
    rig = make_rig(tmp_path, cfg_for("locationiq"), handler=router({}, empty=FakeResponse(status, text="denied")))
    out, stats = run([lone_row(1), lone_row(2)], rig, cfg_for("locationiq"))
    assert stats.aborted == f"HTTP {status}" and len(rig.session.calls) == 1
    assert all(r.geocode_status == "" for r in out)


@pytest.mark.parametrize("verdict", ["OVER_QUERY_LIMIT", "OVER_DAILY_LIMIT", "REQUEST_DENIED"])
def test_google_quota_and_denial_statuses_in_a_200_body_abort_the_run(tmp_path, verdict):
    rig = make_rig(tmp_path, cfg_for("google"), handler=router({}, empty=FakeResponse(200, {"status": verdict, "results": []})))
    out, stats = run([lone_row(1), lone_row(2)], rig, cfg_for("google"))
    assert stats.aborted == f"Google {verdict}"
    assert len(rig.session.calls) == 1 and all(r.geocode_status == "" for r in out)
    assert not rig.cache_file.exists()


def test_google_zero_results_is_a_cached_miss_and_unknown_error_is_retried(tmp_path):
    rig = make_rig(tmp_path, cfg_for("google"), replies=[FakeResponse(200, {"status": "UNKNOWN_ERROR", "results": []}),
                                                         FakeResponse(200, {"status": "ZERO_RESULTS", "results": []})])
    assert rig.geo.lookup(EXACT_Q, "Dubai") is None
    assert len(rig.session.calls) == 2
    assert rig.geo.lookup(EXACT_Q, "Dubai") is None and len(rig.session.calls) == 2


@pytest.mark.parametrize("status", ["INVALID_REQUEST", ["list"], 5, None])
def test_google_odd_statuses_never_crash(tmp_path, status):
    rig = make_rig(tmp_path, cfg_for("google"), handler=router({}, empty=FakeResponse(200, {"status": status, "results": []})))
    rig.geo.begin_run(TODAY)
    try:
        rig.geo.lookup_detailed(EXACT_Q, "Dubai")
    except LookupInterrupted as stop:
        assert stop.kind in ("inconclusive", "abort")


def test_only_a_quota_abort_counts_as_routine():
    for reason in ("HTTP 429", "Google OVER_QUERY_LIMIT", "Google OVER_DAILY_LIMIT"):
        assert gc.abort_is_quota(reason), reason
    for reason in ("HTTP 401", "HTTP 402", "HTTP 403", "Google REQUEST_DENIED", "5 consecutive backend failures", ""):
        assert not gc.abort_is_quota(reason), reason


@pytest.mark.parametrize("status, level", [(429, logging.WARNING), (403, logging.ERROR)])
def test_an_abort_is_logged_at_the_severity_main_acts_on(tmp_path, caplog, status, level):
    # A quota stop is routine (main.py exits 0); a refused key is not (main.py exits 1).
    rig = make_rig(tmp_path, replies=[FakeResponse(status)])
    rig.geo.begin_run(TODAY)
    with caplog.at_level(logging.DEBUG, logger=gc.logger.name), pytest.raises(LookupInterrupted):
        rig.geo.lookup_detailed(lone_q(1), "Dubai")
    records = [r for r in caplog.records if "aborted the run" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == level


def test_after_an_abort_nothing_more_is_requested_and_the_budget_reads_zero(tmp_path):
    rig = make_rig(tmp_path, replies=[FakeResponse(429)])
    rig.geo.begin_run(TODAY)
    with pytest.raises(LookupInterrupted) as err:
        rig.geo.lookup_detailed(lone_q(1), "Dubai")
    assert err.value.kind == "abort" and rig.geo.aborted == "HTTP 429"
    assert rig.geo.lookup(lone_q(2), "Dubai") is None
    assert len(rig.session.calls) == 1 and rig.geo.budget_remaining == 0


def test_begin_run_clears_an_earlier_abort(tmp_path):
    rig = make_rig(tmp_path, replies=[FakeResponse(429), FakeResponse(200, [osm_place(*DOWNTOWN)])])
    rig.geo.lookup(lone_q(1), "Dubai")
    assert rig.geo.aborted
    rig.geo.begin_run(TODAY)
    assert rig.geo.aborted == "" and rig.geo.lookup(lone_q(2), "Dubai") is not None


def test_flush_still_runs_after_an_abort(tmp_path):
    flushed = []
    rig = make_rig(tmp_path, handler=router({lone_q(0): [osm_place(*DOWNTOWN)]}, empty=FakeResponse(429)))
    out, stats = run([lone_row(i) for i in range(3)], rig, flush=flushed.append)
    assert len(flushed) == 1 and stats.aborted
    assert [r.has_coordinates() for r in flushed[0]] == [True, False, False]


# ---------------------------------------------------------------------------
# Public log: no provider text, no query, no project name
# ---------------------------------------------------------------------------
# The Actions log of a public repository is public. Only counts, backend names, HTTP status codes, a provider's
# fixed status token and fixed phrases may appear. The marker is not secret-shaped on purpose: no scrubber can know
# it, so only "never printed" keeps it out. Each path below also states what MUST stay (the evidence).
LEAK_MARKER = "zq-hostile-marker-4471"  # SYNTHETIC


def _assert_nothing_hostile(caplog, *extra: object) -> None:
    haystack = log_text(caplog) + " ".join(str(e) for e in extra)
    assert LEAK_MARKER not in haystack, f"provider text reached the log: {haystack[:400]}"
    assert "Test Residences" not in haystack, f"a project name reached the log: {haystack[:400]}"


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_429_body_never_reaches_the_log_the_stats_or_the_exception(tmp_path, caplog, backend):
    body = f"<html>Slow down. Your query was: {lone_q(1)} {LEAK_MARKER}</html>"
    rig = make_rig(tmp_path, cfg_for(backend), handler=router({}, empty=FakeResponse(429, text=body)))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        rig.geo.begin_run(TODAY)
        with pytest.raises(LookupInterrupted) as err:
            rig.geo.lookup_detailed(lone_q(1), "Dubai")
        _, stats = run([lone_row(1)], make_rig(tmp_path, cfg_for(backend), handler=router({}, empty=FakeResponse(429, text=body)),
                                              cache_name="b.json"), cfg_for(backend))
    _assert_nothing_hostile(caplog, err.value, err.value.reason, stats.aborted, stats.as_log_dict())
    # the quota-versus-failure decision (main.py: exit 0 or 1) is made from the status alone
    assert stats.aborted == "HTTP 429" and err.value.reason == "HTTP 429" and gc.abort_is_quota(stats.aborted)
    aborts = [r for r in caplog.records if "aborted the run" in r.getMessage()]
    assert aborts and all(r.levelno == logging.WARNING for r in aborts)
    assert "HTTP 429" in caplog.text and "rate limit or quota is spent" in caplog.text


@pytest.mark.parametrize("status", [401, 402, 403])
def test_a_refused_key_body_never_reaches_the_log_and_stays_a_failure(tmp_path, caplog, status):
    body = f"Forbidden for {lone_q(1)}: {LEAK_MARKER}"
    rig = make_rig(tmp_path, cfg_for("locationiq"), handler=router({}, empty=FakeResponse(status, text=body)))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        _, stats = run([lone_row(1)], rig, cfg_for("locationiq"))
    _assert_nothing_hostile(caplog, stats.aborted, stats.as_log_dict())
    assert stats.aborted == f"HTTP {status}" and not gc.abort_is_quota(stats.aborted)
    aborts = [r for r in caplog.records if "aborted the run" in r.getMessage()]
    assert aborts and all(r.levelno == logging.ERROR for r in aborts)
    assert f"HTTP {status}" in caplog.text and "check the API key" in caplog.text


@pytest.mark.parametrize("verdict, quota", [("OVER_QUERY_LIMIT", True), ("OVER_DAILY_LIMIT", True), ("REQUEST_DENIED", False)])
def test_googles_error_message_never_reaches_the_log_and_the_token_still_decides(tmp_path, caplog, verdict, quota):
    payload = {"status": verdict, "results": [], "error_message": f"You have exceeded your rate-limit for {lone_q(1)} {LEAK_MARKER}"}
    rig = make_rig(tmp_path, cfg_for("google"), handler=router({}, empty=FakeResponse(200, payload)))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        rig.geo.begin_run(TODAY)
        with pytest.raises(LookupInterrupted) as err:
            rig.geo.lookup_detailed(lone_q(1), "Dubai")
    _assert_nothing_hostile(caplog, err.value, err.value.reason)
    assert err.value.reason == f"Google {verdict}" and gc.abort_is_quota(err.value.reason) is quota
    aborts = [r for r in caplog.records if "aborted the run" in r.getMessage()]
    assert [r.levelno for r in aborts] == [logging.WARNING if quota else logging.ERROR]
    assert f"Google {verdict}" in caplog.text


def test_an_unrecognised_google_status_is_named_by_a_fixed_phrase_not_echoed(tmp_path, caplog):
    # A status string outside Google's documented list is the provider's own text: it can say anything.
    payload = {"status": f"{LEAK_MARKER} {lone_q(1)}", "results": []}
    rig = make_rig(tmp_path, cfg_for("google"), handler=router({}, empty=FakeResponse(200, payload)))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        _, stats = run([lone_row(i) for i in range(1, 8)], rig, cfg_for("google"))
    _assert_nothing_hostile(caplog, stats.aborted, stats.stop_reason, stats.as_log_dict())
    assert stats.aborted.endswith("consecutive backend failures")  # the breaker tripped, so its detail line was logged too
    assert "Google returned an unrecognised status" in caplog.text


def test_googles_documented_invalid_request_token_is_still_named(tmp_path, caplog):
    rig = make_rig(tmp_path, cfg_for("google"), handler=router({}, empty=FakeResponse(200, {"status": "INVALID_REQUEST", "results": []})))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        run([lone_row(1)], rig, cfg_for("google"))
    assert "refused a query (Google INVALID_REQUEST)" in caplog.text


@pytest.mark.parametrize("reply, evidence", [
    (FakeResponse(502, text=f"Bad gateway for {lone_q(1)} {LEAK_MARKER}"), "HTTP 502"),
    (FakeResponse(418, text=f"teapot {lone_q(1)} {LEAK_MARKER}"), "HTTP 418"),
    (FakeResponse(200, text=f"{lone_q(1)} {LEAK_MARKER}", bad_json=True), "response body was not JSON"),
    (RuntimeError(f"boom while fetching {lone_q(1)} {LEAK_MARKER}"), "RuntimeError"),
], ids=["5xx", "other-status", "bad-json", "transport"])
def test_a_failed_attempt_logs_the_status_or_class_but_never_the_text(tmp_path, caplog, reply, evidence):
    rig = make_rig(tmp_path, handler=router({}, empty=reply))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        _, stats = run([lone_row(i) for i in range(1, 8)], rig)
    _assert_nothing_hostile(caplog, stats.aborted, stats.stop_reason, stats.as_log_dict())
    assert evidence in caplog.text


def test_a_rejected_candidate_is_logged_by_its_reason_never_by_its_query(tmp_path, caplog):
    # lookup_detailed's own rejection: a street is not a project.
    query = q(f"Zzyzx {LEAK_MARKER}")
    rig = make_rig(tmp_path, replies=[FakeResponse(200, [osm_street(*DOWNTOWN)])])
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        assert rig.geo.lookup(query, "Dubai") is None
    assert "rejected a candidate (street)" in caplog.text and "nominatim" in caplog.text
    assert LEAK_MARKER not in log_text(caplog) and "Zzyzx" not in log_text(caplog)


def test_a_name_mismatch_in_the_row_pipeline_is_logged_by_its_reason_never_by_the_project_name(tmp_path, caplog):
    # geocode_pending's rejection (the exact hit's own name shares nothing with the project).
    name = f"Zzyzx {LEAK_MARKER}"
    row = mk_row(name, community="Example Community", area="")
    rig = make_rig(tmp_path, handler=router({COMM_Q: [osm_area(*DOWNTOWN)], q(name): [osm_place(*BUSINESS_BAY, name="Random Mosque Car Park")]}))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        _, stats = run([row], rig)
    assert stats.rejections.get("name-mismatch") == 1
    assert "rejected a candidate (name-mismatch)" in caplog.text
    assert LEAK_MARKER not in log_text(caplog) and "Zzyzx" not in log_text(caplog)


def test_a_failing_flush_logs_its_class_never_its_text(tmp_path, caplog):
    def broken_flush(rows):
        raise RuntimeError(f"sheet write failed for {lone_q(1)} {LEAK_MARKER} (row 'Test Residences 1')")

    rig = make_rig(tmp_path, handler=router({lone_q(1): [osm_place(*DOWNTOWN)]}))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        out, stats = run([lone_row(1)], rig, flush=broken_flush)
    _assert_nothing_hostile(caplog, stats.as_log_dict())
    assert out[0].has_coordinates() and stats.flush_failures == 1
    failures = [r for r in caplog.records if "flush failed" in r.getMessage()]
    assert failures and all("RuntimeError" in r.getMessage() for r in failures)


# ---------------------------------------------------------------------------
# Transient failures: 5xx, timeouts, odd bodies, circuit breaker
# ---------------------------------------------------------------------------
def test_5xx_is_retried_with_backoff_and_the_retry_succeeds(tmp_path):
    rig = make_rig(tmp_path, replies=[FakeResponse(503), FakeResponse(502), FakeResponse(200, [osm_place(*DOWNTOWN)])])
    rig.geo.delay = 0.0  # take the pacing wait out, so the sleeps are the backoff alone
    assert rig.geo.lookup(EXACT_Q, "Dubai") is not None
    assert len(rig.session.calls) == 3 and rig.geo.lookups_used == 1
    assert rig.clock.sleeps == [2.0, 5.0]


def test_pacing_still_holds_between_the_retries_of_one_lookup(tmp_path):
    rig = make_rig(tmp_path, replies=[FakeResponse(503), FakeResponse(502), FakeResponse(200, [osm_place(*DOWNTOWN)])])
    assert rig.geo.lookup(EXACT_Q, "Dubai") is not None
    delay = gc.BACKEND_SETTINGS["nominatim"].delay
    # a retry never lands sooner than `delay` after the request it follows: the backoff and the pacing wait add up to it
    assert rig.clock.sleeps == [2.0, delay - 2.0, 5.0, delay - 5.0]


@pytest.mark.parametrize("failure", [
    FakeResponse(500), FakeResponse(504, text="gateway"), requests.Timeout("timed out"), requests.ConnectionError("reset"),
    FakeResponse(200, bad_json=True, text="<html>half-written"), FakeResponse(200, {"unexpected": "shape"}),
    FakeResponse(200, [None]), FakeResponse(200, [{"lat": "1"}, "x"]), FakeResponse(200, None), FakeResponse(200, "text"),
    FakeResponse(200, {"error": "Unable to geocode"}),
], ids=lambda f: type(f).__name__ + str(getattr(f, "status_code", "")) + str(getattr(f, "_payload", ""))[:12])
def test_a_lookup_that_keeps_failing_is_inconclusive_not_a_miss_and_is_not_cached(tmp_path, failure):
    rig = make_rig(tmp_path, replies=[failure, failure, failure, FakeResponse(200, [osm_place(*DOWNTOWN)])])
    rig.geo.begin_run(TODAY)
    with pytest.raises(LookupInterrupted) as err:
        rig.geo.lookup_detailed(EXACT_Q, "Dubai")
    assert err.value.kind == "inconclusive"
    assert len(rig.session.calls) == 3 and rig.geo.transient_failures == 1
    assert rig.geo.lookup(EXACT_Q, "Dubai") is not None, "the failure was not remembered as an empty answer"
    assert len(rig.session.calls) == 4


def test_the_wait_between_attempts_is_bounded(tmp_path):
    rig = make_rig(tmp_path, replies=[FakeResponse(503)] * 3)
    rig.geo.delay = 0.0  # the bound is on the backoff; the pacing wait has its own test
    rig.geo.lookup(EXACT_Q, "Dubai")
    assert sum(rig.clock.sleeps) <= 10


def test_a_failed_row_is_left_untouched_and_the_run_carries_on(tmp_path):
    table = {lone_q(0): FakeResponse(503), lone_q(1): [osm_place(*DOWNTOWN)]}
    out, stats = run([lone_row(0), lone_row(1)], make_rig(tmp_path, handler=router(table)))
    assert out[0].geocode_status == "" and out[0].latitude is None
    assert out[1].has_coordinates()
    assert stats.transient_failures == 1 and stats.unresolved == 0 and stats.aborted == ""


def test_five_failed_lookups_in_a_row_abort_the_run(tmp_path):
    rows = [lone_row(i) for i in range(8)]
    rig = make_rig(tmp_path, handler=router({}, empty=FakeResponse(503)))
    out, stats = run(rows, rig)
    assert stats.aborted.startswith("5 consecutive backend failures")
    assert len(rig.session.calls) == 15 and stats.skipped_after_abort == 3
    assert all(r.geocode_status == "" for r in out)


def test_a_success_resets_the_failure_streak(tmp_path):
    table = {lone_q(i): FakeResponse(503) for i in range(9) if i != 4}
    table[lone_q(4)] = [osm_place(*DOWNTOWN)]
    out, stats = run([lone_row(i) for i in range(9)], make_rig(tmp_path, handler=router(table)))
    assert stats.aborted == "" and stats.transient_failures == 8 and out[4].has_coordinates()


def test_a_genuine_empty_answer_also_resets_the_streak(tmp_path):
    table = {lone_q(i): FakeResponse(503) for i in range(9) if i != 4}
    out, stats = run([lone_row(i) for i in range(9)], make_rig(tmp_path, handler=router(table)))
    assert stats.aborted == "" and out[4].geocode_status == "unresolved 2026-10-25"


def test_a_4xx_refusal_is_a_miss_for_that_query_never_cached_and_not_retried(tmp_path):
    rig = make_rig(tmp_path, replies=[FakeResponse(400, {"error": "bad"}), FakeResponse(200, [osm_place(*DOWNTOWN)])])
    assert rig.geo.lookup(EXACT_Q, "Dubai") is None
    assert len(rig.session.calls) == 1
    assert rig.geo.lookup(EXACT_Q, "Dubai") is not None and len(rig.session.calls) == 2


def test_repeated_4xx_refusals_trip_the_breaker_too(tmp_path):
    rig = make_rig(tmp_path, handler=router({}, empty=FakeResponse(400)))
    _, stats = run([lone_row(i) for i in range(7)], rig)
    assert stats.aborted.startswith("5 consecutive") and len(rig.session.calls) == 5


def test_404_is_an_ordinary_cached_miss(tmp_path):
    rig = make_rig(tmp_path, cfg_for("locationiq"), replies=[FakeResponse(404, {"error": "Unable to geocode"})])
    for _ in range(2):
        outcome = rig.geo.lookup_detailed(EXACT_Q, "Dubai")
        assert (outcome.hit, outcome.rejection) == (None, "")
    assert len(rig.session.calls) == 1


def test_a_huge_result_list_is_judged_by_its_first_item_and_stored_as_one(tmp_path):
    results = [osm_place(*DOWNTOWN, name=f"Test Residences {i}") for i in range(300)]
    rig = make_rig(tmp_path, replies=[FakeResponse(200, results)])
    assert rig.geo.lookup(EXACT_Q, "Dubai") is not None
    rig.geo.save_cache()
    [entry] = rig.cache_entries().values()
    assert len(entry["results"]) == 1


def test_a_huge_nested_provider_value_is_bounded_in_the_cache(tmp_path):
    nested: Any = "x" * 5000
    for _ in range(40):
        nested = [nested]
    item = _with(osm_place(*DOWNTOWN), extra=nested, longtext="y" * 100_000)
    rig = make_rig(tmp_path, replies=[FakeResponse(200, [item])])
    rig.geo.lookup(EXACT_Q, "Dubai")
    rig.geo.save_cache()
    assert rig.cache_file.stat().st_size < 20_000


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def test_cache_is_keyed_by_backend_and_query_and_stores_provider_items_not_decisions(tmp_path):
    rig = make_rig(tmp_path, replies=[FakeResponse(200, [osm_street(*DOWNTOWN)])])
    rig.geo.lookup("  Test Residences A,   Dubai, UAE ", "Dubai")
    rig.geo.save_cache()
    data = json.loads(rig.cache_file.read_text(encoding="utf-8"))
    [(key, entry)] = data["entries"].items()
    assert key == "nominatim|test residences a, dubai, uae"
    assert entry["results"][0]["category"] == "highway", "the raw provider item is stored, though it was rejected"
    assert "rejection" not in json.dumps(entry) and "hit" not in entry


def test_the_same_query_under_another_backend_is_a_separate_entry(tmp_path):
    a = make_rig(tmp_path, replies=[FakeResponse(200, [osm_place(*DOWNTOWN)])])
    a.geo.lookup(EXACT_Q, "Dubai")
    a.geo.save_cache()
    b = make_rig(tmp_path, cfg_for("locationiq"), replies=[FakeResponse(200, [osm_place(*BUSINESS_BAY)])])
    hit = b.geo.lookup(EXACT_Q, "Dubai")
    assert len(b.session.calls) == 1 and (hit.lat, hit.lon) == pytest.approx(BUSINESS_BAY)


def test_a_cached_hit_is_revalidated_every_time(tmp_path):
    first = make_rig(tmp_path, replies=[FakeResponse(200, [osm_place(*SHARJAH_ALJADA, state="Sharjah Emirate")])])
    assert first.geo.lookup(EXACT_Q, "Sharjah") is not None
    first.geo.save_cache()
    second = make_rig(tmp_path)
    outcome = second.geo.lookup_detailed(EXACT_Q, "Dubai")
    assert outcome.hit is None and outcome.rejection == "wrong-state" and outcome.from_cache
    assert second.session.calls == []


def test_a_cached_hit_is_marked_and_counted(tmp_path):
    first = make_rig(tmp_path, replies=[FakeResponse(200, [osm_place(*DOWNTOWN)])])
    first.geo.lookup(EXACT_Q, "Dubai")
    first.geo.save_cache()
    second = make_rig(tmp_path)
    hit = second.geo.lookup(EXACT_Q, "Dubai")
    assert hit.from_cache is True and second.geo.cache_hits == 1


def test_a_genuine_miss_is_cached(tmp_path):
    first = make_rig(tmp_path, replies=[FakeResponse(200, [])])
    first.geo.lookup(EXACT_Q, "Dubai")
    first.geo.save_cache()
    second = make_rig(tmp_path)
    assert second.geo.lookup(EXACT_Q, "Dubai") is None and second.session.calls == []


def _cached_with_age(tmp_path: Path, fetched: str) -> Rig:
    seed = make_rig(tmp_path, replies=[FakeResponse(200, [osm_place(*DOWNTOWN)])])
    seed.geo.lookup(EXACT_Q, "Dubai")
    seed.geo.save_cache()
    data = json.loads(seed.cache_file.read_text(encoding="utf-8"))
    for entry in data["entries"].values():
        entry["fetched"] = fetched
    seed.cache_file.write_text(json.dumps(data), encoding="utf-8")
    rig = make_rig(tmp_path, replies=[FakeResponse(200, [osm_place(*BUSINESS_BAY)])])
    rig.geo.begin_run(TODAY)
    return rig


@pytest.mark.parametrize("fetched,stale", [("2026-10-25", False), ("2026-09-25", False), ("2026-09-24", True), ("2025-01-01", True),
                                           ("2026-10-26", True)])
def test_cache_entries_expire_after_thirty_days_and_a_future_date_is_not_trusted(tmp_path, fetched, stale):
    rig = _cached_with_age(tmp_path, fetched)
    hit = rig.geo.lookup(EXACT_Q, "Dubai")
    assert bool(rig.session.calls) is stale
    assert (hit.lat, hit.lon) == pytest.approx(BUSINESS_BAY if stale else DOWNTOWN)


CORRUPT_CACHES = [
    ("empty", b""), ("not-json", b"{not json"), ("truncated", b'{"version": 1, "entries": {"a": {"fetched": "2026-10-2'),
    ("json-list", b"[]"), ("json-null", b"null"), ("entries-is-a-list", b'{"entries": []}'),
    ("entry-is-a-number", b'{"entries": {"k": 5}}'), ("bad-date", b'{"entries": {"k": {"fetched": "never", "results": []}}}'),
    ("results-not-dicts", b'{"entries": {"k": {"fetched": "2026-10-20", "results": [1, 2]}}}'),
    ("binary", b"\xff\xfe\x00\x01\x80 binary"), ("huge-integer", b'{"entries": ' + b"9" * 6000 + b"}"),
    ("deeply-nested", b"[" * 200_000),
]


@pytest.mark.parametrize("label,content", CORRUPT_CACHES, ids=[c[0] for c in CORRUPT_CACHES])
def test_a_corrupt_cache_is_ignored_and_rebuilt(tmp_path, label, content):
    cache = tmp_path / "geocode_cache.json"
    cache.write_bytes(content)
    rig = make_rig(tmp_path, replies=[FakeResponse(200, [osm_place(*DOWNTOWN)])])
    assert rig.geo.lookup(EXACT_Q, "Dubai") is not None
    rig.geo.save_cache()
    rebuilt = json.loads(cache.read_text(encoding="utf-8"))
    assert len(rebuilt["entries"]) == 1


def test_a_missing_cache_file_and_directory_are_created_on_save(tmp_path):
    cache_file = tmp_path / "deep" / "er" / "geocode_cache.json"
    geo = Geocoder(Config(), session=FakeSession(replies=[FakeResponse(200, [])]), cache_file=cache_file,
                   sleep=FakeClock().sleep, clock=FakeClock().time)
    geo.lookup(EXACT_Q, "Dubai")
    geo.save_cache()
    assert cache_file.exists()
    assert not list(cache_file.parent.glob("*.tmp")), "the atomic write leaves no temp file"


def test_save_cache_writes_nothing_when_nothing_changed(tmp_path):
    rig = make_rig(tmp_path)
    rig.geo.save_cache()
    assert not rig.cache_file.exists()


def test_an_unwritable_cache_location_is_logged_not_raised(tmp_path, caplog):
    blocker = tmp_path / "geocode_cache.json"
    blocker.mkdir()
    rig = make_rig(tmp_path, replies=[FakeResponse(200, [osm_place(*DOWNTOWN)])])
    rig.geo.lookup(EXACT_Q, "Dubai")
    with caplog.at_level(logging.WARNING, logger="src.geocoder"):
        rig.geo.save_cache()
    assert "could not write geocode cache" in caplog.text


def test_use_cache_false_neither_reads_nor_writes_the_file(tmp_path):
    seed = make_rig(tmp_path, replies=[FakeResponse(200, [osm_place(*DOWNTOWN)])])
    seed.geo.lookup(EXACT_Q, "Dubai")
    seed.geo.save_cache()
    before = seed.cache_file.read_text(encoding="utf-8")
    rig = make_rig(tmp_path, replies=[FakeResponse(200, [osm_place(*BUSINESS_BAY)]), FakeResponse(200, [])], use_cache=False)
    rig.geo.lookup(EXACT_Q, "Dubai")
    rig.geo.lookup(q("Another"), "Dubai")
    rig.geo.save_cache()
    assert len(rig.session.calls) == 2
    assert rig.cache_file.read_text(encoding="utf-8") == before


def test_query_case_and_spacing_share_one_cache_entry(tmp_path):
    rig = make_rig(tmp_path, replies=[FakeResponse(200, [osm_place(*DOWNTOWN)])])
    rig.geo.lookup("Test Residences A, Dubai, UAE", "Dubai")
    rig.geo.lookup("TEST   residences a,\tDubai, UAE", "Dubai")
    assert len(rig.session.calls) == 1


def test_the_run_saves_the_cache_at_the_end(tmp_path):
    rig = make_rig(tmp_path, handler=router({lone_q(1): [osm_place(*DOWNTOWN)]}))
    run([lone_row(1)], rig)
    assert len(rig.cache_entries()) == 1


def test_the_default_cache_location_is_the_gitignored_config_constant():
    from src.config import GEOCODE_CACHE_FILE

    geo = Geocoder(Config(), use_cache=False)
    assert geo.cache_file == GEOCODE_CACHE_FILE
    assert "geocode_cache.json" in (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
def test_scrub_removes_known_values_encoded_forms_and_key_parameters():
    secret = "fake key/with+odd&chars"
    text = f"GET https://x.invalid/v1?q=a&key={KEY_LIQ}&apiKey={KEY_GEO} and {secret} and %s and %s" % (
        "fake%20key%2Fwith%2Bodd%26chars", "fake+key%2Fwith%2Bodd%26chars")
    cleaned = scrub_secrets(text, [secret, KEY_LIQ, KEY_GEO])
    for needle in (KEY_LIQ, KEY_GEO, secret, "fake%20key", "fake+key%2F"):
        assert needle not in cleaned
    assert "q=a" in cleaned


@pytest.mark.parametrize("text", [
    "https://x.invalid/?key=ABCD1234efgh", "https://x.invalid/?a=1&apiKey=ABCD1234efgh&b=2", "token=ABCD1234efgh",
    "Authorization: Bearer ABCD1234efgh", "authorization=Basic ABCD1234efgh", "client_secret=ABCD1234efgh", "x-api-key: ABCD1234efgh",
    "{\"key\": \"ABCD1234efgh\"}", "access_token=ABCD1234efgh;",
])
def test_scrub_redacts_key_shaped_values_even_when_not_known(text):
    assert "ABCD1234efgh" not in scrub_secrets(text)


def test_scrub_leaves_ordinary_text_alone_and_takes_non_strings():
    assert scrub_secrets("Test Residences A, Dubai, UAE", [KEY_LIQ]) == "Test Residences A, Dubai, UAE"
    assert scrub_secrets(None) == "None"
    assert scrub_secrets(ValueError("boom"), [KEY_LIQ]) == "boom"
    assert scrub_secrets("", [KEY_LIQ]) == ""


def test_scrub_ignores_secrets_too_short_to_be_safe_to_replace():
    assert scrub_secrets("a b c abc", ["a", "", "ab", None, 5]) == "a b c abc"


def _assert_no_key_anywhere(caplog, capsys, *extra: object) -> None:
    out = capsys.readouterr()
    haystack = log_text(caplog) + out.out + out.err + " ".join(str(e) for e in extra)
    for key in ALL_KEYS:
        assert key not in haystack
    assert not re.search(r"(?i)(?:api[_-]?key|\bkey)=(?!\*\*\*)", haystack), "a key parameter was logged with a value"


@pytest.mark.parametrize("backend", KEYED_BACKENDS)
def test_a_connection_error_that_quotes_the_url_leaks_nothing(tmp_path, caplog, capsys, backend):
    key = KEYS[backend]
    error = requests.ConnectionError(f"HTTPSConnectionPool(host='x.invalid'): Max retries exceeded with url: /v1/search?key={key}&apiKey={key}&q=x")
    rig = make_rig(tmp_path, cfg_for(backend), handler=router({}, empty=error))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        rig.geo.begin_run(TODAY)
        with pytest.raises(LookupInterrupted) as err:
            rig.geo.lookup_detailed(EXACT_Q, "Dubai")
        _, stats = run([lone_row(1)], make_rig(tmp_path, cfg_for(backend), handler=router({}, empty=error), cache_name="b.json"),
                       cfg_for(backend))
    assert "ConnectionError" in log_text(caplog)
    _assert_no_key_anywhere(caplog, capsys, err.value, err.value.reason, stats.as_log_dict())


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_transport_error_that_quotes_the_query_leaves_no_row_content_in_the_public_log(tmp_path, caplog, capsys, backend):
    # requests puts the request URL, and so the q= text (a project name), in a ConnectionError's message. The log of a
    # public repository is public: only the exception class may appear, at the retry warnings and at the circuit breaker.
    error = requests.ConnectionError(
        "HTTPSConnectionPool(host='geocoder.invalid', port=443): Max retries exceeded with url: "
        "/search?q=Zzyzx+Marina+Heights%2C+Dubai%2C+UAE&format=jsonv2&limit=1&countrycodes=ae (Caused by NameResolutionError)"
    )
    rows = [mk_row(f"Zzyzx Marina Heights {i}", community="", area="") for i in range(1, 8)]
    cfg = cfg_for(backend)
    rig = make_rig(tmp_path, cfg, handler=router({}, empty=error))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        _, stats = run(rows, rig, cfg)
    text = log_text(caplog) + " ".join(str(v) for v in (stats.aborted, stats.stop_reason, stats.as_log_dict()))
    assert stats.aborted.endswith("consecutive backend failures")  # the breaker did trip, so both log sites ran
    assert "ConnectionError" in text
    for piece in ("Zzyzx", "Marina", "format=jsonv2", "/search", "geocoder.invalid", "NameResolutionError"):
        assert piece not in text, piece
    _assert_no_key_anywhere(caplog, capsys)


@pytest.mark.parametrize("backend", KEYED_BACKENDS)
def test_a_429_whose_body_echoes_the_key_leaks_nothing(tmp_path, caplog, capsys, backend):
    key = KEYS[backend]
    body = f'{{"error": "Rate limited for key={key} on https://x.invalid/v1?apiKey={key}"}}'
    rig = make_rig(tmp_path, cfg_for(backend), handler=router({}, empty=FakeResponse(429, text=body)))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        _, stats = run([lone_row(1)], rig, cfg_for(backend))
    assert stats.aborted == "HTTP 429"
    _assert_no_key_anywhere(caplog, capsys, stats.aborted, stats.as_log_dict())


def test_a_google_error_message_that_echoes_the_key_leaks_nothing(tmp_path, caplog, capsys):
    payload = {"status": "REQUEST_DENIED", "results": [], "error_message": f"The provided API key is invalid. key={KEY_GOOGLE}"}
    rig = make_rig(tmp_path, cfg_for("google"), handler=router({}, empty=FakeResponse(200, payload)))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        _, stats = run([lone_row(1)], rig, cfg_for("google"))
    assert stats.aborted == "Google REQUEST_DENIED"
    _assert_no_key_anywhere(caplog, capsys, stats.aborted)


def test_a_5xx_body_and_odd_status_text_that_echo_the_key_leak_nothing(tmp_path, caplog, capsys):
    reply = FakeResponse(502, text=f"Bad gateway for https://x.invalid/?key={KEY_LIQ}")
    rig = make_rig(tmp_path, cfg_for("locationiq"), handler=router({}, empty=reply))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        run([lone_row(1)], rig, cfg_for("locationiq"))
    _assert_no_key_anywhere(caplog, capsys)


def test_a_provider_item_that_echoes_the_key_is_not_written_to_the_cache(tmp_path):
    item = osm_place(*DOWNTOWN, display=f"Test Residences A key={KEY_LIQ} {KEY_LIQ}")
    rig = make_rig(tmp_path, cfg_for("locationiq"), replies=[FakeResponse(200, [item])])
    rig.geo.lookup(EXACT_Q, "Dubai")
    rig.geo.save_cache()
    assert KEY_LIQ not in rig.cache_file.read_text(encoding="utf-8")


def test_a_failing_flush_is_logged_scrubbed_counted_and_does_not_lose_the_rows(tmp_path, caplog):
    def broken_flush(rows):
        raise RuntimeError(f"sheet write failed for https://x.invalid/?key={KEY_LIQ}")

    rig = make_rig(tmp_path, cfg_for("locationiq"), handler=router({lone_q(1): hit_payload("locationiq", *DOWNTOWN)}))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        out, stats = run([lone_row(1)], rig, cfg_for("locationiq"), flush=broken_flush)
    assert out[0].has_coordinates() and stats.flush_failures == 1
    assert "RuntimeError" in caplog.text and KEY_LIQ not in log_text(caplog)


def test_config_errors_and_reprs_never_carry_a_key(tmp_path):
    with pytest.raises(GeocoderConfigError) as err:
        resolve_backend(Config(geocode_backend="nope", locationiq_api_key=KEY_LIQ, geoapify_api_key=KEY_GEO))
    rig = make_rig(tmp_path, cfg_for("locationiq"))
    for text in (str(err.value), repr(rig.geo), repr(rig.geo.cfg), repr(GeocodeStats(backend="locationiq"))):
        assert not any(key in text for key in ALL_KEYS)


def test_nothing_is_ever_printed(tmp_path, capsys):
    rig = make_rig(tmp_path, handler=router({lone_q(0): [osm_place(*DOWNTOWN)]}, empty=FakeResponse(503)))
    run([lone_row(i) for i in range(3)], rig)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


# ---------------------------------------------------------------------------
# Flush
# ---------------------------------------------------------------------------
def test_flush_runs_every_n_concluded_rows_with_the_full_row_list_and_once_at_the_end(tmp_path):
    rows = [lone_row(i) for i in range(5)]
    table = {lone_q(i): [osm_place(*DOWNTOWN)] for i in range(5)}
    snapshots: list[list[str]] = []
    rig = make_rig(tmp_path, handler=router(table))

    def flush(current):
        assert rig.cache_file.exists(), "the cache is saved before the sheet is"
        snapshots.append([r.geocode_status for r in current])

    run(rows, rig, Config(geocode_flush_every=2), flush=flush)
    resolved = "resolved via nominatim exact 2026-10-25"
    assert snapshots == [[resolved, resolved, "", "", ""], [resolved] * 4 + [""], [resolved] * 5]


def test_flush_receives_a_fresh_list_each_time_in_input_order(tmp_path):
    seen: list[list[RegistryRow]] = []
    rows = [lone_row(i) for i in range(3)]
    run(rows, make_rig(tmp_path, handler=router({})), Config(geocode_flush_every=1), flush=seen.append)
    assert len(seen) == 4 and len({id(s) for s in seen}) == 4
    assert all([r.project_name_en for r in s] == [r.project_name_en for r in rows] for s in seen)


def test_flush_counts_unresolved_rows_as_progress_too(tmp_path):
    seen = []
    run([lone_row(i) for i in range(4)], make_rig(tmp_path, handler=router({})), Config(geocode_flush_every=2), flush=seen.append)
    assert len(seen) == 3


def test_flush_with_nothing_to_do_still_runs_once(tmp_path):
    seen = []
    run([lone_row(1, latitude=25.2, longitude=55.3)], make_rig(tmp_path), flush=seen.append)
    assert len(seen) == 1


def test_a_flush_that_raises_does_not_stop_the_run(tmp_path):
    calls = []

    def broken(rows):
        calls.append(1)
        raise OSError("disk full")

    out, stats = run([lone_row(i) for i in range(3)], make_rig(tmp_path, handler=router({})), Config(geocode_flush_every=1), flush=broken)
    assert len(out) == 3 and all(r.geocode_status for r in out)
    assert stats.flush_failures == len(calls) == 4


def test_runs_work_without_a_flush_callable(tmp_path):
    out, _ = run([lone_row(1)], make_rig(tmp_path, handler=router({})))
    assert out[0].geocode_status


# ---------------------------------------------------------------------------
# centroid_for and lifecycle
# ---------------------------------------------------------------------------
def test_centroid_for_returns_the_first_valid_place_and_memoises(tmp_path):
    rig = make_rig(tmp_path, use_cache=False, handler=router({COMM_Q: [osm_area(*DOWNTOWN)]}))
    rig.geo.begin_run(TODAY)
    assert rig.geo.centroid_for("Dubai", ["Example Community", "Example Area"]) == pytest.approx(DOWNTOWN)
    assert rig.geo.centroid_for("dubai", ["Example Community"]) == pytest.approx(DOWNTOWN)
    assert rig.queries == [COMM_Q]


def test_centroid_for_remembers_that_a_place_could_not_be_found(tmp_path):
    rig = make_rig(tmp_path, use_cache=False, handler=router({}))
    rig.geo.begin_run(TODAY)
    assert rig.geo.centroid_for("Dubai", ["Example Community"]) is None
    assert rig.geo.centroid_for("Dubai", ["Example Community"]) is None
    assert rig.queries == [COMM_Q]


def test_centroid_for_does_not_remember_an_interruption(tmp_path):
    rig = make_rig(tmp_path, use_cache=False, replies=[FakeResponse(503)] * 3 + [FakeResponse(200, [osm_area(*DOWNTOWN)])])
    rig.geo.begin_run(TODAY)
    with pytest.raises(LookupInterrupted):
        rig.geo.centroid_for("Dubai", ["Example Community"])
    assert rig.geo.centroid_for("Dubai", ["Example Community"]) == pytest.approx(DOWNTOWN)


def test_begin_run_resets_the_memo_and_counters_but_keeps_the_cache(tmp_path):
    rig = make_rig(tmp_path, handler=router({COMM_Q: [osm_area(*DOWNTOWN)]}))
    rig.geo.begin_run(TODAY)
    rig.geo.centroid_for("Dubai", ["Example Community"])
    assert rig.geo.lookups_used == 1
    rig.geo.begin_run(TODAY)
    assert rig.geo.lookups_used == 0
    assert rig.geo.centroid_for("Dubai", ["Example Community"]) == pytest.approx(DOWNTOWN)
    assert len(rig.session.calls) == 1 and rig.geo.cache_hits == 1


@pytest.mark.parametrize("emirate", ["Abu Dhabi", None, ""])
def test_centroid_for_rejects_an_unsupported_emirate(tmp_path, emirate):
    with pytest.raises(ValueError):
        make_rig(tmp_path).geo.centroid_for(emirate, ["Example Community"])


def test_the_session_is_created_lazily_and_only_when_a_request_is_needed():
    geo = Geocoder(Config(), cache_file=Path("never-written.json"), use_cache=False)
    assert geo.session is None
    assert geo.lookup("", "Dubai") is None and geo.session is None
    assert isinstance(geo._http(), requests.Session)


# ===========================================================================
# Adversarial review, 25 Oct 2026: regression tests for the defects found.
# Every provider response is SYNTHETIC; fake secrets are obviously fake.
# ===========================================================================

# ---------------------------------------------------------------------------
# Name agreement: a confident answer with the wrong NAME must not become an exact pin
# ---------------------------------------------------------------------------
MASAAR_Q = q("Masaar Residences")


def _masaar_run(tmp_path, exact_item, **row_kw):
    row = mk_row("Masaar Residences", community="Example Community", area="", **row_kw)
    rig = make_rig(tmp_path, handler=router({COMM_Q: [osm_area(*DOWNTOWN)], MASAAR_Q: [exact_item]}))
    [out], stats = run([row], rig)
    return out, stats, rig


def test_an_exact_hit_whose_own_name_shares_nothing_with_the_project_is_not_filed_as_exact(tmp_path):
    # Same emirate, 1.6 km from the community centroid, polygon ok, state ok: only the NAME is wrong.
    out, stats, _ = _masaar_run(tmp_path, osm_place(*BUSINESS_BAY, name="Random Mosque Car Park"))
    assert out.location_precision == "community"
    assert (out.latitude, out.longitude) == pytest.approx(DOWNTOWN)
    assert stats.rejections.get("name-mismatch") == 1


def test_a_hit_that_only_mentions_the_project_in_its_address_chain_is_not_exact(tmp_path):
    # jsonv2 'name' is the object's own name; the project name appears only as its neighbourhood.
    item = _with(osm_place(*BUSINESS_BAY, name="Villa 12", display="Villa 12, Masaar Residences, Dubai, United Arab Emirates"),
                 name="Villa 12")
    out, stats, _ = _masaar_run(tmp_path, item)
    assert out.location_precision == "community"
    assert stats.rejections.get("name-mismatch") == 1


def test_an_unnamed_object_matched_through_its_address_is_not_exact(tmp_path):
    item = _with(osm_place(*BUSINESS_BAY, display="Sheikh Example Road, Masaar Residences, Dubai, United Arab Emirates"), name="")
    out, _, _ = _masaar_run(tmp_path, item)
    assert out.location_precision == "community"


@pytest.mark.parametrize("title", [
    "Masaar Residences", "Masaar", "MASAAR RESIDENCES - Phase 2", "Masaar Residence", "Maasar Residences", "masaar-residences",
])
def test_correct_synthetic_positives_still_become_exact_pins(tmp_path, title):
    out, stats, _ = _masaar_run(tmp_path, osm_place(*BUSINESS_BAY, name=title))
    assert out.location_precision == "exact", title
    assert stats.rejections.get("name-mismatch") is None


def test_the_name_check_runs_before_the_centroid_lookup_so_a_mismatch_costs_no_extra_request(tmp_path):
    row = mk_row("Masaar Residences", community="Example Community", area="")
    rig = make_rig(tmp_path, handler=router({COMM_Q: [osm_area(*DOWNTOWN)], MASAAR_Q: [osm_place(*BUSINESS_BAY, name="Unrelated Depot")]}))
    run([row], rig)
    assert rig.queries == [MASAAR_Q, COMM_Q]  # exact, then the community tier; no separate centroid request


def test_the_community_centroid_is_not_name_checked(tmp_path):
    # Registry area names are often not the OSM names ("Marsa Dubai" for Dubai Marina): the centroid must still be usable.
    row = mk_row("Masaar Residences", community="Example Community", area="")
    rig = make_rig(tmp_path, handler=router({COMM_Q: [osm_area(*DOWNTOWN, name="Entirely Different Name")],
                                             MASAAR_Q: [osm_place(*BUSINESS_BAY, name="Masaar Residences")]}))
    [out], _ = run([row], rig)
    assert out.location_precision == "exact"


@pytest.mark.parametrize("names,title,expected", [
    (["Masaar Residences"], "Masaar", True),
    (["Mariam Residences"], "Maryam Residences", True),
    (["Muwaileh Heights"], "Muweilah Heights", True),
    (["Sobha Hartland Greens"], "Hartland Greens", True),
    (["Marina Gardens"], "Marina Garden", True),
    (["Creek Edge Tower 1"], "Creek Edge - Tower 1", True),
    (["Cafe Residences"], "Café Résidences", True),
    (["Masaar\u200f Residences"], "Masaar Residences", True),
    (["Masaar Residences"], "", True),  # the provider gave no name: cannot judge, never reject on silence
    (["Masaar Residences"], None, True),
    (["Masaar Residences"], "Random Mosque Car Park", False),
    (["Masaar Residences"], "Villa 12", False),
    (["Masaar Residences"], "12", False),
    (["Marina Gardens"], "Palm Gardens", False),  # shares only a generic word
    (["Test Residences"], "Other Residences", False),
    (["Burj Crown"], "Burj Khalifa", False),  # Burj means tower
    (["Tower 1"], "Whatever", False),
    (["Masaar", "مسار"], "مسار", True),  # Arabic title vs the Arabic name
    (["Masaar", "مسار"], "المسار", True),  # with the article
    (["Masaar", "مسار"], "الواحة", False),
    (["Masaar Residences"], "مسار", True),  # Arabic-only title, no Arabic name to compare: cannot judge
])
def test_name_agrees(names, title, expected):
    assert name_agrees(names, title) is expected


def test_the_developer_brand_alone_does_not_make_two_names_agree():
    assert name_agrees(["Acme Hartland"], "Acme Head Office", ["Acme"]) is False
    assert name_agrees(["Acme Hartland"], "Hartland Greens", ["Acme"]) is True
    assert name_agrees(["Acme Heights"], "Acme Head Office", ["Acme"]) is True  # nothing but the brand to go on


def test_the_row_arabic_name_is_used_by_the_exact_tier(tmp_path):
    ar = "مسار"
    good = _with(osm_place(*BUSINESS_BAY, name=ar), name=ar)
    out, _, _ = _masaar_run(tmp_path, good, project_name_ar=ar)
    assert out.location_precision == "exact"
    other = "الواحة"
    bad = _with(osm_place(*BUSINESS_BAY, name=other), name=other)
    out, stats, _ = _masaar_run(tmp_path / "b", bad, project_name_ar=ar)
    assert out.location_precision == "community" and stats.rejections.get("name-mismatch") == 1


def test_geoapify_and_google_names_feed_the_same_check(tmp_path):
    geo_bad = geoapify_payload(*BUSINESS_BAY)
    geo_bad["features"][0]["properties"]["name"] = "Unrelated Depot"
    cfg = cfg_for("geoapify")
    row = mk_row("Masaar Residences", community="Example Community", area="")
    rig = make_rig(tmp_path, cfg, handler=router({COMM_Q: geoapify_payload(*DOWNTOWN, result_type="suburb"), MASAAR_Q: geo_bad}))
    [out], _ = run([row], rig, cfg)
    assert out.location_precision == "community"

    # Google's geocoding API normally carries no place name at all: the check cannot judge and stays out of the way.
    gcfg = cfg_for("google")
    rig = make_rig(tmp_path / "g", gcfg, handler=router({COMM_Q: google_payload(*DOWNTOWN, types=("neighborhood", "political")),
                                                         MASAAR_Q: google_payload(*BUSINESS_BAY)}))
    [out], _ = run([row], rig, gcfg)
    assert out.location_precision == "exact"
    payload = google_payload(*BUSINESS_BAY)
    payload["results"][0]["address_components"].append({"long_name": "Unrelated Depot", "short_name": "UD", "types": ["premise"]})
    rig = make_rig(tmp_path / "g2", gcfg, handler=router({COMM_Q: google_payload(*DOWNTOWN, types=("neighborhood", "political")),
                                                          MASAAR_Q: payload}))
    [out], _ = run([row], rig, gcfg)
    assert out.location_precision == "community"


@pytest.mark.parametrize("types,kind", [
    (("colloquial_area", "political"), "area"),
    (("administrative_area_level_4", "political"), "area"),
    (("establishment", "point_of_interest"), "place"),
])
def test_google_area_types_the_docs_list_are_areas(tmp_path, types, kind):
    rig = make_rig(tmp_path, cfg_for("google"), replies=[FakeResponse(200, google_payload(*DOWNTOWN, types=types))])
    assert rig.geo.lookup(EXACT_Q, "Dubai").kind == kind


def test_google_intersection_is_a_street_not_a_project(tmp_path):
    rig = make_rig(tmp_path, cfg_for("google"), replies=[FakeResponse(200, google_payload(*DOWNTOWN, types=("intersection",)))])
    assert rig.geo.lookup_detailed(EXACT_Q, "Dubai").rejection == "street"


# ---------------------------------------------------------------------------
# Queries: invisible and control characters
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dirty", [
    "Masaar\u200b Residences, Dubai, UAE", "\u200fMasaar Residences\u200e, Dubai, UAE", "Masaar\x00 Residences, Dubai, UAE",
    "\ufeffMasaar Residences, Dubai, UAE",
])
def test_invisible_marks_and_control_characters_never_reach_the_provider_or_the_cache_key(tmp_path, dirty):
    rig = make_rig(tmp_path, handler=router({}))
    rig.geo.lookup(dirty, "Dubai")
    rig.geo.lookup("Masaar Residences, Dubai, UAE", "Dubai")
    assert rig.queries == ["Masaar Residences, Dubai, UAE"]  # sent once, clean; the second spelling hit the cache entry


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
# SYNTHETIC service-account document: obviously fake values, PEM-shaped.
FAKE_PEM_LINE_1 = "FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0001"
FAKE_PEM_LINE_2 = "FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0002"
FAKE_PEM = f"-----BEGIN PRIVATE KEY-----\n{FAKE_PEM_LINE_1}\n{FAKE_PEM_LINE_2}\n-----END PRIVATE KEY-----\n"
FAKE_SA = {
    "type": "service_account", "project_id": "fake-project", "private_key_id": "fakekeyid-0123456789abcdef",
    "private_key": FAKE_PEM, "client_email": "fake-sa@fake-project.iam.gserviceaccount.com", "client_id": "100000000000000000001",
}
FAKE_SA_PIECES = (FAKE_PEM_LINE_1, FAKE_PEM_LINE_2, FAKE_SA["private_key_id"], FAKE_SA["client_email"], FAKE_SA["client_id"])


def _sa_cfg(encoding: str = "json") -> Config:
    raw = json.dumps(FAKE_SA)
    if encoding == "base64":
        import base64

        raw = base64.b64encode(raw.encode()).decode()
    return Config(gcp_service_account_key=raw)


@pytest.mark.parametrize("encoding", ["json", "base64"])
@pytest.mark.parametrize("piece", FAKE_SA_PIECES)
def test_a_service_account_member_quoted_by_a_failing_flush_is_never_logged(tmp_path, caplog, capsys, encoding, piece):
    def broken_flush(rows):
        raise RuntimeError(f"sheets refused the write: {piece} (see https://x.invalid/doc)")

    rig = make_rig(tmp_path, handler=router({lone_q(1): [osm_place(*DOWNTOWN)]}))
    with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
        out, stats = run([lone_row(1)], rig, _sa_cfg(encoding), flush=broken_flush)
    assert out[0].has_coordinates() and stats.flush_failures >= 1
    assert "RuntimeError" in caplog.text
    captured = capsys.readouterr()
    assert piece not in log_text(caplog) + captured.out + captured.err


@pytest.mark.parametrize("encoding", ["json", "base64"])
def test_secret_values_takes_the_service_account_document_apart(encoding):
    values = gc._secret_values(_sa_cfg(encoding))
    for piece in FAKE_SA_PIECES:
        assert piece in values


@pytest.mark.parametrize("encoding", ["json", "base64"])
def test_secret_values_also_registers_the_identifiers_of_the_account_that_the_log_handler_scrubs(encoding):
    # REGRESSION (round 5): the geocoder kept its own member rule, which left out the project id and the parts of the
    # service-account e-mail (its name and its domain, which carries the project id), so a line that quoted
    # "/serviceAccounts/<e-mail>/..." or the project id was scrubbed by the log handler and not by this module.
    # (An earlier version of this test pinned the project id as "not a secret"; src.log_policy decides that it is one.)
    values = gc._secret_values(_sa_cfg(encoding))
    local, _, domain = FAKE_SA["client_email"].partition("@")
    for identifier in (FAKE_SA["project_id"], local, domain):
        assert identifier in values, identifier
    cleaned = scrub_secrets(f"GET /serviceAccounts/{FAKE_SA['client_email']}/x in {FAKE_SA['project_id']} by {local}", values)
    for identifier in (FAKE_SA["project_id"], local, domain, FAKE_SA["client_email"]):
        assert identifier not in cleaned, identifier


def test_the_geocoder_and_the_log_handler_scrub_the_same_account_identifiers():
    from src.log_policy import json_secret_members

    document = json.dumps(FAKE_SA)
    assert set(json_secret_members(document)) <= set(gc._json_secret_members(document))


def test_a_deeply_nested_service_account_value_is_no_key_file_and_stops_nothing():
    nested = '{"private_key": ' + "[" * 100_000
    assert gc._json_secret_members(nested) == []
    assert gc._secret_values(Config(gcp_service_account_key=nested)) == (nested,)


@pytest.mark.parametrize("text,secret", [
    (f"boom {FAKE_PEM}", FAKE_PEM_LINE_1),
    (json.dumps({"private_key": FAKE_PEM}), FAKE_PEM_LINE_2),
    ("refresh_token=ABCD1234efgh", "ABCD1234efgh"),
    ("id_token: ABCD1234efgh", "ABCD1234efgh"),
    ("password=hunter2hunter2", "hunter2hunter2"),
    ("{\"client_secret\": \"ABCD1234efgh\"}", "ABCD1234efgh"),
    ("failed with Bearer ABCD1234efghIJKL for the request", "ABCD1234efghIJKL"),
    ("rejected AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE0123 today", "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE0123"),
    ("token ya29.FAKEFAKEFAKEFAKEFAKEFAKEFAKE", "ya29.FAKEFAKEFAKEFAKEFAKEFAKEFAKE"),
    ("jwt eyJFAKEFAKE0.eyJFAKEFAKE1.FAKESIGNATURE2", "eyJFAKEFAKE0"),
    ("GET /v1?x=1&sig=ABCD1234efgh&y=2", "ABCD1234efgh"),
    ("X-Goog-Api-Key: ABCD1234efgh", "ABCD1234efgh"),
    ("session_token=ABCD1234efgh", "ABCD1234efgh"),
])
def test_scrub_redacts_more_secret_shapes_than_a_key_parameter(text, secret):
    assert secret not in scrub_secrets(text)


@pytest.mark.parametrize("backend", KEYED_BACKENDS)
@pytest.mark.parametrize("leak", [
    "https://x.invalid/v1/search?key={k}&q=a", "https://x.invalid/?apiKey={k}", "Authorization: Bearer {k}",
    "{{\"key\": \"{k}\"}}", "token={k}", "x-api-key: {k}", "access_token={k}&z=1", "bare {k} in a sentence",
])
def test_a_secret_in_any_exception_in_a_cause_chain_or_response_body_never_reaches_a_log(tmp_path, caplog, capsys, backend, leak):
    key = KEYS[backend]
    text = leak.format(k=key)
    try:
        try:
            raise OSError(text)
        except OSError as root:
            raise requests.ConnectionError(text) from root
    except requests.ConnectionError as chained:
        error = chained
    for index, reply in enumerate((error, FakeResponse(503, text=text), FakeResponse(429, text=text), FakeResponse(400, text=text))):
        rig = make_rig(tmp_path, cfg_for(backend), handler=router({}, empty=reply), cache_name=f"c{index}.json")
        with caplog.at_level(logging.DEBUG, logger="src.geocoder"):
            _, stats = run([lone_row(1)], rig, cfg_for(backend))
        _assert_no_key_anywhere(caplog, capsys, stats.as_log_dict(), stats.aborted, stats.stop_reason)
        assert key not in " ".join(map(str, stats.as_log_dict().values()))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def test_a_remembered_miss_does_not_outlive_the_retry_cooldown(tmp_path):
    cfg = Config(geocode_retry_cooldown_days=7)
    first = make_rig(tmp_path, cfg, handler=router({}))
    out, _ = geocode_pending([lone_row(1)], {}, cfg, today=date(2026, 10, 1), geocoder=first.geo)
    assert out[0].geocode_status == "unresolved 2026-10-01"
    # A later run (a new process sharing the cache file). The provider has learned the place meanwhile.
    second = make_rig(tmp_path, cfg, handler=router({lone_q(1): [osm_place(*DOWNTOWN)]}))
    again, stats = geocode_pending(out, {}, cfg, today=date(2026, 10, 9), geocoder=second.geo)
    assert again[0].has_coordinates(), "the 8-day-old local miss answered a retry that the cooldown had scheduled"
    assert len(second.session.calls) == 1 and stats.cache_hits == 0


def test_a_miss_is_still_cached_within_the_cooldown_and_for_the_rest_of_the_same_day(tmp_path):
    cfg = Config(geocode_retry_cooldown_days=7)
    first = make_rig(tmp_path, cfg, handler=router({}))
    first.geo.begin_run(date(2026, 10, 1))
    first.geo.lookup(lone_q(1), "Dubai")
    first.geo.save_cache()
    same_day = make_rig(tmp_path, cfg, handler=router({}))
    same_day.geo.begin_run(date(2026, 10, 1))
    same_day.geo.lookup(lone_q(1), "Dubai")
    assert same_day.session.calls == []
    within = make_rig(tmp_path, cfg, handler=router({}))
    within.geo.begin_run(date(2026, 10, 7))  # 6 days: still inside the cooldown
    within.geo.lookup(lone_q(1), "Dubai")
    assert within.session.calls == []
    after = make_rig(tmp_path, cfg, handler=router({}))
    after.geo.begin_run(date(2026, 10, 8))  # 7 days: the cooldown is over
    after.geo.lookup(lone_q(1), "Dubai")
    assert len(after.session.calls) == 1


def test_a_cached_hit_is_not_shortened_by_the_cooldown(tmp_path):
    cfg = Config(geocode_retry_cooldown_days=7)
    first = make_rig(tmp_path, cfg, handler=router({lone_q(1): [osm_place(*DOWNTOWN)]}))
    first.geo.begin_run(date(2026, 10, 1))
    first.geo.lookup(lone_q(1), "Dubai")
    first.geo.save_cache()
    later = make_rig(tmp_path, cfg, handler=router({}))
    later.geo.begin_run(date(2026, 10, 20))
    assert later.geo.lookup(lone_q(1), "Dubai") is not None and later.session.calls == []


def test_the_cache_temp_file_is_per_process_and_never_left_behind(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, handler=router({lone_q(1): [osm_place(*DOWNTOWN)]}))
    rig.geo.lookup(lone_q(1), "Dubai")
    rig.geo.save_cache()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["geocode_cache.json"]

    rig = make_rig(tmp_path, cache_name="other.json", handler=router({lone_q(1): [osm_place(*DOWNTOWN)]}))
    rig.geo.lookup(lone_q(1), "Dubai")
    seen: list[str] = []

    def refuse(src, dst):
        seen.append(Path(src).name)
        raise PermissionError("target is open in another process")

    monkeypatch.setattr(gc.os, "replace", refuse)
    rig.geo.save_cache()
    assert seen and str(gc.os.getpid()) in seen[0]  # the temp name is per process
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]  # and the failed write cleaned it up


# ---------------------------------------------------------------------------
# Map link of an unresolved row
# ---------------------------------------------------------------------------
def test_an_unresolved_row_does_not_keep_a_link_to_coordinates_a_human_cleared(tmp_path):
    pinned = lone_row(2, latitude=25.1972, longitude=55.2744, location_precision="exact", geocode_method="nominatim",
                      geocode_status="resolved via nominatim exact 2026-09-01")
    stale = pinned.model_copy(update={"map_pin_link": pinned.build_map_pin_link()})
    cleared = stale.model_copy(update={"latitude": None, "longitude": None})  # the human wiped lat/lon to force a re-check
    [out], _ = run([cleared], make_rig(tmp_path, handler=router({})))
    assert out.geocode_status == "unresolved 2026-10-25"
    assert "25.1972" not in out.map_pin_link and "55.2744" not in out.map_pin_link
    assert out.map_pin_link == out.build_map_pin_link()


# ===========================================================================
# Adversarial review, second pass (25 Oct 2026). Synthetic data throughout.
# ===========================================================================
@pytest.mark.parametrize("fetched", [20261025, 1, True, 2026.5, ["2026-10-25"], None, {"d": 1}])
def test_a_cache_entry_whose_date_is_not_text_is_ignored_and_never_crashes_the_run(tmp_path, fetched):
    key = "nominatim|" + lone_q(1).lower()
    (tmp_path / "geocode_cache.json").write_text(
        json.dumps({"version": 1, "entries": {key: {"fetched": fetched, "results": []}}}), encoding="utf-8")
    rig = make_rig(tmp_path, handler=router({lone_q(1): [osm_place(*DOWNTOWN, name="Test Residences 1")]}))
    [out], _ = run([lone_row(1)], rig)
    assert out.has_coordinates()  # the odd entry was dropped, so the provider was asked


@pytest.mark.parametrize("backend", ["nominatim", "geoapify", "google"])
def test_a_404_from_a_backend_that_answers_no_result_with_200_is_a_failure_not_a_cached_miss(tmp_path, backend):
    # Only LocationIQ says "no result" with a 404. Anywhere else a 404 is a wrong URL or a broken proxy:
    # caching it as "the place does not exist" would condemn every row for the cooldown.
    rows = [lone_row(i) for i in range(1, 8)]
    rig = make_rig(tmp_path, cfg_for(backend), handler=router({}, empty=FakeResponse(404, {"error": "not found"})))
    out, stats = run(rows, rig, cfg_for(backend))
    assert rig.cache_entries() == {}
    assert stats.aborted.endswith("consecutive backend failures")
    assert len(rig.session.calls) == 5 * 3  # five lookups, each retried; the breaker then stops the run
    assert all(r.geocode_status == "" for r in out)  # no row was judged, so none is condemned for the cooldown


# -- name agreement: spelling noise and community words ------------------------
@pytest.mark.parametrize("project,title", [
    ("One Za'abeel", "One Zabeel"),
    ("One Za'abeel", "One Za’abeel"),
    ("One Za’abeel", "One Zaabeel"),
    ("Ma'arid Heights", "Maarid Heights"),
    ("Maʿarid Heights", "Maarid Heights"),
    ("Dubai Ma‘arid Villas", "Maarid Villas"),
])
def test_an_apostrophe_inside_a_name_does_not_split_the_word(project, title):
    assert name_agrees([project], title) is True


def test_an_apostrophe_still_leaves_a_real_mismatch_a_mismatch():
    assert name_agrees(["One Za'abeel"], "Albadia Mosque") is False


def test_words_of_the_declared_community_do_not_count_as_naming_the_project(tmp_path):
    # Registry names often carry the community as a prefix ("MBR City District One").
    row = mk_row("MBR City District One", community="MBR City", area="")
    ok = router({q("MBR City"): [osm_area(*DOWNTOWN)], q("MBR City District One"): [osm_place(*BUSINESS_BAY, name="District One")]})
    [out], _ = run([row], make_rig(tmp_path, handler=ok))
    assert out.location_precision == "exact"
    # ... and a place that shares ONLY the community words with the project is not that project.
    bad = router({q("MBR City"): [osm_area(*DOWNTOWN)], q("MBR City District One"): [osm_place(*BUSINESS_BAY, name="MBR City Mosque")]})
    [out], stats = run([row], make_rig(tmp_path / "b", handler=bad))
    assert out.location_precision == "community"
    assert stats.rejections.get("name-mismatch") == 1


# -- an exact pin that is the community's own centroid --------------------------
def test_an_exact_hit_that_is_the_community_centroid_itself_is_not_filed_as_exact(tmp_path):
    # Fuzzy search answers "Masaar Residences" with the neighbourhood called Masaar Residences, the very object the
    # community query returns. Same name, same emirate, 0 km from the centroid: every other gate passes.
    hood = osm_place(*DOWNTOWN, name="Masaar Residences", cls="place", typ="neighbourhood", rank=22, addresstype="neighbourhood")
    row = mk_row("Masaar Residences Phase 2", community="Masaar Residences Community", area="")
    rig = make_rig(tmp_path, handler=router({q("Masaar Residences Community"): [_with(hood, display_name="Masaar Residences Community, Dubai")],
                                             q("Masaar Residences Phase 2"): [hood]}))
    [out], stats = run([row], rig)
    assert out.location_precision == "community"
    assert stats.rejections.get("is-community-centroid") == 1


def test_an_exact_hit_a_few_hundred_metres_from_the_centroid_is_still_exact(tmp_path):
    near = (DOWNTOWN[0] + 0.003, DOWNTOWN[1])  # about 330 m
    row = mk_row("Masaar Residences", community="Example Community", area="")
    rig = make_rig(tmp_path, handler=router({COMM_Q: [osm_area(*DOWNTOWN)], MASAAR_Q: [osm_place(*near, name="Masaar Residences")]}))
    [out], _ = run([row], rig)
    assert out.location_precision == "exact"


# -- Geoapify rank.match_type: documented as "only the city / country matched" for a coarse answer ---------------
def _geo_with_match(lat, lon, match_type, **kw):
    payload = geoapify_payload(lat, lon, **kw)
    payload["features"][0]["properties"]["rank"] = {"confidence": 0.5, "match_type": match_type}
    return payload


@pytest.mark.parametrize("match_type", ["match_by_country_or_state", "match_by_city_or_disrict", "match_by_postcode", "match_by_street"])
def test_geoapify_exact_hit_that_matched_only_a_coarse_part_of_the_query_is_not_exact(tmp_path, match_type):
    cfg = cfg_for("geoapify")
    row = mk_row("Test Residences A", community="Example Community", area="")
    table = {COMM_Q: geoapify_payload(*DOWNTOWN, result_type="suburb"), EXACT_Q: _geo_with_match(*BUSINESS_BAY, match_type, result_type="unknown")}
    [out], stats = run([row], make_rig(tmp_path, cfg, handler=router(table)), cfg)
    assert out.location_precision == "community"
    assert stats.rejections.get("coarse-match") == 1


@pytest.mark.parametrize("match_type", ["full_match", "inner_part", "match_by_building", "something_new", None])
def test_geoapify_exact_hit_with_a_fine_or_unknown_match_type_still_passes(tmp_path, match_type):
    cfg = cfg_for("geoapify")
    row = mk_row("Test Residences A", community="Example Community", area="")
    payload = _geo_with_match(*BUSINESS_BAY, match_type) if match_type else geoapify_payload(*BUSINESS_BAY)
    table = {COMM_Q: geoapify_payload(*DOWNTOWN, result_type="suburb"), EXACT_Q: payload}
    [out], _ = run([row], make_rig(tmp_path, cfg, handler=router(table)), cfg)
    assert out.location_precision == "exact"


def test_a_coarse_geoapify_match_is_fine_for_the_community_tier(tmp_path):
    cfg = cfg_for("geoapify")
    row = mk_row("Test Residences A", community="Example Community", area="")
    table = {EXACT_Q: geoapify_payload(*BUSINESS_BAY, result_type="street"), COMM_Q: _geo_with_match(*DOWNTOWN, "match_by_city_or_disrict", result_type="suburb")}
    [out], _ = run([row], make_rig(tmp_path, cfg, handler=router(table)), cfg)
    assert out.location_precision == "community"


# -- a provider item of the wrong shape is a rejection, never a crash of the whole run -----------------------------
@pytest.mark.parametrize("field,value", [
    ("types", 5), ("types", "route"), ("types", {"a": 1}), ("address_components", 7), ("address_components", "x"),
    ("address_components", [{"long_name": "Dubai", "types": 3}]), ("address_components", [{"long_name": "Dubai", "types": "country"}]),
])
def test_a_google_item_with_a_field_of_the_wrong_type_is_rejected_not_a_crash(tmp_path, field, value):
    payload = google_payload(*DOWNTOWN)
    payload["results"][0][field] = value
    rig = make_rig(tmp_path, cfg_for("google"), replies=[FakeResponse(200, payload)])
    outcome = rig.geo.lookup_detailed(EXACT_Q, "Dubai")
    assert outcome.hit is None and outcome.rejection == "bad-shape"


# -- overrides: a name key shared by two different registry projects ---------------------------------------------
def test_a_name_key_override_never_pins_two_different_registry_projects(tmp_path, caplog):
    # DESIGN section 2: two rows with DIFFERENT registry keys are different projects even when their name keys match.
    a = mk_row("Binghatti Heights", developer="Binghatti", number="111")
    b = mk_row("Binghatti Heights", developer="Binghatti", number="222")
    name_key = a.identity_keys()[1]
    assert name_key == b.identity_keys()[1]
    ov = {name_key: GeocodeOverride(project_key=name_key, latitude=DOWNTOWN[0], longitude=DOWNTOWN[1], precision="exact")}
    with caplog.at_level(logging.WARNING, logger="src.geocoder"):
        out, stats = run([a, b], make_rig(tmp_path, handler=router({})), Config(geocode_enabled=False), overrides=ov)
    assert [r.has_coordinates() for r in out] == [False, False]
    assert stats.overrides_applied == 0
    assert "ambiguous" in caplog.text and "1 override key(s)" in caplog.text  # once per key, not once per row
    assert "name:" not in caplog.text and "binghatti" not in caplog.text.lower()  # the key holds the developer and project names


def test_a_registry_key_override_still_pins_its_own_project_when_the_name_key_is_shared(tmp_path):
    a = mk_row("Binghatti Heights", developer="Binghatti", number="111")
    b = mk_row("Binghatti Heights", developer="Binghatti", number="222")
    ov = {"dld:dubai:111": GeocodeOverride(project_key="dld:dubai:111", latitude=DOWNTOWN[0], longitude=DOWNTOWN[1], precision="exact")}
    out, stats = run([a, b], make_rig(tmp_path, handler=router({})), Config(geocode_enabled=False), overrides=ov)
    assert [r.has_coordinates() for r in out] == [True, False]
    assert stats.overrides_applied == 1


def test_a_name_key_override_still_finds_the_one_registry_row_it_was_typed_for(tmp_path):
    a = mk_row("Binghatti Heights", developer="Binghatti", number="111")
    name_key = a.identity_keys()[1]
    ov = {name_key: GeocodeOverride(project_key=name_key, latitude=DOWNTOWN[0], longitude=DOWNTOWN[1], precision="exact")}
    [out], stats = run([a], make_rig(tmp_path, handler=router({})), Config(geocode_enabled=False), overrides=ov)
    assert out.has_coordinates() and stats.overrides_applied == 1


# -- an exception whose own text cannot be produced must not take the run down while it is being logged ----------
class _UnprintableError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("no text for you")


def test_scrub_secrets_survives_an_object_with_no_text():
    assert "RuntimeError" not in scrub_secrets(_UnprintableError("x"))  # no crash, and nothing of the inner error leaks in
    assert "_UnprintableError" in scrub_secrets(_UnprintableError("x"))


def test_a_transport_error_with_no_text_is_a_transient_failure_not_a_crash(tmp_path):
    rig = make_rig(tmp_path, handler=router({}, empty=_UnprintableError("boom")))
    out, stats = run([lone_row(1)], rig)
    assert out[0].geocode_status == "" and stats.transient_failures == 1


def test_a_flush_error_with_no_text_is_counted_not_raised(tmp_path):
    def broken_flush(rows):
        raise _UnprintableError("boom")

    rig = make_rig(tmp_path, handler=router({lone_q(1): [osm_place(*DOWNTOWN, name="Test Residences 1")]}))
    out, stats = run([lone_row(1)], rig, flush=broken_flush)
    assert out[0].has_coordinates() and stats.flush_failures >= 1


# ---------------------------------------------------------------------------
# Audit additions: each test below fails when the hunk it pins is reverted (mutation-checked)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("types", [
    ("postal_town", "political"), ("sublocality_level_3", "sublocality", "political"), ("sublocality_level_4", "political"),
    ("sublocality_level_5", "political"), ("administrative_area_level_5", "political"), ("administrative_area_level_7", "political"),
])
def test_more_google_area_types_the_docs_list_are_areas(tmp_path, types):
    rig = make_rig(tmp_path, cfg_for("google"), replies=[FakeResponse(200, google_payload(*DOWNTOWN, types=types))])
    assert rig.geo.lookup(EXACT_Q, "Dubai").kind == "area"


@pytest.mark.parametrize("component", ["establishment", "point_of_interest", "natural_feature", "park", "airport"])
def test_every_google_name_component_feeds_the_name_check(tmp_path, component):
    row = mk_row("Masaar Residences", community="Example Community", area="")
    gcfg = cfg_for("google")
    payload = google_payload(*BUSINESS_BAY)
    payload["results"][0]["address_components"].append({"long_name": "Unrelated Depot", "short_name": "UD", "types": [component]})
    rig = make_rig(tmp_path, gcfg, handler=router({COMM_Q: google_payload(*DOWNTOWN, types=("neighborhood", "political")),
                                                   MASAAR_Q: payload}))
    [out], stats = run([row], rig, gcfg)
    assert out.location_precision == "community" and stats.rejections.get("name-mismatch") == 1


def test_a_google_component_that_is_only_a_number_is_no_name_so_the_check_cannot_judge(tmp_path):
    row = mk_row("Masaar Residences", community="Example Community", area="")
    gcfg = cfg_for("google")
    payload = google_payload(*BUSINESS_BAY)
    payload["results"][0]["address_components"].append({"long_name": "12", "short_name": "12", "types": ["premise"]})
    rig = make_rig(tmp_path, gcfg, handler=router({COMM_Q: google_payload(*DOWNTOWN, types=("neighborhood", "political")),
                                                   MASAAR_Q: payload}))
    [out], stats = run([row], rig, gcfg)
    assert out.location_precision == "exact" and stats.rejections.get("name-mismatch") is None


def test_a_geoapify_item_without_a_name_is_judged_by_the_first_part_of_its_formatted_address(tmp_path):
    cfg = cfg_for("geoapify")
    row = mk_row("Masaar Residences", community="Example Community", area="")
    payload = geoapify_payload(*BUSINESS_BAY)
    payload["features"][0]["properties"]["formatted"] = "Unrelated Depot, Example Community, Dubai, United Arab Emirates"
    rig = make_rig(tmp_path, cfg, handler=router({COMM_Q: geoapify_payload(*DOWNTOWN, result_type="suburb"), MASAAR_Q: payload}))
    [out], stats = run([row], rig, cfg)
    assert out.location_precision == "community" and stats.rejections.get("name-mismatch") == 1


def test_the_developer_brand_is_not_enough_to_make_a_hit_the_project_through_a_row(tmp_path):
    row = mk_row("Acme Hartland", developer="Acme Properties", community="Example Community", area="")
    q_exact = q("Acme Hartland")
    rig = make_rig(tmp_path, handler=router({COMM_Q: [osm_area(*DOWNTOWN)], q_exact: [osm_place(*BUSINESS_BAY, name="Acme Head Office")]}))
    [out], stats = run([row], rig)
    assert out.location_precision == "community" and stats.rejections.get("name-mismatch") == 1


def test_the_developer_suffix_is_taken_off_the_project_name_before_the_name_check(tmp_path):
    # "Beachfront by Emaar Properties": left whole, the word "Properties" (not a brand word) would let
    # "Emaar Properties Head Office" pass as the project.
    row = mk_row("Beachfront by Emaar Properties", developer="Emaar Properties", community="Example Community", area="")
    rig = make_rig(tmp_path, handler=router({COMM_Q: [osm_area(*DOWNTOWN)],
                                             q("Beachfront by Emaar Properties"): [osm_place(*BUSINESS_BAY, name="Emaar Properties Head Office")]}))
    [out], stats = run([row], rig)
    assert out.location_precision == "community" and stats.rejections.get("name-mismatch") == 1


@pytest.mark.parametrize("names,title,expected", [
    (["Creek Vista"], "Creeks Place", True),  # five letters: too short for the spelling rule, so the prefix rule must catch it
    (["Marina Vista"], "Dubaimarinatower", True),  # a glued word that merely contains the project word
    (["Vida II"], "Marina II", False),  # a Roman numeral never counts as agreement
    (["Vida IV"], "Marina IV", False),
])
def test_name_agrees_prefix_glued_and_roman_numeral_rules(names, title, expected):
    assert name_agrees(names, title) is expected


def test_a_zero_day_cooldown_still_remembers_a_miss_for_the_rest_of_the_same_day(tmp_path):
    cfg = Config(geocode_retry_cooldown_days=0)
    first = make_rig(tmp_path, cfg, handler=router({}))
    first.geo.begin_run(date(2026, 10, 1))
    first.geo.lookup(lone_q(1), "Dubai")
    first.geo.save_cache()
    same_day = make_rig(tmp_path, cfg, handler=router({}))
    same_day.geo.begin_run(date(2026, 10, 1))
    same_day.geo.lookup(lone_q(1), "Dubai")
    assert same_day.session.calls == []  # a repeat of the same query inside one day is not a new question
    next_day = make_rig(tmp_path, cfg, handler=router({}))
    next_day.geo.begin_run(date(2026, 10, 2))
    next_day.geo.lookup(lone_q(1), "Dubai")
    assert len(next_day.session.calls) == 1


@pytest.mark.parametrize("text", ["passwd=ABCD1234efgh", "pwd=ABCD1234efgh", "?user=a&pwd=ABCD1234efgh&x=1"])
def test_scrub_redacts_every_password_spelling(text):
    assert "ABCD1234efgh" not in scrub_secrets(text)


def _sa_values(**members: str) -> tuple[str, ...]:
    return gc._secret_values(Config(gcp_service_account_key=json.dumps({"type": "service_account", **members})))


@pytest.mark.parametrize("name", ["client_secret", "refresh_token", "db_password"])
def test_secret_values_registers_members_named_secret_token_or_password(name):
    assert "Zq9x7vLm2p4W" in _sa_values(**{name: "Zq9x7vLm2p4W"})


def test_secret_values_registers_a_short_member_whole():
    # Under eight characters it has no "line" of its own; the member itself must be registered.
    assert "Zq9x7v" in _sa_values(client_secret="Zq9x7v")


def test_a_private_key_quoted_without_its_line_breaks_is_redacted_to_the_last_character():
    pem = "-----BEGIN PRIVATE KEY-----\nFAKEFAKEFAKEFAKEFAKEFAKE\nzQ==\n-----END PRIVATE KEY-----\n"
    known = _sa_values(private_key=pem)
    cleaned = scrub_secrets("the key FAKEFAKEFAKEFAKEFAKEFAKEzQ== was refused", known)
    assert "zQ==" not in cleaned and "FAKEFAKE" not in cleaned


def test_a_short_multi_line_secret_quoted_as_it_is_is_redacted():
    known = _sa_values(client_secret="k7Q9\nm2")
    cleaned = scrub_secrets("refused k7Q9\nm2 as bad", known)
    assert "k7Q9" not in cleaned and "m2 as" not in cleaned


def test_a_multi_line_secret_quoted_with_an_escaped_line_break_is_redacted():
    known = _sa_values(client_secret="k7Q9\nm2")
    cleaned = scrub_secrets(r"refused k7Q9\nm2 as bad", known)
    assert "k7Q9" not in cleaned and "m2 as" not in cleaned
