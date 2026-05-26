"""Is a segment in Shutesbury? Start OR finish must fall inside the town.

Uses the OSM/Nominatim town boundary polygon (fetched once and cached to
shutesbury_boundary.json, which is committed) and a ray-casting point-in-polygon
test. GeoJSON coordinates are [lon, lat].
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

BOUNDARY_PATH = Path(__file__).resolve().parent / "shutesbury_boundary.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
QUERY = "Shutesbury, Franklin County, Massachusetts, USA"


def fetch_boundary() -> dict:
    resp = httpx.get(
        NOMINATIM,
        params={"q": QUERY, "format": "json", "polygon_geojson": 1, "limit": 1},
        headers={"User-Agent": "kings-of-shutesbury/0.1 (personal project)"},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data or "geojson" not in data[0]:
        raise RuntimeError("Could not fetch a Shutesbury boundary polygon")
    payload = {"display_name": data[0]["display_name"], "geojson": data[0]["geojson"]}
    BOUNDARY_PATH.write_text(json.dumps(payload))
    return data[0]["geojson"]


def load_boundary() -> dict:
    if BOUNDARY_PATH.exists():
        return json.loads(BOUNDARY_PATH.read_text())["geojson"]
    return fetch_boundary()


def _in_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > y) != (y2 > y):
            xint = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xint:
                inside = not inside
    return inside


def _in_polygon(x: float, y: float, polygon: list) -> bool:
    # polygon = [outer_ring, hole1, hole2, ...]
    if not polygon or not _in_ring(x, y, polygon[0]):
        return False
    return not any(_in_ring(x, y, hole) for hole in polygon[1:])


def point_in_shutesbury(lat: float, lng: float, geo: dict | None = None) -> bool:
    geo = geo or load_boundary()
    x, y = lng, lat
    if geo["type"] == "Polygon":
        return _in_polygon(x, y, geo["coordinates"])
    if geo["type"] == "MultiPolygon":
        return any(_in_polygon(x, y, poly) for poly in geo["coordinates"])
    return False


def segment_in_shutesbury(seg: dict, geo: dict | None = None) -> bool:
    geo = geo or load_boundary()
    for lat, lng in ((seg.get("start_lat"), seg.get("start_lng")),
                     (seg.get("end_lat"), seg.get("end_lng"))):
        if lat is not None and lng is not None and point_in_shutesbury(lat, lng, geo):
            return True
    return False


def classify_segment(start_latlng, end_latlng, track, geo: dict | None = None) -> dict:
    """Classify a segment's relationship to Shutesbury from its endpoints and
    full track (a list of [lat, lng] points). Returns starts_in / ends_in /
    passes_through, plus in_shutesbury = starts_in OR ends_in (the inclusion
    rule). A segment that only crosses town without starting or finishing there
    is `passes_through` but NOT `in_shutesbury`."""
    geo = geo or load_boundary()

    def pin(p) -> bool:
        return (p is not None and p[0] is not None and p[1] is not None
                and point_in_shutesbury(p[0], p[1], geo))

    starts = pin(start_latlng)
    ends = pin(end_latlng)
    passes = starts or ends or any(
        pt and pt[0] is not None and pt[1] is not None
        and point_in_shutesbury(pt[0], pt[1], geo) for pt in (track or []))
    return {"starts_in": starts, "ends_in": ends,
            "passes_through": passes, "in_shutesbury": starts or ends}
