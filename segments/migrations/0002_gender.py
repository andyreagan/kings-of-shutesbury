# Add gender tracking for the Queens of Shutesbury women's leaderboard.
#
# Gender lives at both levels:
#   - EffortLog.gender: "M" default (overall board); flipped to "F" when the
#     effort appears on the women's board. The overall INSERT usually wins the
#     dedup race, so we UPDATE after women's fetches.
#   - Athlete.gender: canonical bubble-up. Set to "F" once seen on any women's
#     board. STICKY — F never downgrades back to M (everyone appears on the
#     overall board too).
#
# The data migration sets gender="M" on ALL existing rows so downstream code
# can assume non-NULL for pre-existing data. New women's-board efforts are
# written with "F" by the pipeline and never revert.

from django.db import migrations, models


def set_existing_gender_m(apps, schema_editor):
    """Stamp all pre-existing Athlete and EffortLog rows with gender='M'.
    New women's-board rows will be created with 'F' by the pipeline."""
    db = schema_editor.connection.alias
    apps.get_model("segments", "Athlete").objects.using(db).filter(
        gender__isnull=True
    ).update(gender="M")
    apps.get_model("segments", "EffortLog").objects.using(db).filter(
        gender__isnull=True
    ).update(gender="M")


def noop(apps, schema_editor):
    """Reverse: leave the column in place — data rollback is a noop."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("segments", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="athlete",
            name="gender",
            field=models.TextField(
                blank=True,
                null=True,
                help_text=(
                    "Canonical gender: 'F' once seen on any women's board (sticky — "
                    "never reverts to M). NULL = unset (legacy rows)."
                ),
            ),
        ),
        migrations.AddField(
            model_name="effortlog",
            name="gender",
            field=models.TextField(
                blank=True,
                null=True,
                help_text=(
                    "'M' = sourced from overall board (default); "
                    "'F' = sourced from women's board."
                ),
            ),
        ),
        migrations.RunPython(set_existing_gender_m, reverse_code=noop),
    ]
