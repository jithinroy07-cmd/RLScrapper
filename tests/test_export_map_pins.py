"""
Tests for scripts/export_map_pins.py (docs/DESIGN.md section 7).

Everything here is synthetic. The rows, listings and "secrets" are invented, the
developer is "Example Developer LLC", the "keys" are obviously fake, and nothing
touches the network: an autouse fixture makes any socket connection an error.

What these tests are really guarding:
  * the page can never show a centroid as an exact location;
  * nothing personal or secret can reach the published file, a log line or an
    exception message;
  * a half-written, swapped or foreign coordinate is dropped, never drawn;
  * the same input gives the same bytes, in any row order.
"""

from __future__ import annotations

import copy
import json
import logging
import random
import shutil
import socket
import subprocess
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import export_map_pins as exp
from src import log_policy
from src.config import Config
from src.listing_data import MAX_BEDROOMS, PROPERTY_TYPES, parse_price_per_sqft_aed
from src.log_policy import LOG_HANDLER_MARK
from src.models import PRECISIONS, RegistryRow

ROOT = Path(__file__).resolve().parent.parent
STAMP = "2026-10-25T03:00:00Z"
FAKE_KEY = "fake-key-1234567"
FAKE_PRIVATE = "fake-private-key-000000"
# Text a failing Google / OS call could echo (a cell, a row, a path). It has no secret shape on purpose:
# scrub_secrets leaves it alone, so only "never print the error text" keeps it out of a public CI log.
LEAK_MARKER = "Leaked-Cell-Marker-91823 Marina Tower 4"
# Assembled at run time: the repo hygiene scan (tests/test_repo_hygiene.py) reads this file's text and
# rejects a literal +971 number and a literal public IPv4 address, which these fixtures stand in for.
PHONE_TEXT = "+" + "971" + " 50 123 4567"


def dotted(*octets: int) -> str:
    return ".".join(str(octet) for octet in octets)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """A test that reaches for the network fails loudly instead of passing quietly."""

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(autouse=True)
def tidy_logging():
    """main() configures the process's logging (src/log_policy.py); put it back so tests do not leak into each other."""
    root, own = logging.getLogger(), (logging.getLogger("src"), logging.getLogger("export_map_pins"))
    saved = (root.level, [logger.level for logger in own])
    saved_hook = sys.excepthook  # main() also installs the log-safe hook for the process
    yield
    sys.excepthook = saved_hook
    for handler in [h for h in root.handlers if getattr(h, LOG_HANDLER_MARK, False)]:
        root.removeHandler(handler)
    logging.captureWarnings(False)
    root.setLevel(saved[0])
    for logger, level in zip(own, saved[1]):
        logger.setLevel(level)


# ---------------------------------------------------------------------------
# Synthetic data builders
# ---------------------------------------------------------------------------
def make_row(**overrides) -> RegistryRow:
    """# SYNTHETIC: a located Dubai project."""
    fields = dict(
        emirate="Dubai", registry="DLD", registry_project_no="1001",
        project_name_en="Test Residences A", developer="Example Developer LLC",
        latitude=25.2, longitude=55.27, location_precision="exact",
    )
    fields.update(overrides)
    return RegistryRow(**fields)


def make_sharjah(**overrides) -> RegistryRow:
    """# SYNTHETIC: a located Sharjah project."""
    fields = dict(
        emirate="Sharjah", registry="SRERD", registry_project_no="S-77",
        project_name_en="Test Residences B", developer="Example Developer LLC",
        latitude=25.33, longitude=55.4, location_precision="community",
    )
    fields.update(overrides)
    return RegistryRow(**fields)


def make_listing(**overrides) -> dict:
    """# SYNTHETIC: the shape listing_data.load_listing_records returns for one key."""
    record = dict(
        source_url="https://example.com/p/1", price_from=1_200_000, price_to=2_500_000,
        types=["Apartment"], bedrooms_min=0, bedrooms_max=3, handover="Q4 2027", as_of="2026-10-01",
    )
    record.update(overrides)
    return record


def build(rows, listings=None, **kwargs):
    return exp.build_geojson(rows, {} if listings is None else listings, generated_at=STAMP, **kwargs)


def only_props(collection) -> dict:
    assert len(collection["features"]) == 1, collection["features"]
    return collection["features"][0]["properties"]


def names(collection) -> list[str]:
    return [f["properties"]["name"] for f in collection["features"]]


# ---------------------------------------------------------------------------
# build_geojson: shape, coordinates, drops
# ---------------------------------------------------------------------------
class TestFeatureShape:
    def test_coordinates_are_lng_lat_and_the_collection_has_the_geojson_shape(self):
        out = build([make_row()])
        assert out["type"] == "FeatureCollection"
        feature = out["features"][0]
        assert feature["type"] == "Feature"
        assert feature["geometry"] == {"type": "Point", "coordinates": [55.27, 25.2]}

    def test_empty_input_is_a_valid_empty_collection(self):
        out = build([])
        assert out["features"] == []
        meta = out["meta"]
        assert (meta["total_projects"], meta["total_located"], meta["total_unlocated"]) == (0, 0, 0)
        assert meta["by_precision"] == {"exact": 0, "community": 0, "area": 0, "unknown": 0}
        assert json.loads(exp.serialise(out))["features"] == []

    def test_a_generator_of_rows_is_accepted(self):
        assert len(build((r for r in [make_row()]))["features"]) == 1

    def test_coordinates_are_rounded_not_inflated(self):
        out = build([make_row(latitude=25.123456789, longitude=55.187654321)])
        assert out["features"][0]["geometry"]["coordinates"] == [55.187654, 25.123457]


class TestDroppedRows:
    @pytest.mark.parametrize(
        "lat, lon, reason",
        [
            (None, None, "no_coordinates"),
            (25.2, None, "half_written_coordinates"),
            (None, 55.27, "half_written_coordinates"),
            (255.0, 55.27, "half_written_coordinates"),   # the model nulls an impossible latitude
            (55.27, 25.2, "out_of_range"),                # latitude and longitude swapped
            (48.85, 2.35, "out_of_range"),                # another continent
            (24.45, 54.38, "out_of_range"),               # Abu Dhabi city, outside the coarse gate
        ],
    )
    def test_a_bad_pair_is_counted_and_never_drawn(self, lat, lon, reason):
        out = build([make_row(latitude=lat, longitude=lon)])
        assert out["features"] == []
        assert out["meta"]["dropped"][reason] == 1
        assert sum(out["meta"]["dropped"].values()) == 1
        assert out["meta"]["total_projects"] == 1 and out["meta"]["total_unlocated"] == 1

    def test_a_zero_zero_pair_is_not_drawn(self):
        out = build([make_row(latitude=0.0, longitude=0.0)])
        assert out["features"] == []
        assert sum(out["meta"]["dropped"].values()) == 1

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_coordinates_that_bypass_validation_are_dropped_not_crashed_on(self, bad):
        # model_copy skips validation, which is how a bad value could still arrive.
        row = make_row().model_copy(update={"latitude": bad})
        out = build([row])
        assert out["features"] == []
        assert out["meta"]["dropped"]["out_of_range"] == 1
        exp.serialise(out)  # and the file stays writable (no NaN in it)

    def test_the_edges_of_the_coarse_gate_are_inside_it(self):
        # The corners are not in any emirate, so they now stop at the boundary test; what this pins is that the
        # rectangle itself is inclusive (an exclusive edge would count them as out_of_range instead).
        lat0, lat1, lon0, lon1 = 24.62, 25.70, 53.98, 56.60
        rows = [
            make_row(registry_project_no="1", latitude=lat0, longitude=lon0),
            make_row(registry_project_no="2", latitude=lat1, longitude=lon1),
        ]
        dropped = build(rows)["meta"]["dropped"]
        assert dropped["out_of_range"] == 0 and dropped["outside_emirate"] == 2


# ---------------------------------------------------------------------------
# The emirate boundary: the coarse rectangle is not enough
# ---------------------------------------------------------------------------
# SYNTHETIC rows placed at well-known PUBLIC spots. Every one is inside the coarse UAE rectangle, so only the
# boundary polygons can tell they are not in Dubai.
NOT_IN_DUBAI = {
    "Ajman": (25.4052, 55.5136),
    "Ras Al Khaimah": (25.6741, 55.9804),
    "Fujairah": (25.1288, 56.3265),
    "Umm Al Quwain": (25.5647, 55.5552),
    "the Gulf": (25.60, 54.30),
    "Abu Dhabi emirate": (24.70, 54.70),
    "Dibba Al-Hisn (Sharjah)": (25.62, 56.27),
    "Sharjah city": (25.33, 55.40),
}
DUBAI_PLACES = {
    "Downtown": (25.1972, 55.2744),
    "Palm Jumeirah": (25.1124, 55.139),
    "Jebel Ali": (24.99, 55.06),
    "southern desert": (24.80, 55.10),
    "Hatta exclave": (24.80, 56.12),
}
SHARJAH_PLACES = {
    "Sharjah city": (25.33, 55.40),
    "Kalba exclave": (25.07, 56.35),
    "Khor Fakkan exclave": (25.34, 56.35),
    "Dibba Al-Hisn exclave": (25.62, 56.27),
}


class TestEmirateBoundary:
    @pytest.mark.parametrize("place", sorted(NOT_IN_DUBAI))
    def test_a_dubai_row_pinned_outside_dubai_is_counted_and_never_drawn(self, place):
        # REGRESSION (audit): only the rectangle was checked, so a Dubai-labelled row at Ajman, Ras Al Khaimah,
        # Fujairah, Umm Al Quwain or in the sea was published as an exact Dubai pin.
        lat, lon = NOT_IN_DUBAI[place]
        out = build([make_row(latitude=lat, longitude=lon, location_precision="exact")])
        assert out["features"] == []
        assert out["meta"]["dropped"]["outside_emirate"] == 1
        assert sum(out["meta"]["dropped"].values()) == 1
        assert out["meta"]["by_emirate"]["Dubai"] == {"projects": 1, "located": 0}

    @pytest.mark.parametrize("place", sorted(DUBAI_PLACES))
    def test_real_dubai_places_are_kept_for_a_dubai_row(self, place):
        lat, lon = DUBAI_PLACES[place]
        props = only_props(build([make_row(latitude=lat, longitude=lon)]))
        assert props["emirate"] == "Dubai"

    @pytest.mark.parametrize("place", sorted(SHARJAH_PLACES))
    def test_real_sharjah_places_including_the_exclaves_are_kept_for_a_sharjah_row(self, place):
        lat, lon = SHARJAH_PLACES[place]
        props = only_props(build([make_sharjah(latitude=lat, longitude=lon)]))
        assert props["emirate"] == "Sharjah"

    def test_the_registry_decides_the_emirate_and_the_coordinates_must_agree(self):
        # a Sharjah row at a Dubai point, and a Dubai row at a Sharjah point, are both wrong on one side
        dubai_point = make_sharjah(latitude=25.2, longitude=55.27)
        sharjah_point = make_row(latitude=25.33, longitude=55.4)
        out = build([dubai_point, sharjah_point])
        assert out["features"] == [] and out["meta"]["dropped"]["outside_emirate"] == 2

    def test_a_hand_typed_or_overridden_pin_is_held_to_the_same_rule(self):
        # the geocoder applies a Geocode_Overrides pin "anyway" outside its emirate; the public file does not
        row = make_row(latitude=25.4052, longitude=55.5136, location_precision="exact", geocode_method="override")
        out = build([row])
        assert out["features"] == [] and out["meta"]["dropped"]["outside_emirate"] == 1

    def test_the_outside_check_comes_after_the_cheaper_ones(self):
        rows = [
            make_row(registry_project_no="1", latitude=None, longitude=None),
            make_row(registry_project_no="2", latitude=25.2, longitude=None),
            make_row(registry_project_no="3", latitude=48.85, longitude=2.35),
            make_row(registry_project_no="4", latitude=25.4052, longitude=55.5136),
        ]
        dropped = build(rows)["meta"]["dropped"]
        assert (dropped["no_coordinates"], dropped["half_written_coordinates"], dropped["out_of_range"], dropped["outside_emirate"]) == (1, 1, 1, 1)

    def test_every_drop_reason_is_always_reported_even_at_zero(self):
        assert list(build([make_row()])["meta"]["dropped"]) == list(exp.DROP_REASONS)
        assert {"unknown_emirate", "outside_emirate"} <= set(exp.DROP_REASONS)

    @pytest.mark.parametrize("bad", ["", "Ajman", " Dubai ", "dubai", "DUBAI", "Abu Dhabi", None, 5], ids=repr)
    def test_a_row_whose_emirate_is_not_exactly_dubai_or_sharjah_is_never_drawn(self, bad, caplog):
        # The model refuses such a row, so it can only arrive by bypassing validation. The emirate is never guessed
        # from the point, and a cell value is never a key of the public file or a line of the log.
        row = make_row().model_copy(update={"emirate": bad})
        with caplog.at_level(logging.INFO):
            out = build([row])
        assert out["features"] == []
        assert out["meta"]["dropped"]["unknown_emirate"] == 1 and sum(out["meta"]["dropped"].values()) == 1
        assert list(out["meta"]["by_emirate"]) == ["Dubai", "Sharjah"]
        assert out["meta"]["by_emirate"] == {"Dubai": {"projects": 0, "located": 0}, "Sharjah": {"projects": 0, "located": 0}}
        assert (out["meta"]["total_projects"], out["meta"]["total_unlocated"]) == (1, 1)
        assert "src.emirates" not in {record.name for record in caplog.records}   # which would echo the cell value

    def test_an_unknown_emirate_text_reaches_neither_the_log_nor_the_file(self, caplog):
        row = make_row().model_copy(update={"emirate": "Emirate-Marker-77"})
        with caplog.at_level(logging.DEBUG):
            out = build([row])
        assert "Emirate-Marker-77" not in caplog.text and b"Emirate-Marker-77" not in exp.serialise(out)

    def test_an_unknown_emirate_without_coordinates_is_just_unlocated(self):
        row = make_row(latitude=None, longitude=None).model_copy(update={"emirate": "Ajman"})
        dropped = build([row])["meta"]["dropped"]
        assert dropped["no_coordinates"] == 1 and dropped["unknown_emirate"] == 0

    def test_the_log_counts_the_drops_without_naming_anything(self, caplog):
        # the export step's log may be public: counts only, never a name, key, coordinate or emirate text
        outside = make_row(project_name_en="Fictional Tower", developer="Fictional Builder", latitude=25.4052, longitude=55.5136)
        unknown = make_row(registry_project_no="2", project_name_en="Fictional Court", developer="Fictional Builder").model_copy(
            update={"emirate": "Ajman"}
        )
        caplog.set_level(logging.INFO, logger="export_map_pins")
        build([outside, unknown])
        assert "1 row(s) have coordinates outside their registry emirate's boundary" in caplog.text
        assert "1 row(s) have an emirate other than Dubai or Sharjah" in caplog.text
        for piece in ("dld:", "name:", "Fictional", "fictional", "25.4052", "55.5136", "Ajman"):
            assert piece not in caplog.text, piece

    def test_a_better_placed_duplicate_is_not_displaced_by_one_outside_its_emirate(self):
        inside = make_row(location_precision="area", latitude=25.2, longitude=55.27)
        outside = make_row(location_precision="exact", latitude=25.4052, longitude=55.5136)   # same key, better precision, wrong place
        out = build([outside, inside])
        assert only_props(out)["precision"] == "area"
        assert out["meta"]["dropped"]["outside_emirate"] == 1 and out["meta"]["dropped"]["duplicate_key"] == 0


# ---------------------------------------------------------------------------
# Precision is data
# ---------------------------------------------------------------------------
class TestPrecision:
    def test_blank_precision_with_coordinates_is_unknown_never_exact(self):
        props = only_props(build([make_row(location_precision="")]))
        assert props["precision"] == "unknown"

    @pytest.mark.parametrize("given", ["centroid", "approximate", "rooftop", "0", "n/a", "exactly"])
    def test_an_unrecognised_precision_cannot_become_exact(self, given):
        assert only_props(build([make_row(location_precision=given)]))["precision"] == "unknown"

    @pytest.mark.parametrize("given, expected", [("exact", "exact"), (" EXACT ", "exact"), ("Community", "community"), ("area", "area")])
    def test_known_precisions_are_kept(self, given, expected):
        assert only_props(build([make_row(location_precision=given)]))["precision"] == expected

    def test_every_pin_has_a_precision_the_page_knows(self):
        rows = [make_row(registry_project_no=str(i), location_precision=p) for i, p in enumerate(["exact", "community", "area", ""], 1)]
        precisions = {f["properties"]["precision"] for f in build(rows)["features"]}
        assert precisions == {*PRECISIONS, "unknown"}

    def test_meta_counts_per_precision_and_per_emirate(self):
        rows = [
            make_row(registry_project_no="1"),
            make_row(registry_project_no="2", location_precision=""),
            make_row(registry_project_no="3", latitude=None, longitude=None),
            make_sharjah(),
        ]
        meta = build(rows)["meta"]
        assert meta["by_precision"] == {"exact": 1, "community": 1, "area": 0, "unknown": 1}
        assert meta["by_emirate"] == {"Dubai": {"projects": 3, "located": 2}, "Sharjah": {"projects": 1, "located": 1}}
        assert (meta["total_projects"], meta["total_located"], meta["total_unlocated"]) == (4, 3, 1)
        assert meta["dropped"]["no_coordinates"] == 1


# ---------------------------------------------------------------------------
# Order and determinism
# ---------------------------------------------------------------------------
class TestOrderAndDeterminism:
    def _mixed_rows(self):
        return [
            make_sharjah(registry_project_no="S-1", project_name_en="Sharjah Exact", location_precision="exact"),
            make_row(registry_project_no="10", project_name_en="beta tower"),
            make_row(registry_project_no="11", project_name_en="Alpha tower"),
            make_row(registry_project_no="12", project_name_en="Comm", location_precision="community"),
            make_row(registry_project_no="13", project_name_en="Area", location_precision="area"),
            make_row(registry_project_no="14", project_name_en="Unknown prec", location_precision=""),
        ]

    def test_exact_first_then_emirate_then_name_ignoring_case(self):
        assert names(build(self._mixed_rows())) == [
            "Alpha tower", "beta tower", "Sharjah Exact", "Comm", "Area", "Unknown prec",
        ]

    def test_any_row_order_gives_identical_bytes(self):
        rows = self._mixed_rows()
        listings = {rows[1].project_key: make_listing()}
        reference = exp.serialise(build(rows, listings))
        rng = random.Random(20261025)
        for _ in range(25):
            shuffled = rows[:]
            rng.shuffle(shuffled)
            assert exp.serialise(build(shuffled, listings)) == reference

    def test_the_same_input_twice_is_identical(self):
        rows = self._mixed_rows()
        assert build(rows) == build(rows)

    def test_the_function_never_reads_the_clock_or_the_disk(self, monkeypatch):
        real = exp.utc_now_iso

        def guarded(now=None):
            assert now is not None, "build_geojson asked for the current time"
            return real(now)

        def forbidden(*args, **kwargs):
            raise AssertionError("build_geojson touched the filesystem")

        monkeypatch.setattr(exp, "utc_now_iso", guarded)
        monkeypatch.setattr(exp.tempfile, "NamedTemporaryFile", forbidden)
        monkeypatch.setattr(exp.os, "replace", forbidden)
        assert build(self._mixed_rows())["meta"]["generated_at"] == STAMP

    def test_inputs_are_not_mutated(self):
        rows = self._mixed_rows()
        listings = {rows[0].project_key: make_listing(), "other": make_listing(types=["Villa"])}
        rows_before = [r.model_dump() for r in rows]
        listings_before = copy.deepcopy(listings)
        build(rows, listings)
        assert [r.model_dump() for r in rows] == rows_before
        assert listings == listings_before

    def test_a_large_input_stays_unique_and_ordered(self):
        rows = [
            make_row(registry_project_no=str(i), project_name_en=f"Test Residences {i:05d}",
                     latitude=25.0 + (i % 20) / 100, location_precision=("exact" if i % 3 == 0 else "area"))
            for i in range(1, 1501)
        ]
        out = build(rows)
        keys = [f["properties"]["project_key"] for f in out["features"]]
        assert len(keys) == len(set(keys)) == 1500
        precisions = [f["properties"]["precision"] for f in out["features"]]
        assert precisions == sorted(precisions, key=exp.PRECISION_ORDER.index)


class TestDuplicatesAndMissingIdentity:
    def test_two_rows_with_one_key_export_once_and_the_better_placed_wins(self):
        exact = make_row(project_name_en="Test Residences A", location_precision="exact", latitude=25.2)
        vague = make_row(project_name_en="Test Residences A", location_precision="area", latitude=25.25)
        for order in ([exact, vague], [vague, exact]):
            out = build(order)
            assert len(out["features"]) == 1
            assert only_props(out)["precision"] == "exact"
            assert out["meta"]["dropped"]["duplicate_key"] == 1
            assert out["meta"]["total_projects"] == 2 and out["meta"]["total_unlocated"] == 1

    def test_the_log_counts_the_duplicates_without_naming_their_key(self, caplog):
        # a key is a registry number or a developer and project name, and the export step's log may be public
        vague = make_row(project_name_en="Test Residences A", developer="Fictional Builder", location_precision="area")
        exact = make_row(project_name_en="Test Residences A", developer="Fictional Builder", location_precision="exact")
        caplog.set_level(logging.INFO, logger="export_map_pins")
        build([exact, vague])
        assert "1 row(s) share a project key" in caplog.text
        for piece in ("dld:", "name:", "Fictional", "fictional", "Test Residences"):
            assert piece not in caplog.text, piece

    def test_rows_with_no_identity_are_kept_and_counted_not_merged(self):
        a = make_row(registry="SEED", registry_project_no=None, developer="", project_name_en="Test Residences C")
        b = make_row(registry="SEED", registry_project_no=None, developer="", project_name_en="Test Residences D",
                     latitude=25.21)
        assert a.project_key == "" and b.project_key == ""
        out = build([a, b])
        assert len(out["features"]) == 2
        assert out["meta"]["without_identity"] == 2
        assert all("project_key" not in f["properties"] for f in out["features"])
        assert out["meta"]["dropped"]["duplicate_key"] == 0


# ---------------------------------------------------------------------------
# Joining listings
# ---------------------------------------------------------------------------
class TestListingJoin:
    def test_a_listing_joins_on_any_identity_key(self):
        row = make_row()
        assert len(row.identity_keys()) == 2
        for key in row.identity_keys():
            props = only_props(build([row], {key: make_listing()}))
            assert props["price_from"] == 1_200_000, key

    def test_an_unrelated_key_joins_nothing(self):
        props = only_props(build([make_row()], {"dld:dubai:9999": make_listing(), "name:dubai:x:y": make_listing()}))
        assert "price_from" not in props and "source_url" not in props

    def test_the_newer_as_of_wins_whichever_key_it_sits_under(self):
        row = make_row()
        registry_key, name_key = row.identity_keys()
        older = make_listing(price_from=1_000_000, price_to=2_000_000, as_of="2026-09-01", source_url="https://example.com/old")
        newer = make_listing(price_from=1_500_000, price_to=2_100_000, as_of="2026-10-01", source_url="https://example.com/new")
        for listings in ({registry_key: older, name_key: newer}, {registry_key: newer, name_key: older}):
            props = only_props(build([row], listings))
            assert (props["price_from"], props["source_url"]) == (1_500_000, "https://example.com/new")

    def test_a_tie_goes_to_the_registry_key_and_one_record_is_used_whole(self):
        row = make_row()
        registry_key, name_key = row.identity_keys()
        first = make_listing(price_from=1_000_000, price_to=2_000_000, source_url="https://example.com/registry-entry", handover="")
        second = make_listing(price_from=9_000_000, price_to=9_500_000, source_url="https://example.com/name-entry", handover="Q1 2029")
        props = only_props(build([row], {registry_key: first, name_key: second}))
        assert props["price_from"] == 1_000_000 and props["source_url"].endswith("registry-entry")
        assert "handover" not in props   # not borrowed from the other record

    def test_a_name_key_shared_by_two_registry_projects_joins_neither(self):
        # REGRESSION (audit): DESIGN section 2 says two rows with different registry ids of one authority are
        # different projects even when their name keys match. A listing typed under the shared name key
        # cannot be told which of them it describes, so it was published on BOTH pins (the same price
        # and source on two projects). Blank beats wrong: neither shows it.
        one = make_row(registry_project_no="1001")
        two = make_row(registry_project_no="1002", latitude=25.21, longitude=55.28)
        assert one.name_key() == two.name_key() and one.registry_key() != two.registry_key()
        listings = {one.name_key(): make_listing()}
        for props in (p["properties"] for p in build([one, two], listings)["features"]):
            assert "price_from" not in props and "source_url" not in props, props

    def test_the_registry_key_still_joins_when_the_name_key_is_shared(self):
        one = make_row(registry_project_no="1001")
        two = make_row(registry_project_no="1002", latitude=25.21, longitude=55.28)
        listings = {one.name_key(): make_listing(price_from=1), one.registry_key(): make_listing(price_from=5_000_000, price_to=6_000_000)}
        by_key = {p["properties"]["project_key"]: p["properties"] for p in build([one, two], listings)["features"]}
        assert by_key[one.registry_key()]["price_from"] == 5_000_000
        assert "price_from" not in by_key[two.registry_key()]

    def test_a_duplicate_of_one_project_does_not_make_its_name_key_ambiguous(self):
        # the same registry key twice is one project, not two
        row = make_row()
        twin = make_row(latitude=25.2001)
        assert row.registry_key() == twin.registry_key()
        features = build([row, twin], {row.name_key(): make_listing()})["features"]
        assert features and all(f["properties"].get("price_from") == 1_200_000 for f in features)

    def test_listings_may_be_none_or_empty(self):
        assert len(build([make_row()], None)["features"]) == 1
        assert len(build([make_row()], {})["features"]) == 1


class TestListingFacts:
    def _price_props(self, **overrides):
        return only_props(build([make_row()], {make_row().project_key: make_listing(**overrides)}))

    @pytest.mark.parametrize(
        "bad",
        [1_250_000.5, "1,250,000", "1250000", True, float("nan"), float("inf"), -5, 0, 10**10 + 1, 10**400, None, [1], {"a": 1}],
        ids=repr,
    )
    def test_a_price_that_is_not_a_plausible_whole_number_is_blank_not_guessed(self, bad):
        props = self._price_props(price_from=bad, price_to=2_000_000)
        assert "price_from" not in props
        assert props["price_to"] == 2_000_000

    def test_price_boundaries_and_integer_output(self):
        props = self._price_props(price_from=1.0, price_to=10**10, price_per_sqft=exp.MAX_PRICE_PER_SQFT_AED)
        assert (props["price_from"], props["price_to"], props["price_per_sqft"]) == (1, 10**10, exp.MAX_PRICE_PER_SQFT_AED)
        assert type(props["price_from"]) is int
        assert "price_per_sqft" not in self._price_props(price_per_sqft=exp.MAX_PRICE_PER_SQFT_AED + 1)

    def test_a_total_pasted_into_the_per_sq_ft_column_is_not_published(self):
        # REGRESSION (audit): the ceiling was 10**6, so "850,000" (a TOTAL price) was published as AED 850,000 per sq ft.
        pasted = parse_price_per_sqft_aed("850,000")
        assert pasted == 850_000   # the parser cannot tell; the exporter is the gate
        props = self._price_props(price_per_sqft=pasted)
        assert "price_per_sqft" not in props
        assert props["price_from"] == 1_200_000   # the rest of the record survives

    def test_the_per_sq_ft_ceiling_is_tight_but_real_rates_pass(self):
        assert exp.MAX_PRICE_PER_SQFT_AED == 100_000
        for rate in (1, 1_500, 12_000, 45_000, 100_000):
            assert self._price_props(price_per_sqft=rate)["price_per_sqft"] == rate, rate
        for total in (100_001, 250_000, 850_000, 10**6):
            assert "price_per_sqft" not in self._price_props(price_per_sqft=total), total

    def test_a_range_that_runs_backwards_loses_both_ends(self):
        props = self._price_props(price_from=3_000_000, price_to=1_000_000)
        assert "price_from" not in props and "price_to" not in props
        props = self._price_props(bedrooms_min=4, bedrooms_max=1)
        assert "bedrooms_min" not in props and "bedrooms_max" not in props
        assert props["price_from"] == 1_200_000   # the rest of the record survives

    def test_bedrooms_zero_is_a_studio_and_the_ceiling_is_the_models(self):
        props = self._price_props(bedrooms_min=0, bedrooms_max=MAX_BEDROOMS)
        assert (props["bedrooms_min"], props["bedrooms_max"]) == (0, MAX_BEDROOMS)
        for bad in (-1, MAX_BEDROOMS + 1, 2.5, "3", True):
            props = self._price_props(bedrooms_min=bad, bedrooms_max=3)
            assert "bedrooms_min" not in props, bad
            assert props["bedrooms_max"] == 3

    def test_a_single_known_bedroom_bound_is_kept_alone(self):
        props = self._price_props(bedrooms_min=None, bedrooms_max=2)
        assert props["bedrooms_max"] == 2 and "bedrooms_min" not in props

    @pytest.mark.parametrize(
        "source",
        [
            None, "", "ftp://example.com/x", "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,hi", "//example.com/x",
            "https://", "example.com/no-scheme", " https://example.com", "https://exa mple.com", "https://example.com:notaport/",
            "https://[bad", 5, ["https://example.com"], "https://example.com/" + "a" * 2001,
        ],
        ids=lambda v: repr(v)[:40],
    )
    def test_a_fact_without_a_publishable_source_is_not_published_at_all(self, source):
        out = build([make_row()], {make_row().project_key: make_listing(source_url=source)})
        props = only_props(out)
        assert not {"price_from", "price_to", "bedrooms_min", "handover", "property_types", "source_url", "as_of"} & props.keys()
        assert out["meta"]["with_listing"] == 0

    def test_a_listing_with_a_source_but_no_facts_adds_nothing(self):
        record = {"source_url": "https://example.com/p/1"}
        out = build([make_row()], {make_row().project_key: record})
        assert "source_url" not in only_props(out) and out["meta"]["with_listing"] == 0

    @pytest.mark.parametrize("record", [None, "text", 5, [], ["a"], 3.5], ids=repr)
    def test_a_record_that_is_not_a_mapping_is_ignored(self, record):
        out = build([make_row()], {make_row().project_key: record})
        assert "price_from" not in only_props(out)

    def test_the_source_url_is_scrubbed_before_it_is_published(self):
        url = "https://user:pw@example.com/p?api_key=abc&unit=2#access_token=zz"
        props = only_props(build([make_row()], {make_row().project_key: make_listing(source_url=url)}))
        assert props["source_url"] == "https://example.com/p?unit=2"
        for leak in ("user", "pw", "api_key", "abc", "access_token", "zz"):
            assert leak not in props["source_url"]

    def test_http_is_allowed_as_well_as_https(self):
        props = only_props(build([make_row()], {make_row().project_key: make_listing(source_url="http://example.com/p")}))
        assert props["source_url"] == "http://example.com/p"

    def test_property_types_are_known_unique_and_never_unknown(self):
        dirty = ["Villa", "Villa", "Unknown", "Spaceship", 5, None, ["Villa"], "Apartment"]
        props = self._price_props(types=dirty)
        assert props["property_types"] == ["Villa", "Apartment"]
        assert set(props["property_types"]) <= set(PROPERTY_TYPES) - {"Unknown"}
        assert "property_types" not in self._price_props(types=["Unknown"])
        assert "property_types" not in self._price_props(types=[])

    def test_property_types_given_as_text_are_parsed(self):
        props = self._price_props(types="Apartment, Villa")
        assert set(props["property_types"]) == {"Apartment", "Villa"}

    @pytest.mark.parametrize("given, expected", [("25/10/2026", "2026-10-25"), ("2026-10-25T03:00:00Z", "2026-10-25")])
    def test_as_of_is_normalised_and_garbage_is_left_out(self, given, expected):
        assert self._price_props(as_of=given)["as_of"] == expected
        assert "as_of" not in self._price_props(as_of="soonish")
        assert "as_of" not in self._price_props(as_of=None)


class TestHandover:
    @pytest.mark.parametrize("text", ["Q4 2027", "2027-2028", "12/2027", "H2 2028", "Ready", "الربع الرابع 2027"])
    def test_ordinary_handover_wording_is_kept(self, text):
        assert exp._handover(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            f"call {PHONE_TEXT}", "info@example.com", "see https://example.com/x", "www.example.com", "0501234567 ask",
            "+9715012 now", "(04) 123-45678", "x" * 61,
        ],
    )
    def test_text_that_could_carry_contact_details_is_dropped(self, text):
        assert exp._handover(text) == ""

    @pytest.mark.parametrize(
        "text",
        [
            "call Ahmed 050/123/4567", "050,123,4567", "050;123;4567", "050_123_4567", "050 / 123 / 4567", "050|123|4567",
            "050·123·4567", "050–123–4567", "050—123—4567",
        ],
    )
    def test_a_phone_number_is_dropped_whatever_separates_its_digits(self, text):
        # REGRESSION (map review): only space . ( ) - joined digit runs, so a number typed with a
        # slash, comma, semicolon, underscore or pipe was published to the public file.
        assert exp._handover(text) == ""

    @pytest.mark.parametrize(
        "text",
        [
            "wa.me/c/9715", "t.me/john_smith", "bit.ly/3abcd", "instagram.com/dev", "example.com",
            "john(at)example.com", "ahmed [at] gmail", "ahmed at example dot com",
        ],
    )
    def test_a_link_or_handle_without_a_scheme_is_dropped(self, text):
        # REGRESSION (map review): only "@", "http(s):" and "www." counted as a link.
        assert exp._handover(text) == ""

    @pytest.mark.parametrize(
        "text", ["Q4 2027", "Dec 2027 - Mar 2028", "End of 2027", "Q4 2027 (est.)", "Mid-2028", "Handed over", "H1 2029", "Jan.-Mar. 2028"]
    )
    def test_the_stricter_contact_rules_keep_ordinary_wording(self, text):
        assert exp._handover(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            "+971\u200e50\u200e123\u200e4567",   # left-to-right marks, as pasted from a chat app
            "050\u200b1234567",                  # zero-width space
            "050\u200c123\u200c4567",            # zero-width non-joiner
            "050\u00ad123\u00ad4567",            # soft hyphen
            "+971 50\u200f 123\u200f 4567",      # right-to-left marks
            "05\ufeff0123\ufeff4567",            # byte-order mark / zero-width no-break space
            "ahmed\u200b@example.com",
        ],
    )
    def test_invisible_characters_cannot_hide_a_phone_number_or_an_address(self, text):
        # REGRESSION (map review): the digit-run check was split by invisible format characters
        # (common in numbers copied from chat apps and web pages), so the number was published.
        assert exp._handover(text) == ""

    def test_invisible_characters_in_ordinary_wording_are_not_a_contact(self):
        assert exp._handover("Q4\u200e 2027") == "Q4\u200e 2027"

    @pytest.mark.parametrize(
        "text",
        [
            "ahmed\uff20gmail",                                              # full-width "@"
            "ahmed\ufe6bgmail",                                              # small commercial at
            "ahmed\uff20example\uff0ecom",                                   # full-width "@" and "."
            "write to example\u3002com",                                     # an ideographic full stop reads as "."
            "\u2460\u2461\u2462\u2463\u2464\u2465\u2466\u2467\u2468",        # nine circled digits
            "\uff0b971\uff15\uff10\uff11\uff12\uff13\uff14\uff15\uff16\uff17",   # full-width "+" and digits
            "\u2468\u2467\u2466\u2465\u2464\u2463\u2462\u2461\u2460 call",
        ],
    )
    def test_a_look_alike_spelling_cannot_hide_an_address_or_a_number(self, text):
        # REGRESSION (audit): the checks ran on the raw text, so a full-width "@" or circled digits passed.
        assert exp._handover(text) == ""

    @pytest.mark.parametrize("text", ["Q4\u2026 2027", "H1\u20112028", "\u2163 quarter 2027", "\uff31\uff14 2027"])
    def test_folding_does_not_turn_ordinary_wording_into_a_contact(self, text):
        assert exp._handover(text) == text

    def test_the_phone_thresholds_sit_exactly_where_documented(self):
        assert exp._looks_like_contact("+971501") is False       # 6 digits after a plus
        assert exp._looks_like_contact("+9715012") is True       # 7 digits after a plus
        assert exp._looks_like_contact("12345678") is False      # 8 digits, no plus
        assert exp._looks_like_contact("123456789") is True      # 9 digits

    def test_the_length_limit_is_exact(self):
        assert exp._handover("x" * exp.MAX_HANDOVER_CHARS) == "x" * exp.MAX_HANDOVER_CHARS
        assert exp._handover("x" * (exp.MAX_HANDOVER_CHARS + 1)) == ""

    @pytest.mark.parametrize("value", [None, 5, [], float("nan"), True])
    def test_a_non_text_handover_is_blank(self, value):
        assert exp._handover(value) in ("", "5") and exp._handover(value) != "None"


# ---------------------------------------------------------------------------
# Privacy: the allow-list is the only way into the file
# ---------------------------------------------------------------------------
class TestAllowList:
    MARKERS = (
        "ESCROW-MARKER", "PERMIT-MARKER", "MAPPIN-MARKER", "DATASET-MARKER", "METHOD-MARKER", "ENTERED-BY-MARKER",
        "PAYPLAN-MARKER", "LICENCE-MARKER", "CONTACT-MARKER", "NOTES-MARKER", "LISTING-PERMIT-MARKER",
    )

    def _loaded(self):
        row = make_row(
            escrow_agent="ESCROW-MARKER Bank", permit_no="PERMIT-MARKER-77", map_pin_link="https://example.com/MAPPIN-MARKER",
            source_dataset="DATASET-MARKER", geocode_method="METHOD-MARKER", master_project="Test Community",
            area_en="Test Area", status="ACTIVE", percent_completed=66.666, units=120,
            completion_date="2027-12-31", project_name_ar="مشروع تجريبي",
        )
        listing = make_listing(
            price_per_sqft=1500, entered_by="ENTERED-BY-MARKER", payment_plan=f"PAYPLAN-MARKER call {PHONE_TEXT}",
            source_licence="LICENCE-MARKER", contact="CONTACT-MARKER", notes="NOTES-MARKER", permit_no="LISTING-PERMIT-MARKER",
        )
        return row, listing

    def test_no_internal_or_contact_field_reaches_the_file(self):
        row, listing = self._loaded()
        text = exp.serialise(build([row], {row.project_key: listing})).decode("utf-8")
        for marker in self.MARKERS:
            assert marker not in text, marker
        assert "971" not in text

    def test_every_property_is_on_the_allow_list_and_a_full_pin_uses_most_of_it(self):
        row, listing = self._loaded()
        props = only_props(build([row], {row.project_key: listing}))
        assert set(props) <= set(exp.ALLOWED_PROPERTIES)
        assert list(props) == [n for n in exp.ALLOWED_PROPERTIES if n in props]   # fixed order
        assert set(props) == set(exp.ALLOWED_PROPERTIES)

    def test_the_allow_list_has_no_forbidden_names_and_no_duplicates(self):
        assert len(set(exp.ALLOWED_PROPERTIES)) == len(exp.ALLOWED_PROPERTIES)
        for name in exp.ALLOWED_PROPERTIES:
            assert not any(word in name for word in ("escrow", "permit", "entered", "payment", "phone", "email", "contact", "licence"))

    def test_values_are_normalised(self):
        row, listing = self._loaded()
        props = only_props(build([row], {row.project_key: listing}))
        assert props["percent_completed"] == 66.67
        assert props["units"] == 120
        assert props["registry"] == "DLD" and props["registry_no"] == "1001"
        assert props["project_key"] == "dld:dubai:1001"
        assert only_props(build([make_row(percent_completed=100.0)]))["percent_completed"] == 100
        assert type(only_props(build([make_row(percent_completed=100.0)]))["percent_completed"]) is int

    def test_blank_values_are_omitted_but_the_name_is_always_present(self):
        props = only_props(build([make_row(developer="", project_name_en="", area_en="", units=0)]))
        assert props["name"] == ""
        for absent in ("developer", "area", "master_project", "status", "units", "percent_completed", "name_ar", "price_from"):
            assert absent not in props, absent


class TestTextHygiene:
    def test_arabic_is_kept_and_the_file_is_utf8_not_escaped(self):
        row = make_row(project_name_en="Test Residences A", project_name_ar="مشروع تجريبي")
        out = build([row])
        assert only_props(out)["name_ar"] == "مشروع تجريبي"
        raw = exp.serialise(out)
        assert "مشروع تجريبي".encode("utf-8") in raw
        assert b"\\u0645" not in raw

    def test_controls_and_bidi_overrides_are_removed_from_names(self):
        row = make_row(project_name_en="Test\u202e Resi\x00dences\u2066 A\x07")
        name = only_props(build([row]))["name"]
        assert name == "Test Resi dences A"

    def test_whitespace_is_collapsed_and_long_text_is_cut_with_a_marker(self):
        props = only_props(build([make_row(project_name_en="  Test \n\t Residences   A  ", area_en="y" * 5000)]))
        assert props["name"] == "Test Residences A"
        assert len(props["area"]) == exp.MAX_TEXT_CHARS and props["area"].endswith("…")

    def test_a_lone_surrogate_cannot_make_the_file_unwritable(self):
        # The model refuses a lone surrogate, so it can only arrive in raw listing text.
        row = make_row()
        listing = make_listing(handover="Q4 \ud800 2027")
        raw = exp.serialise(build([row], {row.project_key: listing}))
        assert json.loads(raw.decode("utf-8"))["features"][0]["properties"]["handover"].startswith("Q4 ")
        with pytest.raises(Exception):
            make_row(project_name_en="Test \ud800 Residences")

    @pytest.mark.parametrize("value", [None, True, False, float("nan"), float("inf"), [], ["a"], {"a": 1}, (1, 2), {1}, b"bytes", object()], ids=repr)
    def test_clean_str_blanks_things_that_are_not_text(self, value):
        assert exp._clean_str(value) == ""

    def test_clean_str_keeps_plain_numbers_as_text(self):
        assert exp._clean_str(2027) == "2027"
        assert exp._clean_str(2027.5) == "2027.5"

    def test_a_structure_in_a_listing_cell_is_not_published_as_its_repr(self):
        row = make_row()
        for junk in (["Q4 2027"], {"when": "Q4 2027"}, object()):
            props = only_props(build([row], {row.project_key: make_listing(handover=junk)}))
            assert "handover" not in props


# ---------------------------------------------------------------------------
# build_geojson: argument handling
# ---------------------------------------------------------------------------
class TestArguments:
    @pytest.mark.parametrize("bad", [None, "rows", b"rows", {"a": 1}, 5])
    def test_rows_must_be_an_iterable_of_registry_rows(self, bad):
        with pytest.raises(TypeError):
            build(bad)

    def test_a_non_row_inside_the_rows_is_loud(self):
        with pytest.raises(TypeError, match="RegistryRow"):
            build([make_row(), {"emirate": "Dubai"}])

    @pytest.mark.parametrize("bad", [[], "x", 5, ("a", "b")])
    def test_listings_must_be_a_mapping(self, bad):
        with pytest.raises(TypeError, match="listings"):
            build([make_row()], bad)

    @pytest.mark.parametrize("bad", [-1, True, 1.5, "2", None])
    def test_unreadable_rows_must_be_a_non_negative_integer(self, bad):
        with pytest.raises(ValueError, match="unreadable_rows"):
            build([make_row()], unreadable_rows=bad)

    def test_unreadable_rows_are_reported_in_meta(self):
        assert build([make_row()], unreadable_rows=3)["meta"]["unreadable_rows"] == 3

    def test_generated_at_is_keyword_only(self):
        with pytest.raises(TypeError):
            exp.build_geojson([make_row()], {}, STAMP)   # type: ignore[misc]

    @pytest.mark.parametrize(
        "given, expected",
        [
            ("2026-10-25T03:00:00Z", "2026-10-25T03:00:00Z"),
            ("2026-10-25T03:00:00z", "2026-10-25T03:00:00Z"),
            ("2026-10-25T07:00:00+04:00", "2026-10-25T03:00:00Z"),
            ("  2026-10-25T03:00:00+00:00  ", "2026-10-25T03:00:00Z"),
            (datetime(2026, 10, 25, 7, 0, tzinfo=timezone(timedelta(hours=4))), "2026-10-25T03:00:00Z"),
            (datetime(2026, 10, 25, 3, 0, 5, 999999, tzinfo=timezone.utc), "2026-10-25T03:00:05Z"),
        ],
    )
    def test_generated_at_is_normalised_to_utc(self, given, expected):
        assert exp.build_geojson([], {}, generated_at=given)["meta"]["generated_at"] == expected

    @pytest.mark.parametrize("bad", ["2026-10-25T03:00:00", "yesterday", "", "2026-13-45T00:00:00Z", datetime(2026, 10, 25)])
    def test_generated_at_without_a_zone_or_not_a_timestamp_is_a_value_error(self, bad):
        with pytest.raises(ValueError):
            exp.build_geojson([], {}, generated_at=bad)

    @pytest.mark.parametrize("bad", [None, 20261025, 1.5, b"2026-10-25T03:00:00Z"])
    def test_generated_at_of_the_wrong_type_is_a_type_error(self, bad):
        with pytest.raises(TypeError):
            exp.build_geojson([], {}, generated_at=bad)

    def test_meta_carries_the_attribution_the_licence_requires(self):
        meta = build([make_row()])["meta"]
        assert meta["schema"] == exp.SCHEMA_VERSION == 1
        assert any("OpenStreetMap contributors" in line for line in meta["attribution"])
        assert any("ODbL" in line for line in meta["attribution"])

    def test_meta_counts_listing_coverage(self):
        rows = [make_row(registry_project_no="1"), make_row(registry_project_no="2", project_name_en="Test Residences B"),
                make_row(registry_project_no="3", project_name_en="Test Residences C")]
        listings = {
            rows[0].project_key: make_listing(),
            rows[1].project_key: make_listing(price_from=None, price_to=None, bedrooms_min=None, bedrooms_max=None),
        }
        meta = build(rows, listings)["meta"]
        assert (meta["with_listing"], meta["with_price"], meta["with_bedrooms"]) == (2, 1, 1)


# ---------------------------------------------------------------------------
# sanitize_public_url
# ---------------------------------------------------------------------------
class TestSanitizePublicUrl:
    @pytest.mark.parametrize(
        "given, expected",
        [
            ("https://example.com/a?b=1&c=2", "https://example.com/a?b=1&c=2"),
            ("HTTPS://Example.COM/Path", "https://example.com/Path"),
            ("https://user:pw@example.com/x", "https://example.com/x"),
            ("https://token@example.com/x", "https://example.com/x"),
            ("https://example.com/x?api_key=abc&a=1", "https://example.com/x?a=1"),
            ("https://example.com/x?Token=abc", "https://example.com/x"),
            ("https://example.com/x?key=", "https://example.com/x"),
            ("https://example.com/x?X-Amz-Signature=abc&X-Amz-Credential=q&ok=1", "https://example.com/x?ok=1"),
            ("https://example.com/x#access_token=abc", "https://example.com/x"),
            ("https://example.com/x#section-2", "https://example.com/x#section-2"),
            ("https://example.com:8443/x?a=2", "https://example.com:8443/x?a=2"),
            # (a public address: the documentation range 2001:db8::/32 this once used is not reachable, so it is withheld now)
            ("http://[2606:4700:4700::1111]:8080/x", "http://[2606:4700:4700::1111]:8080/x"),
            # A query that needs no cleaning is kept exactly as the analyst wrote it ...
            ("https://example.com/p?lang=ar&unit=ب", "https://example.com/p?lang=ar&unit=ب"),
            # ... and one that is rebuilt after a secret was removed is properly percent-encoded.
            ("https://example.com/p?token=zz&unit=ب", "https://example.com/p?unit=%D8%A8"),
        ],
    )
    def test_secrets_are_removed_and_the_rest_is_kept(self, given, expected):
        assert exp.sanitize_public_url(given) == expected

    @pytest.mark.parametrize(
        "bad",
        [
            "javascript:alert(1)", "data:text/html,x", "ftp://example.com", "https://", "https://exa mple.com", " https://example.com",
            "https://example.com ", "https://example.com:notaport/", "https://[bad", None, 5, b"https://example.com", "", "\n",
            "https://example.com/" + "a" * 2001,
        ],
        ids=lambda v: repr(v)[:40],
    )
    def test_anything_that_is_not_a_clean_http_url_is_blank(self, bad):
        assert exp.sanitize_public_url(bad) == ""

    def test_the_longest_allowed_url_passes(self):
        url = "https://example.com/" + "a" * (exp.MAX_URL_CHARS - len("https://example.com/"))
        assert exp.sanitize_public_url(url) == url

    @pytest.mark.parametrize(
        "given, expected",
        [
            ("https://example.com/p?email=ahmed%40example.com&x=1", "https://example.com/p?x=1"),
            ("https://example.com/p?e=ahmed@example.com&x=1", "https://example.com/p?x=1"),
            ("https://example.com/p?phone=0501234567", "https://example.com/p"),
            ("https://example.com/p?mobile=%2B971501234567&a=1", "https://example.com/p?a=1"),
            ("https://example.com/p?Contact_Name=Ahmed&a=1", "https://example.com/p?a=1"),
            ("https://example.com/p#email=a@b.com", "https://example.com/p"),
            ("https://example.com/p?jwt=eyJabc.def.ghi", "https://example.com/p"),
            ("https://example.com/unsubscribe/ahmed@example.com", ""),
            ("https://example.com/unsubscribe/ahmed%40example.com", ""),
            # ordinary links that merely look similar are untouched
            ("https://medium.com/@handle/post", "https://medium.com/@handle/post"),
            ("https://example.com/p?pid=202610251234&ref=a", "https://example.com/p?pid=202610251234&ref=a"),
        ],
    )
    def test_a_personal_detail_in_a_link_is_not_published(self, given, expected):
        # REGRESSION (map review): a pasted tracking / CRM link with ?email= or ?phone= went to the public file.
        assert exp.sanitize_public_url(given) == expected

    @pytest.mark.parametrize(
        "given",
        [
            "https://wa.me/971501234567",
            "https://wa.me/971501234567?text=hello",
            "https://WA.ME/971501234567",
            "https://wa.link/abc123",
            "https://api.whatsapp.com/send?text=hello",
            "https://web.whatsapp.com/send?text=hello",
            "https://chat.whatsapp.com/ABCDEFGHIJKL",
            "https://t.me/ahmed_khan",
            "https://telegram.me/ahmed_khan",
            "https://m.me/ahmed.khan",
            "https://signal.me/#p/abc",
            # a fully qualified host name (trailing dot) reaches the same server
            "https://wa.me./971501234567",
            "https://t.me./ahmed_khan",
            "https://api.whatsapp.com./send?text=hello",
        ],
    )
    def test_a_chat_contact_link_is_never_published_as_a_source(self, given):
        # REGRESSION (map review): the number of a WhatsApp link lives in its PATH (wa.me/9715...), which
        # no parameter filter sees, so a pasted "source" published a personal phone number.
        assert exp.sanitize_public_url(given) == ""

    @pytest.mark.parametrize("given", ["https://example.com/p?x=\x00", "https://example.com/p\x07q", "https://example.com/\x7f"])
    def test_a_control_character_in_a_link_blanks_it(self, given):
        # REGRESSION (map review): a control character passed here while the page refuses such a link, so
        # the listing facts were exported and then silently dropped by the page.
        assert exp.sanitize_public_url(given) == ""

    def test_a_listing_whose_only_source_is_a_chat_link_exports_no_facts(self):
        listing = make_listing(source_url="https://wa.me/971501234567")
        props = only_props(build([make_row()], {"dld:dubai:1001": listing}))
        assert "source_url" not in props and "price_from" not in props
        assert "971501234567" not in json.dumps(props)

    def test_a_url_that_cannot_be_written_as_utf8_is_blank_not_a_crash_later(self):
        # REGRESSION (map review): a lone surrogate passed here, then serialise() raised for the whole export.
        assert exp.sanitize_public_url("https://example.com/\ud800") == ""

    # --- audit: gaps in the contact / credential filters ------------------------------------------------------
    BASE = "https://developer.example.com/launch"

    @pytest.mark.parametrize(
        "tail, kept",
        [
            ("?next=https%3A%2F%2Fwa.me%2F971501234567", ""),                       # a chat link wrapped in a value
            ("?next=https%253A%252F%252Fwa.me%252F971501234567&a=1", "?a=1"),        # ... encoded twice
            ("?u=t.me/ahmed_khan&a=1", "?a=1"),                                      # ... or with no scheme
            ("?share=https%3A%2F%2Fapi.whatsapp.com%2Fsend&a=1", "?a=1"),            # ... on a sub-domain
            ("?r=tel:%2B971501234567&a=1", "?a=1"),
            ("?r=mailto%3Aahmed%40example.com&a=1", "?a=1"),
            ("?r=whatsapp%3A%2F%2Fsend%3Fphone%3D971501234567&a=1", "?a=1"),
            ("?ref=%2B971501234567&a=1", "?a=1"),                                    # an international number
            ("?ref=%2B971%2050%20123%204567&a=1", "?a=1"),
            ("#next=https://wa.me/971501234567", ""),
            ("#/contact/t.me/ahmed_khan", ""),
            ("#/agents/971501234567", ""),
            ("#tel:%2B971501234567", ""),
        ],
    )
    def test_a_contact_hidden_in_a_parameter_value_or_the_fragment_is_removed(self, tail, kept):
        # REGRESSION (audit): only an email-shaped value was looked for; a chat link, a tel: link or an
        # international number inside a value (or the fragment) was published unchanged.
        assert exp.sanitize_public_url(self.BASE + tail) == self.BASE + kept

    @pytest.mark.parametrize(
        "tail",
        [
            "?next=https%3A%2F%2Fexample.org%2Fa", "?utm_source=whatsapp", "?ref=output.me", "?pid=202610251234&ref=a",
            "?hotel=1&telephony=x", "?price=1500000", "?lang=ar", "#pid=202610251234", "#section-2", "#/units/1001",
        ],
    )
    def test_values_that_only_look_similar_are_kept(self, tail):
        assert exp.sanitize_public_url(self.BASE + tail) == self.BASE + tail

    @pytest.mark.parametrize(
        "path",
        [
            "/agents/971501234567",
            "/agents/%2B971501234567",
            "/agents/971-50-123-4567",
            "/agents/971%20501234567",
            "/agents/(04)%20123-4567",
            "/agents/971\u200b501234567",  # a zero-width space inside
            "/agents/\u0669\u0667\u0661\u0665\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667",  # Arabic-Indic digits
            "/agents/\uff19\uff17\uff11\uff15\uff10\uff11\uff12\uff13\uff14\uff15\uff16\uff17",  # full-width digits
            "/agents/012345678",                                                            # exactly the minimum
            "/go/wa.me/ahmed",
            "/go/https%3A%2F%2Ft.me%2Fahmed_khan",
        ],
    )
    def test_a_phone_number_or_chat_link_in_the_path_withholds_the_whole_link(self, path):
        # REGRESSION (audit): only an email-shaped path was withheld; /agents/971501234567 was published as the source.
        assert exp.sanitize_public_url("https://developer.example.com" + path) == ""

    @pytest.mark.parametrize("path", ["/unsubscribe/ahmed%2540example.com", "/unsubscribe/ahmed\uff20example.com"])
    def test_an_address_in_the_path_is_found_through_a_second_encoding_layer_and_a_look_alike_at_sign(self, path):
        assert exp.sanitize_public_url("https://example.com" + path) == ""

    @pytest.mark.parametrize(
        "path",
        [
            "/project/12345678", "/2027/handover", "/launch-2027-12-31", "/units/1001/floor-plans", "/media/brochure-2026-10-25.pdf",
            "/2026/10/25/launch", "/p/2026-10-25", "/en/dubai/marina-heights-1234567.html", "/projects/phase-2/tower-7", "/agents/01234567",
        ],
    )
    def test_ordinary_ids_years_and_dates_in_a_path_are_not_mistaken_for_a_phone_number(self, path):
        assert exp.sanitize_public_url("https://developer.example.com" + path) == "https://developer.example.com" + path

    @pytest.mark.parametrize(
        "query",
        [
            "cell=971501234567", "agent=Ahmed+Khan", "pass=hunter2", "code=abcdef123456", "otp=123456", "ticket=hunter2",
            "passcode=1234", "Passcode=1234", "CODE=abc", "agents=Ahmed", "cell_no=0501234567", "cellular=0501234567",
            "userPass=hunter2", "authCode=abc", "access_code=abc", "invite-code=abc", "verification_code=1", "otpCode=1",
            "tokens=abc", "token_id=abc", "tkn=abc", "ticket_id=9",
        ],
    )
    def test_credential_and_person_parameters_are_removed_by_name(self, query):
        # REGRESSION (audit): cell, agent, pass, passcode, code, otp, ticket passed straight through.
        assert exp.sanitize_public_url(f"https://developer.example.com/p?{query}&unit=2") == "https://developer.example.com/p?unit=2"
        assert exp.sanitize_public_url(f"https://developer.example.com/p#{query}") == "https://developer.example.com/p"

    @pytest.mark.parametrize(
        "query",
        [
            "country_code=ae", "postcode=12345", "zip_code=0", "area_code=4", "promo_code=SUMMER", "bypass=1", "compass=n",
            "cellar=1", "passport=1", "agency=Example", "ticketing=1", "tokenizer=x", "codec=h264", "unit=2&lang=en",
        ],
    )
    def test_harmless_parameters_that_share_a_word_are_kept(self, query):
        # "code" is matched as a whole name (or a qualified one), never as a substring.
        assert exp.sanitize_public_url(f"https://developer.example.com/p?{query}") == f"https://developer.example.com/p?{query}"

    @pytest.mark.parametrize(
        "host",
        [
            "localhost", "localhost:8080", "LOCALHOST", "localhost.", "app.localhost", "intranet", "wiki", "printer.local",
            "wiki.internal", "nas.lan", "127.0.0.1", "127.1", "2130706433", "0x7f.0.0.1", "0177.0.0.1", "10.0.0.5", "172.16.0.1",
            "192.168.1.10", "169.254.169.254", "0.0.0.0", "100.64.0.1", dotted(224, 0, 0, 1), "[::1]", "[fe80::1]", "[fc00::1]",
            "[::ffff:127.0.0.1]", "[2001:db8::1]", "971501234567",
            "\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54",  # full-width "localhost"
            "127\u30020\u30020\u30021",  # ideographic full stops
        ],
    )
    def test_a_host_a_public_visitor_cannot_reach_is_withheld(self, host):
        # REGRESSION (audit): http://192.168.1.10/internal and http://localhost:8080/x were published as sources.
        assert exp.sanitize_public_url(f"http://{host}/internal") == ""
        # built in pieces: the repo hygiene scan reads the source text and rejects a literal URL with a password in it
        assert exp.sanitize_public_url("https://" + "user:pw" + "@" + host + "/x?a=1") == ""

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/x", "https://developer.example.com:8443/x", "https://sub.domain.example.co.uk/x",
            "http://[2606:4700:4700::1111]/x", f"http://{dotted(1, 1, 1, 1)}/x", "https://xn--bcher-kva.example/x",
            "https://localhost.example.com/x",
            "https://intranet.example.com/x", "https://local.example.com/x",
        ],
    )
    def test_ordinary_public_hosts_still_pass(self, url):
        assert exp.sanitize_public_url(url) == url

    def test_a_listing_whose_only_source_is_a_private_address_exports_no_facts(self):
        listing = make_listing(source_url="http://192.168.1.10/internal")
        out = build([make_row()], {"dld:dubai:1001": listing})
        assert "source_url" not in only_props(out) and "price_from" not in only_props(out)
        assert out["meta"]["with_listing"] == 0

    def test_a_published_source_never_carries_what_the_filters_removed(self):
        url = "https://developer.example.com/launch?cell=971501234567&next=https%3A%2F%2Fwa.me%2F9715&pass=hunter2&unit=2#otp=123456"
        props = only_props(build([make_row()], {"dld:dubai:1001": make_listing(source_url=url)}))
        assert props["source_url"] == "https://developer.example.com/launch?unit=2"


# ---------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------
class TestScrubSecrets:
    def test_known_secret_values_are_replaced_wherever_they_appear(self):
        text = f"boom {FAKE_KEY} and again {FAKE_KEY}!"
        assert FAKE_KEY not in exp.scrub_secrets(text, [FAKE_KEY])

    def test_a_secret_is_removed_even_in_an_unexpected_shape(self):
        out = exp.scrub_secrets(f"value=({FAKE_KEY})", [FAKE_KEY])
        assert FAKE_KEY not in out

    def test_a_longer_secret_wins_over_a_shorter_one_it_contains(self):
        out = exp.scrub_secrets("x fake-key-1234567-extra y", ["fake-key-1234567", "fake-key-1234567-extra"])
        assert "extra" not in out and "fake-key" not in out

    def test_secrets_shorter_than_the_minimum_are_not_replaced_so_words_survive(self):
        assert exp.scrub_secrets("the cat sat", ["cat"]) == "the cat sat"
        assert exp.scrub_secrets("the cat sat", [""]) == "the cat sat"

    def test_regex_metacharacters_in_a_secret_are_literal(self):
        secret = "fake.*[key](1)+?"
        assert secret not in exp.scrub_secrets(f"a {secret} b", [secret])

    @pytest.mark.parametrize(
        "text, leak",
        [
            ("GET https://api.example.com/v4?key=fake-key-1234567&q=1", "fake-key-1234567"),
            ("GET https://api.example.com/v4?q=1&API_KEY=fake-key-1234567", "fake-key-1234567"),
            ("key=fake-key-1234567", "fake-key-1234567"),
            ("https://api.example.com/?access_token=fake-token-abcdef12", "fake-token-abcdef12"),
            ("Authorization: Bearer fake-token-abcdef12", "fake-token-abcdef12"),
            ("authorization=Basic ZmFrZTpmYWtl", "ZmFrZTpmYWtl"),
            ("sent header Bearer fake-token-abcdef12 to host", "fake-token-abcdef12"),
            ('{"private_key": "fake-private-key-000000", "client_email": "svc@example.com"}', "fake-private-key-000000"),
            ('{"api_secret":"fake-secret-abcdef"}', "fake-secret-abcdef"),
            ("-----BEGIN PRIVATE KEY-----\nfakefakefake\n-----END PRIVATE KEY-----", "fakefakefake"),
            ("-----BEGIN PRIVATE KEY-----\nfakefakefake-truncated", "fakefakefake"),
            ("see https://svc:fake-pass-123@example.com/x", "fake-pass-123"),
            ("password = fake-pass-123456", "fake-pass-123456"),
        ],
    )
    def test_the_usual_shapes_are_redacted_without_knowing_the_secret(self, text, leak):
        out = exp.scrub_secrets(text)
        assert leak not in out
        assert "[redacted" in out

    def test_ordinary_text_is_untouched(self):
        text = "row 12: 'Area (EN)' is blank; keyboard layout, monkey patch, sorted by name"
        assert exp.scrub_secrets(text) == text

    def test_it_is_idempotent_and_accepts_any_object(self):
        once = exp.scrub_secrets(f"key={FAKE_KEY}")
        assert exp.scrub_secrets(once) == once
        assert exp.scrub_secrets(None) == "None"
        assert FAKE_KEY not in exp.scrub_secrets(RuntimeError(f"oops key={FAKE_KEY}"))

    def test_the_percent_encoded_spellings_of_a_secret_are_redacted_too(self):
        secret = "fake key/with+odd=chars"
        from urllib.parse import quote, quote_plus

        text = f"GET /v4?q={quote(secret, safe='')} and ?q={quote_plus(secret)} and {secret}"
        out = exp.scrub_secrets(text, [secret])
        for spelling in (secret, quote(secret, safe=""), quote_plus(secret)):
            assert spelling not in out

    def test_a_secret_inside_a_service_account_json_is_known_on_its_own(self):
        pem_body = "A" * 64
        key_json = json.dumps({
            "type": "service_account", "client_email": "svc@example.com", "private_key_id": "fake-key-id-000000",
            "private_key": f"-----BEGIN PRIVATE KEY-----\n{pem_body}\n-----END PRIVATE KEY-----\n",
        })
        values = exp.secret_values(Config(gcp_service_account_key=key_json))
        assert key_json in values and "fake-key-id-000000" in values and pem_body in values
        # an exception that echoes only the id, or only one body line, is still scrubbed
        for echoed in ("fake-key-id-000000", pem_body):
            assert echoed not in exp.scrub_secrets(f"bad credentials: {echoed}", values)
        # The account's identifiers are collected too (REGRESSION, audit: a library line that quoted the e-mail in a
        # URL put the project's name in a public log). "svc" is below the minimum length of an identifier.
        assert "svc@example.com" in values and "example.com" in values and "svc" not in values

    def test_the_identifiers_of_a_key_file_are_known_to_the_exporter_too(self):
        email, project, client = "zz-runner@zz-proj-demo-01.iam.example.invalid", "zz-proj-demo-01", "99887766554433"
        key_json = json.dumps({
            "type": "service_account", "project_id": project, "private_key_id": "zz-key-id-00112233",
            "private_key": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n", "client_email": email,
            "client_id": client,
        })
        values = exp.secret_values(Config(gcp_service_account_key=key_json))
        line = f"GET /v1/projects/-/serviceAccounts/{email}/allowedLocations client {client} project {project} as zz-runner"
        out = exp.scrub_secrets(line, values)
        for gone in (email, project, client, "zz-runner", "zz-key-id-00112233"):
            assert gone not in out, gone
        assert "allowedLocations" in out

    @pytest.mark.parametrize("text", ["", "not json", "{broken", "[1, 2]", '{"a": 1}', '"just a string"', "{"])
    def test_a_secret_that_is_not_a_json_object_adds_nothing_and_does_not_crash(self, text):
        assert exp._json_secret_members(text) == []
        assert exp.secret_values(Config(gcp_service_account_key=text)) == ([text] if text else [])

    def test_secret_values_collects_only_the_hidden_config_fields(self):
        cfg = Config(
            spreadsheet_id="fake-sheet-id-123456", gcp_service_account_key='{"private_key": "' + FAKE_PRIVATE + '"}',
            gemini_api_key=FAKE_KEY, share_with_emails=("someone@example.com",), google_sheet_name="Visible Sheet Name",
        )
        values = exp.secret_values(cfg)
        assert {"fake-sheet-id-123456", FAKE_KEY, "someone@example.com"} <= set(values)
        assert any(FAKE_PRIVATE in v for v in values)
        assert "Visible Sheet Name" not in values
        assert "" not in values


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
class TestSerialiseAndWrite:
    def test_serialise_is_compact_utf8_and_round_trips(self):
        out = build([make_row(project_name_ar="مشروع")])
        raw = exp.serialise(out)
        assert json.loads(raw.decode("utf-8")) == out
        assert exp.serialise({"a": [1, 2], "b": "x"}) == b'{"a":[1,2],"b":"x"}'

    def test_nan_cannot_be_written(self):
        with pytest.raises(ValueError):
            exp.serialise({"x": float("nan")})

    def test_the_file_is_written_with_its_parent_directory_and_nothing_else(self, tmp_path):
        target = tmp_path / "a" / "b" / "pins.geojson"
        written = exp.write_geojson(build([make_row()]), target)
        assert written == target
        assert json.loads(target.read_text(encoding="utf-8"))["features"][0]["geometry"]["coordinates"] == [55.27, 25.2]
        assert [p.name for p in target.parent.iterdir()] == ["pins.geojson"]

    def test_a_failed_serialise_leaves_the_existing_file_alone(self, tmp_path):
        target = tmp_path / "pins.geojson"
        target.write_bytes(b'{"good":true}')
        with pytest.raises(ValueError):
            exp.write_geojson({"x": float("nan")}, target)
        assert target.read_bytes() == b'{"good":true}'
        assert [p.name for p in tmp_path.iterdir()] == ["pins.geojson"]

    def test_a_failed_rename_cleans_up_and_keeps_the_old_file(self, tmp_path, monkeypatch):
        target = tmp_path / "pins.geojson"
        target.write_bytes(b'{"good":true}')

        def fail(*args, **kwargs):
            raise PermissionError("locked")

        monkeypatch.setattr(exp.os, "replace", fail)
        with pytest.raises(PermissionError):
            exp.write_geojson(build([make_row()]), target)
        monkeypatch.undo()
        assert target.read_bytes() == b'{"good":true}'
        assert [p.name for p in tmp_path.iterdir()] == ["pins.geojson"]

    def test_replacing_an_existing_file_works(self, tmp_path):
        target = tmp_path / "pins.geojson"
        exp.write_geojson(build([make_row()]), target)
        exp.write_geojson(build([]), target)
        assert json.loads(target.read_text(encoding="utf-8"))["features"] == []


# ---------------------------------------------------------------------------
# run() and main(): failures must be loud and must not leak
# ---------------------------------------------------------------------------
class FakeManager:
    """# SYNTHETIC: stands in for SheetManager; only the three methods the exporter uses."""

    def __init__(self, rows=(), problems=(), listings=None, open_error=None, registry_error=None, listing_error=None):
        self.rows, self.problems, self.listings = list(rows), list(problems), listings if listings is not None else {}
        self.open_error, self.registry_error, self.listing_error = open_error, registry_error, listing_error
        self.calls: list[str] = []

    def open(self):
        self.calls.append("open")
        if self.open_error:
            raise self.open_error

    def read_registry(self):
        self.calls.append("read_registry")
        if self.registry_error:
            raise self.registry_error
        return self.rows, self.problems

    def read_listing_records(self):
        self.calls.append("read_listing_records")
        if self.listing_error:
            raise self.listing_error
        return self.listings


def fake_config(**overrides) -> Config:
    base = dict(
        spreadsheet_id="fake-sheet-id-123456", gemini_api_key=FAKE_KEY,
        gcp_service_account_key='{"private_key": "' + FAKE_PRIVATE + '"}',
    )
    base.update(overrides)
    return Config(**base)


class TestRun:
    def test_it_reads_builds_and_writes(self, tmp_path):
        row = make_row()
        manager = FakeManager([row], listings={row.project_key: make_listing()})
        out = tmp_path / "pins.geojson"
        collection = exp.run(manager, out_path=out, generated_at=STAMP)
        assert manager.calls == ["read_registry", "read_listing_records"]
        assert json.loads(out.read_text(encoding="utf-8")) == collection
        assert collection["meta"]["generated_at"] == STAMP
        assert only_props(collection)["price_from"] == 1_200_000

    def test_unreadable_rows_are_reported_in_meta_and_logged(self, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="export_map_pins")
        manager = FakeManager([make_row()], problems=[f"row 5: bad cell key={FAKE_KEY}", "row 9: bad"])
        collection = exp.run(manager, out_path=tmp_path / "p.geojson", generated_at=STAMP, secrets=[FAKE_KEY])
        assert collection["meta"]["unreadable_rows"] == 2
        assert "2 sheet row(s) could not be read" in caplog.text
        assert FAKE_KEY not in caplog.text

    @pytest.mark.parametrize("which", ["registry_error", "listing_error"])
    def test_a_reader_failure_becomes_a_scrubbed_export_error(self, tmp_path, which):
        boom = RuntimeError(f"auth failed for {FAKE_KEY} at https://sheets.example/v4?key={FAKE_KEY}")
        manager = FakeManager([make_row()], **{which: boom})
        out = tmp_path / "pins.geojson"
        with pytest.raises(exp.ExportError) as caught:
            exp.run(manager, out_path=out, generated_at=STAMP, secrets=[FAKE_KEY])
        message = str(caught.value)
        assert FAKE_KEY not in message and "RuntimeError" in message
        assert caught.value.__cause__ is None and caught.value.__suppress_context__
        assert not out.exists()

    @pytest.mark.parametrize("which", ["registry_error", "listing_error"])
    def test_a_reader_failure_names_the_exception_class_but_never_its_text(self, tmp_path, which):
        # Public CI log: a sheet error can quote a row, a cell or a query, and a scrubber cannot know which text is private.
        manager = FakeManager([make_row()], **{which: RuntimeError(f"row 7 cell {LEAK_MARKER}")})
        with pytest.raises(exp.ExportError) as caught:
            exp.run(manager, out_path=tmp_path / "pins.geojson", generated_at=STAMP, secrets=[FAKE_KEY])
        assert str(caught.value) == "reading the sheet failed: RuntimeError"
        assert caught.value.__cause__ is None and caught.value.__suppress_context__

    def test_a_write_failure_names_the_class_and_the_output_path_but_never_the_error_text(self, tmp_path, monkeypatch):
        def refuse(collection, out_path):
            raise PermissionError(13, LEAK_MARKER, "leaked-filename-marker")   # str() carries both extra strings

        monkeypatch.setattr(exp, "write_geojson", refuse)
        out = tmp_path / "pins.geojson"
        with pytest.raises(exp.ExportError) as caught:
            exp.run(FakeManager([make_row()]), out_path=out, generated_at=STAMP, secrets=[FAKE_KEY])
        assert str(caught.value) == f"writing {out} failed: PermissionError"
        assert caught.value.__cause__ is None and caught.value.__suppress_context__

    def test_min_pins_refuses_to_replace_a_good_file_with_a_thin_one(self, tmp_path):
        out = tmp_path / "pins.geojson"
        out.write_bytes(b'{"good":true}')
        manager = FakeManager([make_row(latitude=None, longitude=None)])
        with pytest.raises(exp.ExportError, match="0 located pin"):
            exp.run(manager, out_path=out, generated_at=STAMP, min_pins=1)
        assert out.read_bytes() == b'{"good":true}'

    def _all_outside_their_emirate(self):
        return [make_row(registry_project_no=str(i), latitude=lat, longitude=lon)
                for i, (lat, lon) in enumerate([NOT_IN_DUBAI["Ajman"], NOT_IN_DUBAI["the Gulf"]], 1)]

    def test_min_pins_still_fails_loudly_when_every_pin_is_dropped_by_the_boundary_test(self, tmp_path):
        # The workflow sets --min-pins 1 while the map is published: a run that dropped everything must not
        # replace the last good map with an empty one.
        out = tmp_path / "pins.geojson"
        out.write_bytes(b'{"good":true}')
        with pytest.raises(exp.ExportError, match="0 located pin") as caught:
            exp.run(FakeManager(self._all_outside_their_emirate()), out_path=out, generated_at=STAMP, min_pins=1)
        assert "'outside_emirate': 2" in str(caught.value)
        assert "Test Residences" not in str(caught.value) and "25.4052" not in str(caught.value)
        assert out.read_bytes() == b'{"good":true}'

    def test_the_run_summary_shows_the_boundary_drops_as_a_count(self, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="export_map_pins")
        exp.run(FakeManager([make_row(), *self._all_outside_their_emirate()[:1]]), out_path=tmp_path / "p.geojson", generated_at=STAMP)
        assert "'outside_emirate': 1" in caplog.text and "Wrote 1 pin(s) of 2 project(s)" in caplog.text
        assert "25.4052" not in caplog.text and "Test Residences" not in caplog.text

    def test_an_all_dropped_map_is_written_empty_with_a_warning_when_no_minimum_is_required(self, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="export_map_pins")
        out = tmp_path / "pins.geojson"
        collection = exp.run(FakeManager(self._all_outside_their_emirate()), out_path=out, generated_at=STAMP)
        assert json.loads(out.read_text(encoding="utf-8"))["features"] == [] and collection["meta"]["dropped"]["outside_emirate"] == 2
        assert "no located projects" in caplog.text

    def test_an_empty_map_is_written_with_a_warning_when_allowed(self, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="export_map_pins")
        out = tmp_path / "pins.geojson"
        exp.run(FakeManager([]), out_path=out, generated_at=STAMP)
        assert json.loads(out.read_text(encoding="utf-8"))["features"] == []
        assert "no located projects" in caplog.text

    def test_a_write_failure_is_an_export_error_with_a_scrubbed_message(self, tmp_path):
        directory = tmp_path / "pins.geojson"
        directory.mkdir()   # the target is a directory, so the rename cannot succeed
        with pytest.raises(exp.ExportError, match="writing"):
            exp.run(FakeManager([make_row()]), out_path=directory, generated_at=STAMP)

    def test_a_bad_reader_result_is_not_swallowed_silently(self, tmp_path):
        class Broken(FakeManager):
            def read_registry(self):
                return "not a tuple"

        with pytest.raises(exp.ExportError):
            exp.run(Broken(), out_path=tmp_path / "p.geojson", generated_at=STAMP)


class TestMain:
    def test_success_writes_the_file_and_returns_zero(self, tmp_path):
        out = tmp_path / "pins.geojson"
        manager = FakeManager([make_row()])
        code = exp.main(["--out", str(out)], cfg=fake_config(), manager_factory=lambda cfg: manager)
        assert code == 0
        assert "open" in manager.calls and len(json.loads(out.read_text(encoding="utf-8"))["features"]) == 1

    def test_a_missing_spreadsheet_id_stops_before_any_manager_exists(self, tmp_path, caplog):
        made = []
        code = exp.main(["--out", str(tmp_path / "p.geojson")], cfg=fake_config(spreadsheet_id=""),
                        manager_factory=lambda cfg: made.append(cfg))
        assert code == 1 and made == []
        assert "SPREADSHEET_ID" in caplog.text

    def test_allow_sheet_by_name_proceeds_without_an_id(self, tmp_path):
        manager = FakeManager([make_row()])
        code = exp.main(["--out", str(tmp_path / "p.geojson"), "--allow-sheet-by-name"], cfg=fake_config(spreadsheet_id=""),
                        manager_factory=lambda cfg: manager)
        assert code == 0

    def test_min_pins_failure_returns_one_and_keeps_the_old_file(self, tmp_path):
        out = tmp_path / "p.geojson"
        out.write_bytes(b"old")
        code = exp.main(["--out", str(out), "--min-pins", "1"], cfg=fake_config(), manager_factory=lambda cfg: FakeManager([]))
        assert code == 1 and out.read_bytes() == b"old"

    def test_min_pins_failure_from_the_boundary_test_returns_one_and_keeps_the_old_file(self, tmp_path):
        out = tmp_path / "p.geojson"
        out.write_bytes(b"old")
        rows = [make_row(latitude=25.4052, longitude=55.5136)]
        code = exp.main(["--out", str(out), "--min-pins", "1"], cfg=fake_config(), manager_factory=lambda cfg: FakeManager(rows))
        assert code == 1 and out.read_bytes() == b"old"

    def test_config_warnings_are_logged(self, tmp_path, caplog):
        exp.main(["--out", str(tmp_path / "p.geojson")], cfg=fake_config(warnings=("GEOCODE_MAX_PER_RUN='x' is not an integer",)),
                 manager_factory=lambda cfg: FakeManager([make_row()]))
        assert "GEOCODE_MAX_PER_RUN" in caplog.text

    def test_the_config_comes_from_load_config_when_none_is_injected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(exp, "load_config", lambda: fake_config())
        code = exp.main(["--out", str(tmp_path / "p.geojson")], manager_factory=lambda cfg: FakeManager([make_row()]))
        assert code == 0

    @pytest.mark.parametrize(
        "where",
        ["open_error", "registry_error", "listing_error"],
    )
    def test_no_secret_reaches_a_log_line_the_console_or_a_traceback(self, tmp_path, caplog, capsys, where):
        message = (
            f"auth failed: {FAKE_KEY} {FAKE_PRIVATE} Authorization: Bearer fake-token-abcdef12 "
            f"https://sheets.example/v4?key={FAKE_KEY} fake-sheet-id-123456"
        )
        manager = FakeManager([make_row()], **{where: RuntimeError(message)})
        code = exp.main(["--out", str(tmp_path / "p.geojson")], cfg=fake_config(), manager_factory=lambda cfg: manager)
        assert code == 1
        seen = caplog.text + capsys.readouterr().out + capsys.readouterr().err
        for secret in (FAKE_KEY, FAKE_PRIVATE, "fake-token-abcdef12", "fake-sheet-id-123456"):
            assert secret not in seen, secret
        assert all(record.exc_info is None for record in caplog.records), "a traceback was attached to a log record"
        assert "Traceback" not in seen

    def test_an_unexpected_exception_reports_where_but_not_what(self, tmp_path, caplog):
        def factory(cfg):
            raise KeyError(f"surprise {FAKE_KEY}")

        code = exp.main(["--out", str(tmp_path / "p.geojson")], cfg=fake_config(), manager_factory=factory)
        assert code == 1
        assert FAKE_KEY not in caplog.text
        assert "KeyError" in caplog.text and "test_export_map_pins.py" in caplog.text

    @staticmethod
    def _everything_printed(caplog, capsys) -> str:
        captured = capsys.readouterr()
        return caplog.text + captured.out + captured.err

    @pytest.mark.parametrize("where", ["open_error", "registry_error", "listing_error"])
    def test_a_sheet_error_text_never_reaches_the_public_log(self, tmp_path, caplog, capsys, where):
        manager = FakeManager([make_row()], **{where: RuntimeError(f"row 7 cell {LEAK_MARKER}")})
        code = exp.main(["--out", str(tmp_path / "p.geojson")], cfg=fake_config(), manager_factory=lambda cfg: manager)
        assert code == 1
        assert "RuntimeError" in caplog.text
        assert LEAK_MARKER not in self._everything_printed(caplog, capsys)

    def test_a_write_error_text_never_reaches_the_public_log(self, tmp_path, caplog, capsys, monkeypatch):
        def refuse(collection, out_path):
            raise PermissionError(13, LEAK_MARKER, "leaked-filename-marker")

        monkeypatch.setattr(exp, "write_geojson", refuse)
        code = exp.main(["--out", str(tmp_path / "p.geojson")], cfg=fake_config(), manager_factory=lambda cfg: FakeManager([make_row()]))
        assert code == 1
        assert "writing" in caplog.text and "PermissionError" in caplog.text
        seen = self._everything_printed(caplog, capsys)
        assert LEAK_MARKER not in seen and "leaked-filename-marker" not in seen

    def test_an_unexpected_exceptions_text_never_reaches_the_public_log(self, tmp_path, caplog, capsys):
        def factory(cfg):
            raise KeyError(f"surprise {LEAK_MARKER}")

        code = exp.main(["--out", str(tmp_path / "p.geojson")], cfg=fake_config(), manager_factory=factory)
        assert code == 1
        assert "KeyError" in caplog.text and "test_export_map_pins.py" in caplog.text
        assert LEAK_MARKER not in self._everything_printed(caplog, capsys) and "surprise" not in caplog.text

    def test_a_bad_command_line_exits_with_argparse_status_two(self):
        with pytest.raises(SystemExit) as caught:
            exp.main(["--min-pins", "many"], cfg=fake_config(), manager_factory=lambda cfg: FakeManager())
        assert caught.value.code == 2

    def test_main_installs_the_log_safe_excepthook_and_only_when_it_runs(self, tmp_path, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(sys, "excepthook", sentinel)
        assert sys.excepthook is sentinel  # importing the script (done above) changed nothing
        code = exp.main(["--out", str(tmp_path / "p.geojson")], cfg=fake_config(), manager_factory=lambda cfg: FakeManager([make_row()]))
        assert code == 0
        assert sys.excepthook is log_policy.excepthook

    def test_main_installs_the_hook_before_it_parses_the_command_line(self, monkeypatch):
        monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
        with pytest.raises(SystemExit):
            exp.main(["--min-pins", "many"], cfg=fake_config(), manager_factory=lambda cfg: FakeManager())
        assert sys.excepthook is log_policy.excepthook


class ChattyManager(FakeManager):
    """# SYNTHETIC: a sheet client whose libraries log while it opens, as urllib3, google-auth and an SDK do."""

    def __init__(self, cfg: Config, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = cfg

    def open(self):
        sheet_id = self.cfg.spreadsheet_id
        logging.getLogger("urllib3.connectionpool").warning(
            "Failed to parse headers (url=https://host.example.invalid/search?q=%s&id=%s): %s", LEAK_MARKER, sheet_id, LEAK_MARKER
        )
        try:
            raise ValueError(f"header {LEAK_MARKER}")
        except ValueError:
            logging.getLogger("urllib3.connection").error("raw header bytes %s %s", LEAK_MARKER, sheet_id, exc_info=True)
        logging.getLogger("google.auth._default").warning("Retrying lookup of %s for sheet %s", LEAK_MARKER, sheet_id)
        logging.getLogger("gspread").critical("%s %s", LEAK_MARKER, sheet_id)
        warnings.warn_explicit(
            f"{LEAK_MARKER} {sheet_id}", UserWarning, "site-packages/google/genai/_common.py", 7, module="google.genai._common"
        )
        logging.getLogger("export_map_pins").warning("opening sheet %s", sheet_id)  # the script's own line, with an id in it
        super().open()


class TestPublicLog:
    """REGRESSION (audit): main() called a bare logging.basicConfig(level=INFO), so a library's record reached the console."""

    def run_main(self, tmp_path, **config):
        cfg = fake_config(**config)
        manager = ChattyManager(cfg, [make_row()])
        code = exp.main(["--out", str(tmp_path / "p.geojson")], cfg=cfg, manager_factory=lambda c: manager)
        return code, cfg, manager

    def test_no_library_record_reaches_the_console_or_the_log_capture(self, tmp_path, caplog, capsys):
        def to_stderr(message, category, filename, lineno, file=None, line=None):
            sys.stderr.write(warnings.formatwarning(message, category, filename, lineno, line))

        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.showwarning = to_stderr  # what an unconfigured process does with a warning; restored on exit
            code, cfg, manager = self.run_main(tmp_path)
            logging.captureWarnings(False)  # while the stand-in is still the hook, so it is the one put back
        assert code == 0 and "open" in manager.calls
        captured = capsys.readouterr()
        seen = caplog.text + captured.out + captured.err
        for gone in (LEAK_MARKER, "Failed to parse headers", "raw header bytes", "Retrying lookup"):
            assert gone not in seen, gone
        # the id is in the script's own line too; caplog sees that record before the handler's filter scrubs it
        assert cfg.spreadsheet_id not in captured.out + captured.err
        assert not [r.name for r in caplog.records if r.name != "export_map_pins" and not r.name.startswith("src.")]

    def test_the_scripts_own_lines_still_reach_the_console_with_the_secrets_scrubbed(self, tmp_path, capsys):
        code, cfg, _ = self.run_main(tmp_path)
        err = capsys.readouterr().err
        assert code == 0
        assert "export_map_pins" in err and "Wrote 1 pin(s)" in err
        # the sheet id was in the script's own line; the handler's filter, given the secrets once the config was loaded, removed it
        assert "opening sheet [redacted]" in err and cfg.spreadsheet_id not in err

    def test_main_installs_the_shared_policy_once_even_when_called_twice(self, tmp_path):
        self.run_main(tmp_path)
        self.run_main(tmp_path)
        root = logging.getLogger()
        marked = [h for h in root.handlers if getattr(h, LOG_HANDLER_MARK, False)]
        assert len(marked) == 1
        assert root.level > logging.CRITICAL and logging.getLogger("export_map_pins").level == logging.INFO
        assert [type(f).__name__ for f in marked[0].filters] == ["OwnLoggersOnly", "SecretFilter"]


class TestImportAndScript:
    def test_importing_the_module_does_not_load_the_sheet_code(self):
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import scripts.export_map_pins; "
            "assert 'src.gsheet_manager' not in sys.modules, 'gsheet_manager was imported eagerly'; "
            "assert 'gspread' not in sys.modules, 'gspread was imported eagerly'"
        )
        result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr

    def test_the_script_runs_from_any_directory(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts" / "export_map_pins.py"), "--help"],
            capture_output=True, text=True, timeout=120, cwd=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert "--min-pins" in result.stdout

    def test_the_default_output_is_the_pins_file_of_the_site(self):
        assert exp.DEFAULT_OUT == ROOT / "site" / "pins.geojson"

    def test_the_default_output_is_git_ignored(self):
        # An unpacked archive or a machine without git has nothing to ask: skipped, not failed.
        if shutil.which("git") is None:
            pytest.skip("git is not installed")
        try:
            inside = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT, capture_output=True, text=True, timeout=60
            )
        except OSError:
            pytest.skip("git could not be run")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            pytest.skip("the repository is not a git work tree")
        ignored = subprocess.run(["git", "check-ignore", "-q", "site/pins.geojson"], cwd=ROOT, capture_output=True, timeout=60)
        assert ignored.returncode == 0, "site/pins.geojson must be git-ignored"
