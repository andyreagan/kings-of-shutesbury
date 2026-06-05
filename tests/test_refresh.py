"""`_refresh_one` is the heart of update/--background. Covers:
  - efforts get persisted (and via a fresh connection — proving COMMIT)
  - efforts_fetched_at lands (the bug we fixed)
  - last_depth_pages high-water-mark logic
  - 429 → backoff escalates; success after a 429 resets
  - ladder exhaustion sets `ratelimit-exhausted`
"""

from __future__ import annotations

import db
import update_segments as us
from strava import AuthError, RateLimitError

from conftest import FakeStravaClient, effort


def test_success_persists_efforts_across_connections(conn, db_path, seed_segment):
    """The bug we fixed: set_efforts_fetched_at didn't commit, so the UPDATE
    was rolled back at conn.close(). Verify by re-reading via a fresh
    connection — only committed rows are visible there."""
    seed_segment(1, efforts_fetched_at=None)
    client = FakeStravaClient(
        overall={1: [effort(100, 60, rank=1), effort(101, 65, rank=2)]},
        following={1: []})
    assert us._refresh_one(conn, client, 1, depth=1) is True
    conn.close()

    c2 = db.connect(db_path)
    row = c2.execute(
        "SELECT efforts_fetched_at FROM segments WHERE id = 1").fetchone()
    assert row["efforts_fetched_at"] is not None, \
        "efforts_fetched_at must survive conn.close() — needs an explicit commit"
    n_efforts = c2.execute(
        "SELECT COUNT(*) AS n FROM efforts WHERE segment_id = 1").fetchone()["n"]
    assert n_efforts == 2
    c2.close()


def test_following_supplement_appended(conn, seed_segment):
    """Andy/Owen-style athletes outside top 25 come in via the following board
    with rank=None, then the picker/changelog re-derive their rank from time."""
    seed_segment(1)
    client = FakeStravaClient(
        overall={1: [effort(100, 60, rank=1)]},
        following={1: [effort(us.TRACKED_ATHLETES.__iter__().__next__(),
                              200, rank=12)]})
    assert us._refresh_one(conn, client, 1, depth=1) is True
    # The following row should be stored even with rank=None semantics:
    # _supplement_following rewrites rank to None before storing.
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM efforts WHERE segment_id = 1").fetchone()["n"]
    assert n == 2


def test_depth_one_does_not_bump_high_water(conn, seed_segment):
    """Phase A refreshes (depth=1) should not touch last_depth_pages — if a
    depth-1 refresh ran after a depth=6 dive, we don't want to lose the
    high-water mark."""
    seed_segment(1, last_depth_pages=6)
    client = FakeStravaClient(overall={1: [effort(100, 60)]}, following={1: []})
    us._refresh_one(conn, client, 1, depth=1)
    row = conn.execute(
        "SELECT last_depth_pages FROM segments WHERE id = 1").fetchone()
    assert row["last_depth_pages"] == 6


def test_depth_gt_one_bumps_high_water(conn, seed_segment):
    seed_segment(1, last_depth_pages=2)
    client = FakeStravaClient(overall={1: [effort(100, 60)]}, following={1: []})
    us._refresh_one(conn, client, 1, depth=4)
    row = conn.execute(
        "SELECT last_depth_pages FROM segments WHERE id = 1").fetchone()
    assert row["last_depth_pages"] == 4


def test_bump_never_shrinks(conn, seed_segment):
    """`db.bump_depth_pages` is supposed to be a HIGH-WATER mark — a smaller
    value must not overwrite a larger one."""
    seed_segment(1, last_depth_pages=8)
    db.bump_depth_pages(conn, 1, 3)
    conn.commit()
    row = conn.execute(
        "SELECT last_depth_pages FROM segments WHERE id = 1").fetchone()
    assert row["last_depth_pages"] == 8


# === 429 / backoff interaction ===============================================


def test_rate_limit_escalates_backoff(conn, seed_segment):
    seed_segment(1)
    client = FakeStravaClient(raises=[RateLimitError("simulated")])
    assert us._refresh_one(conn, client, 1, depth=1) is False
    assert us._last_stop == "ratelimit"
    level, until = us._backoff_state(conn)
    assert level == 1
    assert until is not None


def test_success_resets_existing_backoff(conn, seed_segment):
    seed_segment(1)
    # Pretend we'd already escalated 3 steps.
    for _ in range(3):
        us._backoff_escalate(conn)
    assert us._backoff_state(conn)[0] == 3

    client = FakeStravaClient(overall={1: [effort(100, 60)]}, following={1: []})
    assert us._refresh_one(conn, client, 1, depth=1) is True
    level, until = us._backoff_state(conn)
    assert level == 0
    assert until is None


def test_rate_limit_at_max_marks_exhausted(conn, seed_segment):
    seed_segment(1)
    for _ in us.BACKOFF_LADDER_HOURS:
        us._backoff_escalate(conn)
    client = FakeStravaClient(raises=[RateLimitError("still limited")])
    us._refresh_one(conn, client, 1, depth=1)
    assert us._last_stop == "ratelimit-exhausted"


# === auth error ==============================================================


def test_auth_error_stops_without_touching_backoff(conn, seed_segment):
    """A 401/403 is a cookie-expired problem, not a rate limit — don't bump
    the ladder, just signal stop."""
    seed_segment(1)
    client = FakeStravaClient(raises=[AuthError("cookie expired")])
    assert us._refresh_one(conn, client, 1, depth=1) is False
    assert us._last_stop == "auth"
    level, _ = us._backoff_state(conn)
    assert level == 0
