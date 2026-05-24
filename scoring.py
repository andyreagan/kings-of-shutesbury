"""Segment scoring and the overall "King of Shutesbury" standings.

Two stages, both Tour-de-France inspired:

1. Each segment gets a DIFFICULTY (a point pool) from its length, popularity,
   and elevation gain/loss, scaled by terrain. Climbs are worth the most, flats
   a bit less, descents less again (a fast descent is real but less brutal).

2. Each segment's points are awarded to the fastest athletes by rank (like KOM
   points over a climb: 1st gets the most, with a decaying share down the board).
   Summing across all segments gives the overall King standings.

All knobs live at the top — tweak freely.
"""

from __future__ import annotations

import math
from collections import defaultdict

# -- segment difficulty weights ------------------------------------------------
W_GAIN = 1.0      # points per meter climbed (gross)
W_LOSS = 0.5      # points per meter descended (gross) — downhill counts less
W_DIST = 8.0      # points per kilometer
W_POP = 20.0      # popularity: points per log10 of total efforts

# Terrain multiplier applied to the whole pool. Climb >= flat > descent.
TERRAIN_MULT = {"climb": 1.0, "flat": 0.85, "descent": 0.65}

# -- per-segment rank payout (share of the segment's difficulty) ---------------
# Index 0 = 1st place. Ranks beyond this list score nothing.
RANK_SHARES = [1.0, 0.80, 0.66, 0.56, 0.48, 0.40, 0.32, 0.24, 0.16, 0.08]


def segment_difficulty(seg: dict) -> float:
    """Point pool for a segment from its physical + popularity profile."""
    dist_km = (seg.get("distance_m") or 0) / 1000.0
    gain = seg.get("gross_gain") or 0.0
    loss = seg.get("gross_loss") or 0.0
    efforts = seg.get("total_efforts") or 0
    terrain = seg.get("terrain") or "flat"

    elevation = gain * W_GAIN + loss * W_LOSS
    distance = dist_km * W_DIST
    popularity = W_POP * math.log10(1 + efforts)
    pool = elevation + distance + popularity
    return round(pool * TERRAIN_MULT.get(terrain, 0.85), 1)


def points_for_rank(rank: int | None, difficulty: float) -> float:
    if not rank or rank < 1 or rank > len(RANK_SHARES):
        return 0.0
    return round(difficulty * RANK_SHARES[rank - 1], 1)


def king_standings(segments: list[dict]) -> list[dict]:
    """Aggregate per-segment rank payouts into overall athlete standings.

    Each segment dict needs: id, name, difficulty, and efforts =
    [{athlete_id, athlete_name, rank, elapsed_time}, ...].
    Returns athletes sorted by total points (desc), with a breakdown.
    """
    points: dict[int, float] = defaultdict(float)
    names: dict[int, str] = {}
    wins: dict[int, int] = defaultdict(int)
    scored_segments: dict[int, int] = defaultdict(int)

    for seg in segments:
        diff = seg.get("difficulty") or 0.0
        for eff in seg.get("efforts", []):
            aid = eff.get("athlete_id")
            if aid is None:
                continue
            pts = points_for_rank(eff.get("rank"), diff)
            if pts <= 0:
                continue
            points[aid] += pts
            scored_segments[aid] += 1
            if eff.get("rank") == 1:
                wins[aid] += 1
            if eff.get("athlete_name"):
                names[aid] = eff["athlete_name"]

    standings = [
        {
            "athlete_id": aid,
            "name": names.get(aid, f"Athlete {aid}"),
            "points": round(pts, 1),
            "segments_won": wins[aid],
            "segments_scored": scored_segments[aid],
        }
        for aid, pts in points.items()
    ]
    standings.sort(key=lambda s: s["points"], reverse=True)
    for i, s in enumerate(standings, 1):
        s["overall_rank"] = i
    return standings
