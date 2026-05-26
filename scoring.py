"""Segment scoring and the overall "King of Shutesbury" standings.

Two stages, both Tour-de-France inspired:

1. Each segment gets a DIFFICULTY (a point pool) from its length, elevation
   gain/loss, and popularity, scaled by terrain (climbs > flats > descents).

2. That pool is awarded to the fastest athletes, shares decaying linearly to
   zero (3 places -> 100/66/33%). How many places score scales with the
   segment's popularity (the depth tiers). Summing across all segments gives the
   overall King standings.

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

# By total efforts: a difficulty DISCOUNT for rarely-ridden segments (capped at
# 1.0 — it only pulls obscure segments down, never inflates the popular ones) and
# how many places score (controls the tail). (min_efforts, mult, places), high first.
POPULARITY_TIERS = [
    (3000, 1.0, 9),
    (1000, 1.0, 8),
    (250, 0.9, 6),
    (50, 0.6, 3),
    (0, 0.3, 1),
]


def _popularity_tier(total_efforts: int | None) -> tuple[float, int]:
    e = total_efforts or 0
    for threshold, mult, places in POPULARITY_TIERS:
        if e >= threshold:
            return mult, places
    return POPULARITY_TIERS[-1][1], POPULARITY_TIERS[-1][2]


def popularity_mult(total_efforts: int | None) -> float:
    """Obscurity discount (<= 1.0) by how contested a segment is."""
    return _popularity_tier(total_efforts)[0]


def effort_depth(total_efforts: int | None) -> int:
    """How many ranks score on a segment, by how contested it is (total efforts)."""
    return _popularity_tier(total_efforts)[1]


def segment_difficulty(seg: dict) -> float:
    """Point pool for a segment from its length, elevation gain/loss, and
    popularity, scaled by terrain and discounted if rarely ridden."""
    dist_km = (seg.get("distance_m") or 0) / 1000.0
    gain = seg.get("gross_gain") or 0.0
    loss = seg.get("gross_loss") or 0.0
    efforts = seg.get("total_efforts") or 0
    terrain = seg.get("terrain") or "flat"

    elevation = gain * W_GAIN + loss * W_LOSS
    distance = dist_km * W_DIST
    popularity = W_POP * math.log10(1 + efforts)
    pool = elevation + distance + popularity
    pool *= TERRAIN_MULT.get(terrain, 0.85)
    pool *= popularity_mult(efforts)
    return round(pool, 1)


# Tour-de-France KOM point scale by category (INRNG) — more dramatic / front-
# loaded as climbs get bigger. Cima Coppi is reserved for the single hardest segment.
TOUR_POINTS = {
    "Cima Coppi": [50, 30, 20, 14, 10, 6, 4, 2, 1],
    "Cat 1":      [40, 18, 12, 9, 6, 4, 2, 1],
    "Cat 2":      [18, 8, 6, 4, 2, 1],
    "Cat 3":      [9, 4, 2, 1],
    "Cat 4":      [3, 2, 1],
}
# Difficulty -> category. The single highest-difficulty segment becomes Cima Coppi.
CATEGORY_THRESHOLDS = [(250, "Cat 1"), (150, "Cat 2"), (90, "Cat 3"), (0, "Cat 4")]


def segment_category(difficulty: float, is_cima: bool = False) -> str:
    if is_cima:
        return "Cima Coppi"
    for threshold, cat in CATEGORY_THRESHOLDS:
        if (difficulty or 0) >= threshold:
            return cat
    return "Cat 4"


def points_for_rank(rank: int | None, category: str, places_cap: int = 99) -> float:
    """Tour KOM points for finishing `rank`-th on a segment of this category.
    `places_cap` (the popularity depth) limits how many places actually pay, so a
    hard-but-obscure segment still only rewards its KOM or two."""
    pts = TOUR_POINTS.get(category, [])
    n = min(len(pts), places_cap)
    if not rank or rank < 1 or rank > n:
        return 0
    return pts[rank - 1]


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
        cat = segment_category(seg.get("difficulty") or 0.0)
        depth = effort_depth(seg.get("total_efforts"))
        for eff in seg.get("efforts", []):
            aid = eff.get("athlete_id")
            if aid is None:
                continue
            pts = points_for_rank(eff.get("rank"), cat, depth)
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
