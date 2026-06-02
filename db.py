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
    in_town             INTEGER,                     -- 1 if start or end is in town
    starts_in_town      INTEGER,                     -- 1 if the START point is in town
    ends_in_town        INTEGER,                     -- 1 if the END point is in town
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

-- Single source of truth for efforts: an append-only log with ONE row per
-- distinct observed effort, identified by (segment, athlete, effort_id). A
-- leaderboard shows each athlete's PR effort; when they PR again the effort_id
-- changes, so a new row is logged. `observed_at` is when WE first saw that
-- effort (the rewind key). We never delete and never mutate the identity, so an
-- athlete bumped past the fetched depth — or whose PR is superseded — keeps
-- their history. The canonical "best per athlete" + derived rank are NOT stored;
-- they're computed on demand by the `efforts` view below.
CREATE TABLE IF NOT EXISTS effort_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id          INTEGER NOT NULL,
    athlete_id          INTEGER NOT NULL,
    elapsed_time        INTEGER,                     -- seconds, as observed
    avg_speed           REAL,
    avg_watts           REAL,
    avg_hr              REAL,
    effort_id           TEXT,                        -- the attempt's unique id (dedup key)
    activity_id         TEXT,
    start_date_local    TEXT,                        -- when the ride happened (Strava)
    observed_at         TEXT NOT NULL,               -- when WE first logged it (rewind key)
    UNIQUE (segment_id, athlete_id, effort_id),
    FOREIGN KEY (segment_id) REFERENCES segments(id),
    FOREIGN KEY (athlete_id) REFERENCES athletes(id)
);
CREATE INDEX IF NOT EXISTS idx_effort_log_seg ON effort_log(segment_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_effort_log_athlete ON effort_log(segment_id, athlete_id);

-- Canonical best-known effort per (segment, athlete), derived from the log: each
-- athlete's fastest logged time, with `rank` (1 = fastest) derived over the whole
-- segment so a newly-seen faster rider pushes everyone down automatically.
-- `first_seen`/`last_seen` are the min/max observed_at across that athlete's
-- logged efforts. This view replaces the old physical `efforts` table; reads are
-- unchanged (`SELECT ... FROM efforts`).
CREATE VIEW IF NOT EXISTS efforts AS
WITH best AS (
    SELECT segment_id, athlete_id, elapsed_time, avg_speed, avg_watts, avg_hr,
           effort_id, activity_id, start_date_local,
           ROW_NUMBER() OVER (
               PARTITION BY segment_id, athlete_id
               ORDER BY (elapsed_time IS NULL), elapsed_time ASC,
                        start_date_local ASC, id ASC) AS _rn
    FROM effort_log
),
seen AS (
    SELECT segment_id, athlete_id,
           MIN(observed_at) AS first_seen,
           MAX(observed_at) AS last_seen
    FROM effort_log GROUP BY segment_id, athlete_id
)
SELECT
    b.segment_id, b.athlete_id,
    CASE WHEN b.elapsed_time IS NULL THEN NULL ELSE ROW_NUMBER() OVER (
        PARTITION BY b.segment_id, (b.elapsed_time IS NULL)
        ORDER BY b.elapsed_time ASC, b.start_date_local ASC) END AS rank,
    b.elapsed_time, b.avg_speed, b.avg_watts, b.avg_hr,
    b.effort_id, b.activity_id, b.start_date_local,
    s.first_seen, s.last_seen
FROM best b
JOIN seen s ON s.segment_id = b.segment_id AND s.athlete_id = b.athlete_id
WHERE b._rn = 1;
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def set_geo_class(conn: sqlite3.Connection, segment_id: int, cls: dict) -> None:
    """Persist the full geo classification (start/end/pass-through) plus the
    derived `in_town` (start OR finish in town)."""
    conn.execute(
        "UPDATE segments SET starts_in_town = ?, ends_in_town = ?, "
        "passes_through = ?, in_town = ? WHERE id = ?",
        (int(cls["starts_in"]), int(cls["ends_in"]), int(cls["passes_through"]),
         int(cls["in_town"]), segment_id))
    conn.commit()


def set_parent_segment(conn: sqlite3.Connection, segment_id: int,
                       parent_id: int | None) -> None:
    """Persist the sub-segment parent (the longer same-direction segment this is
    a slice of), or NULL to clear it. Label only — does not affect scoring."""
    conn.execute("UPDATE segments SET parent_segment_id = ? WHERE id = ?",
                 (parent_id, segment_id))


def set_efforts_fetched_at(conn: sqlite3.Connection, segment_id: int,
                           when: str) -> None:
    conn.execute("UPDATE segments SET efforts_fetched_at = ? WHERE id = ?",
                 (when, segment_id))


def add_segment(conn: sqlite3.Connection, segment_id: int,
                discipline: str) -> str:
    """Register a segment id with its discipline. Inserts new rows; for an
    already-tracked id, updates the discipline only if it differs. Returns one
    of "added" / "updated" / "unchanged" so the caller can report what happened."""
    row = conn.execute(
        "SELECT discipline FROM segments WHERE id = ?", (segment_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO segments (id, discipline) VALUES (?, ?)",
            (segment_id, discipline))
        conn.commit()
        return "added"
    if row["discipline"] != discipline:
        conn.execute("UPDATE segments SET discipline = ? WHERE id = ?",
                     (discipline, segment_id))
        conn.commit()
        return "updated"
    return "unchanged"


def segment_ids(conn: sqlite3.Connection) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM segments ORDER BY id")]


def unimported_segment_ids(conn: sqlite3.Connection) -> list[int]:
    """Segments registered but never page-imported (fetched_at IS NULL)."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM segments WHERE fetched_at IS NULL ORDER BY id")]


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


def record_efforts(conn: sqlite3.Connection, segment_id: int,
                   efforts: list[dict], observed_at: str) -> None:
    """Log a freshly-fetched leaderboard into effort_log.

    One row per distinct observed effort, identified by (segment, athlete,
    effort_id). Already-seen efforts are ignored (their original observed_at —
    the rewind key — is preserved); only their mutable metrics are refreshed.
    Because the log is append-only and never replaced, an athlete missing from
    this fetch (bumped past the fetched depth) simply keeps their logged rows.
    The canonical best-per-athlete and rank are derived by the `efforts` view, so
    there's nothing to re-rank here."""
    for e in efforts:
        aid = e.get("athlete_id")
        if aid is None:
            continue
        conn.execute(
            "INSERT INTO effort_log (segment_id, athlete_id, elapsed_time, avg_speed, "
            "avg_watts, avg_hr, effort_id, activity_id, start_date_local, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(segment_id, athlete_id, effort_id) DO UPDATE SET "
            "elapsed_time = excluded.elapsed_time, avg_speed = excluded.avg_speed, "
            "avg_watts = excluded.avg_watts, avg_hr = excluded.avg_hr, "
            "activity_id = excluded.activity_id, "
            "start_date_local = excluded.start_date_local",
            (segment_id, aid, e.get("elapsed_time"), e.get("avg_speed"),
             e.get("avg_watts"), e.get("avg_hr"), e.get("effort_id"),
             e.get("activity_id"), e.get("start_date_local"), observed_at))
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
