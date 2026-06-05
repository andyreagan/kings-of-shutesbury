"""Segment scoring and the overall "King of Shutesbury" standings.

Two stages, both Tour-de-France inspired:

1. Each segment gets a DIFFICULTY score from its length, elevation gain/loss,
   and popularity, scaled by terrain (climbs > flats > descents) and discounted
   if rarely ridden.

2. The fastest athletes on each segment score points; how many places pay is
   capped by the segment's popularity (the depth tiers). Two payout methods are
   available, selected by PAYOUT_METHOD:

     "tour" (default) — difficulty sorts the segment into a Tour KOM CATEGORY
       (Cat 4 -> Cat 1, with the single hardest crowned Cima Coppi), and each
       category pays a fixed, front-loaded KOM point table.
     "pool" — the difficulty score IS a point pool, handed to the top finishers
       with shares decaying linearly to zero (3 places -> 100/66/33% of the
       winner's cut).

   Summing each athlete's payouts across all segments gives the overall King
   standings.

All knobs live at the top — tweak freely.
"""

from __future__ import annotations

import math

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


# -- stage 2: turning ranks into points ----------------------------------------
# "tour" maps difficulty to a Tour KOM category and pays a fixed point table.
# "pool" treats the difficulty score as a point pool, shared out with linearly
# decaying shares. Both cap how many places pay by the popularity depth tier.
PAYOUT_METHOD = "tour"  # "tour" | "pool"

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


def pool_shares(pool: float, places: int) -> list[float]:
    """Hand a difficulty `pool` to the top `places` finishers with shares
    decaying linearly to zero: rank r of N gets weight N+1-r, normalized so the
    payouts sum to the pool (3 places -> 100/66/33% of the winner's cut)."""
    if places < 1 or (pool or 0) <= 0:
        return []
    weights = [places - i for i in range(places)]  # N, N-1, ..., 1
    total = sum(weights)
    return [round(pool * w / total, 1) for w in weights]


def points_for_rank(rank: int | None, category: str, places_cap: int = 99,
                    difficulty: float | None = None) -> float:
    """Points for finishing `rank`-th on a segment, under the active
    PAYOUT_METHOD. `places_cap` (the popularity depth) limits how many places
    actually pay, so a hard-but-obscure segment still only rewards its KOM or two.

    - "tour": fixed front-loaded KOM table for the segment's `category`.
    - "pool": the segment's `difficulty` score is a pool shared out with linearly
      decaying shares — pass `difficulty` for this method.
    """
    if PAYOUT_METHOD == "pool":
        shares = pool_shares(difficulty or 0.0, places_cap)
        if not rank or rank < 1 or rank > len(shares):
            return 0
        return shares[rank - 1]
    pts = TOUR_POINTS.get(category, [])
    n = min(len(pts), places_cap)
    if not rank or rank < 1 or rank > n:
        return 0
    return pts[rank - 1]
