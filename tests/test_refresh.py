"""`_refresh_one` is the heart of update/--background. Covers:
  - efforts get persisted (proving ORM auto-commit is in effect)
  - efforts_fetched_at lands (the bug we fixed)
  - last_depth_pages high-water-mark logic
  - 429 → backoff escalates; success after a 429 resets
  - ladder exhaustion sets `ratelimit-exhausted`
"""

from __future__ import annotations

import pytest

from segments import pipeline
from segments.models import EffortLog, Segment
from segments.strava import AuthError, RateLimitError

from conftest import FakeStravaClient, effort

pytestmark = pytest.mark.django_db


def test_success_persists_efforts_across_connections(seed_segment):
    """The bug we fixed: set_efforts_fetched_at didn't commit, so the UPDATE
    was rolled back at conn.close(). With Django ORM auto-commit, we verify
    that _refresh_one persisted by re-querying via the ORM after the call."""
    seed_segment(1, efforts_fetched_at=None)
    client = FakeStravaClient(
        overall={1: [effort(100, 60, rank=1), effort(101, 65, rank=2)]},
        following={1: []})
    assert pipeline._refresh_one(client, 1, depth=1) is True

    seg = Segment.objects.get(pk=1)
    assert seg.efforts_fetched_at is not None, \
        "efforts_fetched_at must be persisted — needs an explicit save/update"
    n_efforts = EffortLog.objects.filter(segment_id=1).count()
    assert n_efforts == 2


def test_following_supplement_appended(seed_segment):
    """Andy/Owen-style athletes outside top 25 come in via the following board
    with rank=None, then the picker/changelog re-derive their rank from time."""
    seed_segment(1)
    client = FakeStravaClient(
        overall={1: [effort(100, 60, rank=1)]},
        following={1: [effort(next(iter(pipeline.TRACKED_ATHLETES)),
                              200, rank=12)]})
    assert pipeline._refresh_one(client, 1, depth=1) is True
    # The following row should be stored even with rank=None semantics:
    # _supplement_following rewrites rank to None before storing.
    n = EffortLog.objects.filter(segment_id=1).count()
    assert n == 2


def test_depth_one_does_not_bump_high_water(seed_segment):
    """Phase A refreshes (depth=1) should not touch last_depth_pages — if a
    depth-1 refresh ran after a depth=6 dive, we don't want to lose the
    high-water mark."""
    seed_segment(1, last_depth_pages=6)
    client = FakeStravaClient(overall={1: [effort(100, 60)]}, following={1: []})
    pipeline._refresh_one(client, 1, depth=1)
    seg = Segment.objects.get(pk=1)
    assert seg.last_depth_pages == 6


def test_depth_gt_one_bumps_high_water(seed_segment):
    seed_segment(1, last_depth_pages=2)
    client = FakeStravaClient(overall={1: [effort(100, 60)]}, following={1: []})
    pipeline._refresh_one(client, 1, depth=4)
    seg = Segment.objects.get(pk=1)
    assert seg.last_depth_pages == 4


def test_partial_fetch_starts_at_from_page(seed_segment):
    """Depth-building ticks pull only the unseen page: from_page reaches the
    client on the overall fetch, while following/women's stay full fetches."""
    seed_segment(1, last_depth_pages=3)
    client = FakeStravaClient(overall={1: [effort(100, 60)]}, following={1: []})
    assert pipeline._refresh_one(client, 1, depth=4, from_page=4) is True
    assert (1, "overall", 4, 4) in client.calls
    seg = Segment.objects.get(pk=1)
    assert seg.last_depth_pages == 4


def test_partial_fetch_does_not_stamp_efforts_fetched_at(seed_segment):
    """A from_page>1 fetch never touched the top-25, so it must not refresh
    the phase-A staleness stamp — but its efforts still land in the log."""
    seed_segment(1, efforts_fetched_at="2026-01-01T00:00:00+00:00",
                 last_depth_pages=3)
    client = FakeStravaClient(overall={1: [effort(100, 60)]}, following={1: []})
    assert pipeline._refresh_one(client, 1, depth=4, from_page=4) is True
    seg = Segment.objects.get(pk=1)
    assert seg.efforts_fetched_at == "2026-01-01T00:00:00+00:00"
    assert EffortLog.objects.filter(segment_id=1).count() == 1


def test_bump_never_shrinks(seed_segment):
    """`bump_depth_pages` is supposed to be a HIGH-WATER mark — a smaller
    value must not overwrite a larger one."""
    seed_segment(1, last_depth_pages=8)
    pipeline.bump_depth_pages(1, 3)
    seg = Segment.objects.get(pk=1)
    assert seg.last_depth_pages == 8


# === 429 / backoff interaction ===============================================


def test_rate_limit_escalates_backoff(seed_segment):
    seed_segment(1)
    client = FakeStravaClient(raises=[RateLimitError("simulated")])
    assert pipeline._refresh_one(client, 1, depth=1) is False
    assert pipeline._last_stop == "ratelimit"
    level, until = pipeline._backoff_state()
    assert level == 1
    assert until is not None


def test_success_resets_existing_backoff(seed_segment):
    seed_segment(1)
    # Pretend we'd already escalated 3 steps.
    for _ in range(3):
        pipeline._backoff_escalate()
    assert pipeline._backoff_state()[0] == 3

    client = FakeStravaClient(overall={1: [effort(100, 60)]}, following={1: []})
    assert pipeline._refresh_one(client, 1, depth=1) is True
    level, until = pipeline._backoff_state()
    assert level == 0
    assert until is None


def test_rate_limit_at_max_marks_exhausted(seed_segment):
    seed_segment(1)
    for _ in pipeline.BACKOFF_LADDER_HOURS:
        pipeline._backoff_escalate()
    client = FakeStravaClient(raises=[RateLimitError("still limited")])
    pipeline._refresh_one(client, 1, depth=1)
    assert pipeline._last_stop == "ratelimit-exhausted"


# === auth error ==============================================================


def test_auth_error_stops_without_touching_backoff(seed_segment):
    """A 401/403 is a cookie-expired problem, not a rate limit — don't bump
    the ladder, just signal stop."""
    seed_segment(1)
    client = FakeStravaClient(raises=[AuthError("cookie expired")])
    assert pipeline._refresh_one(client, 1, depth=1) is False
    assert pipeline._last_stop == "auth"
    level, _ = pipeline._backoff_state()
    assert level == 0
