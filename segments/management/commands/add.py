"""manage.py add — MERGED add+import.

Registers (id, discipline) pairs, then imports every unimported segment
(including the just-added ones). --force additionally re-imports the explicitly
listed ids even if already imported. With NO pairs given, it just imports any
unimported segments (a resume sweep). Network errors handled like cmd_import.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from segments import pipeline
from segments.models import Segment
from segments.strava import AuthError, RateLimitError, StravaClient


class Command(BaseCommand):
    help = (
        "Register (id, discipline) pairs then import every unimported segment. "
        "With no pairs given, imports any unimported segments (resume sweep). "
        "--force re-imports the explicitly listed ids even if already imported."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "pairs", nargs="*", metavar="id|url discipline",
            help=(
                "Alternating id-or-URL and discipline "
                f"({'/'.join(pipeline.VALID_DISCIPLINES)}). "
                "Omit to import unimported segments only."
            ),
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Re-import the explicitly listed ids even if already imported.",
        )

    def handle(self, *args, **options):
        pairs_raw = options["pairs"]
        force = options["force"]

        # --- parse (id, discipline) pairs ------------------------------------
        if len(pairs_raw) % 2 != 0:
            sys.exit("add takes (id, discipline) pairs — got an odd number of args.")

        explicit_ids: list[int] = []
        for ref, disc in zip(pairs_raw[0::2], pairs_raw[1::2]):
            disc = disc.lower()
            if disc not in pipeline.VALID_DISCIPLINES:
                sys.exit(
                    f"discipline must be one of {pipeline.VALID_DISCIPLINES}, "
                    f"got {disc!r}"
                )
            sid = pipeline._parse_ref(ref)
            if sid is None:
                sys.exit(
                    f"couldn't parse a segment id from {ref!r} "
                    "(use a numeric id or a strava.com/segments/<id> URL)"
                )
            explicit_ids.append(sid)
            outcome = pipeline.add_segment(sid, disc)
            mark = {
                "added": "+ added",
                "updated": "~ discipline updated",
                "unchanged": "= already tracked",
            }[outcome]
            print(f"{mark}: {sid} ({disc})")

        # --- decide what to import -------------------------------------------
        # Every unimported segment, plus the explicitly listed ones (so a
        # re-listed already-imported id gets import_one's skip message, or a
        # real re-import under --force).
        targets = list(dict.fromkeys(pipeline.unimported_segment_ids() + explicit_ids))
        if not targets:
            total = Segment.objects.count()
            if total == 0:
                print("No segments tracked yet. Add some (id discipline ...) first.")
            else:
                print(
                    f"All {total} tracked segment(s) already imported. "
                    "Use --force to re-import."
                )
            return

        imported: list[int] = []
        with StravaClient() as client:
            for sid in targets:
                use_force = force and sid in set(explicit_ids)
                try:
                    did_import = pipeline.import_one(client, sid, force=use_force)
                except AuthError:
                    return
                except RateLimitError:
                    break
                if did_import:
                    imported.append(sid)

        if imported:
            pipeline.recompute_derived()
            print(
                f"\nImported {len(imported)} segment(s). "
                "Run `uv run manage.py update --all` to refresh leaderboards."
            )
