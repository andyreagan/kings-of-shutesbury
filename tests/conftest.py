"""Shared fixtures: a fresh test DB and a fake StravaClient."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import db
from strava import RateLimitError, StravaError    # noqa: F401  (re-exported for tests)


# === DB fixtures =============================================================


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_path: Path) -> sqlite3.Connection:
    """A fresh SQLite DB with the live schema (incl. migrations) applied."""
    c = db.connect(db_path)
    db.init(c)
    yield c
    c.close()


# === seed helpers ============================================================


def insert_segment(conn: sqlite3.Connection, seg_id: int, **overrides) -> None:
    """Insert a fully-imported in-town ride segment. Tests override specific
    columns; everything else gets a plausible default so the in-town/ride/
    not-excluded filter passes."""
    defaults = {
        "name": f"Segment {seg_id}",
        "activity_type": "Ride",
        "in_town": 1,
        "excluded": 0,
        "fetched_at": "2026-06-01T00:00:00+00:00",
        "efforts_fetched_at": None,
        "last_depth_pages": None,
        "total_athletes": 200,
        "total_efforts": 1000,
        "terrain": "climb",
        "distance_m": 1000.0,
        "avg_grade": 5.0,
        "gross_gain": 50.0,
        "gross_loss": 0.0,
        "difficulty": 100.0,
        "discipline": "road",
    }
    cols = {**defaults, **overrides}
    placeholders = ", ".join("?" * (len(cols) + 1))
    names = "id, " + ", ".join(cols)
    conn.execute(f"INSERT INTO segments ({names}) VALUES ({placeholders})",
                 (seg_id, *cols.values()))
    conn.commit()


@pytest.fixture
def seed_segment(conn: sqlite3.Connection):
    """Returns a callable to insert a test segment row."""
    def _seed(seg_id: int, **kw):
        insert_segment(conn, seg_id, **kw)
    return _seed


# === fake Strava client ======================================================


def effort(athlete_id: int, elapsed_time: int, *,
           name: str | None = None, effort_id: str | None = None,
           rank: int | None = None) -> dict:
    """One leaderboard row in the shape `_effort_from_row` produces."""
    return {
        "athlete_id": athlete_id,
        "athlete_name": name or f"Athlete {athlete_id}",
        "avatar_url": None,
        "badge": None,
        "rank": rank,
        "elapsed_time": elapsed_time,
        "avg_speed": None,
        "avg_watts": None,
        "avg_hr": None,
        "effort_id": effort_id or f"e{athlete_id}-{elapsed_time}",
        "activity_id": f"a{athlete_id}",
        "start_date_local": "2026-05-01T10:00:00",
    }


class FakeStravaClient:
    """Drop-in for StravaClient — implements only what _refresh_one uses.

    Configure with `.overall` / `.following` dicts keyed by segment id. Optional
    `raises` is a list of exceptions consumed in order on each fetch_leaderboard
    call; pass `None` in a slot to let that call succeed normally.
    """

    def __init__(self,
                 overall: dict[int, list[dict]] | None = None,
                 following: dict[int, list[dict]] | None = None,
                 raises: list[Exception | None] | None = None):
        self.overall = overall or {}
        self.following = following or {}
        self._raises = list(raises or [])
        self.calls: list[tuple[int, str, int]] = []

    def fetch_leaderboard(self, sid: int, filter_type: str = "overall",
                          pages: int = 1) -> list[dict]:
        self.calls.append((sid, filter_type, pages))
        if self._raises:
            err = self._raises.pop(0)
            if err is not None:
                raise err
        bucket = self.following if filter_type == "following" else self.overall
        return list(bucket.get(sid, []))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_client():
    return FakeStravaClient


# === time helpers ============================================================


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
