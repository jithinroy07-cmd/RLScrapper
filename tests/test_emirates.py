"""
Tests for src/emirates.py (which emirate is a coordinate in) and
scripts/fetch_emirate_polygons.py (the builder of the bundled polygon file).

Offline, deterministic and credential-free. Two kinds of data are used and labelled:

  * GROUND-TRUTH coordinates (the table below): public places (landmarks, districts,
    towns, and points in other emirates, open water and other countries) whose emirate
    or country is a plain fact. They are an accuracy yardstick for the polygon test and
    nothing else: they are not registry data, they are never written to the sheet or the
    map, and the scraper (src/) and the map page (site/) do not read them. They are
    real on purpose: invented points could not show that the polygons put real places
    in the right emirate.
  * SYNTHETIC fixtures (`# SYNTHETIC`): invented squares and Nominatim-shaped
    responses, used for the fetcher, the tie rules and the error paths. No network.

PROVENANCE of the ground-truth coordinates. Every named place in the table is a feature
of OpenStreetMap, looked up by name through the Overpass API on 25 Oct 2026 (read-only
queries; the OSM data was at replication timestamps 17:17 to 17:26 UTC that day). `lat`
and `lon` are that feature's position (a node's own coordinates, or the `center`
Overpass reports for a way or relation) rounded to 5 decimals, and `osm` is its element
id (n = node, w = way, r = relation), so any row can be looked up again. `label` is the
feature's OSM `name` (its `name:en` where the `name` tag is Arabic), with a few words in
brackets saying what kind of feature it is or where. The comment at the end of each row
gives the OSM tag the feature was picked by. Where a name matched several OSM features
(Al Nahda, Al Qusais, Palm Jumeirah), the one inside the expected emirate was chosen and
the label or the comment says which. The only rows without an OSM element are five
round-number points (N10-N12 in open water, N18 at (0, 0), N20 in empty desert): they
are not taken from any source. No row was copied from a property portal, an encyclopedia,
a search engine or a geocoding service, and no earlier value of this table was used as
truth. The table is public place coordinates, used only as an accuracy yardstick and
never shipped to users.
Contains data from OpenStreetMap contributors, ODbL
(https://www.openstreetmap.org/copyright).

HOW the expected values were checked (25 Oct 2026). Each Dubai / Sharjah row names a
place whose emirate is a plain fact (Hatta and Burj Khalifa are Dubai; Kalba and the
Sharjah Aquarium are Sharjah), and the expected value is that fact, not the output of
src/emirates.py. All fifty of them (D01-D25, S01-S25) were then run through Overpass
`is_in` in one batched query, and each came back inside the admin-level-4 area of the
expected emirate. The None rows went through the same query: none came back as Dubai or
Sharjah. `is_in` is a separate implementation of point-in-area, but it reads the same
OSM relations the bundled polygons were built from, so it checks the polygon code and its
simplification, not OSM's border itself. It was a manual, networked step done outside
the test suite, and its output is not kept in this repository. OVERPASS_CONFIRMED lists
exactly the Dubai / Sharjah keys it covered.

OSM draws the Dubai / Sharjah border imperfectly in places (Al Nahda is split between
the two) and it was NOT audited against official survey data. Points within about 300 m
of the nearest polygon edge are flagged `border_sensitive`: the place's emirate is
certain, so they stay in the table, but a surveyor-grade border could disagree with OSM
about them.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pytest
import requests
import shapely
from shapely.errors import GEOSException
from shapely.geometry import MultiPolygon, Point, Polygon, box, mapping, shape
from shapely.ops import nearest_points, unary_union

from scripts import fetch_emirate_polygons as fetcher
from src import emirates
from src.emirates import (
    EMIRATES,
    UNION_BBOX,
    distance_to_border_km,
    emirate_for_point,
    in_union_bbox,
    is_border_zone,
    point_in_emirate,
)

DATA_FILE = Path(emirates._DATA_PATH)


# ---------------------------------------------------------------------------
# REAL ground truth
# ---------------------------------------------------------------------------
class GroundTruth(NamedTuple):
    key: str
    label: str  # the feature's OSM name (name:en where the name tag is Arabic), plus a few words in brackets
    lat: float  # the OSM feature's position rounded to 5 decimals (round-number points aside, see `osm`)
    lon: float
    expected: str | None  # "Dubai", "Sharjah" or None (neither)
    border_sensitive: bool = False  # within ~300 m of the OSM border: OSM's border is unaudited here
    osm: str = ""  # the OSM element: n<id> node, w<id> way, r<id> relation; empty only for the round-number points


# Every row was looked up in OpenStreetMap by name on 25 Oct 2026 (see the module docstring for how).
GROUND_TRUTH: tuple[GroundTruth, ...] = (
    # --- Dubai ---------------------------------------------------------------------
    GroundTruth("D01", "Hatta", 24.79756, 56.12064, "Dubai", osm="n8128209859"),  # place=city
    GroundTruth("D02", "Hatta Fort Hotel", 24.82039, 56.13534, "Dubai", osm="w387003353"),  # tourism=hotel, Hatta exclave
    GroundTruth("D03", "Dubai Marina", 25.07864, 55.13525, "Dubai", osm="n8143204856"),  # place=suburb
    GroundTruth("D04", "Palm Jumeirah (suburb)", 25.11932, 55.1354, "Dubai", osm="w874795235"),  # place=suburb (same name as D05, which is the place=island relation)
    GroundTruth("D05", "Palm Jumeirah (island)", 25.11937, 55.13074, "Dubai", osm="r1297972"),  # place=island
    GroundTruth("D06", "Burj Khalifa", 25.19704, 55.27421, "Dubai", osm="w446646206"),  # tourism=attraction
    GroundTruth("D07", "Dubai Creek Harbour", 25.19797, 55.35815, "Dubai", osm="w1012353010"),  # landuse=residential
    GroundTruth("D08", "Dubai International Airport (aerodrome)", 25.25156, 55.3702, "Dubai", osm="w1429712446"),  # aeroway=aerodrome
    GroundTruth("D09", "Al Nahda (metro station)", 25.27321, 55.36948, "Dubai", osm="n6581397376"),  # railway=station; several features are named Al Nahda: this one is the Dubai metro station
    GroundTruth("D10", "Al Nahda (district, Dubai side)", 25.28865, 55.37207, "Dubai", osm="r18471785"),  # admin_level=10 boundary; of the Al Nahda features, the one inside Dubai
    GroundTruth("D11", "Al Qusais (metro station)", 25.26274, 55.38727, "Dubai", osm="n6584833641"),  # railway=station
    GroundTruth("D12", "Al Qusais (district)", 25.26995, 55.38522, "Dubai", osm="r18471660"),  # admin_level=10 boundary
    GroundTruth("D13", "Stadium (metro station)", 25.27791, 55.36147, "Dubai", osm="n6581397379"),  # railway=station
    GroundTruth("D14", "City Centre Mirdif (metro station)", 25.21342, 55.40971, "Dubai", osm="n12470790686"),  # railway=station
    GroundTruth("D15", "Dubai Festival City", 25.2217, 55.35776, "Dubai", osm="w838663334"),  # place=suburb
    GroundTruth("D16", "Dubai Silicon Oasis", 25.12125, 55.38661, "Dubai", osm="w364819827"),  # place=neighbourhood
    GroundTruth("D17", "Al Warqa", 25.19019, 55.43061, "Dubai", osm="r18471737"),  # admin_level=10 boundary
    GroundTruth("D18", "Al Twar 3", 25.2496, 55.39546, "Dubai", osm="n13706209521"),  # place=suburb
    GroundTruth("D19", "Dubai South", 24.91444, 55.16151, "Dubai", osm="n11795445497"),  # place=suburb
    GroundTruth("D20", "Jebel Ali Hills", 24.90305, 54.96953, "Dubai", osm="n4898024258"),  # place=suburb
    GroundTruth("D21", "International City", 25.13219, 55.39592, "Dubai", osm="r18471686"),  # admin_level=10 boundary, place=suburb
    GroundTruth("D22", "Dubai Hills", 25.09741, 55.26844, "Dubai", osm="n8143204842"),  # place=suburb
    GroundTruth("D23", "Jebel Ali Industrial Area", 24.99928, 55.12321, "Dubai", osm="n8143204860"),  # place=suburb
    GroundTruth("D24", "Al Garhoud", 25.24304, 55.35262, "Dubai", osm="r18471788"),  # admin_level=10 boundary
    GroundTruth("D25", "Al Mamzar Beach", 25.30533, 55.35269, "Dubai", border_sensitive=True, osm="r7601363"),  # natural=beach, on the Dubai side of the Al Mamzar border
    # --- Sharjah -------------------------------------------------------------------
    GroundTruth("S01", "Kalba Fort", 25.0807, 56.35783, "Sharjah", osm="w572719177"),  # historic=castle, Kalba
    GroundTruth("S02", "Kalba", 25.0513, 56.35422, "Sharjah", osm="n8559776151"),  # place=city
    GroundTruth("S03", "Khor Fakkan", 25.34917, 56.34781, "Sharjah", osm="n8128209870"),  # place=city
    GroundTruth("S04", "Al Hamriyah", 25.45651, 55.51336, "Sharjah", osm="n8560566489"),  # place=town
    GroundTruth("S05", "Sharjah Industrial Area 9", 25.29845, 55.40201, "Sharjah", border_sensitive=True, osm="w145581513"),  # landuse=industrial, admin_level=8
    GroundTruth("S06", "Sharjah Industrial Area 8", 25.30366, 55.39191, "Sharjah", osm="w145581514"),  # landuse=industrial, admin_level=8
    GroundTruth("S07", "Aljada Skate Park + Park", 25.31817, 55.47495, "Sharjah", osm="w993856848"),  # leisure=park
    GroundTruth("S08", "Al Khan", 25.31439, 55.37539, "Sharjah", osm="n8143204767"),  # place=suburb
    GroundTruth("S09", "Sahara Centre", 25.29785, 55.37324, "Sharjah", border_sensitive=True, osm="w146379257"),  # shop=mall
    GroundTruth("S10", "Sharjah International Airport (aerodrome)", 25.33043, 55.51636, "Sharjah", osm="w626238342"),  # aeroway=aerodrome
    GroundTruth("S11", "Sharjah Aquarium", 25.33037, 55.35832, "Sharjah", osm="w173974603"),  # tourism=aquarium
    GroundTruth("S12", "Sharjah Cricket Stadium", 25.33085, 55.42095, "Sharjah", osm="w74954610"),  # leisure=stadium
    GroundTruth("S13", "Muwaileh (district)", 25.29544, 55.48745, "Sharjah", osm="r18471750"),  # admin_level=8 boundary
    GroundTruth("S14", "Muweilah (school)", 25.30053, 55.44987, "Sharjah", osm="n4492434390"),  # amenity=school
    GroundTruth("S15", "Sharjah Research Technology and Innovation Park", 25.32211, 55.4883, "Sharjah", osm="w1027564342"),  # building=commercial
    GroundTruth("S16", "American University Of Sharjah", 25.311, 55.49193, "Sharjah", osm="w205717627"),  # amenity=university
    GroundTruth("S17", "TILAL", 25.26369, 55.59098, "Sharjah", osm="w920168091"),  # place=neighbourhood
    GroundTruth("S18", "Al Zahia", 25.31783, 55.46076, "Sharjah", osm="w1044753592"),  # place=neighbourhood
    GroundTruth("S19", "Al Rahmaniya", 25.33477, 55.58114, "Sharjah", osm="r18471682"),  # admin_level=8 boundary, place=suburb
    GroundTruth("S20", "Dibba Al Hisn", 25.61774, 56.26893, "Sharjah", osm="n8560554403"),  # place=city
    GroundTruth("S21", "Al Dhaid", 25.28069, 55.8781, "Sharjah", osm="n8143186389"),  # place=town
    GroundTruth("S22", "Al Madam", 24.91202, 55.7763, "Sharjah", osm="n1334365116"),  # place=city
    GroundTruth("S23", "Al Qasba (park)", 25.32259, 55.37639, "Sharjah", osm="r17386345"),  # leisure=park
    GroundTruth("S24", "Al Nahda (district, Sharjah side)", 25.30324, 55.37446, "Sharjah", osm="r18471733"),  # admin_level=8 boundary; of the Al Nahda features, the one inside Sharjah
    GroundTruth("S25", "Al Majaz (district)", 25.33013, 55.3823, "Sharjah", osm="r18471746"),  # admin_level=8 boundary
    # --- Neither: other emirates, open water, other countries ------------------------
    GroundTruth("N01", "Ajman (city, inside both emirates' bounding boxes)", 25.39366, 55.44514, None, osm="n262479959"),  # place=city
    GroundTruth("N02", "Umm Al Quwain (city)", 25.55204, 55.5475, None, osm="n896422314"),  # place=city
    GroundTruth("N03", "Fujairah (city, just north of Kalba)", 25.12446, 56.33551, None, osm="n4388736118"),  # place=city
    GroundTruth("N04", "Dibba Al Fujairah (next to Sharjah's Dibba Al Hisn)", 25.59933, 56.28432, None, osm="n13637913145"),  # place=city
    GroundTruth("N05", "Abu Dhabi (city)", 24.45384, 54.3774, None, osm="n261611194"),  # place=city
    GroundTruth("N06", "Ghantoot (Abu Dhabi emirate, next to Jebel Ali)", 24.86473, 54.90083, None, osm="n1280214391"),  # place=village
    GroundTruth("N07", "Masfut (Ajman exclave beside Hatta)", 24.81543, 56.0971, None, osm="n12153504632"),  # place=town
    GroundTruth("N08", "Manama (Ajman exclave)", 25.32758, 56.01338, None, osm="n8128209864"),  # place=town
    GroundTruth("N09", "Ras Al Khaimah (city)", 25.77377, 55.93823, None, osm="n262479317"),  # place=city
    GroundTruth("N10", "Gulf offshore, north-west of Sharjah's waters", 25.65, 54.45, None),  # round coordinates in open water, no OSM element
    GroundTruth("N11", "Gulf offshore, north of Dubai", 25.62, 54.9, None),  # round coordinates in open water, no OSM element
    GroundTruth("N12", "Gulf offshore, north of Ajman", 25.75, 55.3, None),  # round coordinates in open water, no OSM element
    GroundTruth("N13", "Muscat (Oman)", 23.61236, 58.59381, None, osm="n226881400"),  # place=city
    GroundTruth("N14", "Doha (Qatar)", 25.28563, 51.52642, None, osm="n1676664890"),  # place=city
    GroundTruth("N15", "Bandar Abbas (Iran)", 27.1842, 56.28925, None, osm="n4736094012"),  # place=city
    GroundTruth("N16", "Mumbai (India)", 19.055, 72.8692, None, osm="n16173235"),  # place=city
    GroundTruth("N17", "London", 51.50745, -0.12777, None, osm="n107775"),  # place=city
    GroundTruth("N18", "Null Island (0, 0)", 0.0, 0.0, None),  # (0, 0) by definition, no OSM element
    GroundTruth("N19", "Lima (Musandam, Oman)", 25.94263, 56.42268, None, osm="n1718102747"),  # place=town
    GroundTruth("N20", "Desert south of Dubai, Abu Dhabi emirate", 24.55, 55.4, None),  # round coordinates, no OSM element
)

# Entries confirmed by Overpass `is_in` on 25 Oct 2026 (see the module docstring for what was run): all of D01-D25 and S01-S25.
OVERPASS_CONFIRMED = frozenset(
    {f"D{i:02d}" for i in range(1, 26)} | {f"S{i:02d}" for i in range(1, 26)}
)

# A round-number window (min_lat, max_lat, min_lon, max_lon) around the Dubai / Sharjah urban border zone: both emirates
# have places inside it, so neither emirate's bounding box can separate them there. A test-local choice, not data.
URBAN_OVERLAP_BOX = (25.20, 25.32, 55.34, 55.60)
HARD_DUBAI = ("D08", "D09", "D10", "D11", "D12", "D13", "D14")
HARD_SHARJAH = ("S09", "S13", "S14", "S16", "S17", "S18")

# Sources this file must not claim or use. Written with character classes so that this very line does not match.
FORBIDDEN_SOURCES = re.compile(r"propert[y][\s_-]*finde[r]|wikiped[i]a|wikidat[a]|project[\s_-]+brie[f]|bayu[t]|dubizzl[e]", re.IGNORECASE)
OSM_ELEMENT_ID = re.compile(r"^[nwr][1-9][0-9]*$")  # n<id> node, w<id> way, r<id> relation
BORDER_SENSITIVE_M = 300.0  # the `border_sensitive` flag means "within about this far of the OSM border"
BORDER_SLACK_M = 50.0  # the bundled outline may sit this far (max_deviation_m) from OSM's, so the rule has a band

BY_KEY = {gt.key: gt for gt in GROUND_TRUTH}
DUBAI_POINTS = [gt for gt in GROUND_TRUTH if gt.expected == "Dubai"]
SHARJAH_POINTS = [gt for gt in GROUND_TRUTH if gt.expected == "Sharjah"]
NEITHER_POINTS = [gt for gt in GROUND_TRUTH if gt.expected is None]


# ---------------------------------------------------------------------------
# Helpers independent of src/emirates.py
# ---------------------------------------------------------------------------
def shipped_geometries() -> dict[str, MultiPolygon]:
    """The bundled polygons parsed straight from the file, not through the module under test."""
    collection = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {f["properties"]["emirate"]: shape(f["geometry"]) for f in collection["features"]}


def haversine_np_km(lat: float, lon: float, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    p1, p2 = math.radians(lat), np.radians(lats)
    a = np.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * np.cos(p2) * np.sin(np.radians(lons - lon) / 2) ** 2
    return 2 * 6371.0088 * np.arcsin(np.minimum(1.0, np.sqrt(a)))


def brute_force_distance_km(lat: float, lon: float, polygon) -> float:
    """Distance to a polygon by exhaustive search over its boundary densified to about 22 m."""
    if shapely.intersects_xy(polygon, lon, lat):
        return 0.0
    dense = shapely.segmentize(polygon.boundary, 0.0002)
    coords = shapely.get_coordinates(dense)
    return float(haversine_np_km(lat, lon, coords[:, 1], coords[:, 0]).min())


def margin_km(lat: float, lon: float, geoms: dict) -> float:
    """Distance from a point to the nearest edge of either polygon (inside or out)."""
    scale = math.cos(math.radians(25.16))
    edges = unary_union([g.boundary for g in geoms.values()])
    _, near = nearest_points(Point(lon * scale, lat), shapely.transform(edges, lambda xy: xy * [scale, 1.0]))
    return float(haversine_np_km(lat, lon, np.array([near.y]), np.array([near.x / scale]))[0])


def classify(geoms: dict, lat: float, lon: float) -> str | None:
    inside = [name for name in EMIRATES if shapely.contains_xy(geoms[name], lon, lat)]
    return inside[0] if len(inside) == 1 else None


@pytest.fixture
def fresh_loader():
    """Clear the module's one-time polygon cache before and after, so patched paths never leak."""
    emirates._boundaries.cache_clear()
    yield
    emirates._boundaries.cache_clear()


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------
def test_constants_match_the_design():
    assert EMIRATES == ("Dubai", "Sharjah")
    assert UNION_BBOX == (24.62, 25.70, 53.98, 56.60)


def test_public_api_is_exactly_the_documented_names():
    assert set(emirates.__all__) == {
        "EMIRATES", "UNION_BBOX", "in_union_bbox", "point_in_emirate",
        "emirate_for_point", "distance_to_border_km", "is_border_zone",
    }
    for name in emirates.__all__:
        assert hasattr(emirates, name)


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------
def test_the_ground_truth_table_is_big_and_balanced_enough():
    assert len(GROUND_TRUTH) >= 30
    assert len(DUBAI_POINTS) >= 15 and len(SHARJAH_POINTS) >= 15 and len(NEITHER_POINTS) >= 10
    assert len({gt.key for gt in GROUND_TRUTH}) == len(GROUND_TRUTH)


def test_enough_ground_truth_points_were_confirmed_independently():
    """Bookkeeping only: the Overpass `is_in` confirmation is recorded as a manual, networked step on 25 Oct 2026.

    This test cannot re-run it or check the record (the suite is offline). It only guards the table against drift:
    every key claimed as confirmed must still exist, be a Dubai / Sharjah point, and cover the
    hard border cases. The real verification is `test_emirate_for_point_matches_ground_truth`.
    """
    table = {gt.key: gt for gt in GROUND_TRUTH}
    assert OVERPASS_CONFIRMED <= set(table), "a confirmed key is not in the table"
    assert all(table[key].expected is not None for key in OVERPASS_CONFIRMED)
    assert len(OVERPASS_CONFIRMED) >= 30
    # Every hard border case and every border-sensitive point is among them.
    assert set(HARD_DUBAI) | set(HARD_SHARJAH) <= OVERPASS_CONFIRMED
    assert {gt.key for gt in GROUND_TRUTH if gt.border_sensitive} <= OVERPASS_CONFIRMED


def test_every_ground_truth_row_cites_an_osm_element_unless_it_is_a_round_number():
    """Provenance guard: a coordinate without a recorded OSM element must be an obvious round number.

    A remembered or copied coordinate (several decimals, no element id) cannot be added to the table.
    """
    seen: set[str] = set()
    for gt in GROUND_TRUTH:
        assert round(gt.lat, 5) == gt.lat and round(gt.lon, 5) == gt.lon, f"{gt.key}: more than 5 decimals"
        if gt.osm:
            assert OSM_ELEMENT_ID.match(gt.osm), f"{gt.key}: not an OSM element id"
            assert gt.osm not in seen, f"{gt.key}: element cited twice"
            seen.add(gt.osm)
        else:
            assert gt.expected is None, f"{gt.key}: a Dubai / Sharjah row must cite an OSM element"
            assert round(gt.lat, 2) == gt.lat and round(gt.lon, 2) == gt.lon, f"{gt.key}: unsourced and not round"
    assert sum(1 for gt in GROUND_TRUTH if not gt.osm) <= 5  # the docstring names exactly these round-number points
    assert len({(gt.lat, gt.lon) for gt in GROUND_TRUTH}) == len(GROUND_TRUTH), "two rows share a coordinate"


def test_ground_truth_labels_are_plain_ascii_and_distinct():
    for gt in GROUND_TRUTH:
        assert gt.label.isascii() and gt.label.isprintable() and gt.label == gt.label.strip(), gt.key
        assert 3 <= len(gt.label) <= 80, gt.key
        assert not FORBIDDEN_SOURCES.search(gt.label), gt.key
    assert len({gt.label for gt in GROUND_TRUTH}) == len(GROUND_TRUTH), "two rows share a label"


def test_module_docstring_credits_openstreetmap_and_this_file_names_no_other_source():
    doc = " ".join((__doc__ or "").split())
    for phrase in (
        "OpenStreetMap", "through the Overpass API on 25 Oct 2026", "accuracy yardstick", "never shipped to users",
        "Contains data from OpenStreetMap contributors, ODbL", "https://www.openstreetmap.org/copyright",
    ):
        assert phrase in doc, phrase
    source = Path(__file__).read_text(encoding="utf-8")
    assert FORBIDDEN_SOURCES.search(source) is None, FORBIDDEN_SOURCES.search(source)


def test_border_sensitive_flags_match_the_distance_to_the_osm_border():
    """The flag is derived from the polygons (about 300 m), so it cannot be set or cleared by hand.

    The band is +/- 50 m because the bundled outline may differ from OSM's by up to that much.
    """
    geoms = shipped_geometries()
    for gt in GROUND_TRUTH:
        if gt.expected is None:
            assert not gt.border_sensitive, f"{gt.key}: the flag only applies to Dubai / Sharjah rows"
            continue
        margin_m = margin_km(gt.lat, gt.lon, geoms) * 1000
        if gt.border_sensitive:
            assert margin_m <= BORDER_SENSITIVE_M + BORDER_SLACK_M, f"{gt.key} is flagged but {margin_m:.0f} m from the border"
        else:
            assert margin_m >= BORDER_SENSITIVE_M - BORDER_SLACK_M, f"{gt.key} is {margin_m:.0f} m from the border, unflagged"


def test_no_precise_coordinate_is_hard_coded_outside_the_ground_truth_table():
    """Every precise UAE coordinate in this file comes from the table, so each one has a recorded OSM source.

    Round numbers (two decimals) are fine: they claim no precision. So is a round number nudged by 0.0001, which
    is how the bounding-box and synthetic-square edge probes (24.6199, 55.0999, ...) are written.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    in_table = {
        id(node)
        for stmt in tree.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == "GROUND_TRUTH"
        for node in ast.walk(stmt)
    }
    stray = sorted(
        {
            (node.lineno, node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, float) and id(node) not in in_table
            and (22.0 <= node.value <= 27.0 or 51.0 <= node.value <= 59.0)  # a UAE-ish latitude or longitude
            and round(node.value, 3) != node.value  # four or more decimals: a claim of precision
            and abs(node.value - round(node.value, 2)) > 1.5e-4  # not a round number nudged by 0.0001
        }
    )
    assert stray == [], f"coordinates outside the table (line, value): {stray}"


@pytest.mark.parametrize("gt", GROUND_TRUTH, ids=lambda gt: gt.key)
def test_emirate_for_point_matches_ground_truth(gt):
    assert emirate_for_point(gt.lat, gt.lon) == gt.expected, gt.label


@pytest.mark.parametrize("gt", GROUND_TRUTH, ids=lambda gt: gt.key)
def test_point_in_emirate_matches_ground_truth_for_both_emirates(gt):
    for emirate in EMIRATES:
        assert point_in_emirate(gt.lat, gt.lon, emirate) is (gt.expected == emirate), (gt.label, emirate)


@pytest.mark.parametrize("gt", [g for g in GROUND_TRUTH if g.expected is not None], ids=lambda gt: gt.key)
def test_every_dubai_and_sharjah_point_passes_the_union_bbox_gate(gt):
    assert in_union_bbox(gt.lat, gt.lon)


def test_hatta_and_kalba_exclaves_are_handled():
    # Dubai's Hatta is ~100 km from the rest of Dubai; Sharjah's Kalba is on the east coast.
    assert emirate_for_point(*_ll("D02")) == "Dubai"  # Hatta Fort Hotel
    assert emirate_for_point(*_ll("S01")) == "Sharjah"  # Kalba Fort
    assert emirate_for_point(*_ll("S03")) == "Sharjah"  # Khor Fakkan, a separate Sharjah piece
    assert emirate_for_point(*_ll("S20")) == "Sharjah"  # Dibba Al Hisn, yet Dibba Al Fujairah (N04) is not
    assert emirate_for_point(*_ll("N04")) is None


# ---------------------------------------------------------------------------
# The reason the polygons exist: boxes cannot separate these emirates
# ---------------------------------------------------------------------------
def test_a_dubai_bounding_box_would_wrongly_accept_sharjah_points():
    west, south, east, north = shipped_geometries()["Dubai"].bounds
    dubai_box_hits = [
        gt for gt in SHARJAH_POINTS if south <= gt.lat <= north and west <= gt.lon <= east
    ]
    # Most Sharjah points sit inside Dubai's rectangle (only the east-coast exclaves do not) ...
    assert len(dubai_box_hits) >= 15
    assert {"S07", "S09", "S17", "S18"} <= {gt.key for gt in dubai_box_hits}
    # ... and a box test would call every one of them Dubai. The polygon test refuses all of them.
    for gt in dubai_box_hits:
        assert point_in_emirate(gt.lat, gt.lon, "Dubai") is False, gt.label
        assert emirate_for_point(gt.lat, gt.lon) == "Sharjah", gt.label


def test_a_sharjah_bounding_box_would_wrongly_accept_dubai_points():
    west, south, east, north = shipped_geometries()["Sharjah"].bounds
    sharjah_box_hits = [gt for gt in DUBAI_POINTS if south <= gt.lat <= north and west <= gt.lon <= east]
    assert len(sharjah_box_hits) >= 15
    for gt in sharjah_box_hits:
        assert point_in_emirate(gt.lat, gt.lon, "Sharjah") is False, gt.label


def test_the_hard_border_cases_sit_in_the_overlap_zone_and_are_still_separated():
    min_lat, max_lat, min_lon, max_lon = URBAN_OVERLAP_BOX
    for key in HARD_DUBAI + HARD_SHARJAH:
        gt = BY_KEY[key]
        assert min_lat <= gt.lat <= max_lat and min_lon <= gt.lon <= max_lon, f"{gt.label} is not a hard case"
    for key in HARD_DUBAI:
        assert emirate_for_point(BY_KEY[key].lat, BY_KEY[key].lon) == "Dubai", BY_KEY[key].label
    for key in HARD_SHARJAH:
        assert emirate_for_point(BY_KEY[key].lat, BY_KEY[key].lon) == "Sharjah", BY_KEY[key].label
    # Al Nahda is split between the emirates: both sides resolve to their own emirate.
    assert emirate_for_point(*_ll("D09")) == "Dubai"  # the Al Nahda metro station
    assert emirate_for_point(*_ll("S09")) == "Sharjah"  # Sahara Centre


def test_ajman_lies_inside_both_bounding_boxes_but_in_neither_polygon():
    ajman = BY_KEY["N01"]
    geoms = shipped_geometries()
    for emirate in EMIRATES:
        west, south, east, north = geoms[emirate].bounds
        assert south <= ajman.lat <= north and west <= ajman.lon <= east
    assert emirate_for_point(ajman.lat, ajman.lon) is None


# ---------------------------------------------------------------------------
# Simplification must not flip ground truth
# ---------------------------------------------------------------------------
def test_the_shipped_file_records_how_much_it_was_simplified_and_is_consistent_with_it():
    geoms = shipped_geometries()
    collection = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    for feature in collection["features"]:
        props = feature["properties"]
        assert props["simplify_tolerance_deg"] in fetcher.TOLERANCE_LADDER_DEG
        assert props["vertices"] == int(shapely.get_num_coordinates(geoms[props["emirate"]]))
        assert props["vertices"] <= props["raw_vertices"]
        assert props["max_deviation_m"] <= 50.0, "bundled outline drifted too far from OSM"


@pytest.mark.parametrize("tolerance", [1e-5, 1e-4, 2.5e-4, 5e-4, 1e-3])
def test_extra_simplification_does_not_flip_any_ground_truth_point(tolerance):
    """Re-simplifying the bundled polygons harder than the build ever did still keeps every point."""
    raw = shipped_geometries()
    simplified = fetcher.simplify_pair(raw, tolerance)
    deviation_km = max(fetcher.max_deviation_m(raw[name], simplified[name]) for name in EMIRATES) / 1000
    flipped = []
    for gt in GROUND_TRUTH:
        if classify(simplified, gt.lat, gt.lon) != gt.expected:
            flipped.append((gt.key, round(margin_km(gt.lat, gt.lon, raw), 3)))
    # A point may only flip if it was closer to the border than the outline moved.
    assert all(margin <= deviation_km for _, margin in flipped), (flipped, deviation_km)
    if tolerance <= 1e-4:
        assert flipped == []  # at about 11 m nothing moves, border-sensitive points included


# ---------------------------------------------------------------------------
# Out-of-area and invalid input
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("gt", NEITHER_POINTS, ids=lambda gt: gt.key)
def test_points_outside_both_emirates_are_none_and_not_in_either(gt):
    assert emirate_for_point(gt.lat, gt.lon) is None
    assert not point_in_emirate(gt.lat, gt.lon, "Dubai")
    assert not point_in_emirate(gt.lat, gt.lon, "Sharjah")


INVALID_PAIRS = [
    (float("nan"), 55.3), (25.2, float("nan")), (float("nan"), float("nan")),
    (float("inf"), 55.3), (25.2, float("-inf")),
    (None, None), (None, 55.3), (25.2, None),
    ("25.2", "55.3"), ("", ""), (b"25.2", b"55.3"),
    (True, False), (True, 55.3),
    (91.0, 55.3), (-91.0, 55.3), (25.2, 180.5), (25.2, -180.5), (1e308, 1e308),
    ([25.2], [55.3]), ({}, ()), (complex(25, 1), 55.3),
]


@pytest.mark.parametrize("lat,lon", INVALID_PAIRS)
def test_invalid_coordinates_never_raise_and_answer_no(lat, lon):
    assert emirate_for_point(lat, lon) is None
    assert point_in_emirate(lat, lon, "Dubai") is False
    assert point_in_emirate(lat, lon, "Sharjah") is False
    assert in_union_bbox(lat, lon) is False
    assert is_border_zone(lat, lon) is False
    assert math.isnan(distance_to_border_km(lat, lon, "Dubai"))
    assert math.isnan(distance_to_border_km(lat, lon, "Sharjah"))


def test_numpy_scalars_and_ints_are_accepted_as_numbers():
    d03_lat, d03_lon = _ll("D03")
    s07_lat, s07_lon = _ll("S07")
    assert emirate_for_point(np.float64(d03_lat), np.float64(d03_lon)) == "Dubai"
    assert emirate_for_point(np.float32(s07_lat), np.float32(s07_lon)) == "Sharjah"
    assert emirate_for_point(0, 0) is None


def test_swapped_latitude_and_longitude_is_rejected_not_misread():
    # (lon, lat) passed as (lat, lon): 55.14 N is valid, 25.08 E is valid, and nowhere near the UAE.
    assert emirate_for_point(55.14, 25.08) is None
    assert not in_union_bbox(55.14, 25.08)


def test_unknown_emirate_names_answer_false_or_nan_without_raising(caplog):
    emirates._warned_emirates.clear()
    with caplog.at_level(logging.WARNING, logger="src.emirates"):
        assert point_in_emirate(25.2, 55.3, "Ajman") is False
        assert point_in_emirate(25.2, 55.3, "Ajman") is False
        assert math.isnan(distance_to_border_km(25.2, 55.3, "Ajman"))
        assert point_in_emirate(25.2, 55.3, None) is False  # type: ignore[arg-type]
        assert point_in_emirate(25.2, 55.3, 3) is False  # type: ignore[arg-type]
    warnings = [r.message for r in caplog.records if "unknown emirate" in r.message]
    assert len(warnings) == 3  # 'Ajman' (once, not per call), None, 3
    emirates._warned_emirates.clear()


def test_the_unknown_emirate_warning_never_repeats_the_value_it_was_given(caplog):
    # Regression: the warning printed repr(name), so a sheet cell passed in by mistake reached the public log.
    marker = "zq7emirate"
    emirates._warned_emirates.clear()
    with caplog.at_level(logging.WARNING, logger="src.emirates"):
        assert point_in_emirate(25.2, 55.3, marker) is False
        assert math.isnan(distance_to_border_km(25.2, 55.3, marker + " Dubai"))
        assert point_in_emirate(25.2, 55.3, 3) is False  # type: ignore[arg-type]
    warnings = [r.getMessage() for r in caplog.records if "unknown emirate" in r.getMessage()]
    assert len(warnings) == 3
    assert marker not in caplog.text
    assert "type str" in warnings[0] and "type int" in warnings[2]
    emirates._warned_emirates.clear()


def test_emirate_names_tolerate_case_and_whitespace():
    assert point_in_emirate(*_ll("D03"), "dubai") is True
    assert point_in_emirate(*_ll("D03"), "  DUBAI ") is True
    assert point_in_emirate(*_ll("S07"), "sharjah") is True
    assert point_in_emirate(*_ll("S07"), "Dubai") is False


# ---------------------------------------------------------------------------
# in_union_bbox
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "lat,lon,expected",
    [
        (24.62, 53.98, True), (25.70, 56.60, True), (24.62, 56.60, True), (25.70, 53.98, True),  # corners are inclusive
        (24.6199, 55.0, False), (25.7001, 55.0, False), (25.0, 53.9799, False), (25.0, 56.6001, False),
        (25.2, 55.3, True), (BY_KEY["N05"].lat, BY_KEY["N05"].lon, False),  # Abu Dhabi city is outside
        (BY_KEY["N13"].lat, BY_KEY["N13"].lon, False), (51.5, -0.1, False),  # Muscat; and a generic far point
    ],
)
def test_union_bbox_is_inclusive_and_coarse(lat, lon, expected):
    assert in_union_bbox(lat, lon) is expected


def test_union_bbox_covers_the_polygons_to_within_the_documented_clip():
    # The rounded box clips the two outermost offshore tips of Sharjah's water by about 250 m.
    min_lat, max_lat, min_lon, max_lon = UNION_BBOX
    west, south, east, north = unary_union(list(shipped_geometries().values())).bounds
    assert min_lat - 0.01 <= south and north <= max_lat + 0.01
    assert min_lon - 0.01 <= west and east <= max_lon + 0.01
    assert (west, south, east, north) != (min_lon, min_lat, max_lon, max_lat)


# ---------------------------------------------------------------------------
# Distance to the border, border zone
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["S09", "D10", "S24", "D09", "S06", "D25", "S05", "S01", "D03", "D01", "S12", "D18"])
def test_distance_to_border_agrees_with_an_exhaustive_search(key):
    gt = BY_KEY[key]
    geoms = shipped_geometries()
    other = "Sharjah" if gt.expected == "Dubai" else "Dubai"
    got = distance_to_border_km(gt.lat, gt.lon, gt.expected)
    brute = brute_force_distance_km(gt.lat, gt.lon, geoms[other])
    # The densified search over-estimates by up to ~11 m; the module may differ by < 0.5 percent.
    assert got == pytest.approx(brute, abs=0.03, rel=0.01), (gt.label, got, brute)


def test_distance_is_zero_when_the_point_is_actually_in_the_other_emirate():
    sharjah_point = BY_KEY["S07"]
    dubai_point = BY_KEY["D07"]
    assert distance_to_border_km(sharjah_point.lat, sharjah_point.lon, "Dubai") == 0.0  # declared Dubai, landed in Sharjah
    assert distance_to_border_km(dubai_point.lat, dubai_point.lon, "Sharjah") == 0.0


def test_distance_values_are_sensible_for_known_places():
    assert distance_to_border_km(*_ll("S09"), "Sharjah") < 0.2  # Sahara Centre is a stone's throw from Dubai
    assert distance_to_border_km(*_ll("D03"), "Dubai") > 25  # Dubai Marina is far from Sharjah
    assert distance_to_border_km(*_ll("D01"), "Dubai") > 10  # Hatta is far from Sharjah
    assert distance_to_border_km(*_ll("S01"), "Sharjah") > 20  # Kalba is far from Dubai


def _ll(key: str) -> tuple[float, float]:
    return BY_KEY[key].lat, BY_KEY[key].lon


def test_haversine_matches_a_known_arc():
    # One degree of latitude on the mean-radius sphere is 111.195 km; a longitude degree shrinks by cos(lat).
    assert emirates._haversine_km(0.0, 0.0, 1.0, 0.0) == pytest.approx(111.195, abs=0.01)
    assert emirates._haversine_km(25.0, 55.0, 25.0, 56.0) == pytest.approx(111.195 * math.cos(math.radians(25.0)), rel=2e-3)
    assert emirates._haversine_km(25.2, 55.3, 25.2, 55.3) == 0.0


def test_border_zone_true_near_the_shared_border_and_false_elsewhere():
    for key in ("S09", "D10", "S24", "D25", "S06"):
        assert is_border_zone(*_ll(key)), BY_KEY[key].label
    for key in ("D03", "D04", "D06", "D01", "S01", "S03", "S21", "S22", "S20", "N05", "N13"):
        assert not is_border_zone(*_ll(key)), BY_KEY[key].label


def test_border_zone_needs_the_other_emirate_not_just_any_boundary():
    # Ajman city sits ~1 km from Sharjah but ~12 km from Dubai, and Hamriyah hugs Ajman and the sea.
    assert not is_border_zone(*_ll("N01"))
    assert not is_border_zone(*_ll("S04"))


def test_border_zone_threshold_is_inclusive_and_monotonic():
    lat, lon = _ll("S09")
    d = distance_to_border_km(lat, lon, "Sharjah")
    assert 0.05 < d < 0.2
    assert is_border_zone(lat, lon, threshold_km=d) is True
    assert is_border_zone(lat, lon, threshold_km=d - 1e-6) is False
    assert is_border_zone(lat, lon, threshold_km=0.1) is True
    assert is_border_zone(lat, lon, threshold_km=0.05) is False
    # Default threshold is 1.5 km.
    lat2, lon2 = _ll("D09")  # about 2.6 km from Sharjah
    assert is_border_zone(lat2, lon2) is False
    assert is_border_zone(lat2, lon2, threshold_km=3.0) is True
    # Growing the threshold can only add points.
    small = {gt.key for gt in GROUND_TRUTH if is_border_zone(gt.lat, gt.lon, 1.5)}
    large = {gt.key for gt in GROUND_TRUTH if is_border_zone(gt.lat, gt.lon, 5.0)}
    assert small <= large and small != large


@pytest.mark.parametrize("threshold", [float("nan"), -1.0, float("inf"), float("-inf"), "1.5", None, True])
def test_border_zone_rejects_a_bad_threshold(threshold):
    assert is_border_zone(*_ll("S09"), threshold_km=threshold) is False  # type: ignore[arg-type]


def test_the_default_border_threshold_is_the_designs_1_5_km():
    # docs/DESIGN.md section 5: pins within 1.5 km of the other emirate get `border-check`.
    # Regression: no other test pinned the default, so 1.5 could drift to 2.5 unnoticed.
    assert inspect.signature(is_border_zone).parameters["threshold_km"].default == 1.5
    geoms = shipped_geometries()
    d10, s08 = BY_KEY["D10"], BY_KEY["S08"]  # exhaustive-search distances: 0.92 km and 1.68 km
    assert brute_force_distance_km(d10.lat, d10.lon, geoms["Sharjah"]) < 1.5
    assert brute_force_distance_km(s08.lat, s08.lon, geoms["Dubai"]) > 1.5
    assert is_border_zone(d10.lat, d10.lon) is True
    assert is_border_zone(s08.lat, s08.lon) is False
    assert is_border_zone(s08.lat, s08.lon, threshold_km=2.0) is True


# ---------------------------------------------------------------------------
# Tie rule
# ---------------------------------------------------------------------------
def _shared_border_vertex() -> tuple[float, float]:
    geoms = shipped_geometries()
    shared = geoms["Dubai"].boundary.intersection(geoms["Sharjah"].boundary)
    coords = shapely.get_coordinates(shared)
    assert len(coords) > 10, "the OSM pair should share a long border"
    x, y = coords[len(coords) // 2]
    return float(y), float(x)  # (lat, lon)


def test_a_point_exactly_on_the_shared_border_is_in_neither_emirate():
    lat, lon = _shared_border_vertex()
    assert point_in_emirate(lat, lon, "Dubai") is False
    assert point_in_emirate(lat, lon, "Sharjah") is False
    assert emirate_for_point(lat, lon) is None
    assert distance_to_border_km(lat, lon, "Dubai") == 0.0
    assert distance_to_border_km(lat, lon, "Sharjah") == 0.0
    assert is_border_zone(lat, lon) is True
    assert is_border_zone(lat, lon, threshold_km=0.0) is True  # inclusive: 0 km is within 0 km


def test_a_point_on_a_non_shared_edge_is_outside_and_not_a_border_zone():
    geoms = shipped_geometries()
    for lon, lat in geoms["Dubai"].exterior.coords if geoms["Dubai"].geom_type == "Polygon" else [
        c for part in geoms["Dubai"].geoms for c in part.exterior.coords
    ]:
        if geoms["Sharjah"].distance(Point(lon, lat)) > 0.2:  # a coastal vertex far from Sharjah
            assert point_in_emirate(lat, lon, "Dubai") is False  # strict containment: the edge is not inside
            assert emirate_for_point(lat, lon) is None
            assert is_border_zone(lat, lon) is False
            return
    pytest.fail("no Dubai vertex far from Sharjah found")


def test_the_answer_is_deterministic_across_reloads_and_call_order(fresh_loader):
    forward = [emirate_for_point(gt.lat, gt.lon) for gt in GROUND_TRUTH]
    emirates._boundaries.cache_clear()
    backward = [emirate_for_point(gt.lat, gt.lon) for gt in reversed(GROUND_TRUTH)]
    assert forward == list(reversed(backward))
    lat, lon = _shared_border_vertex()
    assert {emirate_for_point(lat, lon) for _ in range(5)} == {None}


def _write_polygon_file(path: Path, dubai, sharjah) -> Path:
    """SYNTHETIC two-feature file in the shipped format."""
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {"emirate": "Dubai"}, "geometry": mapping(dubai)},
                    {"type": "Feature", "properties": {"emirate": "Sharjah"}, "geometry": mapping(sharjah)},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_a_point_inside_both_polygons_is_none_never_a_pick(tmp_path, monkeypatch, fresh_loader):
    # SYNTHETIC: two overlapping squares, the overlap being lon 55.10..55.15.
    path = _write_polygon_file(tmp_path / "overlap.geojson", box(55.0, 25.0, 55.15, 25.1), box(55.10, 25.0, 55.25, 25.1))
    monkeypatch.setattr(emirates, "_DATA_PATH", path)
    assert emirate_for_point(25.05, 55.05) == "Dubai"
    assert emirate_for_point(25.05, 55.20) == "Sharjah"
    assert emirate_for_point(25.05, 55.125) is None  # in both
    assert point_in_emirate(25.05, 55.125, "Dubai") is True and point_in_emirate(25.05, 55.125, "Sharjah") is True
    assert is_border_zone(25.05, 55.125) is True


# ---------------------------------------------------------------------------
# Loading the data file
# ---------------------------------------------------------------------------
def test_a_missing_polygon_file_raises_loudly_and_names_the_builder(tmp_path, monkeypatch, fresh_loader):
    monkeypatch.setattr(emirates, "_DATA_PATH", tmp_path / "absent.geojson")
    with pytest.raises(FileNotFoundError, match="fetch_emirate_polygons"):
        emirate_for_point(25.2, 55.3)


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"type": "FeatureCollection"}, "FeatureCollection"),
        ({"type": "FeatureCollection", "features": [{"properties": {"emirate": "Ajman"}, "geometry": mapping(box(0, 0, 1, 1))}]}, "unexpected emirate"),
        ({"type": "FeatureCollection", "features": [{"properties": {"emirate": "Dubai"}, "geometry": mapping(box(55, 25, 55.1, 25.1))}]}, "no polygon for Sharjah"),
        (
            {"type": "FeatureCollection", "features": [
                {"properties": {"emirate": "Dubai"}, "geometry": mapping(box(55, 25, 55.1, 25.1))},
                {"properties": {"emirate": "Dubai"}, "geometry": mapping(box(55, 25, 55.1, 25.1))},
            ]},
            "two features for Dubai",
        ),
        (
            {"type": "FeatureCollection", "features": [
                {"properties": {"emirate": "Dubai"}, "geometry": mapping(Point(55, 25))},
                {"properties": {"emirate": "Sharjah"}, "geometry": mapping(box(55, 25, 55.1, 25.1))},
            ]},
            "not a valid, non-empty polygon",
        ),
        (
            {"type": "FeatureCollection", "features": [  # a self-intersecting bow-tie
                {"properties": {"emirate": "Dubai"}, "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}},
                {"properties": {"emirate": "Sharjah"}, "geometry": mapping(box(55, 25, 55.1, 25.1))},
            ]},
            "not a valid, non-empty polygon",
        ),
    ],
)
def test_a_corrupt_polygon_file_raises_instead_of_answering_wrongly(tmp_path, monkeypatch, fresh_loader, payload, match):
    path = tmp_path / "bad.geojson"  # SYNTHETIC
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(emirates, "_DATA_PATH", path)
    with pytest.raises(ValueError, match=match):
        emirate_for_point(25.2, 55.3)


# ---------------------------------------------------------------------------
# The bundled file
# ---------------------------------------------------------------------------
def test_bundled_file_has_the_documented_shape_and_provenance():
    collection = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    assert collection["type"] == "FeatureCollection"
    assert [f["properties"]["emirate"] for f in collection["features"]] == ["Dubai", "Sharjah"]
    relations = {f["properties"]["emirate"]: f["properties"]["osm_relation"] for f in collection["features"]}
    assert relations == {"Dubai": 3766483, "Sharjah": 3766486}
    for feature in collection["features"]:
        props = feature["properties"]
        assert props["osm_license"] == "ODbL 1.0"
        assert props["attribution"] == "© OpenStreetMap contributors"
        stamp = datetime.strptime(props["fetched_at"], "%Y-%m-%dT%H:%M:%SZ")  # UTC, ISO-8601, Z suffix
        assert stamp.year >= 2026
        assert props["admin_level"] == 4
        assert feature["geometry"]["type"] == "MultiPolygon"


def test_bundled_file_is_small_ascii_and_valid():
    raw = DATA_FILE.read_bytes()
    assert len(raw) < 1_500_000
    raw.decode("ascii")  # no stray non-ASCII bytes: the copyright sign is escaped
    for name, geometry in shipped_geometries().items():
        assert geometry.is_valid and not geometry.is_empty, name


def test_bundled_polygons_keep_the_exclaves_as_separate_parts():
    geoms = shipped_geometries()
    dubai_parts, sharjah_parts = list(geoms["Dubai"].geoms), list(geoms["Sharjah"].geoms)
    assert len(dubai_parts) >= 2 and len(sharjah_parts) >= 4

    def part_with(parts, key):
        gt = BY_KEY[key]
        hits = [i for i, p in enumerate(parts) if p.contains(Point(gt.lon, gt.lat))]
        assert len(hits) == 1, key
        return hits[0]

    assert part_with(dubai_parts, "D01") != part_with(dubai_parts, "D03")  # Hatta is not the main body
    assert len({part_with(sharjah_parts, k) for k in ("S01", "S03", "S20", "S07")}) == 4  # Kalba, Khor Fakkan, Dibba, main


def test_the_two_emirates_share_a_clean_border_with_no_overlap():
    geoms = shipped_geometries()
    assert geoms["Dubai"].intersection(geoms["Sharjah"]).area < 1e-9
    shared = geoms["Dubai"].boundary.intersection(geoms["Sharjah"].boundary)
    assert shared.length > 0.5  # degrees: a long common border, not a point touch
    assert unary_union(list(geoms.values())).area == pytest.approx(sum(g.area for g in geoms.values()), rel=1e-9)


def test_loading_is_cached_and_does_not_reread_the_file(monkeypatch):
    emirates._boundaries.cache_clear()
    first = emirates._boundaries()
    monkeypatch.setattr(emirates, "_DATA_PATH", Path("does-not-exist.geojson"))
    assert emirates._boundaries() is first  # served from the cache
    emirates._boundaries.cache_clear()
    monkeypatch.undo()
    assert emirate_for_point(*_ll("D03")) == "Dubai"


# ===========================================================================
# scripts/fetch_emirate_polygons.py  (all offline, SYNTHETIC)
# ===========================================================================
# Two unit squares near Dubai that share the edge lon = 55.10; ids are invented.
SYN_DUBAI = fetcher.EmirateSpec("Dubai", 9000001, "Dubai Emirate", "AE-DU", inside=((25.05, 55.05),), outside=((25.05, 55.15),))
SYN_SHARJAH = fetcher.EmirateSpec("Sharjah", 9000002, "Sharjah Emirate", "AE-SH", inside=((25.05, 55.15),), outside=((25.05, 55.05),))
SYN_SPECS = (SYN_DUBAI, SYN_SHARJAH)


def syn_feature(spec, geometry, **props):
    base = {
        "osm_type": "relation", "osm_id": spec.relation, "category": "boundary", "type": "administrative",
        "name": spec.osm_name, "extratags": {"admin_level": "4"}, "address": {"ISO3166-2-lvl4": spec.iso_code},
    }
    base.update(props)
    return {"type": "Feature", "properties": base, "geometry": mapping(geometry)}


def syn_response(dubai=None, sharjah=None):
    return {
        "type": "FeatureCollection",
        "features": [
            syn_feature(SYN_DUBAI, dubai or box(55.0, 25.0, 55.10, 25.10)),
            syn_feature(SYN_SHARJAH, sharjah or box(55.10, 25.0, 55.20, 25.10)),
        ],
    }


def real_shaped_response(**overrides):
    """A response shaped like Nominatim's, carrying the bundled (verbatim OSM) geometry and the real ids."""
    geoms = shipped_geometries()
    features = []
    for spec in fetcher.SPECS:
        props = {"admin_level": "4"} if False else {}
        feature = syn_feature(spec, geoms[spec.emirate], **props)
        features.append(feature)
    response = {"type": "FeatureCollection", "features": features}
    for key, value in overrides.items():
        response[key] = value
    return response


# --- verification ------------------------------------------------------------------
def test_verify_feature_accepts_a_correct_relation():
    fetcher.verify_feature(syn_response()["features"][0], SYN_DUBAI)


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"osm_id": 12345}, "expected relation 9000001"),
        ({"osm_type": "way"}, "expected relation"),
        ({"category": "place", "type": "city"}, "boundary/administrative"),
        ({"extratags": {"admin_level": "2"}}, "admin_level 4"),
        ({"extratags": {}}, "admin_level 4"),
        ({"name": "Ajman Emirate"}, "expected name"),
        ({"address": {"ISO3166-2-lvl4": "AE-AJ"}}, "ISO 3166-2"),
    ],
)
def test_verify_feature_rejects_the_wrong_relation(changes, match):
    feature = syn_response()["features"][0]
    feature["properties"].update(changes)
    with pytest.raises(fetcher.BoundaryFetchError, match=match):
        fetcher.verify_feature(feature, SYN_DUBAI)


def test_verify_feature_rejects_a_point_geometry():
    feature = syn_response()["features"][0]
    feature["geometry"] = {"type": "Point", "coordinates": [55.0, 25.0]}
    with pytest.raises(fetcher.BoundaryFetchError, match="not a polygon"):
        fetcher.verify_feature(feature, SYN_DUBAI)


def test_real_relation_ids_and_names_are_the_ones_the_design_claims():
    assert [(s.emirate, s.relation, s.osm_name, s.iso_code) for s in fetcher.SPECS] == [
        ("Dubai", 3766483, "Dubai Emirate", "AE-DU"),
        ("Sharjah", 3766486, "Sharjah Emirate", "AE-SH"),
    ]
    assert fetcher.BOUNDARY_ADMIN_LEVEL == "4"


# --- geometry helpers ---------------------------------------------------------------
def test_as_multipolygon_orients_and_sorts_without_moving_vertices():
    small = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])  # clockwise
    big = Polygon([(10, 10), (14, 10), (14, 14), (10, 14)])
    result = fetcher.as_multipolygon(MultiPolygon([small, big]))
    assert [round(p.area) for p in result.geoms] == [16, 1]
    assert all(p.exterior.is_ccw for p in result.geoms)
    assert {tuple(c) for p in result.geoms for c in p.exterior.coords} == {tuple(c) for p in (small, big) for c in p.exterior.coords}


def test_as_multipolygon_rejects_non_polygons():
    with pytest.raises(fetcher.BoundaryFetchError):
        fetcher.as_multipolygon(Point(1, 1))
    with pytest.raises(fetcher.BoundaryFetchError):
        fetcher.as_multipolygon(Polygon())


def test_geometry_from_feature_repairs_a_bow_tie(caplog):
    bow_tie = {"type": "Polygon", "coordinates": [[[55.0, 25.0], [55.1, 25.1], [55.1, 25.0], [55.0, 25.1], [55.0, 25.0]]]}
    feature = {"geometry": bow_tie}
    with caplog.at_level(logging.WARNING, logger="fetch_emirate_polygons"):
        repaired = fetcher.geometry_from_feature(feature)
    assert repaired.is_valid and repaired.geom_type == "MultiPolygon" and len(repaired.geoms) == 2
    assert any("invalid" in r.message for r in caplog.records)


def test_check_geometry_flags_a_missing_exclave_a_foreign_outline_and_a_wrong_overlap():
    good = box(55.0, 25.0, 55.10, 25.10)
    fetcher.check_geometry(good, SYN_DUBAI)
    no_hatta = fetcher.EmirateSpec("Dubai", 1, "Dubai Emirate", "AE-DU", inside=((24.80, 56.14),), outside=())
    with pytest.raises(fetcher.BoundaryFetchError, match="does not contain"):
        fetcher.check_geometry(good, no_hatta)
    with pytest.raises(fetcher.BoundaryFetchError, match="wrongly contains"):
        fetcher.check_geometry(box(55.0, 25.0, 55.30, 25.10), SYN_DUBAI)  # swallows the Sharjah proof point
    with pytest.raises(fetcher.BoundaryFetchError, match="outside the UAE window"):
        fetcher.check_geometry(box(10.0, 45.0, 11.0, 46.0), SYN_DUBAI)


def test_check_pair_allows_a_shared_edge_warns_on_a_sliver_and_rejects_a_big_overlap(caplog):
    clean = {"Dubai": box(55.0, 25.0, 55.10, 25.10), "Sharjah": box(55.10, 25.0, 55.20, 25.10)}
    assert fetcher.check_pair(clean) == 0.0
    sliver = {"Dubai": box(55.0, 25.0, 55.10, 25.10), "Sharjah": box(55.0999, 25.0, 55.20, 25.10)}
    with caplog.at_level(logging.WARNING, logger="fetch_emirate_polygons"):
        assert 0 < fetcher.check_pair(sliver) < fetcher.MAX_OVERLAP_DEG2
    assert any("overlap" in r.message for r in caplog.records)
    big = {"Dubai": box(55.0, 25.0, 55.15, 25.10), "Sharjah": box(55.05, 25.0, 55.20, 25.10)}
    with pytest.raises(fetcher.BoundaryFetchError, match="corrupt"):
        fetcher.check_pair(big)


# --- simplification -----------------------------------------------------------------
def jagged_pair(amplitude=0.00002):
    """SYNTHETIC: two polygons whose shared edge zig-zags by `amplitude` degrees."""
    zig = [(55.10 + (amplitude if i % 2 else 0.0), 25.0 + i * 0.005) for i in range(21)]
    west = Polygon([(55.0, 25.0)] + zig + [(55.0, 25.1)])
    east = Polygon([(55.2, 25.0)] + zig + [(55.2, 25.1)])
    return {"Dubai": west, "Sharjah": east}


def test_simplify_pair_with_zero_tolerance_leaves_every_vertex():
    pair = jagged_pair()
    result = fetcher.simplify_pair(pair, 0.0)
    assert {tuple(c) for g in result.values() for p in g.geoms for c in p.exterior.coords} == {
        tuple(c) for g in pair.values() for c in g.exterior.coords
    }


@pytest.mark.skipif(not hasattr(shapely, "coverage_simplify"), reason="needs shapely 2.1+")
def test_coverage_simplification_keeps_the_shared_border_identical():
    pair = jagged_pair()
    # coverage_simplify is area-based (triangle area against tolerance squared), so the
    # tolerance must be large enough for the 5 m zig-zag over 0.005 degree steps to collapse.
    result = fetcher.simplify_pair(pair, 0.001, use_coverage=True)
    assert result["Dubai"].is_valid and result["Sharjah"].is_valid
    assert fetcher.overlap_area(result["Dubai"], result["Sharjah"]) == 0.0
    assert shapely.get_num_coordinates(result["Dubai"]) < shapely.get_num_coordinates(pair["Dubai"])
    shared = result["Dubai"].boundary.intersection(result["Sharjah"].boundary)
    assert shared.length == pytest.approx(0.1, abs=1e-4)  # the whole straightened edge is still common to both
    # The polygons still tile the same ground: the union loses and gains nothing.
    assert unary_union(list(result.values())).area == pytest.approx(sum(g.area for g in result.values()), rel=1e-12)


def test_fallback_simplification_without_coverage_stays_valid_and_checks_overlap():
    pair = jagged_pair()
    result = fetcher.simplify_pair(pair, 0.0001, use_coverage=False)
    assert all(g.is_valid for g in result.values())
    assert fetcher.overlap_area(result["Dubai"], result["Sharjah"]) <= fetcher.WARN_OVERLAP_DEG2


def test_fallback_simplification_refuses_to_ship_a_new_overlap(monkeypatch):
    calls = iter([0.0, 0.5])  # no overlap before, a big one after
    monkeypatch.setattr(fetcher, "overlap_area", lambda a, b: next(calls))
    with pytest.raises(fetcher.BoundaryFetchError, match="opened an overlap"):
        fetcher.simplify_pair(jagged_pair(), 0.0001, use_coverage=False)


def test_simplify_pair_rejects_a_negative_tolerance():
    with pytest.raises(ValueError):
        fetcher.simplify_pair(jagged_pair(), -1.0)


def test_choose_tolerance_prefers_no_simplification_when_the_file_fits():
    raw = shipped_geometries()
    tolerance, result = fetcher.choose_tolerance(raw, fetcher.SPECS, budget_bytes=1_500_000)
    assert tolerance == 0.0
    assert {name: int(shapely.get_num_coordinates(g)) for name, g in result.items()} == {
        name: int(shapely.get_num_coordinates(g)) for name, g in raw.items()
    }


def test_choose_tolerance_takes_the_smallest_tolerance_that_fits_a_tight_budget():
    raw = shipped_geometries()
    tolerance, result = fetcher.choose_tolerance(raw, fetcher.SPECS, budget_bytes=25_000)
    assert 0.0 < tolerance <= 5e-4
    size = len(fetcher.dumps(fetcher.build_collection(result, raw, fetcher.SPECS, fetched_at="2026-10-25T00:00:00Z", tolerance_deg=tolerance)))
    assert size <= 25_000
    smaller = fetcher.TOLERANCE_LADDER_DEG[fetcher.TOLERANCE_LADDER_DEG.index(tolerance) - 1]
    larger_file = fetcher.simplify_pair(raw, smaller)
    assert len(fetcher.dumps(fetcher.build_collection(larger_file, raw, fetcher.SPECS, fetched_at="2026-10-25T00:00:00Z", tolerance_deg=smaller))) > 25_000


def test_choose_tolerance_fails_loudly_when_nothing_fits():
    with pytest.raises(fetcher.BoundaryFetchError, match="budget"):
        fetcher.choose_tolerance(shipped_geometries(), fetcher.SPECS, budget_bytes=1_000)


# --- polite HTTP --------------------------------------------------------------------
class FakeClock:
    """SYNTHETIC clock: sleeping advances time instantly."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, status=200, headers=None, payload=None, content=b"{}"):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class FakeSession:
    def __init__(self, script, clock=None):
        self.script = list(script)
        self.calls: list[tuple] = []
        self.clock = clock

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout, self.clock() if self.clock else None))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def run_get(script, **kwargs):
    clock = FakeClock()
    session = FakeSession(script, clock)
    throttle = fetcher.Throttle(clock=clock, sleep=clock.sleep)
    response = fetcher.get_with_backoff(session, "https://example.invalid/lookup", {"q": 1}, throttle=throttle, sleep=clock.sleep, **kwargs)
    return response, session, clock, throttle


def test_throttle_spaces_requests_by_at_least_a_second():
    clock = FakeClock()
    throttle = fetcher.Throttle(clock=clock, sleep=clock.sleep)
    throttle.wait()
    clock.now += 0.2  # the request took 0.2 s
    throttle.wait()
    assert clock.slept == [pytest.approx(fetcher.MIN_INTERVAL_SECONDS - 0.2)]
    assert fetcher.MIN_INTERVAL_SECONDS >= 1.0
    assert throttle.requests_made == 2
    clock.now += 5
    throttle.wait()  # already long enough: no extra sleep
    assert len(clock.slept) == 1


def test_a_clean_200_is_one_request_and_no_sleep_beyond_the_first():
    response, session, clock, throttle = run_get([FakeResponse(200)])
    assert response.status_code == 200 and len(session.calls) == 1 and clock.slept == []


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_statuses_are_retried_with_growing_backoff(status):
    response, session, clock, _ = run_get([FakeResponse(status), FakeResponse(status), FakeResponse(200)])
    assert response.status_code == 200 and len(session.calls) == 3
    backoffs = [s for s in clock.slept if s >= 5]
    assert backoffs == [5.0, 15.0]
    times = [call[3] for call in session.calls]
    assert all(b - a >= 1.0 for a, b in zip(times, times[1:])), times  # never faster than 1 request per second


def test_retry_after_is_honoured_capped_and_ignored_when_not_a_number():
    _, _, clock, _ = run_get([FakeResponse(429, {"Retry-After": "7"}), FakeResponse(200)])
    assert 7.0 in clock.slept
    _, _, clock, _ = run_get([FakeResponse(429, {"Retry-After": "99999"}), FakeResponse(200)])
    assert fetcher.MAX_RETRY_AFTER_SECONDS in clock.slept
    _, _, clock, _ = run_get([FakeResponse(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), FakeResponse(200)])
    assert 5.0 in clock.slept  # fell back to our own first backoff step


def test_a_nan_retry_after_is_ignored_instead_of_crashing_the_sleep():
    # Regression: float("nan") parses, min(nan, 300) stays nan, and time.sleep(nan) raises ValueError.
    assert fetcher._retry_after_seconds(FakeResponse(429, {"Retry-After": "nan"})) is None
    response, _, clock, _ = run_get([FakeResponse(429, {"Retry-After": "nan"}), FakeResponse(200)])
    assert response.status_code == 200
    assert all(math.isfinite(s) for s in clock.slept)
    assert 5.0 in clock.slept  # fell back to our own first backoff step


def test_every_request_carries_a_finite_connect_and_read_timeout():
    # Regression: a missing timeout lets one stalled connection hang the build forever.
    _, session, _, _ = run_get([FakeResponse(200)])
    timeout = session.calls[0][2]
    assert isinstance(timeout, tuple) and len(timeout) == 2
    assert all(isinstance(t, (int, float)) and 0 < t < 600 for t in timeout), timeout


def test_connection_errors_and_timeouts_are_retried():
    response, session, _, _ = run_get([requests.ConnectionError("down"), requests.Timeout("slow"), FakeResponse(200)])
    assert response.status_code == 200 and len(session.calls) == 3


@pytest.mark.parametrize("status", [400, 403, 404, 500])
def test_non_transient_statuses_fail_immediately_without_retry(status):
    clock = FakeClock()
    session = FakeSession([FakeResponse(status), FakeResponse(200)], clock)
    with pytest.raises(fetcher.BoundaryFetchError, match=str(status)):
        fetcher.get_with_backoff(session, "u", {}, throttle=fetcher.Throttle(clock=clock, sleep=clock.sleep), sleep=clock.sleep)
    assert len(session.calls) == 1


def test_giving_up_after_the_last_backoff_raises():
    with pytest.raises(fetcher.BoundaryFetchError, match="gave up after 5 attempts"):
        run_get([FakeResponse(504)] * 5)
    clock = FakeClock()
    session = FakeSession([FakeResponse(504)] * 3, clock)
    with pytest.raises(fetcher.BoundaryFetchError, match="gave up after 3 attempts"):
        fetcher.get_with_backoff(session, "u", {}, throttle=fetcher.Throttle(clock=clock, sleep=clock.sleep), sleep=clock.sleep, backoff=(1.0, 2.0))


def test_the_session_identifies_itself_and_the_lookup_is_one_request_for_both_relations():
    assert fetcher.make_session().headers["User-Agent"] == "uae-real-estate-scraper/1.0 (boundary build)"
    assert fetcher.USER_AGENT == "uae-real-estate-scraper/1.0 (boundary build)"
    params = fetcher.lookup_params(fetcher.SPECS)
    assert params["osm_ids"] == "R3766483,R3766486"
    assert params["polygon_geojson"] == 1 and params["polygon_threshold"] == 0 and params["format"] == "geojson"
    assert params["accept-language"] == "en"
    clock = FakeClock()
    payload = {"type": "FeatureCollection", "features": []}
    session = FakeSession([FakeResponse(200, payload=payload)], clock)
    got = fetcher.fetch_lookup(session=session, throttle=fetcher.Throttle(clock=clock, sleep=clock.sleep), sleep=clock.sleep)
    assert got == payload and len(session.calls) == 1
    assert session.calls[0][0] == fetcher.LOOKUP_URL and session.calls[0][1] == params


def test_a_non_json_lookup_response_is_an_error():
    clock = FakeClock()
    session = FakeSession([FakeResponse(200, payload=None)], clock)
    with pytest.raises(fetcher.BoundaryFetchError, match="not JSON"):
        fetcher.fetch_lookup(session=session, throttle=fetcher.Throttle(clock=clock, sleep=clock.sleep), sleep=clock.sleep)


# --- building the file ----------------------------------------------------------------
def test_build_from_response_produces_the_documented_collection(tmp_path):
    collection = fetcher.build_from_response(syn_response(), SYN_SPECS, fetched_at="2026-10-25T03:00:00Z")
    assert collection["type"] == "FeatureCollection" and len(collection["features"]) == 2
    first = collection["features"][0]["properties"]
    assert first["emirate"] == "Dubai" and first["osm_relation"] == 9000001
    assert first["osm_license"] == "ODbL 1.0" and first["attribution"] == "© OpenStreetMap contributors"
    assert first["fetched_at"] == "2026-10-25T03:00:00Z"
    assert first["simplify_tolerance_deg"] == 0.0 and first["vertices"] == first["raw_vertices"]
    # The output is exactly what src/emirates.py loads.
    path = tmp_path / "out.geojson"
    path.write_text(fetcher.dumps(collection), encoding="utf-8")
    loaded = emirates._load_boundaries(path)
    assert set(loaded) == set(EMIRATES)
    assert shapely.contains_xy(loaded["Dubai"].geometry, 55.05, 25.05)
    assert not shapely.contains_xy(loaded["Dubai"].geometry, 55.15, 25.05)


def test_build_from_response_rejects_missing_swapped_and_unverified_relations():
    missing = syn_response()
    missing["features"].pop()
    with pytest.raises(fetcher.BoundaryFetchError, match="missing from the response"):
        fetcher.build_from_response(missing, SYN_SPECS)
    swapped = syn_response()
    swapped["features"][0]["properties"]["osm_id"], swapped["features"][1]["properties"]["osm_id"] = 9000002, 9000001
    with pytest.raises(fetcher.BoundaryFetchError):
        fetcher.build_from_response(swapped, SYN_SPECS)  # names no longer match the ids
    wrong_level = syn_response()
    wrong_level["features"][0]["properties"]["extratags"]["admin_level"] = "6"
    with pytest.raises(fetcher.BoundaryFetchError, match="admin_level"):
        fetcher.build_from_response(wrong_level, SYN_SPECS)
    with pytest.raises(fetcher.BoundaryFetchError, match="features"):
        fetcher.build_from_response({"nope": 1}, SYN_SPECS)


def test_build_from_response_rejects_a_geometry_that_loses_its_proof_point():
    with pytest.raises(fetcher.BoundaryFetchError, match="does not contain"):
        fetcher.build_from_response(syn_response(dubai=box(55.0, 25.0, 55.02, 25.02)), SYN_SPECS)


def test_build_from_response_rejects_overlapping_emirates():
    response = syn_response(dubai=box(55.0, 25.0, 55.14, 25.10), sharjah=box(55.06, 25.0, 55.20, 25.10))
    with pytest.raises(fetcher.BoundaryFetchError):
        fetcher.build_from_response(response, SYN_SPECS)


def test_rebuilding_from_the_bundled_geometry_reproduces_it_exactly(tmp_path):
    """Idempotence: feeding the shipped outline back through the builder changes no vertex."""
    raw_file = tmp_path / "raw.json"
    raw_file.write_text(json.dumps(real_shaped_response()), encoding="utf-8")
    out = tmp_path / "rebuilt.geojson"
    assert fetcher.main(["--from-file", str(raw_file), "--out", str(out)]) == 0
    rebuilt = {f["properties"]["emirate"]: shape(f["geometry"]) for f in json.loads(out.read_text(encoding="utf-8"))["features"]}
    shipped = shipped_geometries()
    for name in EMIRATES:
        assert rebuilt[name].equals(shipped[name]), name
        assert rebuilt[name].symmetric_difference(shipped[name]).area == 0.0
    assert not list(tmp_path.glob("*.tmp"))  # atomic write left nothing behind


def _wrong_shape_responses():
    """SYNTHETIC lookup bodies that are valid JSON but not the shape Nominatim returns."""
    def broken(mutate):
        response = real_shaped_response()
        mutate(response["features"][0])
        return response

    return {
        "list body": [],
        "null body": None,
        "string body": "x",
        "feature is a string": {"features": ["x"]},
        "feature is null": {"features": [None]},
        "properties is a string": broken(lambda f: f.update(properties="x")),
        "extratags is a list": broken(lambda f: f["properties"].update(extratags=["admin_level"])),
        "address is a string": broken(lambda f: f["properties"].update(address="AE-DU")),
        "ring of two points": broken(lambda f: f.update(geometry={"type": "Polygon", "coordinates": [[[55.0, 25.0], [55.1, 25.1]]]})),
        "coordinates are junk": broken(lambda f: f.update(geometry={"type": "Polygon", "coordinates": "junk"})),
        "ring that does not close": broken(lambda f: f.update(geometry={"type": "Polygon", "coordinates": [[[55.0, 25.0], [55.1, 25.0], [55.1, 25.1], [55.0, 25.2]]]})),
    }


@pytest.mark.parametrize("label", list(_wrong_shape_responses()))
def test_a_lookup_body_of_the_wrong_shape_is_a_boundary_error_and_a_clean_exit_1(tmp_path, label):
    # Regression: a list / string body or a bad ring escaped as AttributeError / GEOSException,
    # so main() raised a traceback instead of returning 1 as its contract says.
    body = _wrong_shape_responses()[label]
    with pytest.raises(fetcher.BoundaryFetchError):
        fetcher.build_from_response(body)
    raw_file = tmp_path / "raw.json"
    raw_file.write_text(json.dumps(body), encoding="utf-8")
    out = tmp_path / "never.geojson"
    assert fetcher.main(["--from-file", str(raw_file), "--out", str(out)]) == 1
    assert not out.exists() and not list(tmp_path.glob("*.tmp"))


def test_build_from_response_rechecks_the_proof_points_after_simplifying(monkeypatch):
    # Regression: the post-simplification check_geometry call was untested; deleting it passed everything.
    degraded = {
        "Dubai": MultiPolygon([box(55.0, 25.0, 55.02, 25.02)]),  # no longer contains the proof point (25.05, 55.05)
        "Sharjah": MultiPolygon([box(55.10, 25.0, 55.20, 25.10)]),
    }
    monkeypatch.setattr(fetcher, "choose_tolerance", lambda raw, specs, **kwargs: (0.001, degraded))
    with pytest.raises(fetcher.BoundaryFetchError, match="does not contain"):
        fetcher.build_from_response(syn_response(), SYN_SPECS)


def test_a_failed_write_exits_1_keeps_the_old_file_and_leaves_no_temp_file(tmp_path, monkeypatch):
    # Regression: write_atomic ran outside main()'s try, so a failed replace raised PermissionError and
    # stranded `<name>.tmp` next to the target.
    raw_file = tmp_path / "raw.json"
    raw_file.write_text(json.dumps(real_shaped_response()), encoding="utf-8")
    out = tmp_path / "polygons.geojson"
    out.write_text("PREVIOUS GOOD FILE", encoding="utf-8")

    def refuse(src, dst):
        raise PermissionError("target is locked")  # SYNTHETIC

    monkeypatch.setattr(fetcher.os, "replace", refuse)
    assert fetcher.main(["--from-file", str(raw_file), "--out", str(out)]) == 1
    assert out.read_text(encoding="utf-8") == "PREVIOUS GOOD FILE"  # the old file is untouched: the write is atomic
    assert not list(tmp_path.glob("*.tmp"))


def test_writing_over_a_directory_exits_1_with_nothing_left_behind(tmp_path):
    raw_file = tmp_path / "raw.json"
    raw_file.write_text(json.dumps(real_shaped_response()), encoding="utf-8")
    target = tmp_path / "polygons.geojson"
    target.mkdir()
    assert fetcher.main(["--from-file", str(raw_file), "--out", str(target)]) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["polygons.geojson", "raw.json"]


def test_a_failed_build_exits_non_zero_and_writes_nothing(tmp_path):
    response = real_shaped_response()
    response["features"][0]["properties"]["extratags"]["admin_level"] = "2"
    raw_file = tmp_path / "raw.json"
    raw_file.write_text(json.dumps(response), encoding="utf-8")
    out = tmp_path / "never.geojson"
    assert fetcher.main(["--from-file", str(raw_file), "--out", str(out)]) == 1
    assert not out.exists()


def test_main_exits_non_zero_when_the_saved_response_is_unreadable(tmp_path):
    assert fetcher.main(["--from-file", str(tmp_path / "absent.json"), "--out", str(tmp_path / "x.geojson")]) == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert fetcher.main(["--from-file", str(bad), "--out", str(tmp_path / "x.geojson")]) == 1


def test_write_atomic_replaces_the_target_and_cleans_up(tmp_path):
    target = tmp_path / "nested" / "file.geojson"
    fetcher.write_atomic(target, "one\n")
    fetcher.write_atomic(target, "two\n")
    assert target.read_text(encoding="utf-8") == "two\n"
    assert [p.name for p in target.parent.iterdir()] == ["file.geojson"]


# --- what the helper prints about a failure --------------------------------------------
# The polygon builder is a one-off helper, not a CI step, but its output can still be pasted
# into a public issue. Only its own checks (fixed phrases) may show text; the message of any
# other exception can quote the response or a path, so those show as the class name. MARKER
# stands in for third-party text inside such a message.
FAILURE_MARKER = "zq7poly"


class MarkerBodyResponse(FakeResponse):
    def json(self):
        raise ValueError(FAILURE_MARKER)


def test_a_non_json_lookup_error_shows_the_class_name_never_the_parsers_message():
    clock = FakeClock()
    session = FakeSession([MarkerBodyResponse(200)], clock)
    with pytest.raises(fetcher.BoundaryFetchError) as caught:
        fetcher.fetch_lookup(session=session, throttle=fetcher.Throttle(clock=clock, sleep=clock.sleep), sleep=clock.sleep)
    assert "not JSON" in str(caught.value) and "ValueError" in str(caught.value)
    assert FAILURE_MARKER not in str(caught.value)


@pytest.mark.parametrize("error", [KeyError, TypeError, ValueError, AttributeError, GEOSException])
def test_a_malformed_response_error_shows_the_class_name_never_the_exception_message(monkeypatch, error):
    def explode(feature, spec):
        raise error(FAILURE_MARKER)

    monkeypatch.setattr(fetcher, "verify_feature", explode)
    with pytest.raises(fetcher.BoundaryFetchError) as caught:
        fetcher.build_from_response(syn_response(), SYN_SPECS)
    assert "malformed lookup response" in str(caught.value) and error.__name__ in str(caught.value)
    assert FAILURE_MARKER not in str(caught.value)


def test_a_failed_coverage_simplification_shows_the_class_name_never_the_geos_message(monkeypatch):
    def explode(geometries, tolerance):
        raise GEOSException(FAILURE_MARKER)

    monkeypatch.setattr(shapely, "coverage_simplify", explode, raising=False)
    with pytest.raises(fetcher.BoundaryFetchError) as caught:
        fetcher.simplify_pair(jagged_pair(), 0.001, use_coverage=True)
    assert "coverage simplification failed" in str(caught.value) and "GEOSException" in str(caught.value)
    assert FAILURE_MARKER not in str(caught.value)


@pytest.mark.parametrize("error", [OSError, ValueError, PermissionError])
def test_main_logs_only_the_class_name_for_an_error_that_is_not_its_own(tmp_path, monkeypatch, caplog, error):
    raw_file = tmp_path / "raw.json"
    raw_file.write_text("{}", encoding="utf-8")

    def explode(response, **kwargs):
        raise error(FAILURE_MARKER)

    monkeypatch.setattr(fetcher, "build_from_response", explode)
    with caplog.at_level(logging.ERROR):
        assert fetcher.main(["--from-file", str(raw_file), "--out", str(tmp_path / "x.geojson")]) == 1
    assert "boundary build FAILED" in caplog.text and error.__name__ in caplog.text
    assert FAILURE_MARKER not in caplog.text


def test_main_keeps_the_text_of_its_own_error(tmp_path, monkeypatch, caplog):
    raw_file = tmp_path / "raw.json"
    raw_file.write_text("{}", encoding="utf-8")

    def explode(response, **kwargs):
        raise fetcher.BoundaryFetchError("Dubai polygon does not contain the proof point")

    monkeypatch.setattr(fetcher, "build_from_response", explode)
    with caplog.at_level(logging.ERROR):
        assert fetcher.main(["--from-file", str(raw_file), "--out", str(tmp_path / "x.geojson")]) == 1
    assert "does not contain the proof point" in caplog.text
