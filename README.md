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

# Give an athlete their own page (linked from the dashboard):
uv run update_segments.py add-athlete 136573 129008249

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

## Being gentle on the API

Paced requests (~3s + jitter), leaderboard capped at 100 athletes, recently-fetched
segments skipped, and segments classified *before* their leaderboard is pulled (so
out-of-town/run segments cost a single request). On HTTP 429 the client backs off
exponentially (honoring `Retry-After`) and, if still limited, stops cleanly with all
progress saved — just rerun to resume.
