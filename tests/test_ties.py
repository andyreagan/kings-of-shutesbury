"""Tied times use Strava-style competition ranking: equal times share a rank
(both get the KOM), each tied place earns that rank's FULL points, and the
next distinct time skips the tied-out places — 1, 1, 3 and 1, 2, 3, 3, 5."""

from __future__ import annotations

import scoring
import update_segments
from tests.conftest import insert_segment


def log_effort(conn, seg_id: int, athlete_id: int, elapsed: int,
               date: str = "2026-05-01T10:00:00Z") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO athletes (id, name) VALUES (?, ?)",
        (athlete_id, f"Athlete {athlete_id}"))
    conn.execute(
        "INSERT INTO effort_log (segment_id, athlete_id, elapsed_time, "
        "effort_id, start_date_local, observed_at) VALUES (?, ?, ?, ?, ?, ?)",
        (seg_id, athlete_id, elapsed, f"e{athlete_id}", date,
         "2026-06-01T00:00:00+00:00"))
    conn.commit()


def view_ranks(conn, seg_id: int) -> dict[int, int]:
    return {r["athlete_id"]: r["rank"] for r in conn.execute(
        "SELECT athlete_id, rank FROM efforts WHERE segment_id = ?", (seg_id,))}


def test_two_way_tie_shares_the_kom(conn):
    """The W Pelham Mini-Wall case: two athletes tied to the second, years
    apart — BOTH hold rank 1 (the ride date must not break the tie), and the
    next rider is 3rd."""
    insert_segment(conn, 1)
    log_effort(conn, 1, 101, 35, date="2014-04-29T16:48:26Z")
    log_effort(conn, 1, 102, 35, date="2026-05-20T17:44:02Z")
    log_effort(conn, 1, 103, 39)
    assert view_ranks(conn, 1) == {101: 1, 102: 1, 103: 3}


def test_mid_board_tie_skips_the_next_place(conn):
    """Times 100, 110, 120, 120, 130 place 1, 2, 3, 3, 5."""
    insert_segment(conn, 2)
    for aid, t in [(201, 100), (202, 110), (203, 120), (204, 120), (205, 130)]:
        log_effort(conn, 2, aid, t)
    assert view_ranks(conn, 2) == {201: 1, 202: 2, 203: 3, 204: 3, 205: 5}


def test_tied_places_each_earn_full_points(conn):
    """Both tied leaders collect FIRST-place points; second-place points go
    unawarded; the next rider scores as 3rd. (Cat 3 pays 9/4/2/1, so a 1-1-3
    board pays 9, 9, 2.)"""
    insert_segment(conn, 3, difficulty=100.0, total_efforts=1000)  # Cat 3, depth 8
    log_effort(conn, 3, 301, 35)
    log_effort(conn, 3, 302, 35)
    log_effort(conn, 3, 303, 39)
    ranks = view_ranks(conn, 3)
    pts = {aid: scoring.points_for_rank(r, "Cat 3", 8, 100.0)
           for aid, r in ranks.items()}
    assert pts == {301: 9, 302: 9, 303: 2}


def test_changelog_ranks_are_competition_style():
    """The changelog's in-memory rank derivation must agree with the view:
    ties share a rank and the next time skips ahead."""
    state = {201: 100, 202: 110, 203: 120, 204: 120, 205: 130, 206: None}
    assert update_segments.competition_ranks(state) == {
        201: 1, 202: 2, 203: 3, 204: 3, 205: 5}
