#!/usr/bin/env python3
"""Kings of Shutesbury — segment pipeline.

Six self-contained commands, one per stage:

    add <id> <discipline> [<id> <discipline>...]
                                  register (id, discipline) pairs in the DB.
                                  No network. discipline = road|gravel|mtb.

    import (<id> | --all) [--force]
                                  pull each segment's overview page (metadata,
                                  streams, geo) and seed its top-25 efforts
                                  from the embedded leaderboard. Sets
                                  fetched_at. --all skips already-imported
                                  segments unless --force is given, so a clean
                                  second `import --all` is a no-op.

    update (<id> | --all | --background) [--depth N] [--no-jitter]
                                  refresh efforts from the leaderboard
                                  endpoint (depth = N pages of 25, default 1).
                                  --all walks stalest first by
                                  efforts_fetched_at. --background does ONE
                                  jittered tick (sleep 0..5m, auto-pick the
                                  single most important segment, fetch, exit)
                                  for use under a 5-min scheduler. Prints a
                                  changelog of what changed in this run.

    export                        rebuild web/data.json from the DB. No
                                  network, no options.

    list                          show every tracked segment.

    log [--since DATE] [--until DATE]
                                  effort changelog over a date range. Default
                                  --since picks up the most recent batch of
                                  observations (i.e. the last run).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import geo
import scoring
from strava import AuthError, RateLimitError, StravaClient, StravaError

WEB_DIR = Path(__file__).resolve().parent / "web"
DATA_JSON = WEB_DIR / "data.json"
PROFILE_POINTS = 120            # downsample elevation profile to this many points
MAP_TRACK_POINTS = 64           # downsample GPS track for the map to this many points
EFFORTS_PER_SEGMENT = 100       # cap efforts shipped per segment (keeps page small)
VALID_DISCIPLINES = ("road", "gravel", "mtb")
# --background tunables. The picker keeps every segment's top-25 fresh on at
# least a weekly cadence (phase A), then spends remaining ticks deepening the
# shallowest leaderboards one page at a time (phase B). Per-tick jitter
# desynchronizes a 5-min cron from anything else hitting strava.com.
BACKGROUND_JITTER_S = 300       # random 0..N seconds at the start of every tick
TOP25_FRESHNESS_DAYS = 7        # phase A threshold: top-25 older than this gets refreshed
# Dynamic 429 backoff. Each consecutive 429 escalates one step; a successful
# fetch in any mode resets the ladder. Past the last step we abort the tick.
# Foreground (id / --all) ignores the gate (the user is right there) but still
# updates the ladder, so background and foreground share one rate-limit memory.
BACKOFF_LADDER_HOURS = [1, 4, 12, 24, 48]
# Athletes whose times we always try to refresh via the "following" board, even
# when they're outside a segment's top 25 (the logged-in session must follow them).
# Andy Reagan = 136573, Owen Skorupski = 129008249.
TRACKED_ATHLETES = {136573, 129008249}


# === shared helpers ==========================================================


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ref(ref: str) -> int | None:
    """Numeric id or /segments/<id> URL → int. No network (shortlinks unsupported)."""
    ref = ref.strip()
    if ref.isdigit():
        return int(ref)
    m = re.search(r"/segments/(\d+)", ref)
    return int(m.group(1)) if m else None


def _store_efforts(conn, sid: int, efforts: list, when: str) -> None:
    """Upsert the fetched athletes and append their efforts into the log.
    `when` is the observation time — the rewind key for effort_log."""
    for eff in efforts:
        if eff["athlete_id"] is None:
            continue
        db.upsert_athlete(conn, {
            "id": eff["athlete_id"], "name": eff["athlete_name"],
            "avatar_url": eff["avatar_url"], "badge": eff["badge"]})
    db.record_efforts(conn, sid,
                      [e for e in efforts if e["athlete_id"] is not None], when)
    conn.commit()


def _supplement_following(client, sid: int, efforts: list) -> list:
    """Append TRACKED_ATHLETES' (Andy/Owen) times from the following board when
    they're not already in `efforts`. Following ranks are relative to who you
    follow, so store rank=None (shown, worth 0 points). Network call — must
    sit inside the caller's try/except for rate limits."""
    if not TRACKED_ATHLETES:
        return efforts
    have = {e["athlete_id"] for e in efforts}
    for e in client.fetch_leaderboard(sid, "following"):
        if e["athlete_id"] in TRACKED_ATHLETES and e["athlete_id"] not in have:
            efforts.append({**e, "rank": None})
    return efforts


def _downsample(xs: list, n: int) -> list:
    if not xs or len(xs) <= n:
        return xs
    step = (len(xs) - 1) / (n - 1)
    return [xs[round(i * step)] for i in range(n)]


def _parse_ts(ts: str) -> datetime:
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# === backoff ladder ==========================================================
# State lives in the kv table: backoff_level (int as str), backoff_until (ISO).
# Both modes write the ladder; only --background gates on backoff_until.


def _backoff_state(conn) -> tuple[int, datetime | None]:
    level = int(db.kv_get(conn, "backoff_level") or "0")
    until_raw = db.kv_get(conn, "backoff_until")
    until = _parse_ts(until_raw) if until_raw else None
    return level, until


def _backoff_gate(conn, mode: str) -> bool:
    """Return True if we should proceed. --background refuses to fetch while
    backoff_until is in the future; foreground always proceeds but prints a
    warning so the user knows they're racing the ladder."""
    level, until = _backoff_state(conn)
    if until is None or until <= datetime.now(timezone.utc):
        return True
    wait_left = until - datetime.now(timezone.utc)
    hours = wait_left.total_seconds() / 3600
    if mode == "background":
        print(f"[background] backed off (level {level}/{len(BACKOFF_LADDER_HOURS)}); "
              f"~{hours:.1f}h to go (until {until.isoformat(timespec='seconds')}). "
              "Skipping this tick.")
        return False
    print(f"! note: still in 429 backoff (level {level}, "
          f"~{hours:.1f}h left until {until.isoformat(timespec='seconds')}). "
          "Proceeding anyway since you asked.")
    return True


def _backoff_escalate(conn) -> bool:
    """Bump the ladder one step after a 429. Returns False if we've blown
    past the last step (caller should abort with non-zero exit)."""
    level, _ = _backoff_state(conn)
    new_level = level + 1
    if new_level > len(BACKOFF_LADDER_HOURS):
        print(f"\n!!! backoff exhausted (already waited "
              f"{BACKOFF_LADDER_HOURS[-1]}h and got 429 again). Aborting.")
        return False
    hours = BACKOFF_LADDER_HOURS[new_level - 1]
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    db.kv_set(conn, "backoff_level", str(new_level))
    db.kv_set(conn, "backoff_until", until.isoformat(timespec="seconds"))
    db.kv_set(conn, "backoff_last_429", now())
    print(f"  → escalated backoff to level {new_level}: pause {hours}h "
          f"(until {until.isoformat(timespec='seconds')}).")
    return True


def _backoff_reset(conn) -> None:
    """Clear the ladder after any successful fetch."""
    level, _ = _backoff_state(conn)
    if level > 0:
        print(f"  → cleared backoff (was level {level}).")
    db.kv_set(conn, "backoff_level", None)
    db.kv_set(conn, "backoff_until", None)


def _require_id_xor_all(args: argparse.Namespace, cmd: str) -> None:
    """import/update both take exactly one of <id> or --all. argparse's
    mutually_exclusive_group misbehaves with `nargs="?"` positionals, so check
    here instead."""
    have_id = args.id is not None
    if have_id and args.all_:
        sys.exit(f"{cmd}: pass <id> OR --all, not both.")
    if not have_id and not args.all_:
        sys.exit(f"{cmd}: pass a segment <id> or --all.")


def _require_update_mode(args: argparse.Namespace) -> None:
    """update takes exactly one of <id>, --all, or --background. Same argparse
    caveat as above — check by hand."""
    modes = [args.id is not None, args.all_, args.background]
    if sum(modes) != 1:
        sys.exit("update: pass exactly one of <id>, --all, or --background.")
    if args.background and args.depth != 1:
        sys.exit("update --background chooses depth itself; "
                 "don't combine with --depth.")


# === add =====================================================================


def cmd_add(args: argparse.Namespace) -> None:
    if len(args.pairs) % 2 != 0:
        sys.exit("add takes (id, discipline) pairs — got an odd number of args.")
    pairs: list[tuple[int, str]] = []
    for ref, disc in zip(args.pairs[0::2], args.pairs[1::2]):
        disc = disc.lower()
        if disc not in VALID_DISCIPLINES:
            sys.exit(f"discipline must be one of {VALID_DISCIPLINES}, got {disc!r}")
        sid = _parse_ref(ref)
        if sid is None:
            sys.exit(f"couldn't parse a segment id from {ref!r} "
                     "(use a numeric id or a strava.com/segments/<id> URL)")
        pairs.append((sid, disc))

    conn = db.connect()
    db.init(conn)
    try:
        for sid, disc in pairs:
            outcome = db.add_segment(conn, sid, disc)
            mark = {"added": "+ added", "updated": "~ discipline updated",
                    "unchanged": "= already tracked"}[outcome]
            print(f"{mark}: {sid} ({disc})")
    finally:
        conn.close()
    print("\nRun `uv run update_segments.py import --all` to pull page data.")


# === import ==================================================================


def cmd_import(args: argparse.Namespace) -> None:
    _require_id_xor_all(args, "import")
    conn = db.connect()
    db.init(conn)
    try:
        targets = _import_targets(conn, args)
        if not targets:
            return
        try:
            boundary = geo.load_boundary()
        except Exception as e:                                      # noqa: BLE001
            boundary = None
            print(f"! Shutesbury boundary unavailable ({e}); treating all as in-town")

        imported: list[int] = []
        with StravaClient() as client:
            for sid in targets:
                print(f"> page {sid} ...", flush=True)
                try:
                    seg = client.fetch_segment(sid)
                except AuthError as e:
                    print(f"\nAUTH ERROR: {e}")
                    return
                except RateLimitError as e:
                    print(f"\nRATE LIMITED: {e}")
                    break
                except StravaError as e:
                    print(f"! failed {sid}: {e}")
                    continue

                if boundary is not None:
                    cls = geo.classify_segment(
                        [seg.get("start_lat"), seg.get("start_lng")],
                        [seg.get("end_lat"), seg.get("end_lng")],
                        (seg.get("streams") or {}).get("location") or [], boundary)
                else:
                    cls = {"starts_in": True, "ends_in": True,
                           "passes_through": True, "in_town": True}
                seg["in_town"] = 1 if cls["in_town"] else 0
                seg["fetched_at"] = now()
                db.upsert_segment(conn, seg)
                db.set_geo_class(conn, sid, cls)

                # Seed top-25 from the embedded initialLeaderboard. The "following"
                # board (Andy/Owen below top 25) is left for `update` — keeping
                # import strictly one request per segment.
                seeded = seg.get("leaders") or []
                if cls["in_town"] and (seg["activity_type"] or "").lower() == "ride":
                    _store_efforts(conn, sid, seeded, now())

                imported.append(sid)
                where = ("in Shutesbury" if cls["in_town"]
                         else ("passes through (no start/finish in town)"
                               if cls["passes_through"] else "OUTSIDE Shutesbury"))
                if (seg["activity_type"] or "").lower() != "ride":
                    where += f" ({seg['activity_type']})"
                print(f"  {seg['name']} ({seg['display_location']}) — {where}")

        if imported:
            _recompute_derived(conn)
            print(f"\nImported {len(imported)} segment(s). "
                  "Run `uv run update_segments.py update --all` to refresh leaderboards.")
    finally:
        conn.close()


def _import_targets(conn, args) -> list[int]:
    """Resolve the segments `import` should fetch, printing the no-op reason
    when there's nothing to do."""
    if args.id is not None:
        row = conn.execute(
            "SELECT id, fetched_at FROM segments WHERE id = ?", (args.id,)).fetchone()
        if row is None:
            sys.exit(f"segment {args.id} is not tracked — `add` it first.")
        if row["fetched_at"] and not args.force:
            print(f"Segment {args.id} already imported "
                  f"(at {row['fetched_at']}). Use --force to re-import.")
            return []
        return [args.id]
    # --all
    if args.force:
        targets = db.segment_ids(conn)
        if not targets:
            print("No segments tracked yet. `add` some first.")
        return targets
    targets = db.unimported_segment_ids(conn)
    if not targets:
        total = len(db.segment_ids(conn))
        print(f"All {total} tracked segment(s) already imported. "
              "Use --force to re-import.")
    return targets


def _recompute_derived(conn) -> None:
    """Sub-segment parents + difficulty across the current segment set. Run
    after an import that changed pages; both are idempotent and cheap."""
    seg_rows = conn.execute(
        "SELECT * FROM segments WHERE fetched_at IS NOT NULL").fetchall()
    segments = [dict(r) for r in seg_rows]

    # Sub-segment parents: same-direction slice of a longer included segment.
    # Only considered for included segments (ride + in_town + not excluded).
    def is_included(s) -> bool:
        return (s["excluded"] != 1
                and (s["activity_type"] or "").lower() == "ride"
                and s["in_town"] == 1)
    included = [s for s in segments if is_included(s)]
    sub_inputs = [{
        "id": s["id"], "length_m": s["distance_m"],
        "track": json.loads(s.get("streams_json") or "{}").get("location") or [],
    } for s in included]
    n_sub = 0
    for ci in sub_inputs:
        parent = geo.find_sub_segment_parent(ci, sub_inputs)
        db.set_parent_segment(conn, ci["id"], parent)
        n_sub += bool(parent)

    # Difficulty for every page-imported segment.
    for seg in segments:
        db.set_difficulty(conn, seg["id"], scoring.segment_difficulty(seg))
    conn.commit()
    print(f"  recomputed difficulty for {len(segments)} segment(s); "
          f"tagged {n_sub} sub-segment(s)")


# === update ==================================================================


def cmd_update(args: argparse.Namespace) -> None:
    _require_update_mode(args)
    if args.background:
        cmd_update_background(args)
        return
    conn = db.connect()
    db.init(conn)
    try:
        _backoff_gate(conn, "foreground")     # informational only
        targets = _update_targets(conn, args)
        if not targets:
            return
        # Captured BEFORE any fetch so the changelog reads every observation
        # this run wrote.
        run_started_at = now()
        with StravaClient() as client:
            for sid in targets:
                _refresh_one(conn, client, sid, args.depth)
                if _last_stop:
                    break
        print_changelog(conn, run_started_at)
        if _last_stop == "ratelimit-exhausted":
            sys.exit(2)
    finally:
        conn.close()


# Sentinel set by `_refresh_one` when a stop-the-run condition fires (auth /
# rate-limit). Used by the loop in cmd_update to break out cleanly.
_last_stop: str | None = None


def _refresh_one(conn, client, sid: int, depth: int) -> bool:
    """Fetch the leaderboard for one segment at `depth` and persist. Returns
    True on success. Updates the 429 backoff ladder: success resets it; a
    rate-limit escalates it (and sets `_last_stop` to either 'ratelimit' or
    'ratelimit-exhausted' so the caller can decide whether to abort the whole
    process or just stop the loop)."""
    global _last_stop
    _last_stop = None
    print(f"> leaderboard {sid} (depth {depth}) ...", flush=True)
    try:
        efforts = client.fetch_leaderboard(sid, "overall", pages=depth)
        efforts = _supplement_following(client, sid, efforts)
    except AuthError as e:
        print(f"\nAUTH ERROR: {e}")
        _last_stop = "auth"
        return False
    except RateLimitError as e:
        print(f"\nRATE LIMITED: {e}")
        if not _backoff_escalate(conn):
            _last_stop = "ratelimit-exhausted"
        else:
            _last_stop = "ratelimit"
        return False
    except StravaError as e:
        print(f"! failed {sid}: {e}")
        return False
    stamp = now()
    _store_efforts(conn, sid, efforts, stamp)
    db.set_efforts_fetched_at(conn, sid, stamp)
    if depth > 1:
        db.bump_depth_pages(conn, sid, depth)
    # Single commit per segment: set_efforts_fetched_at and bump_depth_pages
    # don't self-commit, so without this the final segment's metadata UPDATE
    # gets rolled back at conn.close(). (The earlier code masked this for
    # --all because the NEXT iteration's _store_efforts.commit() happened to
    # flush the previous UPDATE — but the last segment always lost it.)
    conn.commit()
    _backoff_reset(conn)
    print(f"  {len(efforts)} efforts")
    return True


def cmd_update_background(args: argparse.Namespace) -> None:
    """One background tick: sleep a jittered amount, pick the single most
    important segment to refresh, fetch it, exit. Designed to be fired every
    5 minutes by an external scheduler (launchd / cron)."""
    if not args.no_jitter:
        sleep_s = random.uniform(0, BACKGROUND_JITTER_S)
        print(f"[background] jitter sleep {sleep_s:.0f}s ...", flush=True)
        time.sleep(sleep_s)

    conn = db.connect()
    db.init(conn)
    try:
        if not _backoff_gate(conn, "background"):
            return
        pick = _background_pick(conn)
        if pick is None:
            print("[background] no tracked in-town ride segments — nothing to do.")
            return
        sid, depth, phase = pick
        print(f"[background] phase {phase} — segment {sid}, depth {depth}")
        run_started_at = now()
        with StravaClient() as client:
            _refresh_one(conn, client, sid, depth)
        print_changelog(conn, run_started_at)
        if _last_stop == "ratelimit-exhausted":
            sys.exit(2)
    finally:
        conn.close()


def _background_pick(conn) -> tuple[int, int, str] | None:
    """Return (segment_id, depth_pages, phase_label) for this tick — or None
    if there are no candidates at all. Priority:
      A. stalest top-25 if older than TOP25_FRESHNESS_DAYS (or never refreshed)
      B. shallowest segment that hasn't reached its full leaderboard yet —
         go one page deeper than last time
      C. fallback maintenance refresh of the stalest top-25
    """
    week_ago = (datetime.now(timezone.utc)
                - timedelta(days=TOP25_FRESHNESS_DAYS)
                ).isoformat(timespec="seconds")
    base_where = (
        "in_town = 1 AND lower(activity_type) = 'ride' "
        "AND excluded = 0 AND fetched_at IS NOT NULL")

    row = conn.execute(
        f"SELECT id FROM segments WHERE {base_where} "
        "AND (efforts_fetched_at IS NULL OR efforts_fetched_at < ?) "
        "ORDER BY efforts_fetched_at IS NULL DESC, efforts_fetched_at ASC "
        "LIMIT 1", (week_ago,)).fetchone()
    if row:
        return row["id"], 1, "A (stale top-25)"

    # Phase B: build depth on the shallowest segment that still has more
    # leaderboard pages to fetch. last_depth_pages is the high-water mark;
    # NULL means we've never gone past the depth-1 import seed.
    row = conn.execute(
        f"SELECT id, COALESCE(last_depth_pages, 1) AS depth "
        f"FROM segments WHERE {base_where} "
        "AND COALESCE(last_depth_pages, 1) * 25 < COALESCE(total_athletes, 0) "
        "ORDER BY COALESCE(last_depth_pages, 1) ASC, "
        "         efforts_fetched_at ASC NULLS FIRST "
        "LIMIT 1").fetchone()
    if row:
        return row["id"], row["depth"] + 1, "B (build depth)"

    # Phase C: everything's fresh AND everyone's at max depth. Refresh the
    # stalest top-25 anyway so the cron never silently no-ops.
    row = conn.execute(
        f"SELECT id FROM segments WHERE {base_where} "
        "ORDER BY efforts_fetched_at ASC NULLS FIRST LIMIT 1").fetchone()
    if row:
        return row["id"], 1, "C (idle maintenance)"
    return None


def _update_targets(conn, args) -> list[int]:
    """In-town ride, not excluded, page already imported. With --all, stalest
    efforts_fetched_at first (NULLs first — freshly imported segments still
    only have page-seeded top-25 without the following supplement)."""
    if args.id is not None:
        row = conn.execute(
            "SELECT id, fetched_at, in_town, activity_type, excluded "
            "FROM segments WHERE id = ?", (args.id,)).fetchone()
        if row is None:
            sys.exit(f"segment {args.id} is not tracked.")
        if row["fetched_at"] is None:
            sys.exit(f"segment {args.id} hasn't been imported yet — "
                     f"run `import {args.id}` first.")
        if (row["in_town"] != 1 or (row["activity_type"] or "").lower() != "ride"
                or row["excluded"] == 1):
            sys.exit(f"segment {args.id} doesn't score "
                     "(not in-town, not a ride, or excluded) — nothing to update.")
        return [args.id]
    rows = conn.execute(
        "SELECT id FROM segments WHERE fetched_at IS NOT NULL "
        "AND in_town = 1 AND lower(activity_type) = 'ride' AND excluded = 0 "
        "ORDER BY efforts_fetched_at IS NULL DESC, efforts_fetched_at ASC").fetchall()
    targets = [r["id"] for r in rows]
    if not targets:
        print("No in-town ride segments to update. "
              "Have you run `import --all` yet?")
    return targets


# === export ==================================================================


def cmd_export(args: argparse.Namespace) -> None:
    conn = db.connect()
    db.init(conn)
    try:
        export_data_json(conn)
    finally:
        conn.close()


def _segment_with_efforts(conn, seg_row) -> dict:
    seg = dict(seg_row)
    efforts = [dict(r) for r in conn.execute(
        "SELECT e.*, a.name AS athlete_name, a.avatar_url, a.badge "
        "FROM efforts e JOIN athletes a ON a.id = e.athlete_id "
        "WHERE e.segment_id = ? ORDER BY e.rank IS NULL, e.rank", (seg["id"],))]
    seg["efforts"] = efforts
    return seg


def _capped_efforts(efforts: list) -> list:
    """Ship at most EFFORTS_PER_SEGMENT ranked efforts per segment to keep
    data.json small, but always keep tracked athletes (Andy/Owen) and the
    rank-NULL following supplements regardless of rank."""
    kept, n = [], 0
    for e in efforts:
        if (n < EFFORTS_PER_SEGMENT or e["rank"] is None
                or e["athlete_id"] in TRACKED_ATHLETES):
            kept.append(e)
            if e["rank"] is not None:
                n += 1
    return kept


def export_data_json(conn) -> None:
    """Serialize the current DB into web/data.json. Pure read — assumes import
    has already set geo/difficulty/sub-seg. Defensive: any segment with NULL
    difficulty gets it computed on the fly so a stale DB still exports."""
    seg_rows = conn.execute(
        "SELECT * FROM segments WHERE fetched_at IS NOT NULL").fetchall()
    segments = [_segment_with_efforts(conn, r) for r in seg_rows]

    try:
        boundary = geo.load_boundary()
    except Exception as e:                                          # noqa: BLE001
        boundary = None
        print(f"! Shutesbury boundary unavailable ({e}); skipping geo in payload")

    # Fallback fill for difficulty in case `import` was never run for these
    # rows (e.g. a hand-edited DB). Cheap; keeps export side-effect-free
    # otherwise.
    for seg in segments:
        if seg["difficulty"] is None:
            seg["difficulty"] = scoring.segment_difficulty(seg)
            db.set_difficulty(conn, seg["id"], seg["difficulty"])
    conn.commit()

    def is_included(seg) -> bool:
        if seg["excluded"] == 1:
            return False
        ride = (seg["activity_type"] or "").lower() == "ride"
        in_town = seg["in_town"] == 1 if boundary is not None else True
        return ride and in_town

    def track_of(seg) -> list:
        loc = json.loads(seg.get("streams_json") or "{}").get("location") or []
        return _downsample(loc, MAP_TRACK_POINTS)

    included = [s for s in segments if is_included(s)]
    filtered = []
    for s in segments:
        if is_included(s):
            continue
        reasons = []
        if s["excluded"] == 1:
            reasons.append("manually excluded")
        if (s["activity_type"] or "").lower() != "ride":
            reasons.append((s["activity_type"] or "non-ride").lower())
        if boundary is not None and s["in_town"] != 1:
            reasons.append("passes through" if s["passes_through"] == 1
                           else "outside Shutesbury")
        filtered.append({"id": s["id"], "name": s["name"],
                         "location": s["display_location"],
                         "reason": ", ".join(reasons) or "excluded",
                         "terrain": s["terrain"], "distance_m": s["distance_m"],
                         "avg_grade": s["avg_grade"], "difficulty": s["difficulty"],
                         "track": track_of(s)})

    # The single hardest in-town ride is the "Cima Coppi".
    cima_id = max(included, key=lambda s: s["difficulty"])["id"] if included else None

    out_segments = []
    for seg in included:
        streams = json.loads(seg.get("streams_json") or "{}")
        depth = scoring.effort_depth(seg["total_efforts"])
        category = scoring.segment_category(seg["difficulty"], seg["id"] == cima_id)
        leader = seg["efforts"][0] if seg["efforts"] else None
        out_segments.append({
            "id": seg["id"],
            "name": seg["name"],
            "location": seg["display_location"],
            "activity_type": seg["activity_type"],
            "discipline": seg["discipline"],
            "category": category,
            "terrain": seg["terrain"],
            "distance_m": seg["distance_m"],
            "avg_grade": seg["avg_grade"],
            "elev_gain": seg["elev_gain"],
            "gross_gain": seg["gross_gain"],
            "gross_loss": seg["gross_loss"],
            "total_efforts": seg["total_efforts"],
            "total_athletes": seg["total_athletes"],
            "difficulty": seg["difficulty"],
            "parent_segment_id": seg["parent_segment_id"],
            "is_sub_segment": seg["parent_segment_id"] is not None,
            "map_image_url": seg["map_image_url"],
            "start_latlng": [seg["start_lat"], seg["start_lng"]],
            "end_latlng": [seg["end_lat"], seg["end_lng"]],
            "track": track_of(seg),
            "leader": {"name": leader["athlete_name"],
                       "elapsed_time": leader["elapsed_time"]} if leader else None,
            "profile": {
                "distance": _downsample(streams.get("distance") or [], PROFILE_POINTS),
                "elevation": _downsample(streams.get("elevation") or [], PROFILE_POINTS),
            },
            "efforts": [{
                "rank": e["rank"], "athlete_id": e["athlete_id"],
                "name": e["athlete_name"], "elapsed_time": e["elapsed_time"],
                "avg_watts": e["avg_watts"], "avatar_url": e["avatar_url"],
                "badge": e["badge"],
                "points": scoring.points_for_rank(e["rank"], category, depth,
                                                  seg["difficulty"]),
                "effort_id": e["effort_id"], "activity_id": e["activity_id"],
            } for e in _capped_efforts(seg["efforts"])],
        })
    out_segments.sort(key=lambda s: s["difficulty"], reverse=True)

    payload = {
        "generated_at": now(),
        "segments": out_segments,
        "filtered": filtered,
        "boundary": boundary,
    }
    WEB_DIR.mkdir(exist_ok=True)
    DATA_JSON.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {DATA_JSON} — {len(out_segments)} in-Shutesbury ride "
          f"segments, {len(filtered)} filtered out.")


# === list ====================================================================


def cmd_list(args: argparse.Namespace) -> None:
    conn = db.connect()
    db.init(conn)
    try:
        rows = conn.execute(
            "SELECT id, name, discipline, terrain, difficulty, fetched_at, "
            "efforts_fetched_at FROM segments "
            "ORDER BY difficulty DESC NULLS LAST, id").fetchall()
        if not rows:
            print("No segments tracked yet.")
            return
        for r in rows:
            page = r["fetched_at"][:10] if r["fetched_at"] else "—"
            lb = r["efforts_fetched_at"][:10] if r["efforts_fetched_at"] else "—"
            print(f"{r['id']:>12}  {r['name'] or '(unimported)':30} "
                  f"{r['discipline'] or '?':6} {r['terrain'] or '':8} "
                  f"diff={r['difficulty'] or '-':>6}  "
                  f"page={page}  lb={lb}")
    finally:
        conn.close()


# === log =====================================================================


def cmd_log(args: argparse.Namespace) -> None:
    conn = db.connect()
    db.init(conn)
    try:
        since = _resolve_log_since(conn, args.since)
        print_changelog(conn, since, args.until)
    finally:
        conn.close()


def _resolve_log_since(conn, raw: str | None) -> str:
    """Default: a minute before the most recent observation, so `log` with no
    args shows the last run's changes."""
    if raw:
        return raw
    row = conn.execute(
        "SELECT MAX(observed_at) AS m FROM effort_log").fetchone()
    if row and row["m"]:
        return (_parse_ts(row["m"]) - timedelta(minutes=1)).isoformat()
    return (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()


def print_changelog(conn, since: str, until: str | None = None) -> None:
    """Print a changelog of effort changes whose observed_at falls in (since, until].

    Sourced from `effort_log`: every effort first logged within the window is a
    change to surface. For each affected segment we reconstruct the prior state
    (each athlete's latest observation strictly before `since`), derive prior vs
    current ranks, score both, and report:

      - PR  — a faster time from someone we already knew
      - NEW — first observation we have of this athlete on this segment
      - ↕   — "bumped": no new time, but their rank shifted because someone
              else's new effort restructured the board

    Two views: by segment (what happened where) and by athlete (the King-points
    ledger). The athlete totals are net change in King points across the window."""
    upper_clause = "AND observed_at <= ?" if until else ""
    upper_args: tuple = (until,) if until else ()
    affected = [r[0] for r in conn.execute(
        f"SELECT DISTINCT segment_id FROM effort_log "
        f"WHERE observed_at >= ? {upper_clause}",
        (since, *upper_args)).fetchall()]
    range_str = f"since {since}" + (f" until {until}" if until else "")
    if not affected:
        print(f"\nNo effort changes {range_str} — nothing to changelog.")
        return

    in_clause = ",".join("?" * len(affected))
    seg_rows = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT id, name, difficulty, total_efforts, terrain, in_town, "
        f"activity_type, excluded FROM segments WHERE id IN ({in_clause})",
        affected).fetchall()}
    cima_row = conn.execute(
        "SELECT id FROM segments WHERE in_town = 1 "
        "AND lower(activity_type) = 'ride' AND excluded = 0 "
        "AND difficulty IS NOT NULL ORDER BY difficulty DESC LIMIT 1").fetchone()
    cima_id = cima_row["id"] if cima_row else None

    name_map = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM athletes").fetchall()}

    def aname(aid: int) -> str:
        return name_map.get(aid) or f"Athlete {aid}"

    def fmt_t(t):
        if t is None:
            return "—"
        m, s = divmod(int(t), 60)
        return f"{m}:{s:02d}"

    def derived_ranks(state: dict) -> dict:
        items = sorted((t, aid) for aid, t in state.items() if t is not None)
        return {aid: i + 1 for i, (_, aid) in enumerate(items)}

    def points_of(seg: dict, rank: int | None) -> int:
        if not rank:
            return 0
        cat = scoring.segment_category(seg.get("difficulty") or 0,
                                       seg["id"] == cima_id)
        depth = scoring.effort_depth(seg.get("total_efforts"))
        return scoring.points_for_rank(rank, cat, depth,
                                       seg.get("difficulty") or 0)

    by_segment, per_athlete, new_segments = [], {}, []

    for sid in affected:
        seg = seg_rows.get(sid)
        if not seg:
            continue
        scores = (seg["in_town"] == 1
                  and (seg["activity_type"] or "").lower() == "ride"
                  and seg["excluded"] != 1)

        prior_rows = conn.execute(
            "SELECT eo.athlete_id, eo.elapsed_time FROM effort_log eo "
            "JOIN (SELECT athlete_id, MAX(id) AS mid FROM effort_log "
            "      WHERE segment_id = ? AND observed_at < ? GROUP BY athlete_id) j "
            "  ON j.athlete_id = eo.athlete_id AND j.mid = eo.id",
            (sid, since)).fetchall()
        prior = {r["athlete_id"]: r["elapsed_time"] for r in prior_rows}
        # "Current" = best-known state at the end of the window. When `until` is
        # None this is just the materialized `efforts` view; otherwise we
        # recompute the best time per athlete bounded by `until`.
        if until is None:
            cur_rows = conn.execute(
                "SELECT athlete_id, elapsed_time FROM efforts WHERE segment_id = ?",
                (sid,)).fetchall()
        else:
            cur_rows = conn.execute(
                "SELECT athlete_id, MIN(elapsed_time) AS elapsed_time "
                "FROM effort_log WHERE segment_id = ? AND observed_at <= ? "
                "GROUP BY athlete_id",
                (sid, until)).fetchall()
        current = {r["athlete_id"]: r["elapsed_time"] for r in cur_rows}

        is_new = not prior
        if is_new and scores:
            new_segments.append(seg)

        prior_ranks = derived_ranks(prior)
        cur_ranks = derived_ranks(current)

        changes = []
        for aid in set(prior) | set(current):
            old_t, new_t = prior.get(aid), current.get(aid)
            old_r, new_r = prior_ranks.get(aid), cur_ranks.get(aid)
            old_p = points_of(seg, old_r) if scores else 0
            new_p = points_of(seg, new_r) if scores else 0
            if old_t is None and new_t is not None:
                kind = "new"
            elif (old_t is not None and new_t is not None and new_t < old_t):
                kind = "pr"
            elif old_p != new_p:
                kind = "bump"
            else:
                continue
            entry = {"aid": aid, "kind": kind,
                     "old_t": old_t, "new_t": new_t,
                     "old_r": old_r, "new_r": new_r,
                     "old_p": old_p, "new_p": new_p}
            changes.append(entry)
            if scores and old_p != new_p:
                per_athlete.setdefault(aid, []).append({"name": seg["name"], **entry})
        if changes:
            by_segment.append((seg, is_new, scores, changes))

    print(f"\n=== CHANGELOG ({range_str}) ===")
    if new_segments:
        print(f"\n{len(new_segments)} new segment(s) tracked this window:")
        for s in new_segments:
            print(f"  + {s['name']}  (id {s['id']})")

    print(f"\nBy segment ({len(by_segment)} affected):")
    for seg, is_new, scores, changes in sorted(
            by_segment, key=lambda x: -(x[0].get("difficulty") or 0)):
        cat = scoring.segment_category(seg.get("difficulty") or 0,
                                       seg["id"] == cima_id)
        tag = (" · NEW SEGMENT" if is_new
               else ("" if scores else " · filtered (doesn't score)"))
        print(f"\n  {seg['name']}  ({cat}{tag})")
        for c in sorted(changes, key=lambda x: x["new_r"] or 9999):
            mark = {"new": "NEW ", "pr": "PR  ", "bump": "↕   "}[c["kind"]]
            d = c["new_p"] - c["old_p"]
            pts_str = f"  pts {c['old_p']} → {c['new_p']} ({d:+})" if d else ""
            print(f"    {mark}{aname(c['aid'])[:26]:<26}  "
                  f"{fmt_t(c['old_t']):>5} → {fmt_t(c['new_t']):<5}  "
                  f"rank {str(c['old_r'] or '-'):>3} → {str(c['new_r'] or '-'):<3}"
                  f"{pts_str}")

    def describe(c) -> str:
        if c["kind"] == "new":
            return f"NEW effort on {c['name']} at rank {c['new_r']}"
        if c["kind"] == "pr":
            return (f"PR on {c['name']}: {fmt_t(c['old_t'])} → "
                    f"{fmt_t(c['new_t'])}, rank {c['old_r']} → {c['new_r']}")
        places = (c["old_r"] or 0) - (c["new_r"] or 0)
        if places > 0:
            verb = f"gained {places} place{'s' if places != 1 else ''}"
        elif places < 0:
            verb = f"lost {-places} place{'s' if -places != 1 else ''}"
        else:
            verb = f"rank {c['old_r']} → {c['new_r']}"
        return f"{verb} on {c['name']} (rank {c['old_r']} → {c['new_r']})"

    if per_athlete:
        ranked = sorted(((a, cs) for a, cs in per_athlete.items()
                         if sum(c["new_p"] - c["old_p"] for c in cs) != 0),
                        key=lambda kv: -abs(sum(c["new_p"] - c["old_p"]
                                                for c in kv[1])))
        print(f"\nBy athlete ({len(ranked)} with point changes):")
        for aid, contribs in ranked:
            tot = sum(c["new_p"] - c["old_p"] for c in contribs)
            verb = "gained" if tot > 0 else "lost"
            label = f"{verb} {abs(tot)} pt{'s' if abs(tot) != 1 else ''}"
            print(f"\n  {aname(aid)} ({aid}) — {label}")
            for c in sorted(contribs, key=lambda x: -abs(x["new_p"] - x["old_p"])):
                d = c["new_p"] - c["old_p"]
                print(f"    · {describe(c)}  ({d:+})")
    print()


# === entry ===================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="register (id, discipline) pairs")
    p_add.add_argument("pairs", nargs="+", metavar="id|url discipline",
                       help="alternating id-or-URL and "
                            f"discipline ({'/'.join(VALID_DISCIPLINES)})")
    p_add.set_defaults(func=cmd_add)

    p_imp = sub.add_parser("import", help="pull the segment page (geo + seed efforts)")
    p_imp.add_argument("id", nargs="?", type=int, help="a single segment id")
    p_imp.add_argument("--all", dest="all_", action="store_true",
                       help="import every unimported segment (no-op if none)")
    p_imp.add_argument("--force", action="store_true",
                       help="re-import even if already imported")
    p_imp.set_defaults(func=cmd_import)

    p_upd = sub.add_parser("update", help="refresh efforts from the leaderboard")
    p_upd.add_argument("id", nargs="?", type=int, help="a single segment id")
    p_upd.add_argument("--all", dest="all_", action="store_true",
                       help="refresh every in-town ride, stalest first")
    p_upd.add_argument("--background", action="store_true",
                       help="one background tick — jittered sleep, then "
                            "auto-pick + refresh ONE segment (top-25 weekly, "
                            "depth-build with leftover ticks). Wire to a "
                            "5-minute cron / launchd job.")
    p_upd.add_argument("--no-jitter", action="store_true",
                       help="with --background: skip the random sleep "
                            "(testing, or if the scheduler already jitters)")
    p_upd.add_argument("--depth", type=int, default=1, metavar="N",
                       help="pages of 25 to pull (default 1 = top 25); "
                            "ignored with --background")
    p_upd.set_defaults(func=cmd_update)

    p_exp = sub.add_parser("export", help="rebuild web/data.json from the DB")
    p_exp.set_defaults(func=cmd_export)

    p_list = sub.add_parser("list", help="list every tracked segment")
    p_list.set_defaults(func=cmd_list)

    p_log = sub.add_parser("log", help="effort changelog over a date range")
    p_log.add_argument("--since", metavar="DATE",
                       help="ISO date or datetime (default: last run)")
    p_log.add_argument("--until", metavar="DATE",
                       help="ISO date or datetime (default: now)")
    p_log.set_defaults(func=cmd_log)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
