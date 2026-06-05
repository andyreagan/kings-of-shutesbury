# Kings of Shutesbury

A personal dashboard tracking progress on Strava cycling segments in and around
Shutesbury, MA. It pulls segment data and leaderboards from Strava's internal web
API using a logged-in session cookie, scores each segment (Tour-de-France style),
and ranks athletes into an overall **King of Shutesbury** standing.

## How it works

```
add ──▶  import (page) ──▶  update (leaderboard) ──▶  export ──▶  web/data.json
 │           │                      │                                  │
 ▼           ▼                      ▼                                  ▼
 segments    +metadata               +effort_log rows                  static site
 row         +geo class              +efforts_fetched_at                opens locally
             +seeded top-25          (recurring; stalest first)
             +difficulty
             +sub-segment parents
```

- **`strava.db`** is the source of truth and is committed with the build. The
  list of segment IDs lives in the `segments` table.
- **`web/`** is a dependency-free static site — open `web/index.html` (or host it
  anywhere). It reads `web/data.json`.

## Setup

```sh
uv sync                       # install deps (httpx)
cp .env.example .env          # then paste your _strava4_session cookie value
```

Run the test suite (mocked Strava API, no network):

```sh
uv run --group dev pytest
```

## The pipeline (six commands)

Each command does one stage and nothing else. Pages are pulled once per segment;
the recurring command is `update`.

### 1. `add <id> <discipline> [<id> <discipline>...]`
Register `(id, discipline)` pairs in the DB. No network. `<id>` is a numeric
Strava segment id or a `strava.com/segments/<id>` URL; `<discipline>` is
`road`, `gravel`, or `mtb`. Adding an id that already exists updates the
discipline only if it differs.

```sh
uv run update_segments.py add 38206226 gravel 8429503 road
```

### 2. `import (<id> | --all) [--force]`
Pull each target's overview page — metadata, streams, geo classification — and
seed its top-25 efforts from the embedded leaderboard. Sets `fetched_at`.

`--all` only touches segments where `fetched_at IS NULL`, so a clean second
`import --all` is a no-op. `--force` re-pulls.

After any import that actually fetched something, sub-segment parents and
difficulty are recomputed across the imported set (both idempotent, fast).

```sh
uv run update_segments.py import --all          # bring new segments up to date
uv run update_segments.py import 38206226       # one specific segment
uv run update_segments.py import 38206226 --force
```

### 3. `update (<id> | --all | --background) [--depth N] [--no-jitter]`
Refresh efforts from the leaderboard endpoint. Hits
`/frontend/segments/<id>/leaderboard?filter_type=overall` (depth `N` ⇒ N × 25
athletes; default 1 ⇒ top 25), then supplements with the following board so
Andy/Owen's times outside the top 25 still get logged.

`--all` walks every in-town, non-excluded ride segment, **stalest first** by
`efforts_fetched_at` (NULLs first — freshly-imported segments sort to the head
because their page-seeded top-25 doesn't yet include the following supplement).

`--background` does ONE jittered tick (sleep 0–5 min, auto-pick the single
most-important segment, fetch, exit) for use under an external 5-min
scheduler. See "Running in the background" below.

Stores everything in `effort_log`, sets `efforts_fetched_at`, and prints a
changelog of what changed during this run.

```sh
uv run update_segments.py update --all
uv run update_segments.py update 38206226 --depth 4   # pull top 100
uv run update_segments.py update --background         # one cron tick
```

#### Dynamic 429 backoff
Every successful fetch resets the ladder. A 429 escalates one step on this
ladder of pauses: **1h → 4h → 12h → 24h → 48h**. While `backoff_until` is in
the future, `--background` ticks log "backed off" and skip; foreground modes
(`<id>` / `--all`) only print a warning and proceed (your call). One more 429
after the 48h step exhausts the ladder and aborts with exit code 2 — wire
your scheduler to surface that.

State lives in the `kv` table (`backoff_level`, `backoff_until`,
`backoff_last_429`).

### 4. `export`
Rebuild `web/data.json` from the current DB. Pure read. No options.

```sh
uv run update_segments.py export
```

### 5. `list`
One line per tracked segment — id, name, discipline, terrain, difficulty, the
date the page was last imported, and the date efforts were last refreshed.

```sh
uv run update_segments.py list
```

### 6. `log [--since DATE] [--until DATE]`
Effort changelog over a date range. `--since`/`--until` accept ISO dates or
datetimes; with neither, `log` shows the most recent batch of observations
(handy after a manual `update` to re-read its summary).

```sh
uv run update_segments.py log                          # last run
uv run update_segments.py log --since 2026-05-01       # everything since
uv run update_segments.py log --since 2026-05-01 --until 2026-05-15
```

## Filtering

- **Shutesbury only:** a segment counts toward the standings only if it
  **starts or finishes** inside the Shutesbury town boundary (OSM polygon,
  cached in `shutesbury_boundary.json`). The result is saved to
  `segments.in_town` at `import` time. Out-of-town segments stay in the DB but
  appear under "Filtered out" on the dashboard.
- **Rides only:** `Run` segments are excluded automatically (Strava only
  exposes Ride/Run at the segment level; the finer road/gravel/mtb
  classification you provided at `add` time is shown in the dashboard).

## Running in the background

Two equivalent ways to run it: an open shell (you watch progress; Ctrl+C to
stop) or launchd (set-and-forget; survives reboot).

```sh
# Open-shell loop — same picker, prints each tick to stdout.
uv run update_segments.py update --background --loop

# launchd plist (see below) — same thing, headless, logs to .background.log.
```

`update --background` is a single-shot tick designed to be fired every 5
minutes by an external scheduler. `--loop` adds an internal 5-minute sleep
between ticks (and skips the per-tick jitter, since we're not racing any
external schedule) so you can run it directly in a shell. Each tick:

1. Sleeps a random 0–5 minutes (`--no-jitter` to skip).
2. Picks ONE segment by priority:
   - **Phase A** — stalest top-25 if older than 7 days (or never): refresh at
     depth 1 (~2 requests).
   - **Phase B** — shallowest segment that still has more leaderboard pages:
     fetch one page deeper than last time.
   - **Phase C** — fallback maintenance refresh of the stalest top-25.
3. Fetches, persists, prints any changelog, exits.

At 5-minute cadence with default depth 1 (~2 requests/tick) the worst-case
window holds 4 requests vs the ~100-request CloudFront limit — comfortable
4× headroom even if the cron and the prior tick's jitter happen to collide.
Phase A alone keeps every in-town ride segment refreshed weekly (24 ticks/day
out of 288); the remaining ~264 ticks/day go to Phase B and fill depth fast.

### launchd (macOS native)

Drop a `.plist` into `~/Library/LaunchAgents/`, then `launchctl load` it. It
survives reboot and writes stdout to a log file you can `tail -f`:

```xml
<!-- ~/Library/LaunchAgents/com.andyreagan.kings-of-shutesbury.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.andyreagan.kings-of-shutesbury</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string><string>sh</string><string>-c</string>
    <string>cd /Users/andyreagan/projects/2026/kings-of-shutesbury &amp;&amp; uv run update_segments.py update --background</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>/Users/andyreagan/projects/2026/kings-of-shutesbury/.background.log</string>
  <key>StandardErrorPath</key><string>/Users/andyreagan/projects/2026/kings-of-shutesbury/.background.log</string>
</dict></plist>
```

```sh
launchctl load   ~/Library/LaunchAgents/com.andyreagan.kings-of-shutesbury.plist
launchctl unload ~/Library/LaunchAgents/com.andyreagan.kings-of-shutesbury.plist  # to stop
tail -f .background.log
```

### cron (cross-platform alternative)

```cron
*/5 * * * * cd /path/to/kings-of-shutesbury && uv run update_segments.py update --background >> .background.log 2>&1
```

## Being gentle on the API

Each pass through the rate-limited Strava endpoints costs:

| Command | Requests per segment |
|---|---|
| `import` | 1 (the page) |
| `update` | 2 (overall leaderboard + following board) |

Requests are paced (~4s + jitter). On the first CloudFront 429 (a header-less
~100 req/window limit) the client **stops the whole run** with all progress
saved — just rerun to resume from where it left off.

If you only want the cheap setup, `import --all` is the safe one-time cost.
The recurring command is `update --all`, which spends two requests per in-town
ride segment per refresh.
