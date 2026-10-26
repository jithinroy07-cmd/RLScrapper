"""
Which emirate is a coordinate in? Answered with real boundaries, never with boxes.

WHY polygons: the Dubai and Sharjah bounding boxes overlap by about 85 percent and
15 of 16 border test points sat inside BOTH, so no rectangle can separate the two
(docs/DESIGN.md section 5). The geocoder uses this module to reject a hit that lands
outside the emirate the registry declared, and to flag pins close to the shared
border. The emirate of a project still comes from its registry, never from here;
this module only validates coordinates against it.

Data: src/data/emirate_polygons.geojson, built by scripts/fetch_emirate_polygons.py
from the OpenStreetMap administrative boundaries of Dubai (relation 3766483) and
Sharjah (relation 3766486), including their exclaves (Dubai's Hatta; Sharjah's Kalba,
Khor Fakkan and Dibba Al-Hisn pieces). The boundaries are (c) OpenStreetMap
contributors, ODbL 1.0; any public display must carry that attribution.

Tie rule (documented because a border is exactly where points land on it):
  * Containment is STRICT. A point exactly on a polygon's edge or vertex, including a
    vertex shared by both emirates along the common border, is inside neither.
  * `emirate_for_point` answers only when exactly one polygon strictly contains the
    point. A point strictly inside both (an overlap sliver, should OSM ever draw one) or
    inside neither gets None. We never pick a winner: blank beats invented.
  * The OSM pair shares its land border vertex for vertex, so today the only ambiguous
    points are those exactly on the border. They are also reported as a border zone.

Bad input never raises. Non-numbers, booleans, NaN, infinities and out-of-range
coordinates answer False / None / NaN. A missing or corrupt data file DOES raise,
because that is an installation fault and must not look like "not in any emirate".

Distance method: the nearest point on a polygon is found in a plane where longitude is
scaled by cos(25.16 N) (the centre of UNION_BBOX), which makes degrees nearly isotropic
across the whole UAE (the scale error is under 0.5 percent at the box edges), and the
distance reported is then the exact haversine between the query point and that nearest
boundary point. So the point chosen is within about 0.5 percent of optimal and the
distance is spherical; for the 1.5 km border check that is a few metres.
"""

from __future__ import annotations

import json
import logging
import math
import numbers
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import shapely
from shapely import affinity
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points

logger = logging.getLogger(__name__)

__all__ = [
    "EMIRATES",
    "UNION_BBOX",
    "in_union_bbox",
    "point_in_emirate",
    "emirate_for_point",
    "distance_to_border_km",
    "is_border_zone",
]

EMIRATES = ("Dubai", "Sharjah")

# (min_lat, max_lat, min_lon, max_lon). A COARSE sanity gate that discards obviously
# foreign geocoder hits (other continents, Abu Dhabi city). It must never decide an
# emirate. It is rounded, so it clips the two outermost offshore tips of the Sharjah
# polygon by about 250 m; nothing is built there.
UNION_BBOX = (24.62, 25.70, 53.98, 56.60)

_DATA_PATH = Path(__file__).resolve().parent / "data" / "emirate_polygons.geojson"
_EARTH_RADIUS_KM = 6371.0088
_LON_SCALE = math.cos(math.radians((UNION_BBOX[0] + UNION_BBOX[1]) / 2))
_DEFAULT_BORDER_THRESHOLD_KM = 1.5

_warned_emirates: set[str] = set()


@dataclass(frozen=True)
class _Boundary:
    emirate: str
    geometry: BaseGeometry  # lon/lat, prepared once for fast containment
    scaled: BaseGeometry  # lon scaled by cos(reference latitude), for nearest-point search


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _load_boundaries(path: Path) -> dict[str, _Boundary]:
    """Read and validate the polygon file. Raises on anything short of two clean polygons."""
    try:
        collection = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"{path} is missing; build it with `python scripts/fetch_emirate_polygons.py`"
        ) from exc
    features = collection.get("features") if isinstance(collection, dict) else None
    if not isinstance(features, list):
        raise ValueError(f"{path} is not a GeoJSON FeatureCollection")
    found: dict[str, _Boundary] = {}
    for feature in features:
        emirate = (feature.get("properties") or {}).get("emirate")
        if emirate not in EMIRATES:
            raise ValueError(f"{path} has a feature for unexpected emirate {emirate!r}")
        if emirate in found:
            raise ValueError(f"{path} has two features for {emirate}")
        geometry = shape(feature["geometry"])
        if geometry.geom_type not in ("Polygon", "MultiPolygon") or geometry.is_empty or not geometry.is_valid:
            raise ValueError(f"{path}: the {emirate} geometry is not a valid, non-empty polygon")
        scaled = affinity.scale(geometry, xfact=_LON_SCALE, yfact=1.0, origin=(0, 0))
        shapely.prepare(geometry)
        found[emirate] = _Boundary(emirate=emirate, geometry=geometry, scaled=scaled)
    missing = [name for name in EMIRATES if name not in found]
    if missing:
        raise ValueError(f"{path} has no polygon for {', '.join(missing)}")
    return found


@lru_cache(maxsize=1)
def _boundaries() -> dict[str, _Boundary]:
    """The two emirate boundaries, loaded once per process."""
    return _load_boundaries(_DATA_PATH)


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------
def _clean_coordinates(lat: Any, lon: Any) -> tuple[float, float] | None:
    """(lat, lon) as finite in-range floats, or None. Booleans and strings are rejected on purpose."""
    for value in (lat, lon):
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            return None
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(la) and math.isfinite(lo)):
        return None
    if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
        return None
    return la, lo


def _canonical_emirate(name: Any) -> str | None:
    """'Dubai' / 'Sharjah' from a case- and whitespace-tolerant spelling, else None (warned once)."""
    if isinstance(name, str):
        key = name.strip().casefold()
        for emirate in EMIRATES:
            if emirate.casefold() == key:
                return emirate
    label = repr(name)  # only the once-per-value key: the value may be a sheet cell, so it is never logged
    if label not in _warned_emirates:
        _warned_emirates.add(label)
        logger.warning("unknown emirate (type %s); only %s are supported", type(name).__name__, ", ".join(EMIRATES))
    return None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((phi2 - phi1) / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _strictly_inside(emirate: str, lat: float, lon: float) -> bool:
    return bool(shapely.contains_xy(_boundaries()[emirate].geometry, lon, lat))


def _distance_to_polygon_km(emirate: str, lat: float, lon: float) -> float:
    """Kilometres from a (clean) point to `emirate`'s polygon; 0.0 when inside or on its edge."""
    boundary = _boundaries()[emirate]
    if shapely.intersects_xy(boundary.geometry, lon, lat):
        return 0.0
    _, nearest = nearest_points(Point(lon * _LON_SCALE, lat), boundary.scaled)
    return _haversine_km(lat, lon, nearest.y, nearest.x / _LON_SCALE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def in_union_bbox(lat: Any, lon: Any) -> bool:
    """True if the point is inside UNION_BBOX (inclusive). A coarse gate, never an emirate test."""
    clean = _clean_coordinates(lat, lon)
    if clean is None:
        return False
    la, lo = clean
    min_lat, max_lat, min_lon, max_lon = UNION_BBOX
    return min_lat <= la <= max_lat and min_lon <= lo <= max_lon


def point_in_emirate(lat: Any, lon: Any, emirate: str) -> bool:
    """True if the point is strictly inside that emirate's polygon (edge points are not).

    False for invalid coordinates and for an unsupported emirate name (logged once).
    """
    clean = _clean_coordinates(lat, lon)
    name = _canonical_emirate(emirate)
    if clean is None or name is None:
        return False
    return _strictly_inside(name, *clean)


def emirate_for_point(lat: Any, lon: Any) -> str | None:
    """'Dubai' or 'Sharjah' when exactly one polygon strictly contains the point, else None.

    None covers: other emirates, the open Gulf, other countries, invalid input, and the
    ambiguous cases (exactly on the shared border, or inside both polygons). See the tie
    rule in the module docstring.
    """
    clean = _clean_coordinates(lat, lon)
    if clean is None:
        return None
    hits = [name for name in EMIRATES if _strictly_inside(name, *clean)]
    return hits[0] if len(hits) == 1 else None


def distance_to_border_km(lat: Any, lon: Any, emirate: str) -> float:
    """Kilometres from the point to the OTHER emirate's polygon, given the point's declared emirate.

    `emirate` is the emirate the project is declared to be in; the result is how far the
    point is from the neighbour, so a small value means "close to the border". 0.0 means
    the point is inside or on the edge of the other emirate (a geocode that landed on the
    wrong side). NaN for invalid coordinates or an unsupported emirate. See the module
    docstring for the distance method.
    """
    clean = _clean_coordinates(lat, lon)
    name = _canonical_emirate(emirate)
    if clean is None or name is None:
        return math.nan
    other = EMIRATES[1 - EMIRATES.index(name)]
    return _distance_to_polygon_km(other, *clean)


def is_border_zone(lat: Any, lon: Any, threshold_km: float = _DEFAULT_BORDER_THRESHOLD_KM) -> bool:
    """True if the point is within `threshold_km` of BOTH polygons (inside one counts as 0 km).

    That is exactly "close to the Dubai/Sharjah border": a point inside Dubai is within
    range of Sharjah, a point inside Sharjah within range of Dubai, and a point in neither
    (the water, or a sliver) must be near both. A point deep inside one emirate, or near
    only a different neighbour such as Ajman, is not. Invalid coordinates, or a threshold
    that is not a finite number >= 0, give False. The comparison is inclusive.
    """
    clean = _clean_coordinates(lat, lon)
    if clean is None:
        return False
    if isinstance(threshold_km, bool) or not isinstance(threshold_km, numbers.Real):
        return False
    threshold = float(threshold_km)
    if not math.isfinite(threshold) or threshold < 0:
        return False
    return all(_distance_to_polygon_km(name, *clean) <= threshold for name in EMIRATES)
