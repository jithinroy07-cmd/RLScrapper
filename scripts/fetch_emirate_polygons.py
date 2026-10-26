"""
Builds src/data/emirate_polygons.geojson: the Dubai and Sharjah emirate boundaries.

WHY real polygons: the Dubai and Sharjah bounding boxes overlap by about 85
percent and 15 of 16 border test points sat inside BOTH boxes, so a box can never
say which emirate a coordinate is in (docs/DESIGN.md section 5). The geocoder uses
the polygons in src/emirates.py to reject hits that fall outside the declared
emirate and to flag pins near the shared border.

Source: OpenStreetMap administrative boundaries (ODbL 1.0, attribution
"(c) OpenStreetMap contributors"), fetched from the public Nominatim lookup API:

    Dubai Emirate    relation 3766483   admin_level 4   ISO 3166-2 AE-DU
    Sharjah Emirate  relation 3766486   admin_level 4   ISO 3166-2 AE-SH

Both ids were checked against the live data on 25 Oct 2026 (name, admin_level,
ISO code). Each relation is a multipolygon that includes the exclaves: Dubai's
Hatta, Sharjah's Kalba / Khor Fakkan / Dibba Al-Hisn pieces. An Overpass
`out geom` export of the same relations was compared with the Nominatim geometry
and was identical (symmetric difference 0), so Nominatim is a faithful, simpler
source and no second backend is implemented.

The two polygons share their land border vertex for vertex (zero overlap area), so
the file is a topologically clean pair. That has two consequences this script
respects: simplification must treat them as a coverage (shapely.coverage_simplify)
or independent simplification can open a sliver along the border; and a point
exactly on the border is inside neither polygon (see src/emirates.py).

Politeness (Nominatim usage policy): identifying User-Agent, at most one request
per second, ONE lookup request for both relations in the normal case, retries with
backoff on 429 / 502 / 503 / 504 and on connection errors, Retry-After honoured.

Fail loud: a wrong relation, a wrong name or admin_level, a non-polygon geometry, a
geometry missing its exclaves, or a pair that overlaps raises BoundaryFetchError and
exits non-zero. Nothing is written in that case.

Usage:
    python scripts/fetch_emirate_polygons.py                      # fetch + write
    python scripts/fetch_emirate_polygons.py --from-file raw.json # offline rebuild
    python scripts/fetch_emirate_polygons.py --save-raw raw.json  # keep the response
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import requests
import shapely
from shapely.errors import GEOSException
from shapely.geometry import MultiPolygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.validation import make_valid

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.emirates import EMIRATES, UNION_BBOX  # noqa: E402

logger = logging.getLogger("fetch_emirate_polygons")

DEFAULT_OUT = ROOT_DIR / "src" / "data" / "emirate_polygons.geojson"

USER_AGENT = "uae-real-estate-scraper/1.0 (boundary build)"
LOOKUP_URL = "https://nominatim.openstreetmap.org/lookup"
MIN_INTERVAL_SECONDS = 1.1  # the policy allows 1 request/second; stay just under it
REQUEST_TIMEOUT = (10, 120)  # connect, read: the geometry response is tens of KB, but the server is shared
RETRY_STATUSES = frozenset({429, 502, 503, 504})
BACKOFF_SECONDS = (5.0, 15.0, 45.0, 120.0)  # waits before retry 1..4 (5 attempts in all)
MAX_RETRY_AFTER_SECONDS = 300.0

OSM_LICENSE = "ODbL 1.0"
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
BOUNDARY_ADMIN_LEVEL = "4"  # OSM admin_level for a UAE emirate

MAX_FILE_BYTES = 1_500_000
# Candidate Douglas-Peucker tolerances in degrees, smallest first (1e-5 deg is about 1.1 m).
# The first one whose output fits the size budget wins; 0.0 means "do not simplify".
TOLERANCE_LADDER_DEG = (0.0, 1e-5, 2.5e-5, 5e-5, 1e-4, 2.5e-4, 5e-4, 1e-3)
METRES_PER_DEGREE = 111_320.0  # upper bound: a longitude degree is shorter at 25 N
SANITY_BBOX_PAD_DEG = 0.05  # the polygons may poke slightly past UNION_BBOX offshore
MAX_OVERLAP_DEG2 = 1e-4  # about 1.2 km2; real corruption, not an edit-war sliver
WARN_OVERLAP_DEG2 = 1e-9


class BoundaryFetchError(RuntimeError):
    """Any failure to obtain a trustworthy boundary. Never swallowed, never papered over."""


@dataclass(frozen=True)
class EmirateSpec:
    """What a relation must be to be trusted, plus points that prove its exclaves are present."""

    emirate: str
    relation: int
    osm_name: str  # Nominatim `name` with accept-language=en
    iso_code: str  # ISO 3166-2, from address.ISO3166-2-lvl4
    inside: tuple[tuple[float, float], ...]  # (lat, lon) strictly inside the polygon
    outside: tuple[tuple[float, float], ...]  # (lat, lon) strictly outside it


_HATTA = (24.80, 56.14)  # Dubai's mountain exclave
_KALBA = (25.0486, 56.35)  # Sharjah's east-coast exclave
_DUBAI_MARINA = (25.08, 55.14)
_BURJ_KHALIFA = (25.19704, 55.27421)
_ALJADA = (25.3183, 55.4727)
_HAMRIYAH = (25.4565, 55.5134)

SPECS: tuple[EmirateSpec, ...] = (
    EmirateSpec(
        emirate="Dubai",
        relation=3766483,
        osm_name="Dubai Emirate",
        iso_code="AE-DU",
        inside=(_HATTA, _DUBAI_MARINA, _BURJ_KHALIFA),
        outside=(_ALJADA, _KALBA, _HAMRIYAH),
    ),
    EmirateSpec(
        emirate="Sharjah",
        relation=3766486,
        osm_name="Sharjah Emirate",
        iso_code="AE-SH",
        inside=(_KALBA, _ALJADA, _HAMRIYAH),
        outside=(_HATTA, _DUBAI_MARINA, _BURJ_KHALIFA),
    ),
)


# ---------------------------------------------------------------------------
# Polite HTTP
# ---------------------------------------------------------------------------
class Throttle:
    """Enforces a minimum gap between request starts. Clock and sleep are injectable for tests."""

    def __init__(
        self,
        min_interval: float = MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self.requests_made = 0

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.min_interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()
        self.requests_made += 1


def _retry_after_seconds(response: requests.Response | None) -> float | None:
    """The Retry-After header as seconds (capped), or None if absent / not a plain number."""
    if response is None:
        return None
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None  # an HTTP-date form: fall back to our own backoff
    if math.isnan(seconds) or seconds < 0:  # float("nan") parses, and time.sleep(nan) raises
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def get_with_backoff(
    session: requests.Session,
    url: str,
    params: Mapping[str, Any],
    *,
    throttle: Throttle,
    sleep: Callable[[float], None] = time.sleep,
    backoff: Sequence[float] = BACKOFF_SECONDS,
    timeout: tuple[float, float] = REQUEST_TIMEOUT,
) -> requests.Response:
    """GET with throttling and backoff. 200 returns; transient failures retry; anything else raises.

    Transient = 429 / 502 / 503 / 504 or a connection error / timeout. A 403 or 404 is not
    retried: it means we are blocked or the URL is wrong, and hammering would make it worse.
    """
    last_problem = "no attempt made"
    for attempt in range(len(backoff) + 1):
        throttle.wait()
        response: requests.Response | None = None
        try:
            response = session.get(url, params=dict(params), timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_problem = f"{type(exc).__name__} talking to {url}"
        else:
            if response.status_code == 200:
                return response
            if response.status_code not in RETRY_STATUSES:
                raise BoundaryFetchError(f"HTTP {response.status_code} from {url}; not retrying")
            last_problem = f"HTTP {response.status_code} from {url}"
        if attempt == len(backoff):
            break
        delay = _retry_after_seconds(response)
        if delay is None:
            delay = backoff[attempt]
        logger.warning("%s; retry %d of %d in %.0fs", last_problem, attempt + 1, len(backoff), delay)
        sleep(delay)
    raise BoundaryFetchError(f"gave up after {len(backoff) + 1} attempts: {last_problem}")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def lookup_params(specs: Sequence[EmirateSpec]) -> dict[str, Any]:
    """Nominatim /lookup parameters: full geometry (polygon_threshold=0), English names, admin tags."""
    return {
        "osm_ids": ",".join(f"R{spec.relation}" for spec in specs),
        "format": "geojson",
        "polygon_geojson": 1,
        "polygon_threshold": 0,  # explicit: ask the server NOT to simplify
        "extratags": 1,  # carries admin_level
        "addressdetails": 1,  # carries ISO3166-2-lvl4
        "accept-language": "en",
    }


def fetch_lookup(
    specs: Sequence[EmirateSpec] = SPECS,
    *,
    session: requests.Session | None = None,
    throttle: Throttle | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """One lookup request for every relation; returns the parsed GeoJSON response."""
    session = session or make_session()
    throttle = throttle or Throttle(sleep=sleep)
    response = get_with_backoff(session, LOOKUP_URL, lookup_params(specs), throttle=throttle, sleep=sleep)
    logger.info("Nominatim lookup answered (%d bytes) after %d request(s)", len(response.content), throttle.requests_made)
    try:
        return response.json()
    except ValueError as exc:
        # The class name only: a parser's message can quote the body it choked on.
        raise BoundaryFetchError(f"lookup response is not JSON ({type(exc).__name__})") from exc


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def features_by_relation(response: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    """Index the response's features by OSM relation id (only relations are kept)."""
    features = response.get("features")
    if not isinstance(features, list):
        raise BoundaryFetchError("lookup response has no `features` list")
    found: dict[int, dict[str, Any]] = {}
    for feature in features:
        props = feature.get("properties") or {}
        if props.get("osm_type") == "relation":
            try:
                found[int(props["osm_id"])] = feature
            except (KeyError, TypeError, ValueError):
                continue
    return found


def verify_feature(feature: Mapping[str, Any], spec: EmirateSpec) -> None:
    """Raise BoundaryFetchError unless the feature really is the relation `spec` claims.

    Checked: relation id, boundary=administrative, admin_level 4, English name, and the
    ISO 3166-2 code when the response carries it. An id that was ever wrong would
    otherwise ship another emirate's (or another country's) outline under this name.
    """
    props = feature.get("properties") or {}
    problems: list[str] = []
    if props.get("osm_type") != "relation" or str(props.get("osm_id")) != str(spec.relation):
        problems.append(f"expected relation {spec.relation}, got {props.get('osm_type')} {props.get('osm_id')}")
    if props.get("category") != "boundary" or props.get("type") != "administrative":
        problems.append(f"expected boundary/administrative, got {props.get('category')}/{props.get('type')}")
    admin_level = str((props.get("extratags") or {}).get("admin_level"))
    if admin_level != BOUNDARY_ADMIN_LEVEL:
        problems.append(f"expected admin_level {BOUNDARY_ADMIN_LEVEL}, got {admin_level}")
    if str(props.get("name", "")).strip().lower() != spec.osm_name.lower():
        problems.append(f"expected name {spec.osm_name!r}, got {props.get('name')!r}")
    iso = (props.get("address") or {}).get("ISO3166-2-lvl4")
    if iso is not None and iso != spec.iso_code:
        problems.append(f"expected ISO 3166-2 {spec.iso_code}, got {iso}")
    geometry = feature.get("geometry") or {}
    if geometry.get("type") not in ("Polygon", "MultiPolygon"):
        problems.append(f"geometry is {geometry.get('type')}, not a polygon")
    if problems:
        raise BoundaryFetchError(f"relation {spec.relation} ({spec.emirate}) failed verification: " + "; ".join(problems))


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def as_multipolygon(geometry: BaseGeometry) -> MultiPolygon:
    """Polygonal parts of `geometry` as a MultiPolygon, largest part first, rings oriented CCW.

    Orientation follows RFC 7946 (exterior counter-clockwise). It only reorders vertices,
    it never moves one, so the boundary is exactly what OSM holds.
    """
    if geometry.geom_type == "Polygon":
        parts = [geometry]
    elif geometry.geom_type in ("MultiPolygon", "GeometryCollection"):
        parts = []
        for part in geometry.geoms:
            if part.geom_type == "Polygon":
                parts.append(part)
            elif part.geom_type == "MultiPolygon":
                parts.extend(part.geoms)
    else:
        raise BoundaryFetchError(f"geometry is {geometry.geom_type}, not polygonal")
    parts = [orient(p, sign=1.0) for p in parts if not p.is_empty and p.area > 0]
    if not parts:
        raise BoundaryFetchError("geometry has no polygonal area")
    parts.sort(key=lambda p: p.area, reverse=True)
    return MultiPolygon(parts)


def geometry_from_feature(feature: Mapping[str, Any]) -> MultiPolygon:
    """The feature's geometry as a valid MultiPolygon (repairing with make_valid if needed)."""
    geometry = shape(feature["geometry"])
    if not geometry.is_valid:
        logger.warning("source geometry invalid (%s); repairing with make_valid", shapely.is_valid_reason(geometry))
        geometry = make_valid(geometry)
    result = as_multipolygon(geometry)
    if not result.is_valid:
        raise BoundaryFetchError("geometry is still invalid after repair")
    return result


def vertex_count(geometry: BaseGeometry) -> int:
    return int(shapely.get_num_coordinates(geometry))


def _contains(geometry: BaseGeometry, lat: float, lon: float) -> bool:
    return bool(shapely.contains_xy(geometry, lon, lat))


def check_geometry(geometry: BaseGeometry, spec: EmirateSpec) -> None:
    """Sanity-check one emirate's outline: inside the UAE window, exclaves present, no stray points."""
    min_lat, max_lat, min_lon, max_lon = UNION_BBOX
    pad = SANITY_BBOX_PAD_DEG
    west, south, east, north = geometry.bounds
    if west < min_lon - pad or east > max_lon + pad or south < min_lat - pad or north > max_lat + pad:
        raise BoundaryFetchError(f"{spec.emirate} bounds {geometry.bounds} fall outside the UAE window {UNION_BBOX}")
    for lat, lon in spec.inside:
        if not _contains(geometry, lat, lon):
            raise BoundaryFetchError(f"{spec.emirate} polygon does not contain ({lat}, {lon}); exclave missing or wrong relation")
    for lat, lon in spec.outside:
        if _contains(geometry, lat, lon):
            raise BoundaryFetchError(f"{spec.emirate} polygon wrongly contains ({lat}, {lon})")


def overlap_area(first: BaseGeometry, second: BaseGeometry) -> float:
    """Area (square degrees) covered by both geometries. Shared edges have zero area."""
    return float(first.intersection(second).area)


def check_pair(geoms: Mapping[str, BaseGeometry]) -> float:
    """Return the overlap between the emirates; raise if it is large, warn if it is not zero."""
    overlap = overlap_area(geoms[EMIRATES[0]], geoms[EMIRATES[1]])
    if overlap > MAX_OVERLAP_DEG2:
        raise BoundaryFetchError(f"Dubai and Sharjah overlap by {overlap:.6f} deg2; the source is corrupt")
    if overlap > WARN_OVERLAP_DEG2:
        logger.warning("Dubai and Sharjah overlap by %.9f deg2 (points in the sliver classify as neither)", overlap)
    return overlap


def simplify_pair(
    geoms: Mapping[str, BaseGeometry],
    tolerance_deg: float,
    *,
    use_coverage: bool | None = None,
) -> dict[str, MultiPolygon]:
    """Douglas-Peucker both emirates at `tolerance_deg` without opening a gap or overlap on the border.

    tolerance 0 returns the geometries untouched. Otherwise the pair is simplified as a
    coverage (shapely.coverage_simplify, shapely 2.1+), which simplifies each shared edge
    once so the border stays identical for both. On older shapely the fallback is
    `simplify(preserve_topology=True)` per emirate, followed by a check that no overlap
    appeared; if one did, we raise rather than ship a sliver.
    """
    if tolerance_deg < 0:
        raise ValueError("tolerance must be >= 0")
    if tolerance_deg == 0:
        return {name: as_multipolygon(geom) for name, geom in geoms.items()}
    names = list(geoms)
    coverage = getattr(shapely, "coverage_simplify", None)
    if use_coverage is None:
        use_coverage = coverage is not None
    if use_coverage and coverage is None:
        raise BoundaryFetchError("shapely.coverage_simplify needs shapely 2.1 or newer")
    if use_coverage:
        try:
            simplified = list(coverage([geoms[name] for name in names], tolerance_deg))
        except GEOSException as exc:  # invalid or overlapping input
            # The class name only: GEOS messages quote coordinates from the input.
            raise BoundaryFetchError(f"coverage simplification failed ({type(exc).__name__})") from exc
    else:
        simplified = [geoms[name].simplify(tolerance_deg, preserve_topology=True) for name in names]
    result = {name: as_multipolygon(geom) for name, geom in zip(names, simplified)}
    for name, geom in result.items():
        if not geom.is_valid:
            raise BoundaryFetchError(f"{name} became invalid after simplification at {tolerance_deg}")
    if not use_coverage:
        before = overlap_area(*(geoms[name] for name in names))
        after = overlap_area(*(result[name] for name in names))
        if after > before + WARN_OVERLAP_DEG2:
            raise BoundaryFetchError(
                f"independent simplification at {tolerance_deg} deg opened an overlap of {after:.9f} deg2 "
                "on the shared border; use shapely>=2.1 (coverage_simplify) or a smaller tolerance"
            )
    return result


def max_deviation_m(raw: BaseGeometry, simplified: BaseGeometry) -> float:
    """Upper bound, in metres, on how far simplification moved the outline (Hausdorff distance)."""
    return float(raw.hausdorff_distance(simplified)) * METRES_PER_DEGREE


# ---------------------------------------------------------------------------
# GeoJSON assembly
# ---------------------------------------------------------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_collection(
    simplified: Mapping[str, BaseGeometry],
    raw: Mapping[str, BaseGeometry],
    specs: Sequence[EmirateSpec],
    *,
    fetched_at: str,
    tolerance_deg: float,
    feature_names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The 2-feature FeatureCollection that src/emirates.py loads."""
    features = []
    for spec in specs:
        geom = simplified[spec.emirate]
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "emirate": spec.emirate,
                    "osm_relation": spec.relation,
                    "osm_license": OSM_LICENSE,
                    "attribution": OSM_ATTRIBUTION,
                    "fetched_at": fetched_at,
                    # Provenance extras: what the source called it and how much it was reduced.
                    "osm_name": (feature_names or {}).get(spec.emirate, spec.osm_name),
                    "admin_level": int(BOUNDARY_ADMIN_LEVEL),
                    "source": "nominatim lookup polygon_geojson=1",
                    "simplify_tolerance_deg": tolerance_deg,
                    "raw_vertices": vertex_count(raw[spec.emirate]),
                    "vertices": vertex_count(geom),
                    "max_deviation_m": round(max_deviation_m(raw[spec.emirate], geom), 3),
                },
                "geometry": mapping(geom),
            }
        )
    return {"type": "FeatureCollection", "features": features}


def dumps(collection: Mapping[str, Any]) -> str:
    """Compact, ASCII-only, newline-terminated JSON (so the file is byte-stable for a given input)."""
    return json.dumps(collection, separators=(",", ":")) + "\n"


def choose_tolerance(
    raw: Mapping[str, BaseGeometry],
    specs: Sequence[EmirateSpec],
    *,
    budget_bytes: int = MAX_FILE_BYTES,
    ladder: Sequence[float] = TOLERANCE_LADDER_DEG,
    fetched_at: str = "1970-01-01T00:00:00Z",
) -> tuple[float, dict[str, MultiPolygon]]:
    """The smallest tolerance on the ladder whose serialised file fits `budget_bytes`.

    Smallest, not largest: every vertex removed is a vertex of the real border we no longer
    honour. For the 25 Oct 2026 OSM data the raw outline is about 32 KB, so this returns 0.
    """
    for tolerance in ladder:
        candidate = simplify_pair(raw, tolerance)
        size = len(dumps(build_collection(candidate, raw, specs, fetched_at=fetched_at, tolerance_deg=tolerance)).encode("ascii"))
        logger.info("tolerance %g deg -> %d bytes (budget %d)", tolerance, size, budget_bytes)
        if size <= budget_bytes:
            return tolerance, candidate
    raise BoundaryFetchError(f"no tolerance up to {ladder[-1]} deg fits the {budget_bytes} byte budget")


def build_from_response(
    response: Mapping[str, Any],
    specs: Sequence[EmirateSpec] = SPECS,
    *,
    fetched_at: str | None = None,
    budget_bytes: int = MAX_FILE_BYTES,
) -> dict[str, Any]:
    """Verify, check, simplify and package a Nominatim lookup response. Pure: no network, no disk."""
    fetched_at = fetched_at or utc_now_iso()
    raw: dict[str, MultiPolygon] = {}
    names: dict[str, str] = {}
    try:
        indexed = features_by_relation(response)
        for spec in specs:
            feature = indexed.get(spec.relation)
            if feature is None:
                raise BoundaryFetchError(f"relation {spec.relation} ({spec.emirate}) missing from the response")
            verify_feature(feature, spec)
            raw[spec.emirate] = geometry_from_feature(feature)
            names[spec.emirate] = str(feature["properties"].get("name", spec.osm_name))
            check_geometry(raw[spec.emirate], spec)
    except BoundaryFetchError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, GEOSException) as exc:
        # A JSON body of the wrong shape (a list, a string, a ring with two points) is a failed
        # fetch like any other: the caller must see BoundaryFetchError, never a bare AttributeError.
        # The class name only: a KeyError or GEOS message quotes the response it choked on.
        raise BoundaryFetchError(f"malformed lookup response ({type(exc).__name__})") from exc
    check_pair(raw)
    tolerance, simplified = choose_tolerance(raw, specs, budget_bytes=budget_bytes, fetched_at=fetched_at)
    for spec in specs:  # simplification must not flip a proof point either way
        check_geometry(simplified[spec.emirate], spec)
    check_pair(simplified)
    return build_collection(simplified, raw, specs, fetched_at=fetched_at, tolerance_deg=tolerance, feature_names=names)


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file so a crash never leaves a half-written boundary file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except BaseException:
        try:  # a failed replace (target is a directory, file locked) must not strand the temp file
            tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch OSM Dubai + Sharjah emirate polygons into src/data/.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output GeoJSON (default: src/data/emirate_polygons.geojson)")
    parser.add_argument("--from-file", type=Path, help="rebuild from a saved lookup response instead of calling the network")
    parser.add_argument("--save-raw", type=Path, help="also save the raw lookup response here (kept out of the repo by default)")
    parser.add_argument("--budget-bytes", type=int, default=MAX_FILE_BYTES, help="size budget that drives simplification")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler(sys.stdout)])

    try:
        if args.from_file:
            response = json.loads(args.from_file.read_text(encoding="utf-8"))
        else:
            response = fetch_lookup()
            if args.save_raw:
                write_atomic(args.save_raw, json.dumps(response))
        collection = build_from_response(response, budget_bytes=args.budget_bytes)
        text = dumps(collection)
        write_atomic(args.out, text)
    except (BoundaryFetchError, OSError, ValueError) as exc:
        # Only this script's own error keeps its text (its messages are built from fixed phrases and
        # the expected-versus-found checks above); an OSError or ValueError can carry a path or a
        # quoted fragment of the input, so those show as the class name.
        logger.error("boundary build FAILED: %s", exc if isinstance(exc, BoundaryFetchError) else type(exc).__name__)
        return 1

    for feature in collection["features"]:
        p = feature["properties"]
        logger.info(
            "%s: relation %s, %d vertices (raw %d), tolerance %g deg, max deviation %.1f m",
            p["emirate"], p["osm_relation"], p["vertices"], p["raw_vertices"], p["simplify_tolerance_deg"], p["max_deviation_m"],
        )
    logger.info("wrote %s (%d bytes)", args.out, len(text.encode("ascii")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
