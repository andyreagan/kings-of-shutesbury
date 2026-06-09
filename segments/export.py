"""Export the current DB as web/data.json (kings) and web/data-queens.json.

The kings output is byte-identical to the legacy export — same dict key order,
same rounding, same athlete_name key mapping.

The queens output shares the same top-level shape and segment structure, but
efforts are filtered to athletes with Athlete.gender == "F" and re-ranked among
women only (competition_ranks by elapsed_time; NULL elapsed_times unranked).
"""

from __future__ import annotations

import json
from pathlib import Path

from django.db.models import F

from . import geo
from . import scoring
from .models import Athlete, Effort, Segment
from .pipeline import competition_ranks, now, TRACKED_ATHLETES

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DATA_JSON = WEB_DIR / "data.json"
DATA_QUEENS_JSON = WEB_DIR / "data-queens.json"
DATA_LEGENDS_JSON = WEB_DIR / "data-legends.json"
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
            "athlete_gender": e.athlete.gender,
        })
    seg["efforts"] = efforts
    return seg


def _build_legends_payload(segments: list[dict]) -> dict:
    """Build the legends board: every athlete (both genders) ranked by how many
    of the tracked segments they've completed.

    "Completed" = the athlete has any effort in the `efforts` view for that
    segment, ranked OR timeless (rank-NULL following supplements count — they
    still rode it). Computed from the FULL effort set, not the 100-cap shipped
    in data.json, so completion is exact even on segments with 1000s of efforts.

    The frontend applies the discipline filter (all/road/gravel/mtb) client-side
    using each segment's discipline, so the denominator narrows with the filter.
    There is deliberately no attempt-count tiebreak: Strava's leaderboard payload
    gives one row per athlete (their best effort) with no per-athlete attempt
    count, so we have nothing to break ties on — riders tied on completion stay
    tied (name-ordered for a stable display).
    """
    athletes: dict[int, dict] = {}
    completion: dict[int, set] = {}
    for seg in segments:
        sid = seg["id"]
        for e in seg["efforts"]:
            aid = e["athlete_id"]
            a = athletes.get(aid)
            if a is None:
                athletes[aid] = {
                    "athlete_id": aid, "name": e["athlete_name"],
                    "avatar_url": e["avatar_url"], "badge": e["badge"],
                    "gender": e["athlete_gender"],
                }
            else:
                # An athlete spans many segments; fill missing meta and keep a
                # sticky "F" (matches the Athlete-row gender semantics).
                a["name"] = a["name"] or e["athlete_name"]
                a["avatar_url"] = a["avatar_url"] or e["avatar_url"]
                a["badge"] = a["badge"] or e["badge"]
                if e["athlete_gender"] == "F":
                    a["gender"] = "F"
            completion.setdefault(aid, set()).add(sid)

    legends = [{**athletes[aid], "segs": sorted(completion[aid])}
               for aid in completion]
    # Default order: most segments first, then name — the frontend re-sorts per
    # filter, but a sensible order keeps the raw file readable.
    legends.sort(key=lambda lg: (-len(lg["segs"]), (lg["name"] or "").lower()))

    # Per-segment coverage so the frontend can show how trustworthy a segment's
    # completion is. `captured` (distinct athletes we have) vs `total_athletes`
    # (Strava's count) is the breadth; `efforts_fetched_at` is the recency — a
    # deep sweep goes stale as new riders complete the segment, so when we last
    # swept matters as much as how deep. NULL efforts_fetched_at = never swept
    # (only the page-embedded top-25 seed).
    seg_meta = [{
        "id": s["id"], "name": s["name"], "location": s["display_location"],
        "discipline": s["discipline"], "terrain": s["terrain"],
        "difficulty": s["difficulty"],
        "parent_segment_id": s["parent_segment_id"],
        "is_sub_segment": s["parent_segment_id"] is not None,
        "captured": len(s["efforts"]),
        "total_athletes": s["total_athletes"],
        "efforts_fetched_at": s["efforts_fetched_at"],
    } for s in segments]

    return {"generated_at": now(), "segments": seg_meta, "legends": legends}


def _build_payload(segments: list[dict], filtered: list[dict],
                   boundary, cima_id: int | None,
                   queens: bool = False) -> dict:
    """Build the full export payload.

    queens=False → kings board (all efforts, rank from the `efforts` view).
    queens=True  → women's board (efforts filtered to Athlete.gender=="F",
                   re-ranked among women by elapsed_time using competition_ranks,
                   re-scored using the same category/depth).
    """
    out_segments = []
    for seg in segments:
        streams = json.loads(seg.get("streams_json") or "{}")
        depth = scoring.effort_depth(seg["total_efforts"])
        category = scoring.segment_category(seg["difficulty"], seg["id"] == cima_id)

        if queens:
            # Filter to women only and re-rank among them.
            women_efforts = [e for e in seg["efforts"]
                             if e.get("athlete_gender") == "F"]
            # Re-rank by elapsed_time (NULL = unranked, same as the view).
            time_map = {e["athlete_id"]: e["elapsed_time"]
                        for e in women_efforts if e["elapsed_time"] is not None}
            women_ranks = competition_ranks(time_map)
            ranked_efforts = []
            for e in women_efforts:
                new_rank = women_ranks.get(e["athlete_id"])  # None if timeless
                ranked_efforts.append({**e, "rank": new_rank})
            # Sort: ranked first (ascending rank), then NULL-rank, tie-break by date.
            ranked_efforts.sort(key=lambda e: (
                e["rank"] is None, e["rank"] or 0, e["start_date_local"] or ""))
            display_efforts = ranked_efforts
        else:
            display_efforts = seg["efforts"]

        leader_raw = display_efforts[0] if display_efforts else None
        # For queens: only use a ranked row as leader (skip timeless supplements).
        if queens and leader_raw and leader_raw["rank"] is None:
            leader_raw = next(
                (e for e in display_efforts if e["rank"] is not None), None)
        leader = ({"name": leader_raw["athlete_name"],
                   "elapsed_time": leader_raw["elapsed_time"]}
                  if leader_raw else None)

        def track_of(seg) -> list:
            loc = json.loads(seg.get("streams_json") or "{}").get("location") or []
            return _downsample(loc, MAP_TRACK_POINTS)

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
            "leader": leader,
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
            } for e in _capped_efforts(display_efforts)],
        })
    out_segments.sort(key=lambda s: s["difficulty"], reverse=True)

    return {
        "generated_at": now(),
        "segments": out_segments,
        "filtered": filtered,
        "boundary": boundary,
    }


def export_data_json() -> None:
    """Serialize the current DB into web/data.json (kings) and
    web/data-queens.json (queens). Pure read — assumes import has already set
    geo/difficulty/sub-seg. Defensive: any segment with NULL difficulty gets it
    computed on the fly so a stale DB still exports."""
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

    WEB_DIR.mkdir(exist_ok=True)

    # Kings payload — all efforts, overall-board ranks (unchanged from before).
    kings_payload = _build_payload(included, filtered, boundary, cima_id, queens=False)
    DATA_JSON.write_text(json.dumps(kings_payload, indent=2))

    # Queens payload — same shape; efforts filtered to Athlete.gender=="F",
    # re-ranked and re-scored among women only.
    queens_payload = _build_payload(included, filtered, boundary, cima_id, queens=True)
    DATA_QUEENS_JSON.write_text(json.dumps(queens_payload, indent=2))

    # Legends payload — every athlete (both genders) ranked by segment completion.
    legends_payload = _build_legends_payload(included)
    DATA_LEGENDS_JSON.write_text(json.dumps(legends_payload, indent=2))

    n_kings = len(kings_payload["segments"])
    n_queens = len(queens_payload["segments"])
    n_legends = len(legends_payload["legends"])
    print(f"Wrote {DATA_JSON} — {n_kings} in-Shutesbury ride "
          f"segments, {len(filtered)} filtered out.")
    print(f"Wrote {DATA_QUEENS_JSON} — {n_queens} segments (women's board).")
    print(f"Wrote {DATA_LEGENDS_JSON} — {n_legends} athletes (legends board).")
