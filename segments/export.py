"""Export the current DB as web/data.json (ORM port of update_segments.py's export).

The output JSON is byte-identical to the legacy export — same dict key order,
same rounding, same athlete_name key mapping.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.db.models import F

from . import geo
from . import scoring
from .models import Effort, Segment
from .pipeline import now, TRACKED_ATHLETES

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DATA_JSON = WEB_DIR / "data.json"
PROFILE_POINTS = 120        # downsample elevation profile to this many points
MAP_TRACK_POINTS = 64       # downsample GPS track for the map to this many points
EFFORTS_PER_SEGMENT = 100   # cap efforts shipped per segment (keeps page small)


def _downsample(xs: list, n: int) -> list:
    if not xs or len(xs) <= n:
        return xs
    step = (len(xs) - 1) / (n - 1)
    return [xs[round(i * step)] for i in range(n)]


def _capped_efforts(efforts: list) -> list:
    """Ship at most EFFORTS_PER_SEGMENT ranked efforts per segment to keep
    data.json small, but always keep tracked athletes (Andy/Owen) and the
    rank-NULL following supplements regardless of rank."""
    kept, n = [], 0
    for e in efforts:
        if (n < EFFORTS_PER_SEGMENT or e["rank"] is None
                or e["athlete_id"] in TRACKED_ATHLETES):
            kept.append(e)
            if e["rank"] is not None:
                n += 1
    return kept


def _segment_with_efforts(seg_obj: Segment) -> dict:
    """Return a plain dict for the segment with efforts attached.

    The efforts query replicates the legacy SQL:
      ORDER BY e.rank IS NULL, e.rank, e.start_date_local
    which puts ranked efforts first (NULL-rank following supplements last),
    then orders tied ranks by start_date_local ascending (earliest ride first).
    Django equivalent: F("rank").asc(nulls_last=True), "start_date_local".
    """
    seg = {f.name: getattr(seg_obj, f.name) for f in seg_obj._meta.get_fields()
           if hasattr(f, "column") and not f.is_relation}

    effort_qs = (
        Effort.objects
        .filter(segment_id=seg_obj.pk)
        .select_related("athlete")
        .order_by(F("rank").asc(nulls_last=True), "start_date_local")
    )
    efforts = []
    for e in effort_qs:
        efforts.append({
            "segment_id": e.segment_id,
            "athlete_id": e.athlete_id,
            "rank": e.rank,
            "elapsed_time": e.elapsed_time,
            "avg_speed": e.avg_speed,
            "avg_watts": e.avg_watts,
            "avg_hr": e.avg_hr,
            "effort_id": e.effort_id,
            "activity_id": e.activity_id,
            "start_date_local": e.start_date_local,
            "first_seen": e.first_seen,
            "last_seen": e.last_seen,
            "athlete_name": e.athlete.name,
            "avatar_url": e.athlete.avatar_url,
            "badge": e.athlete.badge,
        })
    seg["efforts"] = efforts
    return seg


def export_data_json() -> None:
    """Serialize the current DB into web/data.json. Pure read — assumes import
    has already set geo/difficulty/sub-seg. Defensive: any segment with NULL
    difficulty gets it computed on the fly so a stale DB still exports."""
    seg_objs = list(Segment.objects.filter(fetched_at__isnull=False))
    segments = [_segment_with_efforts(s) for s in seg_objs]

    try:
        boundary = geo.load_boundary()
    except Exception as e:                                       # noqa: BLE001
        boundary = None
        print(f"! Shutesbury boundary unavailable ({e}); skipping geo in payload")

    # Fallback fill for difficulty in case `import` was never run for these
    # rows (e.g. a hand-edited DB). Cheap; keeps export side-effect-free
    # otherwise.
    for seg in segments:
        if seg["difficulty"] is None:
            seg["difficulty"] = scoring.segment_difficulty(seg)
            Segment.objects.filter(pk=seg["id"]).update(difficulty=seg["difficulty"])

    def is_included(seg) -> bool:
        if seg["excluded"] == 1:
            return False
        ride = (seg["activity_type"] or "").lower() == "ride"
        in_town = seg["in_town"] == 1 if boundary is not None else True
        return ride and in_town

    def track_of(seg) -> list:
        loc = json.loads(seg.get("streams_json") or "{}").get("location") or []
        return _downsample(loc, MAP_TRACK_POINTS)

    included = [s for s in segments if is_included(s)]
    filtered = []
    for s in segments:
        if is_included(s):
            continue
        reasons = []
        if s["excluded"] == 1:
            reasons.append("manually excluded")
        if (s["activity_type"] or "").lower() != "ride":
            reasons.append((s["activity_type"] or "non-ride").lower())
        if boundary is not None and s["in_town"] != 1:
            reasons.append("passes through" if s["passes_through"] == 1
                           else "outside Shutesbury")
        filtered.append({"id": s["id"], "name": s["name"],
                         "location": s["display_location"],
                         "reason": ", ".join(reasons) or "excluded",
                         "terrain": s["terrain"], "distance_m": s["distance_m"],
                         "avg_grade": s["avg_grade"], "difficulty": s["difficulty"],
                         "track": track_of(s)})

    # The single hardest in-town ride is the "Cima Coppi".
    cima_id = max(included, key=lambda s: s["difficulty"])["id"] if included else None

    out_segments = []
    for seg in included:
        streams = json.loads(seg.get("streams_json") or "{}")
        depth = scoring.effort_depth(seg["total_efforts"])
        category = scoring.segment_category(seg["difficulty"], seg["id"] == cima_id)
        leader = seg["efforts"][0] if seg["efforts"] else None
        out_segments.append({
            "id": seg["id"],
            "name": seg["name"],
            "location": seg["display_location"],
            "activity_type": seg["activity_type"],
            "discipline": seg["discipline"],
            "category": category,
            "terrain": seg["terrain"],
            "distance_m": seg["distance_m"],
            "avg_grade": seg["avg_grade"],
            "elev_gain": seg["elev_gain"],
            "gross_gain": seg["gross_gain"],
            "gross_loss": seg["gross_loss"],
            "total_efforts": seg["total_efforts"],
            "total_athletes": seg["total_athletes"],
            "difficulty": seg["difficulty"],
            "parent_segment_id": seg["parent_segment_id"],
            "is_sub_segment": seg["parent_segment_id"] is not None,
            "map_image_url": seg["map_image_url"],
            "start_latlng": [seg["start_lat"], seg["start_lng"]],
            "end_latlng": [seg["end_lat"], seg["end_lng"]],
            "track": track_of(seg),
            "leader": {"name": leader["athlete_name"],
                       "elapsed_time": leader["elapsed_time"]} if leader else None,
            "profile": {
                "distance": _downsample(streams.get("distance") or [], PROFILE_POINTS),
                "elevation": _downsample(streams.get("elevation") or [], PROFILE_POINTS),
            },
            "efforts": [{
                "rank": e["rank"], "athlete_id": e["athlete_id"],
                "name": e["athlete_name"], "elapsed_time": e["elapsed_time"],
                "avg_watts": e["avg_watts"], "avatar_url": e["avatar_url"],
                "badge": e["badge"],
                "points": scoring.points_for_rank(e["rank"], category, depth,
                                                  seg["difficulty"]),
                "effort_id": e["effort_id"], "activity_id": e["activity_id"],
            } for e in _capped_efforts(seg["efforts"])],
        })
    out_segments.sort(key=lambda s: s["difficulty"], reverse=True)

    payload = {
        "generated_at": now(),
        "segments": out_segments,
        "filtered": filtered,
        "boundary": boundary,
    }
    WEB_DIR.mkdir(exist_ok=True)
    DATA_JSON.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {DATA_JSON} — {len(out_segments)} in-Shutesbury ride "
          f"segments, {len(filtered)} filtered out.")
