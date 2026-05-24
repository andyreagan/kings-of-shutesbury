#!/usr/bin/env python3
"""Refresh tracked Strava segments and rebuild the dashboard data.

Usage:
    uv run update_segments.py add <id|url> [<id|url> ...]   # track new segments
    uv run update_segments.py                               # refresh stale + export
    uv run update_segments.py --force                       # refresh everything
    uv run update_segments.py --list                        # show tracked segments

The list of segment IDs lives in the SQLite `segments` table; the DB is committed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import geo
import scoring
from strava import AuthError, RateLimitError, StravaClient, StravaError

WEB_DIR = Path(__file__).resolve().parent / "web"
DATA_JSON = WEB_DIR / "data.json"
FRESH_HOURS = 24            # skip segments fetched more recently than this
PROFILE_POINTS = 120        # downsample elevation profile to this many points


def resolve_segment_id(client: StravaClient, raw: str) -> int | None:
    """Accept a numeric id, a /segments/<id> URL, or a strava.app.link short URL."""
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    m = re.search(r"/segments/(\d+)", raw)
    if m:
        return int(m.group(1))
    if raw.startswith("http"):
        # Short links (strava.app.link/...) redirect to the canonical segment URL.
        try:
            resp = client._get(raw)
            m = re.search(r"/segments/(\d+)", str(resp.url)) or re.search(
                r"/segments/(\d+)", resp.text)
            if m:
                return int(m.group(1))
        except StravaError as e:
            print(f"  could not resolve {raw}: {e}", file=sys.stderr)
    return None


def _downsample(xs: list, n: int) -> list:
    if not xs or len(xs) <= n:
        return xs
    step = (len(xs) - 1) / (n - 1)
    return [xs[round(i * step)] for i in range(n)]


def cmd_add(args) -> None:
    conn = db.connect()
    db.init(conn)
    with StravaClient() as client:
        for raw in args.refs:
            sid = resolve_segment_id(client, raw)
            if sid is None:
                print(f"! skipped (couldn't parse a segment id): {raw}")
                continue
            added = db.add_segment_id(conn, sid)
            print(f"{'+ added' if added else '= already tracked'}: {sid}")
    conn.close()
    print("\nRun `uv run update_segments.py` to fetch their data.")


def cmd_add_athlete(args) -> None:
    conn = db.connect()
    db.init(conn)
    for aid in args.ids:
        added = db.add_featured_athlete(conn, aid)
        print(f"{'+ added page for' if added else '= already featured'}: athlete {aid}")
    export_data_json(conn)        # no network; just refreshes featured list
    conn.close()


def _is_fresh(fetched_at: str | None) -> bool:
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ts < timedelta(hours=FRESH_HOURS)


def cmd_update(args) -> None:
    conn = db.connect()
    db.init(conn)
    ids = db.segment_ids(conn)
    if not ids:
        print("No segments tracked yet. Add some:\n"
              "  uv run update_segments.py add 38206226")
        return

    rows = {r["id"]: r for r in conn.execute(
        "SELECT id, fetched_at, efforts_fetched_at, in_shutesbury, activity_type "
        "FROM segments")}
    featured_ids = set(db.featured_athlete_ids(conn))
    try:
        boundary = geo.load_boundary()
    except Exception as e:                                      # noqa: BLE001
        boundary = None
        print(f"! Shutesbury boundary unavailable ({e}); treating all as in-town")

    def now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    stopped = False
    with StravaClient() as client:
        for sid in ids:
            row = rows[sid]
            in_town = row["in_shutesbury"] == 1
            is_ride = (row["activity_type"] or "").lower() == "ride"

            # --- Phase 1: segment page (cheap; not the rate-limited endpoint) ---
            need_page = (args.force or row["fetched_at"] is None
                         or not _is_fresh(row["fetched_at"]))
            if need_page:
                print(f"> page {sid} ...", flush=True)
                try:
                    seg = client.fetch_segment(sid)
                except AuthError as e:
                    print(f"\nAUTH ERROR: {e}")
                    return
                except RateLimitError as e:
                    print(f"\nRATE LIMITED (segment page): {e}")
                    stopped = True
                    break
                except StravaError as e:
                    print(f"! failed page {sid}: {e}")
                    continue
                in_town = (geo.segment_in_shutesbury(seg, boundary)
                           if boundary is not None else True)
                is_ride = (seg["activity_type"] or "").lower() == "ride"
                seg["in_shutesbury"] = 1 if in_town else 0
                seg["fetched_at"] = now()
                db.upsert_segment(conn, seg)
                db.set_in_shutesbury(conn, sid, seg["in_shutesbury"])
                conn.commit()
                where = "in Shutesbury" if in_town else "OUTSIDE Shutesbury"
                if not is_ride:
                    where += f" ({seg['activity_type']})"
                print(f"  {seg['name']} ({seg['display_location']}) — {where}")

            # --- Phase 2: leaderboard (the rate-limited endpoint) ---
            if args.pages_only:
                continue
            need_lb = (in_town and is_ride) and (
                args.force or row["efforts_fetched_at"] is None
                or not _is_fresh(row["efforts_fetched_at"]))
            if not need_lb:
                continue
            print(f"> leaderboard {sid} ...", flush=True)
            try:
                # Overall page 1 (top 25) — authoritative ranks for scoring.
                efforts = client.fetch_leaderboard(sid, "overall")
                # Following board: ONLY featured athletes' times not already in
                # the top 25. Following ranks are relative, so store rank=None.
                if featured_ids:
                    have = {e["athlete_id"] for e in efforts}
                    for e in client.fetch_leaderboard(sid, "following"):
                        if e["athlete_id"] in featured_ids and e["athlete_id"] not in have:
                            efforts.append({**e, "rank": None})
            except AuthError as e:
                print(f"\nAUTH ERROR: {e}")
                return
            except RateLimitError as e:
                print(f"\nRATE LIMITED (leaderboard): {e}\n"
                      "Progress saved — rerun later to resume the leaderboards.")
                stopped = True
                break
            except StravaError as e:
                print(f"! failed leaderboard {sid}: {e}")
                continue
            for eff in efforts:
                if eff["athlete_id"] is None:
                    continue
                db.upsert_athlete(conn, {
                    "id": eff["athlete_id"], "name": eff["athlete_name"],
                    "avatar_url": eff["avatar_url"], "badge": eff["badge"]})
            db.replace_efforts(conn, sid, [
                {k: eff[k] for k in (
                    "athlete_id", "rank", "elapsed_time", "avg_speed",
                    "avg_watts", "avg_hr", "effort_id", "activity_id",
                    "start_date_local")}
                for eff in efforts if eff["athlete_id"] is not None])
            db.set_efforts_fetched_at(conn, sid, now())
            conn.commit()
            print(f"  {len(efforts)} efforts")
    conn.commit()
    export_data_json(conn)
    if stopped:
        print("\nStopped early on a rate limit. Rerun `uv run update_segments.py` "
              "to continue from where we left off.")
    conn.close()


def _segment_with_efforts(conn, seg_row) -> dict:
    seg = dict(seg_row)
    efforts = [dict(r) for r in conn.execute(
        "SELECT e.*, a.name AS athlete_name, a.avatar_url, a.badge "
        "FROM efforts e JOIN athletes a ON a.id = e.athlete_id "
        "WHERE e.segment_id = ? ORDER BY e.rank IS NULL, e.rank", (seg["id"],))]
    seg["efforts"] = efforts
    return seg


def export_data_json(conn) -> None:
    seg_rows = conn.execute(
        "SELECT * FROM segments WHERE fetched_at IS NOT NULL").fetchall()
    segments = [_segment_with_efforts(conn, r) for r in seg_rows]

    # Geo-classify: a segment counts only if it STARTS or FINISHES in Shutesbury.
    # Computed once and persisted to `in_shutesbury` so later builds don't refilter.
    try:
        boundary = geo.load_boundary()
    except Exception as e:                                      # noqa: BLE001
        boundary = None
        print(f"! Shutesbury boundary unavailable ({e}); not applying geo filter")
    if boundary is not None:
        newly = 0
        for seg in segments:
            if seg["in_shutesbury"] is None:
                seg["in_shutesbury"] = 1 if geo.segment_in_shutesbury(seg, boundary) else 0
                db.set_in_shutesbury(conn, seg["id"], seg["in_shutesbury"])
                newly += 1
        conn.commit()
        if newly:
            print(f"  classified {newly} segment(s) against the Shutesbury boundary")

    # Compute + persist difficulty for every fetched segment (so --list is useful).
    for seg in segments:
        seg["difficulty"] = scoring.segment_difficulty(seg)
        db.set_difficulty(conn, seg["id"], seg["difficulty"])
    conn.commit()

    # A segment only counts toward standings if it's a Ride that's in Shutesbury.
    def is_included(seg) -> bool:
        ride = (seg["activity_type"] or "").lower() == "ride"
        in_town = seg["in_shutesbury"] == 1 if boundary is not None else True
        return ride and in_town

    included = [s for s in segments if is_included(s)]
    filtered = []
    for s in segments:
        if is_included(s):
            continue
        reasons = []
        if (s["activity_type"] or "").lower() != "ride":
            reasons.append((s["activity_type"] or "non-ride").lower())
        if boundary is not None and s["in_shutesbury"] != 1:
            reasons.append("outside Shutesbury")
        filtered.append({"id": s["id"], "name": s["name"],
                         "location": s["display_location"],
                         "reason": ", ".join(reasons) or "excluded"})

    king = scoring.king_standings(included)

    featured = []
    for aid in db.featured_athlete_ids(conn):
        row = conn.execute(
            "SELECT name, avatar_url FROM athletes WHERE id = ?", (aid,)).fetchone()
        featured.append({
            "id": aid,
            "name": row["name"] if row else None,
            "avatar_url": row["avatar_url"] if row else None,
        })

    out_segments = []
    for seg in included:
        streams = json.loads(seg.get("streams_json") or "{}")
        leader = seg["efforts"][0] if seg["efforts"] else None
        out_segments.append({
            "id": seg["id"],
            "name": seg["name"],
            "location": seg["display_location"],
            "activity_type": seg["activity_type"],
            "discipline": seg["discipline"],
            "terrain": seg["terrain"],
            "distance_m": seg["distance_m"],
            "avg_grade": seg["avg_grade"],
            "elev_gain": seg["elev_gain"],
            "gross_gain": seg["gross_gain"],
            "gross_loss": seg["gross_loss"],
            "total_efforts": seg["total_efforts"],
            "total_athletes": seg["total_athletes"],
            "difficulty": seg["difficulty"],
            "map_image_url": seg["map_image_url"],
            "start_latlng": [seg["start_lat"], seg["start_lng"]],
            "end_latlng": [seg["end_lat"], seg["end_lng"]],
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
                "points": scoring.points_for_rank(e["rank"], seg["difficulty"]),
            } for e in seg["efforts"]],
        })
    out_segments.sort(key=lambda s: s["difficulty"], reverse=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "king": king,
        "featured_athletes": featured,
        "segments": out_segments,
        "filtered": filtered,
    }
    WEB_DIR.mkdir(exist_ok=True)
    DATA_JSON.write_text(json.dumps(payload, indent=2))
    print(f"\nWrote {DATA_JSON} — {len(out_segments)} in-Shutesbury ride "
          f"segments, {len(king)} ranked athletes, {len(filtered)} filtered out.")


def cmd_list(args) -> None:
    conn = db.connect()
    db.init(conn)
    rows = conn.execute(
        "SELECT id, name, terrain, difficulty, fetched_at FROM segments "
        "ORDER BY difficulty DESC NULLS LAST, id").fetchall()
    if not rows:
        print("No segments tracked yet.")
        return
    for r in rows:
        print(f"{r['id']:>12}  {r['name'] or '(unfetched)':30} "
              f"{r['terrain'] or '':8} diff={r['difficulty'] or '-':>6}  "
              f"{'fetched ' + r['fetched_at'] if r['fetched_at'] else 'never fetched'}")
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="refetch even recently-updated segments")
    parser.add_argument("--pages-only", action="store_true",
                        help="fetch + classify segment pages, skip leaderboards "
                             "(the rate-limited endpoint) for later")
    parser.add_argument("--list", action="store_true",
                        help="list tracked segments and exit")
    sub = parser.add_subparsers(dest="command")
    p_add = sub.add_parser("add", help="track new segment id(s) or URL(s)")
    p_add.add_argument("refs", nargs="+")
    p_addath = sub.add_parser("add-athlete", help="give an athlete their own page")
    p_addath.add_argument("ids", nargs="+", type=int)
    args = parser.parse_args()

    if args.command == "add":
        cmd_add(args)
    elif args.command == "add-athlete":
        cmd_add_athlete(args)
    elif args.list:
        cmd_list(args)
    else:
        cmd_update(args)


if __name__ == "__main__":
    main()
