"""manage.py list — show every tracked segment."""

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "List every tracked segment."

    def handle(self, *args, **options):
        # Use raw SQL for correct NULLS LAST ordering (SQLite requires explicit
        # "NULLS LAST" syntax; Django ORM wraps it as a CASE for older backends).
        with connection.cursor() as cur:
            cur.execute(
                "SELECT id, name, discipline, terrain, difficulty, fetched_at, "
                "efforts_fetched_at FROM segments_segment "
                "ORDER BY difficulty DESC NULLS LAST, id"
            )
            rows = cur.fetchall()
        if not rows:
            print("No segments tracked yet.")
            return
        for r in rows:
            sid, name, discipline, terrain, difficulty, fetched_at, efforts_fetched_at = r
            page = fetched_at[:10] if fetched_at else "—"
            lb = efforts_fetched_at[:10] if efforts_fetched_at else "—"
            print(f"{sid:>12}  {name or '(unimported)':30} "
                  f"{discipline or '?':6} {terrain or '':8} "
                  f"diff={difficulty or '-':>6}  "
                  f"page={page}  lb={lb}")
