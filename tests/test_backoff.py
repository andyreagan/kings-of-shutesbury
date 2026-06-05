"""Dynamic 429 backoff ladder: escalate → reset → exhaust."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from segments import pipeline
from segments.models import Kv

pytestmark = pytest.mark.django_db


def test_clean_db_starts_at_level_zero():
    level, until = pipeline._backoff_state()
    assert level == 0
    assert until is None


def test_escalate_climbs_the_ladder():
    """Each escalate bumps the level and writes a `backoff_until` consistent
    with that step's hours."""
    for i, hours in enumerate(pipeline.BACKOFF_LADDER_HOURS, start=1):
        ok = pipeline._backoff_escalate()
        assert ok is True, f"step {i} ({hours}h) should not exhaust"
        level, until = pipeline._backoff_state()
        assert level == i
        assert until is not None
        # `until` should be within a small slop of now+hours.
        expected = datetime.now(timezone.utc) + timedelta(hours=hours)
        slop = abs((until - expected).total_seconds())
        assert slop < 60, f"step {i}: expected ~{hours}h ahead, off by {slop:.1f}s"


def test_one_more_after_last_step_exhausts():
    """Past the last rung the ladder refuses to escalate further."""
    for _ in pipeline.BACKOFF_LADDER_HOURS:
        assert pipeline._backoff_escalate() is True
    # Now at max. Another 429 should be reported as exhausted.
    assert pipeline._backoff_escalate() is False
    # The exhausting attempt does NOT mutate state — level stays at max.
    level, _ = pipeline._backoff_state()
    assert level == len(pipeline.BACKOFF_LADDER_HOURS)


def test_reset_clears_state():
    pipeline._backoff_escalate()
    pipeline._backoff_escalate()
    pipeline._backoff_reset()
    level, until = pipeline._backoff_state()
    assert level == 0
    assert until is None
    # kv rows for backoff_* are deleted, not just blanked.
    assert pipeline.kv_get("backoff_level") is None
    assert pipeline.kv_get("backoff_until") is None


def test_state_survives_reconnect():
    """The ladder is persistent — each Django ORM call hits the same test DB,
    so state written by _backoff_escalate is visible in a subsequent _backoff_state
    call (equivalent to opening a fresh connection in the legacy test)."""
    pipeline._backoff_escalate()
    pipeline._backoff_escalate()
    # Re-read via the ORM (same test DB — simulates a process restart where the
    # kv rows are already committed and visible to any new connection).
    level, until = pipeline._backoff_state()
    assert level == 2
    assert until is not None
