"""Strava internal-web-API client.

Reads segment metadata/measurements/streams from the Next.js page payload
(`__NEXT_DATA__`) and athlete efforts from the leaderboard JSON endpoint, using
a logged-in `_strava4_session` cookie.

Deliberately gentle: a single client, paced requests with jitter, and a cap on
leaderboard pages. Reads (no writes), so no CSRF token is needed.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path

import httpx

ENV_PATH = Path(__file__).resolve().parent / ".env"
BASE = "https://www.strava.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

# Politeness knobs.
MIN_INTERVAL = 4.0          # minimum seconds between requests
JITTER = (1.0, 2.5)         # extra random seconds added to each gap
MAX_LEADERBOARD_PAGES = 1   # 25 athletes/page; top-10 (all that scores) fits easily
# Strava's www endpoints sit behind CloudFront and return a header-less 429
# (no Retry-After, no X-RateLimit). Re-poking just keeps the rolling window hot,
# so we STOP the whole run on the first 429 and resume after a real cooldown.

# Terrain classification by average grade (percent).
CLIMB_GRADE = 1.5
DESCENT_GRADE = -1.5

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


class StravaError(RuntimeError):
    pass


class AuthError(StravaError):
    pass


class RateLimitError(StravaError):
    """Raised when Strava keeps returning 429 after we back off and retry."""
    pass


def load_session_cookie() -> str:
    """Cookie value from env, falling back to the .env file."""
    for key in ("_STRAVA4_SESSION", "STRAVA_SESSION"):
        if os.environ.get(key):
            return os.environ[key].strip()
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() in ("_STRAVA4_SESSION", "STRAVA_SESSION"):
                return v.strip().strip('"').strip("'")
    raise AuthError(
        "No session cookie found. Set _STRAVA4_SESSION in .env "
        "(copy .env.example)."
    )


def classify_terrain(avg_grade: float | None) -> str:
    if avg_grade is None:
        return "flat"
    if avg_grade >= CLIMB_GRADE:
        return "climb"
    if avg_grade <= DESCENT_GRADE:
        return "descent"
    return "flat"


def _gross_gain_loss(elevation: list[float]) -> tuple[float, float]:
    gain = loss = 0.0
    for a, b in zip(elevation, elevation[1:]):
        d = b - a
        if d > 0:
            gain += d
        else:
            loss -= d
    return round(gain, 1), round(loss, 1)


class StravaClient:
    def __init__(self, session_cookie: str | None = None):
        cookie = session_cookie or load_session_cookie()
        self._client = httpx.Client(
            base_url=BASE,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
            cookies={"_strava4_session": cookie},
            timeout=30.0,
            follow_redirects=True,
        )
        self._last_request = 0.0

    def __enter__(self) -> "StravaClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        gap = MIN_INTERVAL + random.uniform(*JITTER)
        wait = self._last_request + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _get(self, url: str, **kwargs) -> httpx.Response:
        self._throttle()
        resp = self._client.get(url, **kwargs)
        if resp.status_code in (401, 403):
            raise AuthError(
                f"{resp.status_code} for {url} — session cookie is likely "
                "expired. Re-paste _strava4_session into .env."
            )
        if resp.status_code == 429:
            raise RateLimitError(
                f"429 from CloudFront on {url} (no Retry-After header). "
                "Stopping the run to let the rolling window drain; "
                "rerun later to resume from where we left off."
            )
        resp.raise_for_status()
        return resp

    # -- segment page ---------------------------------------------------------

    def fetch_segment(self, segment_id: int) -> dict:
        resp = self._get(f"/segments/{segment_id}")
        m = _NEXT_DATA_RE.search(resp.text)
        if not m:
            raise StravaError(f"No __NEXT_DATA__ for segment {segment_id}")
        pp = json.loads(m.group(1))["props"]["pageProps"]
        if pp.get("measurements") is None:
            raise AuthError(
                f"Segment {segment_id} returned the logged-out payload — "
                "session cookie is not being accepted."
            )
        meta = pp.get("metadata") or {}
        meas = pp.get("measurements") or {}
        streams = pp.get("streams") or {}
        elevation = streams.get("elevation") or []
        location = streams.get("location") or []
        gross_gain, gross_loss = _gross_gain_loss(elevation)
        map_images = pp.get("mapImages") or []

        return {
            "id": int(segment_id),
            "name": meta.get("name"),
            "activity_type": meta.get("activityType"),
            "display_location": meta.get("displayLocation"),
            "climb_category": meta.get("climbCategory"),
            "is_verified": 1 if meta.get("isVerified") else 0,
            "distance_m": meas.get("distance"),
            "avg_grade": meas.get("avgGrade"),
            "elev_low": meas.get("elevLow"),
            "elev_high": meas.get("elevHigh"),
            "elev_gain": meas.get("elevGain"),
            "gross_gain": gross_gain,
            "gross_loss": gross_loss,
            "terrain": classify_terrain(meas.get("avgGrade")),
            "total_athletes": pp.get("totalAthletes"),
            "total_efforts": pp.get("totalEfforts"),
            "star_count": pp.get("starCount"),
            "start_lat": location[0][0] if location else None,
            "start_lng": location[0][1] if location else None,
            "end_lat": location[-1][0] if location else None,
            "end_lng": location[-1][1] if location else None,
            "map_image_url": map_images[0]["url"] if map_images else None,
            "streams": {
                "distance": streams.get("distance"),
                "elevation": elevation,
                "location": location,
            },
        }

    # -- leaderboard ----------------------------------------------------------

    def fetch_leaderboard(self, segment_id: int,
                          filter_type: str = "overall") -> list[dict]:
        """Full overall leaderboard as effort dicts (one per athlete)."""
        efforts: list[dict] = []
        total = None
        for page in range(1, MAX_LEADERBOARD_PAGES + 1):
            resp = self._get(
                f"/frontend/segments/{segment_id}/leaderboard",
                params={"filter_type": filter_type, "gender": "overall",
                        "page": page},
            )
            data = resp.json()
            rows = data.get("leaderboard") or []
            if total is None:
                total = data.get("totalCount")
            for r in rows:
                efforts.append({
                    "athlete_id": r.get("athleteId"),
                    "athlete_name": r.get("displayName"),
                    "avatar_url": r.get("avatar"),
                    "badge": r.get("badge"),
                    "rank": r.get("rank"),
                    "elapsed_time": r.get("elapsedTime"),
                    "avg_speed": r.get("avgSpeed"),
                    "avg_watts": r.get("avgWatts"),
                    "avg_hr": r.get("avgHr"),
                    "effort_id": r.get("effortId"),
                    "activity_id": r.get("activityId"),
                    "start_date_local": r.get("startDateLocal"),
                })
            if not rows or (total is not None and len(efforts) >= total):
                break
        return efforts
