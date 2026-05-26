# Kings of Shutesbury

A personal dashboard tracking progress on Strava cycling segments in and around
Shutesbury, MA. It pulls segment data and leaderboards from Strava's internal web
API using a logged-in session cookie, scores each segment (Tour-de-France style),
and ranks athletes into an overall **King of Shutesbury** standing.

## How it works

```
segments table (IDs)  ──update_segments.py──▶  strava.db  ──▶  web/data.json  ──▶  static site
        ▲                  (httpx + cookie)      (committed)      (committed)        (web/index.html)
   add IDs by hand
```

- **`strava.db`** is the source of truth and is committed/shipped with the build.
  The list of segment IDs to track lives in the `segments` table.
- **`web/`** is a dependency-free static site — open `web/index.html` (or host it
  anywhere). It reads `web/data.json`.

## Setup

```sh
uv sync                      # install deps (httpx)
cp .env.example .env         # then paste your _strava4_session cookie value
```

## Usage

```sh
# Add segment IDs to track (accepts numeric IDs or strava.com/strava.app.link URLs):
uv run update_segments.py add 38206226
uv run update_segments.py add https://www.strava.com/segments/8429503

# Refresh all tracked segments + rebuild web/data.json (skips ones fetched < 24h ago):
uv run update_segments.py

# Force a full refresh:
uv run update_segments.py --force

# List what's tracked:
uv run update_segments.py --list
```

## Filtering

- **Shutesbury only:** a segment counts toward the standings only if it **starts or
  finishes** inside the Shutesbury town boundary (OSM polygon, cached in
  `shutesbury_boundary.json`). The result is saved to `segments.in_shutesbury` so it
  isn't recomputed. Out-of-town segments stay in the DB but are listed under
  "Filtered out" on the dashboard.
- **Rides only:** `Run` segments are excluded (Strava only exposes Ride/Run, not
  MTB/Road/Gravel — finer discipline tags would need to be set by hand in the
  `segments.discipline` column).

## Classifying discipline (road/gravel/mtb)

Strava doesn't expose road/gravel/MTB at the segment level, so discipline is
hand-classified and stored in `segments.discipline` (`road`, `gravel`, or `mtb`;
`NULL` = unclassified). The dashboard uses this field to filter/group segments.

**Interactive (resumable):**

```sh
uv run classify.py
```

Walks you through every unclassified in-town ride segment one at a time. Press
`r`, `g`, `m`, `s` (skip), or `q` (quit). Each classification is committed
immediately, so you can quit and resume any time.

**One-off edits in a Python shell:**

```python
import sqlite3
db = sqlite3.connect("strava.db"); db.row_factory = sqlite3.Row
for r in db.execute("SELECT id,name,display_location FROM segments "
                    "WHERE in_shutesbury=1 AND discipline IS NULL ORDER BY name"):
    print(r["id"], r["name"], "—", r["display_location"])
db.execute("UPDATE segments SET discipline=? WHERE id=?", ("gravel", 648901)); db.commit()
```

After classifying, run `uv run update_segments.py --export` (or quit `classify.py`)
to refresh `web/data.json` with the updated discipline tags.

## Being gentle on the API

Per in-town ride segment the overall **top 25** leaderboard is seeded straight from
the segment page's embedded `initialLeaderboard` — so a normal refresh costs **one
request per segment** (the page) and no separate leaderboard calls. Top 25 is all
that scores (points only reach the top 10). Use `--lb-pages N` to pull deeper boards
(N*25 athletes) for a bounded set of segments when you want them. Out-of-town/run
segments are classified from the page and excluded.

Requests are paced (~4s + jitter), recently-fetched segments are skipped, and on the
first HTTP 429 (a header-less CloudFront limit, ~100 req/window) the client **stops
the whole run** with all progress saved — just rerun to resume from where it left off.
