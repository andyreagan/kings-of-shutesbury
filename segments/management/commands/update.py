"""manage.py update — refresh efforts from the leaderboard.

  update (<id> | --all | --background) [--depth N] [--no-jitter] [--loop]

Ports cmd_update + cmd_update_background verbatim including mode validation
messages, changelog print, and SystemExit(2) on rate-limit exhaustion
(CommandError sets exit code 1, so we raise SystemExit directly to get 2).
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from segments import pipeline
from segments.strava import StravaClient


class Command(BaseCommand):
    help = "Refresh efforts from the leaderboard."

    def add_arguments(self, parser):
        parser.add_argument(
            "id", nargs="?", type=int,
            help="A single segment id.",
        )
        parser.add_argument(
            "--all", dest="all_", action="store_true",
            help="Refresh every in-town ride, stalest first.",
        )
        parser.add_argument(
            "--background", action="store_true",
            help=(
                "One background tick — jittered sleep, then auto-pick + refresh "
                "ONE segment (top-25 weekly, depth-build with leftover ticks). "
                "Wire to a 5-minute cron / launchd job."
            ),
        )
        parser.add_argument(
            "--no-jitter", action="store_true",
            help="With --background: skip the random sleep (testing, or if the "
                 "scheduler already jitters).",
        )
        parser.add_argument(
            "--loop", action="store_true",
            help=(
                "With --background: keep ticking every 5 min "
                "(implies --no-jitter; Ctrl+C to stop). "
                "For running in a shell when you want to watch."
            ),
        )
        parser.add_argument(
            "--depth", type=int, default=1, metavar="N",
            help="Pages of 25 to pull (default 1 = top 25); ignored with --background.",
        )

    def handle(self, *args, **options):
        sid = options["id"]
        all_ = options["all_"]
        background = options["background"]
        no_jitter = options["no_jitter"]
        loop = options["loop"]
        depth = options["depth"]

        # Validate mode — update takes exactly one of <id>, --all, --background.
        # argparse's mutually_exclusive_group misbehaves with nargs="?" positionals,
        # so check by hand.
        modes = [sid is not None, all_, background]
        if sum(modes) != 1:
            sys.exit("update: pass exactly one of <id>, --all, or --background.")
        if background and depth != 1:
            sys.exit("update --background chooses depth itself; "
                     "don't combine with --depth.")
        if loop and not background:
            sys.exit("update --loop only makes sense with --background.")

        if background:
            pipeline.run_background(loop=loop, no_jitter=no_jitter)
            return

        # Foreground (single id or --all)
        pipeline._backoff_gate("foreground")     # informational only
        targets = pipeline.update_targets(sid, all_)
        if not targets:
            return
        # Captured BEFORE any fetch so the changelog reads every observation
        # this run wrote.
        run_started_at = pipeline.now()
        with StravaClient() as client:
            for target_sid in targets:
                pipeline._refresh_one(client, target_sid, depth)
                if pipeline._last_stop:
                    break
        pipeline.print_changelog(run_started_at)
        if pipeline._last_stop == "ratelimit-exhausted":
            raise SystemExit(2)
