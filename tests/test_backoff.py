"""Dynamic 429 backoff ladder: escalate → reset → exhaust."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db
import update_segments as us


def test_clean_db_starts_at_level_zero(conn):
    level, until = us._backoff_state(conn)
    assert level == 0
    assert until is None


def test_escalate_climbs_the_ladder(conn):
    """Each escalate bumps the level and writes a `backoff_until` consistent
    with that step's hours."""
    for i, hours in enumerate(us.BACKOFF_LADDER_HOURS, start=1):
        ok = us._backoff_escalate(conn)
        assert ok is True, f"step {i} ({hours}h) should not exhaust"
        level, until = us._backoff_state(conn)
        assert level == i
        assert until is not None
        # `until` should be within a small slop of now+hours.
        expected = datetime.now(timezone.utc) + timedelta(hours=hours)
        slop = abs((until - expected).total_seconds())
        assert slop < 60, f"step {i}: expected ~{hours}h ahead, off by {slop:.1f}s"


def test_one_more_after_last_step_exhausts(conn):
    """Past the last rung the ladder refuses to escalate further."""
    for _ in us.BACKOFF_LADDER_HOURS:
        assert us._backoff_escalate(conn) is True
    # Now at max. Another 429 should be reported as exhausted.
    assert us._backoff_escalate(conn) is False
    # The exhausting attempt does NOT mutate state — level stays at max.
    level, _ = us._backoff_state(conn)
    assert level == len(us.BACKOFF_LADDER_HOURS)


def test_reset_clears_state(conn):
    us._backoff_escalate(conn)
    us._backoff_escalate(conn)
    us._backoff_reset(conn)
    level, until = us._backoff_state(conn)
    assert level == 0
    assert until is None
    # kv rows for backoff_* are deleted, not just blanked.
    assert db.kv_get(conn, "backoff_level") is None
    assert db.kv_get(conn, "backoff_until") is None


def test_state_survives_reconnect(db_path):
    """The ladder is persistent — a process restart shouldn't lose level."""
    c1 = db.connect(db_path)
    db.init(c1)
    us._backoff_escalate(c1)
    us._backoff_escalate(c1)
    c1.close()

    c2 = db.connect(db_path)
    level, until = us._backoff_state(c2)
    assert level == 2
    assert until is not None
    c2.close()
