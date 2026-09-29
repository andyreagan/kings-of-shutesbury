# Scheduler state for the fresh/walk background picker.
#
# The leaderboard endpoint takes a date_range filter, so "what changed" can be
# asked for directly instead of re-walking every page:
#   - delta_*: the date-window pull (this_month / this_year) that keeps every
#     rank fresh. delta_fetched_at is the start time of the last COMPLETE pull
#     (any window), delta_year_fetched_at of the last this_year one;
#     delta_cursor holds a pull that spans ticks.
#   - walk_*: the slow full re-walk of the all-time board, which catches what a
#     window cannot (late uploads, un-hidden old efforts).
#
# All five start NULL: every segment is immediately due for a this_year pull
# and is queued for a re-walk.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("segments", "0003_climb_category_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="segment", name="delta_fetched_at",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="segment", name="delta_year_fetched_at",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="segment", name="delta_cursor",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="segment", name="walk_completed_at",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="segment", name="walk_cursor",
            field=models.TextField(blank=True, null=True),
        ),
    ]
