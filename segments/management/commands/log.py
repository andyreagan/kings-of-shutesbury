"""manage.py log — effort changelog over a date range.

Default --since picks up the most recent batch of observations (i.e. the last run).
"""

from django.core.management.base import BaseCommand

from segments import pipeline


class Command(BaseCommand):
    help = "Effort changelog over a date range. Default --since is the last run."

    def add_arguments(self, parser):
        parser.add_argument(
            "--since", metavar="DATE",
            help="ISO date or datetime (default: last run).",
        )
        parser.add_argument(
            "--until", metavar="DATE",
            help="ISO date or datetime (default: now).",
        )

    def handle(self, *args, **options):
        since = pipeline.resolve_log_since(options.get("since"))
        pipeline.print_changelog(since, options.get("until"))
