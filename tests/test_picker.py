"""Background picker: phase A (stale top-25) → B (build depth) → C (idle)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import update_segments as us


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(days=days)).isoformat(timespec="seconds")


def test_empty_db_returns_none(conn):
    assert us._background_pick(conn) is None


# === Phase A: stalest top-25 =================================================


def test_phase_a_picks_never_refreshed_first(conn, seed_segment):
    """NULL `efforts_fetched_at` outranks any non-NULL stalest timestamp."""
    seed_segment(1, efforts_fetched_at=_iso_days_ago(30))         # very stale
    seed_segment(2, efforts_fetched_at=None)                       # never
    seed_segment(3, efforts_fetched_at=_iso_days_ago(10))          # stale-ish
    sid, depth, phase = us._background_pick(conn)
    assert sid == 2
    assert depth == 1
    assert phase.startswith("A")


def test_phase_a_picks_oldest_when_no_nulls(conn, seed_segment):
    seed_segment(1, efforts_fetched_at=_iso_days_ago(20))
    seed_segment(2, efforts_fetched_at=_iso_days_ago(8))
    seed_segment(3, efforts_fetched_at=_iso_days_ago(15))
    sid, _, phase = us._background_pick(conn)
    assert sid == 1
    assert phase.startswith("A")


def test_phase_a_skips_fresh_segments(conn, seed_segment):
    """A segment refreshed in the last week is NOT phase-A material — the
    picker falls through to phase B or C."""
    seed_segment(1, efforts_fetched_at=_iso_days_ago(2),
                 last_depth_pages=8, total_athletes=200)            # at max
    sid, depth, phase = us._background_pick(conn)
    assert sid == 1
    # Either B (if not at max depth) or C (if at max). With last_depth_pages=8
    # and total_athletes=200 → 8*25 == 200, so we're AT max → phase C.
    assert phase.startswith("C")
    assert depth == 1


# === Phase B: build depth ====================================================


def test_phase_b_picks_shallowest(conn, seed_segment):
    fresh = _iso_days_ago(1)
    seed_segment(1, efforts_fetched_at=fresh,
                 last_depth_pages=3, total_athletes=500)
    seed_segment(2, efforts_fetched_at=fresh,
                 last_depth_pages=1, total_athletes=500)
    seed_segment(3, efforts_fetched_at=fresh,
                 last_depth_pages=2, total_athletes=500)
    sid, depth, phase = us._background_pick(conn)
    assert sid == 2
    assert depth == 2          # one page deeper than current 1
    assert phase.startswith("B")


def test_phase_b_treats_null_as_depth_one(conn, seed_segment):
    """`last_depth_pages IS NULL` (never deepened) counts as 1, and the next
    deepening attempt should aim for page 2."""
    fresh = _iso_days_ago(1)
    seed_segment(1, efforts_fetched_at=fresh,
                 last_depth_pages=None, total_athletes=300)
    sid, depth, phase = us._background_pick(conn)
    assert sid == 1
    assert depth == 2
    assert phase.startswith("B")


def test_phase_b_skips_segments_at_max_depth(conn, seed_segment):
    """`last_depth_pages * 25 >= total_athletes` means we've paged through
    everyone — no more depth to build, drop to phase C."""
    fresh = _iso_days_ago(1)
    # At-max segment shouldn't get picked for B.
    seed_segment(1, efforts_fetched_at=fresh,
                 last_depth_pages=4, total_athletes=100)            # 100 athletes, depth 4 = done
    # Shallower one still has room.
    seed_segment(2, efforts_fetched_at=fresh,
                 last_depth_pages=1, total_athletes=100)
    sid, depth, phase = us._background_pick(conn)
    assert sid == 2
    assert depth == 2
    assert phase.startswith("B")


# === Phase C: idle maintenance ===============================================


def test_phase_c_when_all_fresh_and_at_max(conn, seed_segment):
    fresh = _iso_days_ago(1)
    seed_segment(1, efforts_fetched_at=fresh,
                 last_depth_pages=4, total_athletes=100)
    seed_segment(2, efforts_fetched_at=_iso_days_ago(3),
                 last_depth_pages=4, total_athletes=100)
    sid, depth, phase = us._background_pick(conn)
    assert sid == 2                # the stalest of the two fresh ones
    assert depth == 1
    assert phase.startswith("C")


# === Filter correctness ======================================================


def test_picker_skips_excluded(conn, seed_segment):
    seed_segment(1, excluded=1)
    seed_segment(2)
    sid, _, _ = us._background_pick(conn)
    assert sid == 2


def test_picker_skips_non_ride(conn, seed_segment):
    seed_segment(1, activity_type="Run")
    seed_segment(2)
    sid, _, _ = us._background_pick(conn)
    assert sid == 2


def test_picker_skips_out_of_town(conn, seed_segment):
    seed_segment(1, in_town=0)
    seed_segment(2)
    sid, _, _ = us._background_pick(conn)
    assert sid == 2


def test_picker_skips_unimported(conn, seed_segment):
    """`fetched_at IS NULL` means import never ran — nothing to update yet."""
    seed_segment(1, fetched_at=None)
    seed_segment(2)
    sid, _, _ = us._background_pick(conn)
    assert sid == 2
