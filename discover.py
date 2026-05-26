#!/usr/bin/env python3
"""Discover candidate segments from a Strava map-page HAR capture.

Strava's segments map (the "personal heatmap → segments" view) draws its
segments from Mapbox Vector Tiles at `cdn-1.strava.com/tiles/segments/<athlete>/
<z>/<x>/<y>` (mimeType application/vnd.mapbox-vector-tile). Each tile feature
carries the segment id, name, total efforts/athletes, and the KOM/QOM holder.
With `intent=popular` those tiles list every popular Ride segment in view — so a
single HAR capture is a zero-leaderboard-request way to enumerate the segments in
an area and find which ones we aren't tracking yet.

This decodes the tiles straight out of the HAR (no network), converts the tile
geometry to lat/lon, keeps segments whose track crosses the Shutesbury boundary,
and diffs them against the DB.

Usage:
    uv run discover.py                       # default HAR, list in-town candidates
    uv run discover.py path/to/capture.har   # a different capture
    uv run discover.py --all                 # include already-tracked segments
    uv run discover.py --add                 # register the in-town untracked ones
"""

from __future__ import annotations

import argparse
import base64
import json
import math
from pathlib import Path

import db
import geo

HERE = Path(__file__).resolve().parent
DEFAULT_HAR = HERE / "page-captures" / "maps-biking-segments.har"


# --- minimal protobuf / MVT reader (varint + length-delimited only) ----------
def _varint(b: bytes, i: int) -> tuple[int, int]:
    shift = res = 0
    while True:
        x = b[i]; i += 1
        res |= (x & 0x7F) << shift
        if not (x & 0x80):
            return res, i
        shift += 7


def _fields(b: bytes):
    """Yield (field_number, wire_type, value) for a protobuf message."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i); yield fn, wt, v
        elif wt == 2:
            ln, i = _varint(b, i); yield fn, wt, b[i:i + ln]; i += ln
        elif wt == 5:
            yield fn, wt, b[i:i + 4]; i += 4
        elif wt == 1:
            yield fn, wt, b[i:i + 8]; i += 8
        else:
            raise ValueError(f"bad wire type {wt}")


def _value(b: bytes):
    for fn, _wt, v in _fields(b):
        if fn == 1:
            return v.decode("utf-8", "replace")     # string
        if fn in (4, 5):
            return v                                 # int64 / uint64
        if fn == 6:
            return (v >> 1) ^ -(v & 1)               # sint64 (zigzag)
        if fn == 7:
            return bool(v)                            # bool
    return None


def _layer(b: bytes):
    name, keys, values, feats, extent = None, [], [], [], 4096
    for fn, wt, v in _fields(b):
        if fn == 1:
            name = v.decode("utf-8", "replace")
        elif fn == 3 and wt == 2:
            keys.append(v.decode("utf-8", "replace"))
        elif fn == 4 and wt == 2:
            values.append(_value(v))
        elif fn == 5:
            extent = v
        elif fn == 2 and wt == 2:
            feats.append(v)
    return name, keys, values, feats, extent


def _feature(b: bytes):
    fid, tags, geom = None, [], b""
    for fn, wt, v in _fields(b):
        if fn == 1:
            fid = v
        elif fn == 2 and wt == 2:
            j, t = 0, []
            while j < len(v):
                x, j = _varint(v, j); t.append(x)
            tags = t
        elif fn == 4 and wt == 2:
            geom = v
    return fid, tags, geom


def _geometry_points(geom: bytes) -> list[tuple[int, int]]:
    """Decode MVT command/parameter integers into tile-space (x, y) points."""
    arr, j = [], 0
    while j < len(geom):
        x, j = _varint(geom, j); arr.append(x)
    pts, i, cx, cy = [], 0, 0, 0
    while i < len(arr):
        cmd = arr[i]; i += 1
        cmd_id, count = cmd & 7, cmd >> 3
        if cmd_id in (1, 2):                          # MoveTo / LineTo
            for _ in range(count):
                dx, dy = arr[i], arr[i + 1]; i += 2
                cx += (dx >> 1) ^ -(dx & 1)
                cy += (dy >> 1) ^ -(dy & 1)
                pts.append((cx, cy))
        # ClosePath (7) takes no parameters
    return pts


def _tile_to_lonlat(px, py, z, tx, ty, extent):
    wx, wy = tx + px / extent, ty + py / extent
    lon = wx / (2 ** z) * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * wy / (2 ** z)))))
    return lon, lat


def _tile_zxy(url: str) -> tuple[int, int, int]:
    p = url.split("?")[0].split("/tiles/segments/")[1].split("/")
    return int(p[1]), int(p[2]), int(p[3])


def decode_har(har_path: Path, boundary) -> dict:
    """Decode all segment vector tiles in a HAR into {segment_id: {...}}.
    `in_town` is True when any of the segment's tile vertices fall inside the
    Shutesbury boundary (a candidate filter; exact start/finish is settled when
    the segment page is fetched)."""
    har = json.loads(har_path.read_text())
    segs: dict[int, dict] = {}
    for entry in har["log"]["entries"]:
        url = entry["request"]["url"]
        if "/tiles/segments/" not in url:
            continue
        content = entry["response"].get("content") or {}
        if content.get("mimeType") != "application/vnd.mapbox-vector-tile":
            continue
        text = content.get("text")
        if not text:
            continue
        raw = (base64.b64decode(text) if content.get("encoding") == "base64"
               else text.encode("latin1"))
        z, tx, ty = _tile_zxy(url)
        for fn, wt, v in _fields(raw):
            if fn != 3 or wt != 2:                    # Tile.layers
                continue
            _name, keys, values, feats, extent = _layer(v)
            for fb in feats:
                fid, tags, geom = _feature(fb)
                props = {keys[tags[k]]: values[tags[k + 1]]
                         for k in range(0, len(tags) - 1, 2)
                         if tags[k] < len(keys) and tags[k + 1] < len(values)}
                sid = props.get("segmentId") or fid
                if sid is None:
                    continue
                in_town = False
                if boundary is not None:
                    for px, py in _geometry_points(geom):
                        lon, lat = _tile_to_lonlat(px, py, z, tx, ty, extent)
                        if geo.point_in_shutesbury(lat, lon, boundary):
                            in_town = True
                            break
                rec = segs.setdefault(sid, {
                    "name": props.get("name"),
                    "efforts": props.get("attemptsAllTime"),
                    "athletes": props.get("athletesAllTime"),
                    "kom_athlete_id": props.get("komAthleteId"),
                    "in_town": False})
                rec["in_town"] = rec["in_town"] or in_town
    return segs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("har", nargs="?", default=str(DEFAULT_HAR),
                    help=f"HAR capture to decode (default: {DEFAULT_HAR.name})")
    ap.add_argument("--all", action="store_true",
                    help="include segments already tracked in the DB")
    ap.add_argument("--add", action="store_true",
                    help="register the in-town, untracked segments for tracking")
    args = ap.parse_args()

    har_path = Path(args.har)
    if not har_path.exists():
        ap.error(f"HAR not found: {har_path}")
    try:
        boundary = geo.load_boundary()
    except Exception as e:                            # noqa: BLE001
        boundary = None
        print(f"! boundary unavailable ({e}); skipping geo filter")

    segs = decode_har(har_path, boundary)
    conn = db.connect()
    db.init(conn)
    tracked = set(db.segment_ids(conn))

    in_town = {sid: r for sid, r in segs.items() if r["in_town"]}
    candidates = sorted(
        ((sid, r) for sid, r in in_town.items() if args.all or sid not in tracked),
        key=lambda kv: -(kv[1]["efforts"] or 0))

    print(f"{len(segs)} segments in capture · "
          f"{len(in_town)} cross the Shutesbury boundary · "
          f"{sum(1 for s in in_town if s in tracked)} already tracked\n")
    if not candidates:
        print("No new in-town candidates — everything in view is already tracked.")
    else:
        label = "in-town segments" if args.all else "in-town, UNTRACKED"
        print(f"{label} (by total efforts):")
        for sid, r in candidates:
            mark = " [tracked]" if sid in tracked else ""
            print(f"  {sid:>10}  efforts={str(r['efforts']):>6}  "
                  f"{(r['name'] or '?')}{mark}")

    if args.add:
        new = [sid for sid, _ in candidates if sid not in tracked]
        for sid in new:
            db.add_segment_id(conn, sid)
        print(f"\n+ registered {len(new)} segment(s). "
              f"Run `uv run update_segments.py` to fetch them.")
    conn.close()


if __name__ == "__main__":
    main()
