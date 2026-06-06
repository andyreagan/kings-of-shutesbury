"""Kings of Shutesbury — pipeline logic (ORM port of update_segments.py + db.py).

Public functions mirror the legacy module; the leading `conn` parameter has been
dropped everywhere — the ORM (and Django's implicit connection) replaces it.

This module is the shared brain; management commands are thin shells that call
into here.  Keep the legacy comments — they explain WHY (depth pages, backoff
ladder, changelog semantics).
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.db import connection
from django.db.models import F

from . import geo
from . import scoring
from .models import Athlete, EffortLog, Effort, Kv, Segment
from .strava import AuthError, RateLimitError, StravaClient, StravaError

VALID_DISCIPLINES = ("road", "gravel", "mtb")

# --background tunables. The picker keeps every segment's top-25 fresh on at
# least a weekly cadence (phase A), then spends remaining ticks deepening the
# shallowest leaderboards one page at a time (phase B). Per-tick jitter
# desynchronizes a 5-min cron from anything else hitting strava.com.
BACKGROUND_JITTER_S = 300        # random 0..N seconds at the start of every tick
BACKGROUND_TICK_INTERVAL_S = 300  # --loop: gap between ticks (script is the scheduler)
TOP25_FRESHNESS_DAYS = 7         # phase A threshold: top-25 older than this gets refreshed

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


def _parse_ts(ts: str) -> datetime:
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# === kv store ================================================================


def kv_get(key: str) -> str | None:
    """Read a kv entry; returns None if absent."""
    try:
        return Kv.objects.get(key=key).value
    except Kv.DoesNotExist:
        return None


def kv_set(key: str, value: str | None) -> None:
    """Set (or, with value=None, clear) a kv entry. Committed immediately so
    it survives a crash."""
    if value is None:
        Kv.objects.filter(key=key).delete()
    else:
        Kv.objects.update_or_create(key=key, defaults={"value": value})


# === backoff ladder ==========================================================
# State lives in the kv table: backoff_level (int as str), backoff_until (ISO).
# Both modes write the ladder; only --background gates on backoff_until.


def _backoff_state() -> tuple[int, datetime | None]:
    level = int(kv_get("backoff_level") or "0")
    until_raw = kv_get("backoff_until")
    until = _parse_ts(until_raw) if until_raw else None
    return level, until


def _backoff_gate(mode: str) -> bool:
    """Return True if we should proceed. --background refuses to fetch while
    backoff_until is in the future; foreground always proceeds but prints a
    warning so the user knows they're racing the ladder."""
    level, until = _backoff_state()
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


def _backoff_escalate() -> bool:
    """Bump the ladder one step after a 429. Returns False if we've blown
    past the last step (caller should abort with non-zero exit)."""
    level, _ = _backoff_state()
    new_level = level + 1
    if new_level > len(BACKOFF_LADDER_HOURS):
        print(f"\n!!! backoff exhausted (already waited "
              f"{BACKOFF_LADDER_HOURS[-1]}h and got 429 again). Aborting.")
        return False
    hours = BACKOFF_LADDER_HOURS[new_level - 1]
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    kv_set("backoff_level", str(new_level))
    kv_set("backoff_until", until.isoformat(timespec="seconds"))
    kv_set("backoff_last_429", now())
    print(f"  → escalated backoff to level {new_level}: pause {hours}h "
          f"(until {until.isoformat(timespec='seconds')}).")
    return True


def _backoff_reset() -> None:
    """Clear the ladder after any successful fetch."""
    level, _ = _backoff_state()
    if level > 0:
        print(f"  → cleared backoff (was level {level}).")
    kv_set("backoff_level", None)
    kv_set("backoff_until", None)


# === segment ORM helpers =====================================================


def add_segment(segment_id: int, discipline: str) -> str:
    """Register a segment id with its discipline. Returns one of
    'added' / 'updated' / 'unchanged' so the caller can report what happened."""
    try:
        seg = Segment.objects.get(pk=segment_id)
    except Segment.DoesNotExist:
        Segment.objects.create(id=segment_id, discipline=discipline)
        return "added"
    if seg.discipline != discipline:
        seg.discipline = discipline
        seg.save(update_fields=["discipline"])
        return "updated"
    return "unchanged"


def upsert_segment(seg: dict) -> None:
    """Upsert parsed segment fields. `streams` is stored as JSON text."""
    seg = dict(seg)
    if "streams" in seg:
        seg["streams_json"] = json.dumps(seg.pop("streams"))
    # Drop keys that aren't model fields
    seg.pop("leaders", None)
    cols = [
        "id", "name", "activity_type", "display_location", "climb_category",
        "is_verified", "distance_m", "avg_grade", "elev_low", "elev_high",
        "elev_gain", "gross_gain", "gross_loss", "terrain", "total_athletes",
        "total_efforts", "star_count", "start_lat", "start_lng", "end_lat",
        "end_lng", "map_image_url", "streams_json", "fetched_at",
    ]
    defaults = {c: seg.get(c) for c in cols if c != "id"}
    Segment.objects.update_or_create(id=seg["id"], defaults=defaults)


def upsert_athlete(athlete: dict, gender: str = "M") -> None:
    """Upsert an athlete row; avatar/badge are COALESCE-safe (only overwrite
    if new value is non-null, matching the db.py behaviour).

    gender applies only to NEW athletes created here — sets their initial
    gender to "M" when sourced from the overall board. For women's board
    rows the gender flip on existing Athlete rows is done in record_efforts
    (after bulk_create) not here, to avoid a per-row UPDATE overhead."""
    aid = athlete["id"]
    try:
        obj = Athlete.objects.get(pk=aid)
        obj.name = athlete.get("name")
        update_fields = ["name"]
        if athlete.get("avatar_url") is not None:
            obj.avatar_url = athlete["avatar_url"]
            update_fields.append("avatar_url")
        if athlete.get("badge") is not None:
            obj.badge = athlete["badge"]
            update_fields.append("badge")
        obj.save(update_fields=update_fields)
    except Athlete.DoesNotExist:
        # New athletes created during an overall-board fetch get gender="M";
        # during a women's-board fetch get gender="F". The sticky rule means
        # once "F" is set it is never overwritten back to "M".
        Athlete.objects.create(
            id=aid,
            name=athlete.get("name"),
            avatar_url=athlete.get("avatar_url"),
            badge=athlete.get("badge"),
            gender=gender,
        )


def record_efforts(segment_id: int, efforts: list[dict], observed_at: str,
                   gender: str = "M") -> None:
    """Log a freshly-fetched leaderboard into segments_effortlog.

    One row per distinct observed effort, identified by (segment, athlete,
    effort_id). Already-seen efforts are ignored — INSERT OR IGNORE semantics via
    bulk_create(ignore_conflicts=True).  The original observed_at is preserved
    for the rewind key.  An athlete missing from this fetch keeps their logged rows.

    gender="M"  (overall board, default): new rows stamped "M"; existing rows
                 left unchanged — never overwrite an "F" (sticky rule).
    gender="F"  (women's board): new rows stamped "F"; ALSO updates matching
                 EXISTING rows to "F" (overall INSERT usually beats the dedup race)
                 and flips involved Athlete rows to gender="F".
    """
    objs = []
    athlete_ids = []
    effort_keys: list[tuple[int, int, str | None]] = []  # (segment_id, athlete_id, effort_id)
    for e in efforts:
        aid = e.get("athlete_id")
        if aid is None:
            continue
        athlete_ids.append(aid)
        effort_keys.append((segment_id, aid, e.get("effort_id")))
        objs.append(EffortLog(
            segment_id=segment_id,
            athlete_id=aid,
            elapsed_time=e.get("elapsed_time"),
            avg_speed=e.get("avg_speed"),
            avg_watts=e.get("avg_watts"),
            avg_hr=e.get("avg_hr"),
            effort_id=e.get("effort_id"),
            activity_id=e.get("activity_id"),
            start_date_local=e.get("start_date_local"),
            observed_at=observed_at,
            gender=gender,
        ))
    if not objs:
        return
    EffortLog.objects.bulk_create(objs, ignore_conflicts=True)

    if gender == "F" and effort_keys:
        # The overall-board INSERT often wins the dedup race for the same
        # (segment, athlete, effort_id) triple, leaving those rows stamped "M".
        # Flip every matching existing row to "F" now (effort_id is unique
        # within a segment, so this hits exactly the fetched rows).
        EffortLog.objects.filter(
            segment_id=segment_id,
            effort_id__in=[eid for _, _, eid in effort_keys if eid is not None],
        ).update(gender="F")
        # Bubble gender="F" up to Athlete rows (sticky — never reverts to M).
        Athlete.objects.filter(
            id__in=set(athlete_ids)
        ).update(gender="F")


def set_geo_class(segment_id: int, cls: dict) -> None:
    """Persist the full geo classification (start/end/pass-through) plus the
    derived `in_town` (start OR finish in town)."""
    Segment.objects.filter(pk=segment_id).update(
        starts_in_town=int(cls["starts_in"]),
        ends_in_town=int(cls["ends_in"]),
        passes_through=int(cls["passes_through"]),
        in_town=int(cls["in_town"]),
    )


def set_parent_segment(segment_id: int, parent_id: int | None) -> None:
    """Persist the sub-segment parent (the longer same-direction segment this is
    a slice of), or NULL to clear it. Label only — does not affect scoring."""
    Segment.objects.filter(pk=segment_id).update(parent_segment_id=parent_id)


def set_efforts_fetched_at(segment_id: int, when: str) -> None:
    Segment.objects.filter(pk=segment_id).update(efforts_fetched_at=when)


def bump_depth_pages(segment_id: int, pages: int) -> None:
    """Record that we've fetched `pages` pages on this segment. Only writes
    if it deepens the existing high-water mark (so a depth=1 refresh after a
    depth=8 dive doesn't reset progress)."""
    Segment.objects.filter(pk=segment_id).filter(
        **{"last_depth_pages__lt": pages}
    ).update(last_depth_pages=pages)
    # Also handle NULL case (treat NULL as 0)
    Segment.objects.filter(pk=segment_id).filter(
        last_depth_pages__isnull=True
    ).update(last_depth_pages=pages)


def set_difficulty(segment_id: int, difficulty: float) -> None:
    Segment.objects.filter(pk=segment_id).update(difficulty=difficulty)


def segment_ids() -> list[int]:
    return list(Segment.objects.order_by("id").values_list("id", flat=True))


def unimported_segment_ids() -> list[int]:
    """Segments registered but never page-imported (fetched_at IS NULL)."""
    return list(Segment.objects.filter(
        fetched_at__isnull=True).order_by("id").values_list("id", flat=True))


# === effort helpers ===========================================================


def _store_efforts(sid: int, efforts: list, when: str, gender: str = "M") -> None:
    """Upsert the fetched athletes and append their efforts into the log.
    `when` is the observation time — the rewind key for effort_log.
    `gender` is passed through to record_efforts and upsert_athlete."""
    for eff in efforts:
        if eff["athlete_id"] is None:
            continue
        upsert_athlete({
            "id": eff["athlete_id"], "name": eff["athlete_name"],
            "avatar_url": eff["avatar_url"], "badge": eff["badge"]},
            gender=gender)
    record_efforts(sid,
                   [e for e in efforts if e["athlete_id"] is not None], when,
                   gender=gender)


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


# === refresh one segment ======================================================

# Sentinel set by `_refresh_one` when a stop-the-run condition fires (auth /
# rate-limit). Used by the loop in update command to break out cleanly.
_last_stop: str | None = None


def _refresh_one(client, sid: int, depth: int) -> bool:
    """Fetch the leaderboard for one segment at `depth` and persist. Returns
    True on success. Updates the 429 backoff ladder: success resets it; a
    rate-limit escalates it (and sets `_last_stop` to either 'ratelimit' or
    'ratelimit-exhausted' so the caller can decide whether to abort the whole
    process or just stop the loop).

    Each refresh is now 3 requests: overall (depth pages) + following + women's.
    All three share the same try/except — a 429 on the women's fetch escalates
    the ladder exactly like the others."""
    global _last_stop
    _last_stop = None
    print(f"> leaderboard {sid} (depth {depth}) ...", flush=True)
    try:
        efforts = client.fetch_leaderboard(sid, "overall", pages=depth)
        efforts = _supplement_following(client, sid, efforts)
        women = client.fetch_leaderboard(sid, "overall", pages=1, gender="female")
    except AuthError as e:
        print(f"\nAUTH ERROR: {e}")
        _last_stop = "auth"
        return False
    except RateLimitError as e:
        print(f"\nRATE LIMITED: {e}")
        if not _backoff_escalate():
            _last_stop = "ratelimit-exhausted"
        else:
            _last_stop = "ratelimit"
        return False
    except StravaError as e:
        print(f"! failed {sid}: {e}")
        return False
    stamp = now()
    _store_efforts(sid, efforts, stamp, gender="M")
    _store_efforts(sid, women, stamp, gender="F")
    set_efforts_fetched_at(sid, stamp)
    if depth > 1:
        bump_depth_pages(sid, depth)
    # Single commit per segment: set_efforts_fetched_at and bump_depth_pages
    # don't self-commit in the ORM, but their .update() calls auto-commit
    # in Django's default autocommit mode, so nothing extra is needed.
    _backoff_reset()
    print(f"  {len(efforts)} efforts (+{len(women)} women's board)")
    return True


# === background scheduler =====================================================


def _background_pick() -> tuple[int, int, str] | None:
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

    # Phase A: top-25 stale or never fetched
    with connection.cursor() as cur:
        cur.execute(
            "SELECT id FROM segments_segment WHERE "
            "in_town = 1 AND lower(activity_type) = 'ride' "
            "AND excluded = 0 AND fetched_at IS NOT NULL "
            "AND (efforts_fetched_at IS NULL OR efforts_fetched_at < %s) "
            "ORDER BY efforts_fetched_at IS NULL DESC, efforts_fetched_at ASC "
            "LIMIT 1", [week_ago])
        row = cur.fetchone()
    if row:
        return row[0], 1, "A (stale top-25)"

    # Phase B: build depth on the shallowest segment that still has more
    # leaderboard pages to fetch. last_depth_pages is the high-water mark;
    # NULL means we've never gone past the depth-1 import seed.
    with connection.cursor() as cur:
        cur.execute(
            "SELECT id, COALESCE(last_depth_pages, 1) AS depth "
            "FROM segments_segment WHERE "
            "in_town = 1 AND lower(activity_type) = 'ride' "
            "AND excluded = 0 AND fetched_at IS NOT NULL "
            "AND COALESCE(last_depth_pages, 1) * 25 < COALESCE(total_athletes, 0) "
            "ORDER BY COALESCE(last_depth_pages, 1) ASC, "
            "         efforts_fetched_at ASC NULLS FIRST "
            "LIMIT 1")
        row = cur.fetchone()
    if row:
        return row[0], row[1] + 1, "B (build depth)"

    # Phase C: everything's fresh AND everyone's at max depth. Refresh the
    # stalest top-25 anyway so the cron never silently no-ops.
    with connection.cursor() as cur:
        cur.execute(
            "SELECT id FROM segments_segment WHERE "
            "in_town = 1 AND lower(activity_type) = 'ride' "
            "AND excluded = 0 AND fetched_at IS NOT NULL "
            "ORDER BY efforts_fetched_at ASC NULLS FIRST LIMIT 1")
        row = cur.fetchone()
    if row:
        return row[0], 1, "C (idle maintenance)"
    return None


def background_tick(no_jitter: bool = False) -> None:
    """One unit of background work: jitter (if enabled), pick, fetch, report."""
    if not no_jitter:
        sleep_s = random.uniform(0, BACKGROUND_JITTER_S)
        print(f"[background] jitter sleep {sleep_s:.0f}s ...", flush=True)
        time.sleep(sleep_s)

    if not _backoff_gate("background"):
        return
    pick = _background_pick()
    if pick is None:
        print("[background] no tracked in-town ride segments — nothing to do.")
        return
    sid, depth, phase = pick
    print(f"[background] phase {phase} — segment {sid}, depth {depth}")
    run_started_at = now()
    with StravaClient() as client:
        _refresh_one(client, sid, depth)
    print_changelog(run_started_at)


def run_background(loop: bool = False, no_jitter: bool = False) -> None:
    """Background mode — picks ONE segment per tick by the phase-A/B/C
    priority. Without loop, runs exactly one tick (cron / launchd style).
    With loop, keeps ticking every BACKGROUND_TICK_INTERVAL_S seconds until
    Ctrl+C — the script is its own scheduler, so we skip the per-tick jitter
    (jitter exists to desynchronize cron firings, not to space loops)."""
    if loop:
        no_jitter = True
        try:
            while True:
                background_tick(no_jitter=no_jitter)
                if _last_stop == "ratelimit-exhausted":
                    print("[background] giving up the loop — rerun once the "
                          "rate limit has actually cleared.")
                    return
                print(f"[background] sleeping {BACKGROUND_TICK_INTERVAL_S}s "
                      "until next tick — Ctrl+C to stop", flush=True)
                time.sleep(BACKGROUND_TICK_INTERVAL_S)
        except KeyboardInterrupt:
            print("\n[background] stopped.")
        return
    background_tick(no_jitter=no_jitter)
    if _last_stop == "ratelimit-exhausted":
        raise SystemExit(2)


# === update targets ===========================================================


def update_targets(sid: int | None, all_: bool) -> list[int]:
    """In-town ride, not excluded, page already imported. With all_, stalest
    efforts_fetched_at first (NULLs first — freshly imported segments still
    only have page-seeded top-25 without the following supplement)."""
    if sid is not None:
        try:
            seg = Segment.objects.get(pk=sid)
        except Segment.DoesNotExist:
            sys.exit(f"segment {sid} is not tracked.")
        if seg.fetched_at is None:
            sys.exit(f"segment {sid} hasn't been imported yet — "
                     f"run `import {sid}` first.")
        if (seg.in_town != 1 or (seg.activity_type or "").lower() != "ride"
                or seg.excluded == 1):
            sys.exit(f"segment {sid} doesn't score "
                     "(not in-town, not a ride, or excluded) — nothing to update.")
        return [sid]
    # --all: use raw SQL for correct NULLS FIRST ordering and lower()
    with connection.cursor() as cur:
        cur.execute(
            "SELECT id FROM segments_segment WHERE fetched_at IS NOT NULL "
            "AND in_town = 1 AND lower(activity_type) = 'ride' AND excluded = 0 "
            "ORDER BY efforts_fetched_at IS NULL DESC, efforts_fetched_at ASC")
        targets = [r[0] for r in cur.fetchall()]
    if not targets:
        print("No in-town ride segments to update. "
              "Have you run `add --all` yet?")
    return targets


# === import logic =============================================================


def import_one(client, sid: int, force: bool = False) -> bool:
    """Fetch one segment's page (metadata, streams, geo) and seed its efforts.
    Returns True if the segment was imported (or re-imported), False if skipped."""
    try:
        seg_obj = Segment.objects.get(pk=sid)
    except Segment.DoesNotExist:
        sys.exit(f"segment {sid} is not tracked — `add` it first.")

    if seg_obj.fetched_at and not force:
        print(f"Segment {sid} already imported "
              f"(at {seg_obj.fetched_at}). Use --force to re-import.")
        return False

    print(f"> page {sid} ...", flush=True)
    try:
        seg = client.fetch_segment(sid)
    except AuthError as e:
        print(f"\nAUTH ERROR: {e}")
        raise
    except RateLimitError as e:
        print(f"\nRATE LIMITED: {e}")
        raise
    except StravaError as e:
        print(f"! failed {sid}: {e}")
        return False

    try:
        boundary = geo.load_boundary()
    except Exception as e:                                       # noqa: BLE001
        boundary = None
        print(f"! Shutesbury boundary unavailable ({e}); treating all as in-town")

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
    upsert_segment(seg)
    set_geo_class(sid, cls)

    # Seed top-25 from the embedded initialLeaderboard. The "following"
    # board (Andy/Owen below top 25) is left for `update` — keeping
    # import strictly one request per segment.
    seeded = seg.get("leaders") or []
    if cls["in_town"] and (seg["activity_type"] or "").lower() == "ride":
        _store_efforts(sid, seeded, now())

    where = ("in Shutesbury" if cls["in_town"]
             else ("passes through (no start/finish in town)"
                   if cls["passes_through"] else "OUTSIDE Shutesbury"))
    if (seg["activity_type"] or "").lower() != "ride":
        where += f" ({seg['activity_type']})"
    print(f"  {seg['name']} ({seg['display_location']}) — {where}")
    return True


def recompute_derived() -> None:
    """Sub-segment parents + difficulty across the current segment set. Run
    after an import that changed pages; both are idempotent and cheap."""
    seg_qs = Segment.objects.filter(fetched_at__isnull=False)
    segments = list(seg_qs.values(
        "id", "excluded", "activity_type", "in_town", "distance_m",
        "streams_json", "difficulty", "total_efforts", "terrain",
        "gross_gain", "gross_loss", "avg_grade",
    ))

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
        set_parent_segment(ci["id"], parent)
        n_sub += bool(parent)

    # Difficulty for every page-imported segment.
    for seg in segments:
        set_difficulty(seg["id"], scoring.segment_difficulty(seg))
    print(f"  recomputed difficulty for {len(segments)} segment(s); "
          f"tagged {n_sub} sub-segment(s)")


# === competition ranks ========================================================


def competition_ranks(state: dict) -> dict:
    """Strava-style competition ranking over {athlete_id: elapsed_time}: tied
    times share a rank and the next distinct time skips the tied-out places
    (1, 1, 3). Must agree with the RANK() in the `efforts` view (db.py).
    Timeless (None) entries are unranked."""
    items = sorted((t, aid) for aid, t in state.items() if t is not None)
    ranks, prev_t, rank = {}, None, 0
    for i, (t, aid) in enumerate(items):
        if t != prev_t:
            rank, prev_t = i + 1, t
        ranks[aid] = rank
    return ranks


# === changelog ================================================================


def print_changelog(since: str, until: str | None = None) -> None:
    """Print a changelog of effort changes whose observed_at falls in (since, until].

    Sourced from `segments_effortlog`: every effort first logged within the window is a
    change to surface. For each affected segment we reconstruct the prior state
    (each athlete's latest observation strictly before `since`), derive prior vs
    current ranks, score both, and report:

      - PR  — a faster time from someone we already knew
      - NEW — first observation we have of this athlete on this segment
      - ↕   — "bumped": no new time, but their rank shifted because someone
              else's new effort restructured the board

    Two views: by segment (what happened where) and by athlete (the King-points
    ledger). The athlete totals are net change in King points across the window."""
    upper_clause = "AND observed_at <= %s" if until else ""
    upper_args: list = [until] if until else []
    with connection.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT segment_id FROM segments_effortlog "
            f"WHERE observed_at >= %s {upper_clause}",
            [since, *upper_args])
        affected = [r[0] for r in cur.fetchall()]

    range_str = f"since {since}" + (f" until {until}" if until else "")
    if not affected:
        print(f"\nNo effort changes {range_str} — nothing to changelog.")
        return

    in_clause = ",".join(["%s"] * len(affected))
    with connection.cursor() as cur:
        cur.execute(
            f"SELECT id, name, difficulty, total_efforts, terrain, in_town, "
            f"activity_type, excluded FROM segments_segment WHERE id IN ({in_clause})",
            affected)
        cols = [d[0] for d in cur.description]
        seg_rows = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}

        cur.execute(
            "SELECT id FROM segments_segment WHERE in_town = 1 "
            "AND lower(activity_type) = 'ride' AND excluded = 0 "
            "AND difficulty IS NOT NULL ORDER BY difficulty DESC LIMIT 1")
        cima_row = cur.fetchone()
    cima_id = cima_row[0] if cima_row else None

    with connection.cursor() as cur:
        cur.execute("SELECT id, name FROM segments_athlete")
        name_map = {r[0]: r[1] for r in cur.fetchall()}

    def aname(aid: int) -> str:
        return name_map.get(aid) or f"Athlete {aid}"

    def fmt_t(t):
        if t is None:
            return "—"
        m, s = divmod(int(t), 60)
        return f"{m}:{s:02d}"

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

        with connection.cursor() as cur:
            cur.execute(
                "SELECT eo.athlete_id, eo.elapsed_time FROM segments_effortlog eo "
                "JOIN (SELECT athlete_id, MAX(id) AS mid FROM segments_effortlog "
                "      WHERE segment_id = %s AND observed_at < %s GROUP BY athlete_id) j "
                "  ON j.athlete_id = eo.athlete_id AND j.mid = eo.id",
                [sid, since])
            prior_rows = cur.fetchall()
        prior = {r[0]: r[1] for r in prior_rows}

        # "Current" = best-known state at the end of the window. When `until` is
        # None this is just the materialized `efforts` view; otherwise we
        # recompute the best time per athlete bounded by `until`.
        if until is None:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT athlete_id, elapsed_time FROM efforts WHERE segment_id = %s",
                    [sid])
                cur_rows = cur.fetchall()
        else:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT athlete_id, MIN(elapsed_time) AS elapsed_time "
                    "FROM segments_effortlog WHERE segment_id = %s AND observed_at <= %s "
                    "GROUP BY athlete_id",
                    [sid, until])
                cur_rows = cur.fetchall()
        current = {r[0]: r[1] for r in cur_rows}

        is_new = not prior
        if is_new and scores:
            new_segments.append(seg)

        prior_ranks = competition_ranks(prior)
        cur_ranks = competition_ranks(current)

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


# === resolve log since ========================================================


def resolve_log_since(raw: str | None) -> str:
    """Default: a minute before the most recent observation, so `log` with no
    args shows the last run's changes."""
    if raw:
        return raw
    with connection.cursor() as cur:
        cur.execute("SELECT MAX(observed_at) AS m FROM segments_effortlog")
        row = cur.fetchone()
    if row and row[0]:
        return (_parse_ts(row[0]) - timedelta(minutes=1)).isoformat()
    return (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
