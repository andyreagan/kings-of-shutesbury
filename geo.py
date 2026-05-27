"""Is a segment in Shutesbury? Start OR finish must fall inside the town.

Uses the OSM/Nominatim town boundary polygon (fetched once and cached to
shutesbury_boundary.json, which is committed) and a ray-casting point-in-polygon
test. GeoJSON coordinates are [lon, lat].
"""

from __future__ import annotations

import json
import math
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


# --- sub-segment detection ----------------------------------------------------
# A "sub-segment" is a shorter segment that is a contiguous, SAME-DIRECTION slice
# of a longer one (e.g. "Cancer" is the middle of "Lake Wyola to Shutesbury").
# Direction matters: a segment running the OPPOSITE way along the same road (a
# climb vs its descent) is a distinct effort, not a nested slice.

def _to_local_m(lat: float, lng: float, lat0: float) -> tuple[float, float]:
    """Project lat/lng to local meters (equirectangular about lat0). Good enough
    for the ~km distances and tight tolerances used here."""
    return (lng * 111320.0 * math.cos(math.radians(lat0)), lat * 111320.0)


def _point_seg_dist(p, a, b) -> tuple[float, float]:
    """Distance from point p to segment a-b (all in meters), plus the projection
    fraction t in [0, 1] along a-b."""
    px, py = p; ax, ay = a; bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay), 0.0
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)), t


def track_containment(child_track, parent_track, tol_m: float = 25.0) -> dict | None:
    """How much of `child_track` runs along `parent_track`, and which way.

    Both are lists of [lat, lng]. Returns {coverage, direction, span} or None if
    either track is too short:
      - coverage  = fraction of child points within `tol_m` of the parent line
      - direction = 'same' / 'reverse' from whether the matched points advance
                    along the parent in child order (sign of the regression slope)
      - span      = fraction of the parent's length the matched points cover
    """
    if len(child_track) < 3 or len(parent_track) < 3:
        return None
    lat0 = parent_track[0][0]
    ch = [_to_local_m(la, lo, lat0) for la, lo in child_track]
    pa = [_to_local_m(la, lo, lat0) for la, lo in parent_track]
    cum = [0.0]
    for i in range(1, len(pa)):
        cum.append(cum[-1] + math.hypot(pa[i][0] - pa[i - 1][0], pa[i][1] - pa[i - 1][1]))
    total = cum[-1] or 1.0
    matched, pos = [], []
    for c in ch:
        best, bestpos = float("inf"), None
        for j in range(len(pa) - 1):
            d, t = _point_seg_dist(c, pa[j], pa[j + 1])
            if d < best:
                best = d
                bestpos = (cum[j] + t * (cum[j + 1] - cum[j])) / total
        matched.append(best <= tol_m)
        pos.append(bestpos)
    coverage = sum(matched) / len(matched)
    idx = [i for i, m in enumerate(matched) if m]
    if len(idx) < 3:
        return {"coverage": coverage, "direction": "same", "span": 0.0}
    xs = idx
    ys = [pos[i] for i in idx]
    n = len(xs); mx = sum(xs) / n; my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs) or 1.0
    slope = num / den
    matched_pos = [pos[i] for i in idx]
    return {"coverage": coverage,
            "direction": "same" if slope > 0 else "reverse",
            "span": max(matched_pos) - min(matched_pos)}


def find_sub_segment_parent(child, candidates, tol_m: float = 25.0,
                            min_coverage: float = 0.9) -> int | None:
    """Return the id of the longest segment `child` is a same-direction slice of,
    or None if it stands alone.

    `child` and each candidate are dicts with `id`, `length_m`, and `track` (a
    list of [lat, lng]). A candidate qualifies when it is meaningfully longer and
    the child runs >= `min_coverage` along it in the SAME direction. High
    coverage at this tight tolerance, plus matching direction, is what proves a
    real overlap: a segment merely sharing a corridor falls below the tolerance,
    and a reverse-direction effort (a climb vs. its descent) is excluded as a
    distinct effort. A winding parent or a loop (e.g. "Beetlemania climb" sitting
    on the "no-touch-challenge loop") still counts — the child genuinely rides
    its path."""
    child_len = child.get("length_m") or 0
    best_id, best_len = None, 0.0
    for cand in candidates:
        if cand["id"] == child["id"]:
            continue
        parent_len = cand.get("length_m") or 0
        if parent_len <= child_len * 1.05:
            continue
        r = track_containment(child.get("track") or [], cand.get("track") or [], tol_m)
        if not r or r["coverage"] < min_coverage or r["direction"] != "same":
            continue
        if parent_len > best_len:
            best_id, best_len = cand["id"], parent_len
    return best_id
