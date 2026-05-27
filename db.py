"""SQLite schema and helpers for Kings of Shutesbury.

The database (strava.db) is the source of truth and is committed with the repo.
The list of segment IDs to track lives in the `segments` table — adding a row
(with no fetched data yet) is how you tell the pipeline to track a new segment.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "strava.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id                  INTEGER PRIMARY KEY,        -- Strava segment id
    name                TEXT,
    activity_type       TEXT,
    display_location    TEXT,
    climb_category      INTEGER,
    is_verified         INTEGER,
    distance_m          REAL,
    avg_grade           REAL,
    elev_low            REAL,
    elev_high           REAL,
    elev_gain           REAL,                       -- net gain reported by Strava
    gross_gain          REAL,                       -- summed from elevation stream
    gross_loss          REAL,                       -- summed from elevation stream
    terrain             TEXT,                        -- climb | flat | descent
    total_athletes      INTEGER,
    total_efforts       INTEGER,
    star_count          INTEGER,
    start_lat           REAL,
    start_lng           REAL,
    end_lat             REAL,
    end_lng             REAL,
    map_image_url       TEXT,
    streams_json        TEXT,                        -- {distance, elevation, location}
    difficulty          REAL,                        -- computed by scoring.py
    in_shutesbury       INTEGER,                     -- 1 if start or end is in town
    starts_in_shutesbury INTEGER,                    -- 1 if the START point is in town
    ends_in_shutesbury  INTEGER,                     -- 1 if the END point is in town
    passes_through      INTEGER,                     -- 1 if the track crosses town at all
    parent_segment_id   INTEGER,                     -- longer same-direction segment this is a slice of (sub-segment); NULL = standalone
    excluded            INTEGER DEFAULT 0,           -- 1 = manually excluded from scoring/display
    discipline          TEXT,                        -- road | gravel | mtb (manual; NULL=unset)
    fetched_at          TEXT,                        -- when the PAGE was fetched (NULL=never)
    efforts_fetched_at  TEXT                         -- when the LEADERBOARD was fetched
);

CREATE TABLE IF NOT EXISTS athletes (
    id                  INTEGER PRIMARY KEY,
    name                TEXT,
    avatar_url          TEXT,
    badge               TEXT
);

CREATE TABLE IF NOT EXISTS api_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,               -- ISO8601 UTC, when the request returned
    method              TEXT,
    endpoint            TEXT,                        -- segment_page | leaderboard_overall | ...
    url                 TEXT,
    segment_id          INTEGER,
    status              INTEGER,                     -- HTTP status (429 = rate limited)
    elapsed_ms          REAL,
    response_body       TEXT                         -- pageProps (pages) / raw JSON (leaderboard)
);
CREATE INDEX IF NOT EXISTS idx_api_log_ts ON api_log(ts);

CREATE TABLE IF NOT EXISTS efforts (
    segment_id          INTEGER NOT NULL,
    athlete_id          INTEGER NOT NULL,
    rank                INTEGER,
    elapsed_time        INTEGER,                     -- seconds
    avg_speed           REAL,
    avg_watts           REAL,
    avg_hr              REAL,
    effort_id           TEXT,
    activity_id         TEXT,
    start_date_local    TEXT,
    PRIMARY KEY (segment_id, athlete_id),
    FOREIGN KEY (segment_id) REFERENCES segments(id),
    FOREIGN KEY (athlete_id) REFERENCES athletes(id)
);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def set_in_shutesbury(conn: sqlite3.Connection, segment_id: int,
                      value: int | None) -> None:
    conn.execute("UPDATE segments SET in_shutesbury = ? WHERE id = ?",
                 (value, segment_id))


def set_geo_class(conn: sqlite3.Connection, segment_id: int, cls: dict) -> None:
    """Persist the full geo classification (start/end/pass-through) plus the
    derived `in_shutesbury` (start OR finish in town)."""
    conn.execute(
        "UPDATE segments SET starts_in_shutesbury = ?, ends_in_shutesbury = ?, "
        "passes_through = ?, in_shutesbury = ? WHERE id = ?",
        (int(cls["starts_in"]), int(cls["ends_in"]), int(cls["passes_through"]),
         int(cls["in_shutesbury"]), segment_id))
    conn.commit()


def set_parent_segment(conn: sqlite3.Connection, segment_id: int,
                       parent_id: int | None) -> None:
    """Persist the sub-segment parent (the longer same-direction segment this is
    a slice of), or NULL to clear it. Label only — does not affect scoring."""
    conn.execute("UPDATE segments SET parent_segment_id = ? WHERE id = ?",
                 (parent_id, segment_id))


def set_excluded(conn: sqlite3.Connection, segment_id: int, value: int) -> None:
    """Manually exclude (1) or re-include (0) a segment from scoring/display."""
    conn.execute("UPDATE segments SET excluded = ? WHERE id = ?",
                 (value, segment_id))
    conn.commit()


def set_efforts_fetched_at(conn: sqlite3.Connection, segment_id: int,
                           when: str) -> None:
    conn.execute("UPDATE segments SET efforts_fetched_at = ? WHERE id = ?",
                 (when, segment_id))


def add_segment_id(conn: sqlite3.Connection, segment_id: int) -> bool:
    """Register a segment id to track. Returns True if newly added."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO segments (id) VALUES (?)", (segment_id,)
    )
    conn.commit()
    return cur.rowcount > 0


def segment_ids(conn: sqlite3.Connection) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM segments ORDER BY id")]


def upsert_segment(conn: sqlite3.Connection, seg: dict) -> None:
    """Upsert parsed segment fields. `streams` is stored as JSON text."""
    seg = dict(seg)
    if "streams" in seg:
        seg["streams_json"] = json.dumps(seg.pop("streams"))
    cols = [
        "id", "name", "activity_type", "display_location", "climb_category",
        "is_verified", "distance_m", "avg_grade", "elev_low", "elev_high",
        "elev_gain", "gross_gain", "gross_loss", "terrain", "total_athletes",
        "total_efforts", "star_count", "start_lat", "start_lng", "end_lat",
        "end_lng", "map_image_url", "streams_json", "fetched_at",
    ]
    values = [seg.get(c) for c in cols]
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    conn.execute(
        f"INSERT INTO segments ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        values,
    )
    conn.commit()


def upsert_athlete(conn: sqlite3.Connection, athlete: dict) -> None:
    conn.execute(
        "INSERT INTO athletes (id, name, avatar_url, badge) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
        "avatar_url=COALESCE(excluded.avatar_url, athletes.avatar_url), "
        "badge=COALESCE(excluded.badge, athletes.badge)",
        (athlete["id"], athlete.get("name"), athlete.get("avatar_url"),
         athlete.get("badge")),
    )


def replace_efforts(conn: sqlite3.Connection, segment_id: int,
                    efforts: list[dict]) -> None:
    """Replace all stored efforts for a segment with a fresh leaderboard."""
    conn.execute("DELETE FROM efforts WHERE segment_id = ?", (segment_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO efforts (segment_id, athlete_id, rank, "
        "elapsed_time, avg_speed, avg_watts, avg_hr, effort_id, activity_id, "
        "start_date_local) VALUES (:segment_id, :athlete_id, :rank, "
        ":elapsed_time, :avg_speed, :avg_watts, :avg_hr, :effort_id, "
        ":activity_id, :start_date_local)",
        [{"segment_id": segment_id, **e} for e in efforts],
    )
    conn.commit()


def log_api_request(conn: sqlite3.Connection, ts: str, method: str,
                    endpoint: str, url: str, segment_id: int | None,
                    status: int, elapsed_ms: float,
                    response_body: str | None = None) -> None:
    """Record one API request (with its payload). Committed immediately so the
    log survives a crash or a hard stop on a rate limit."""
    conn.execute(
        "INSERT INTO api_log (ts, method, endpoint, url, segment_id, status, "
        "elapsed_ms, response_body) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, method, endpoint, url, segment_id, status, elapsed_ms, response_body))
    conn.commit()


def set_difficulty(conn: sqlite3.Connection, segment_id: int,
                   difficulty: float) -> None:
    conn.execute("UPDATE segments SET difficulty = ? WHERE id = ?",
                 (difficulty, segment_id))
    conn.commit()
