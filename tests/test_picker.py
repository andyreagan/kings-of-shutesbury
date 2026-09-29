"""Background scheduler: fresh (date-window pulls) and walk (full re-walk)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from segments import pipeline
from segments.models import Athlete, EffortLog, Segment
from segments.strava import RateLimitError

from conftest import FakeStravaClient, effort

pytestmark = pytest.mark.django_db

# A mid-month "now" so this_month is reachable without calendar edge cases.
AT = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _days_before(days: float, at: datetime = AT) -> str:
    return _iso(at - timedelta(days=days))


def _riders(sid: int, n: int) -> None:
    """Log `n` athletes who rode `sid` within the last year."""
    rows = [{**effort(sid * 1000 + i, 100 + i),
             "start_date_local": "2026-06-01T10:00:00Z"} for i in range(n)]
    pipeline._store_efforts(sid, rows, "2026-06-02T00:00:00+00:00")


def _fresh_job(sid: int, **kw) -> dict:
    return {"kind": "fresh", "sid": sid, "window": "this_month",
            "started_at": _iso(AT), "board": "overall", "page": 1, "new": 0,
            **kw}


def _walk_job(sid: int, **kw) -> dict:
    return {"kind": "walk", "sid": sid, "started_at": _iso(AT),
            "board": "overall", "page": 1, **kw}


def test_empty_db_has_no_jobs():
    assert pipeline._next_fresh_job(AT) is None
    assert pipeline._next_walk_job() is None


# === window choice ===========================================================


def test_window_never_pulled_is_this_year():
    assert pipeline._delta_window(None, AT) == "this_year"


def test_window_is_this_month_once_last_month_is_closed():
    assert pipeline._delta_window("2026-09-06T00:00:00+00:00", AT) == "this_month"


def test_window_one_this_year_pull_closes_out_last_month():
    """A this_year pull on Sep 3 came too early to see August's late uploads,
    so one more is owed; the pull on Sep 6 settles it."""
    assert pipeline._delta_window("2026-09-03T00:00:00+00:00", AT) == "this_year"


def test_window_stays_this_month_until_last_month_can_be_closed():
    early = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    assert pipeline._delta_window("2026-08-06T00:00:00+00:00", early) == "this_month"


def test_window_after_a_long_gap_is_this_year():
    assert pipeline._delta_window("2026-07-15T00:00:00+00:00", AT) == "this_year"


def test_interval_scales_with_activity():
    assert pipeline._delta_interval_days(0) == pipeline.DELTA_MAX_DAYS
    assert pipeline._delta_interval_days(45) == 4
    assert pipeline._delta_interval_days(500) == pipeline.DELTA_MIN_DAYS


# === fresh pick ==============================================================


def test_fresh_never_pulled_first_busiest_first(seed_segment):
    seed_segment(1, delta_fetched_at=_days_before(30))
    seed_segment(2)
    seed_segment(3)
    _riders(3, 5)
    job = pipeline._next_fresh_job(AT)
    assert job["sid"] == 3
    assert (job["window"], job["board"], job["page"]) == ("this_year", "overall", 1)


def test_fresh_picks_most_overdue(seed_segment):
    """Overdue is age relative to each segment's own interval: a busy board
    3 days stale outranks a quiet one 8 days stale."""
    seed_segment(1, delta_fetched_at=_days_before(8))      # quiet: 8/7
    seed_segment(2, delta_fetched_at=_days_before(3))      # busy:  3/2
    _riders(2, 90)
    assert pipeline._next_fresh_job(AT)["sid"] == 2


def test_fresh_none_when_nothing_due(seed_segment):
    seed_segment(1, delta_fetched_at=_days_before(1))
    assert pipeline._next_fresh_job(AT) is None


def test_fresh_continues_pull_in_progress(seed_segment):
    cursor = {"window": "this_year", "started_at": _days_before(0.1),
              "board": "overall", "page": 3, "new": 4}
    seed_segment(1, delta_cursor=json.dumps(cursor))
    seed_segment(2)                                        # never pulled
    job = pipeline._next_fresh_job(AT)
    assert job == {"kind": "fresh", "sid": 1, **cursor}


def test_fresh_drops_cursor_when_month_rolled_over(seed_segment):
    cursor = {"window": "this_month", "started_at": "2026-08-31T23:50:00+00:00",
              "board": "overall", "page": 2, "new": 1}
    seed_segment(1, delta_cursor=json.dumps(cursor))
    job = pipeline._next_fresh_job(AT)
    assert (job["sid"], job["page"], job["window"]) == (1, 1, "this_year")
    assert Segment.objects.get(pk=1).delta_cursor is None


# === walk pick ===============================================================


def test_walk_continues_walk_in_progress(seed_segment):
    seed_segment(1, walk_cursor=json.dumps(
        {"started_at": _iso(AT), "board": "female", "page": 2}))
    seed_segment(2)
    job = pipeline._next_walk_job()
    assert (job["sid"], job["board"], job["page"]) == (1, "female", 2)


def test_walk_never_walked_first_smallest_first(seed_segment):
    seed_segment(1, walk_completed_at=_days_before(40))
    seed_segment(2, total_athletes=900)
    seed_segment(3, total_athletes=30)
    job = pipeline._next_walk_job()
    assert (job["sid"], job["board"], job["page"]) == (3, "overall", 1)


def test_walk_picks_oldest_completed(seed_segment):
    seed_segment(1, walk_completed_at=_days_before(5))
    seed_segment(2, walk_completed_at=_days_before(20))
    assert pipeline._next_walk_job()["sid"] == 2


# === filter correctness ======================================================


@pytest.mark.parametrize("skip", [
    {"excluded": 1}, {"activity_type": "Run"}, {"in_town": 0},
    {"fetched_at": None}])
def test_jobs_skip_segments_that_dont_score(seed_segment, skip):
    seed_segment(1, **skip)
    seed_segment(2)
    assert pipeline._next_fresh_job(AT)["sid"] == 2
    assert pipeline._next_walk_job()["sid"] == 2


# === running a fresh page ====================================================


def test_fresh_logs_only_improvements(seed_segment):
    """A window lists each rider's best ride inside it. Only new athletes and
    faster times are news; a slower ride by a known athlete is not."""
    seed_segment(1)
    pipeline._store_efforts(1, [effort(100, 60), effort(101, 70)],
                            "2026-06-02T00:00:00+00:00")
    client = FakeStravaClient()
    client.boards[(1, "overall", "this_month")] = [
        effort(101, 65, effort_id="pr"),       # PR
        effort(100, 80, effort_id="slow"),     # slower than stored 60
        effort(102, 90, effort_id="new"),      # new athlete
    ]
    assert pipeline._run_job(client, _fresh_job(1)) is True
    logged = set(EffortLog.objects.filter(segment_id=1)
                 .values_list("effort_id", flat=True))
    assert {"pr", "new"} <= logged
    assert "slow" not in logged


def test_fresh_quiet_board_completes_in_one_request(seed_segment):
    seed_segment(1)
    client = FakeStravaClient()
    assert pipeline._run_job(client, _fresh_job(1)) is True
    seg = Segment.objects.get(pk=1)
    assert seg.delta_cursor is None
    assert seg.delta_fetched_at == _iso(AT)       # the pull's START time
    assert seg.delta_year_fetched_at is None      # a this_month pull
    assert seg.efforts_fetched_at is not None
    assert client.page_calls == [(1, "overall", "this_month", 1)]


def test_fresh_this_year_pull_records_the_close_out(seed_segment):
    seed_segment(1)
    pipeline._run_job(FakeStravaClient(), _fresh_job(1, window="this_year"))
    assert Segment.objects.get(pk=1).delta_year_fetched_at == _iso(AT)


def test_fresh_new_rides_queue_the_womens_board(seed_segment):
    seed_segment(1)
    client = FakeStravaClient()
    rides = [effort(100, 60), effort(101, 70)]
    client.boards[(1, "overall", "this_month")] = rides
    client.boards[(1, "female", "this_month")] = [rides[1]]
    pipeline._run_job(client, _fresh_job(1))
    job = pipeline._next_fresh_job(AT)
    assert (job["board"], job["page"], job["new"]) == ("female", 1, 2)
    assert Segment.objects.get(pk=1).delta_fetched_at is None   # not done yet

    pipeline._run_job(client, job)
    seg = Segment.objects.get(pk=1)
    assert seg.delta_cursor is None and seg.delta_fetched_at == _iso(AT)
    assert Athlete.objects.get(pk=101).gender == "F"
    assert Athlete.objects.get(pk=100).gender == "M"
    assert EffortLog.objects.get(segment_id=1, athlete_id=101).gender == "F"


def test_fresh_womens_board_tags_riders_whose_ride_wasnt_logged(seed_segment):
    seed_segment(1)
    pipeline._store_efforts(1, [effort(100, 60)], "2026-06-02T00:00:00+00:00")
    client = FakeStravaClient()
    client.boards[(1, "female", "this_month")] = [
        effort(100, 80, effort_id="slow")]
    pipeline._run_job(client, _fresh_job(1, board="female", new=1))
    assert Athlete.objects.get(pk=100).gender == "F"
    assert EffortLog.objects.filter(segment_id=1).count() == 1


def test_fresh_walks_every_page_of_a_big_window(seed_segment):
    seed_segment(1)
    client = FakeStravaClient()
    client.boards[(1, "overall", "this_month")] = [
        effort(100 + i, 60 + i) for i in range(30)]
    pipeline._run_job(client, _fresh_job(1))
    job = pipeline._next_fresh_job(AT)
    assert (job["board"], job["page"], job["new"]) == ("overall", 2, 25)
    pipeline._run_job(client, job)
    assert pipeline._next_fresh_job(AT)["board"] == "female"
    assert EffortLog.objects.filter(segment_id=1).count() == 30


# === running a walk page =====================================================


def test_walk_covers_overall_then_women_then_completes(seed_segment):
    seed_segment(1, total_athletes=10, last_depth_pages=1)
    client = FakeStravaClient()
    board = [effort(100 + i, 60 + i) for i in range(30)]
    client.boards[(1, "overall", "all_time")] = board
    client.boards[(1, "female", "all_time")] = board[:2]

    job = pipeline._next_walk_job()
    while job["sid"] == 1 and Segment.objects.get(pk=1).walk_completed_at is None:
        assert pipeline._run_job(client, job) is True
        job = pipeline._next_walk_job()

    assert client.page_calls == [(1, "overall", "all_time", 1),
                                 (1, "overall", "all_time", 2),
                                 (1, "female", "all_time", 1)]
    seg = Segment.objects.get(pk=1)
    assert seg.walk_cursor is None and seg.walk_completed_at is not None
    assert seg.total_athletes == 30          # refreshed from Strava's count
    assert seg.last_depth_pages == 2
    assert seg.efforts_fetched_at is not None
    assert EffortLog.objects.filter(segment_id=1).count() == 30
    assert Athlete.objects.filter(gender="F").count() == 2


def test_walk_logs_slower_known_efforts_too(seed_segment):
    """The all-time board is the truth as Strava shows it — no filtering."""
    seed_segment(1)
    pipeline._store_efforts(1, [effort(100, 60)], "2026-06-02T00:00:00+00:00")
    client = FakeStravaClient()
    client.boards[(1, "overall", "all_time")] = [
        effort(100, 80, effort_id="other")]
    pipeline._run_job(client, _walk_job(1))
    assert EffortLog.objects.filter(segment_id=1).count() == 2


# === failures ================================================================


def test_rate_limit_keeps_cursor_and_escalates(seed_segment):
    cursor = {"started_at": _iso(AT), "board": "overall", "page": 4}
    seed_segment(1, walk_cursor=json.dumps(cursor))
    client = FakeStravaClient(raises=[RateLimitError("429")])
    assert pipeline._run_job(client, pipeline._next_walk_job()) is False
    assert pipeline._last_stop == "ratelimit"
    assert json.loads(Segment.objects.get(pk=1).walk_cursor) == cursor
    assert pipeline.kv_get("backoff_level") == "1"


# === the tick's request budget ===============================================


@pytest.fixture
def tick(monkeypatch):
    """Run one tick against a FakeStravaClient; returns its page_calls."""
    def _tick(client: FakeStravaClient) -> list:
        monkeypatch.setattr(pipeline, "StravaClient", lambda: client)
        pipeline.background_tick(no_jitter=True)
        return client.page_calls
    return _tick


def _windows(calls: list) -> list[str]:
    return [c[2] for c in calls]


def test_tick_reserves_a_request_for_the_walk(seed_segment, tick):
    """However long the fresh queue, depth still advances every tick."""
    for sid in range(1, 6):
        seed_segment(sid)                       # all never pulled → all due
    assert _windows(tick(FakeStravaClient())) == [
        "this_year", "this_year", "all_time"]


def test_tick_spends_everything_on_the_walk_when_nothing_is_due(
        seed_segment, tick):
    seed_segment(1, delta_fetched_at=pipeline.now(), total_athletes=100)
    client = FakeStravaClient()
    client.boards[(1, "overall", "all_time")] = [
        effort(100 + i, 60 + i) for i in range(100)]
    assert tick(client) == [(1, "overall", "all_time", 1),
                            (1, "overall", "all_time", 2),
                            (1, "overall", "all_time", 3)]


def test_tick_stops_on_first_failure(seed_segment, tick):
    seed_segment(1)
    client = FakeStravaClient(raises=[RateLimitError("429")])
    assert len(tick(client)) == 1


def test_tick_skips_while_backed_off(seed_segment, tick):
    seed_segment(1)
    pipeline.kv_set("backoff_level", "1")
    pipeline.kv_set("backoff_until", _iso(
        datetime.now(timezone.utc) + timedelta(hours=1)))
    assert tick(FakeStravaClient()) == []
