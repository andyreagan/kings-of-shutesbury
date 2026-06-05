"""manage.py export — rebuild web/data.json from the DB.

No network, no options.
"""

from django.core.management.base import BaseCommand

from segments.export import export_data_json


class Command(BaseCommand):
    help = "Rebuild web/data.json from the DB. No network, no options."

    def handle(self, *args, **options):
        export_data_json()
