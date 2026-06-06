"""Django ORM models mirroring the Kings of Shutesbury schema (strava.db).

Table layout (Django default names — one-time rename from legacy names required):
  segments_segment    — Strava segment metadata + computed fields
  segments_athlete    — rider identity (id, name, avatar, badge)
  segments_kv         — key/value store for pipeline state (backoff ladder etc.)
  segments_effortlog  — append-only effort log; one row per observed effort
  efforts             — SQL VIEW (unmanaged): best-per-athlete + competition rank

Ranking semantics (Effort view): Strava-style competition ranking — tied times
share a rank and the next distinct time skips tied-out places (1, 1, 3).
`first_seen`/`last_seen` are the min/max observed_at across each athlete's logged
efforts; rank is NULL when elapsed_time is NULL.
"""

from django.db import models


# ---------------------------------------------------------------------------
# Segment
# ---------------------------------------------------------------------------

class Segment(models.Model):
    """A Strava cycling segment tracked by the pipeline.

    All columns nullable except id; `excluded` defaults to 0. `fetched_at` and
    `efforts_fetched_at` are stored as ISO-8601 TEXT strings and compared
    lexicographically — NOT Django DateTimeField.
    """

    id = models.BigIntegerField(primary_key=True)  # Strava segment id

    name = models.TextField(null=True, blank=True)
    activity_type = models.TextField(null=True, blank=True)
    display_location = models.TextField(null=True, blank=True)
    # Strava's own climb category, verbatim from the page payload (e.g.
    # "Category3"). Display-only provenance — our Tour categories come from
    # difficulty (scoring.py), so this is never parsed or compared.
    climb_category = models.TextField(null=True, blank=True)
    is_verified = models.IntegerField(null=True, blank=True)
    distance_m = models.FloatField(null=True, blank=True)
    avg_grade = models.FloatField(null=True, blank=True)
    elev_low = models.FloatField(null=True, blank=True)
    elev_high = models.FloatField(null=True, blank=True)
    elev_gain = models.FloatField(null=True, blank=True)      # net gain reported by Strava
    gross_gain = models.FloatField(null=True, blank=True)     # summed from elevation stream
    gross_loss = models.FloatField(null=True, blank=True)     # summed from elevation stream
    terrain = models.TextField(null=True, blank=True)         # climb | flat | descent
    total_athletes = models.IntegerField(null=True, blank=True)
    total_efforts = models.IntegerField(null=True, blank=True)
    star_count = models.IntegerField(null=True, blank=True)
    start_lat = models.FloatField(null=True, blank=True)
    start_lng = models.FloatField(null=True, blank=True)
    end_lat = models.FloatField(null=True, blank=True)
    end_lng = models.FloatField(null=True, blank=True)
    map_image_url = models.TextField(null=True, blank=True)
    streams_json = models.TextField(null=True, blank=True)    # raw JSON: {distance, elevation, location}
    difficulty = models.FloatField(null=True, blank=True)     # computed by scoring.py
    in_town = models.IntegerField(null=True, blank=True)      # 1 if start or end in town
    starts_in_town = models.IntegerField(null=True, blank=True)  # 1 if START point is in town
    ends_in_town = models.IntegerField(null=True, blank=True)    # 1 if END point is in town
    passes_through = models.IntegerField(null=True, blank=True)  # 1 if track crosses town
    parent_segment_id = models.IntegerField(null=True, blank=True)  # longer same-direction segment; NULL=standalone
    excluded = models.IntegerField(default=0)                 # 1 = manually excluded
    discipline = models.TextField(null=True, blank=True)      # road | gravel | mtb (manual; NULL=unset)
    fetched_at = models.TextField(null=True, blank=True)      # ISO string; when page was fetched
    efforts_fetched_at = models.TextField(null=True, blank=True)  # ISO string; when leaderboard was fetched
    last_depth_pages = models.IntegerField(null=True, blank=True)  # deepest leaderboard depth ever fetched

    def __str__(self):
        return f"Segment({self.id}, {self.name!r})"


# ---------------------------------------------------------------------------
# Athlete
# ---------------------------------------------------------------------------

class Athlete(models.Model):
    """A Strava rider referenced by effort log entries."""

    id = models.BigIntegerField(primary_key=True)
    name = models.TextField(null=True, blank=True)
    avatar_url = models.TextField(null=True, blank=True)
    badge = models.TextField(null=True, blank=True)
    # Canonical gender for the athlete — bubbled up from women's board fetches.
    # "F" once seen on any women's leaderboard; STICKY (F never downgrades to M
    # even though everyone appears on the overall board). NULL = unset.
    gender = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"Athlete({self.id}, {self.name!r})"


# ---------------------------------------------------------------------------
# Kv
# ---------------------------------------------------------------------------

class Kv(models.Model):
    """Key/value store for cross-run pipeline state.

    Current keys: backoff_level / backoff_until / backoff_last_429.
    """

    key = models.TextField(primary_key=True)
    value = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"Kv({self.key!r})"


# ---------------------------------------------------------------------------
# EffortLog
# ---------------------------------------------------------------------------

class EffortLog(models.Model):
    """Append-only log of observed efforts; one row per distinct (segment, athlete, effort_id).

    `observed_at` is when WE first logged the effort — the rewind key. The row
    is never deleted; an athlete bumped off the leaderboard keeps their history.
    The canonical best-per-athlete and rank are computed by the `efforts` view.
    """

    # implicit BigAutoField pk
    segment = models.ForeignKey(
        Segment, on_delete=models.DO_NOTHING, db_column="segment_id"
    )
    athlete = models.ForeignKey(
        Athlete, on_delete=models.DO_NOTHING, db_column="athlete_id"
    )
    elapsed_time = models.IntegerField(null=True, blank=True)   # seconds
    avg_speed = models.FloatField(null=True, blank=True)
    avg_watts = models.FloatField(null=True, blank=True)
    avg_hr = models.FloatField(null=True, blank=True)
    effort_id = models.TextField(null=True, blank=True)         # dedup key
    activity_id = models.TextField(null=True, blank=True)
    start_date_local = models.TextField(null=True, blank=True)  # when the ride happened (Strava)
    observed_at = models.TextField()                            # when WE first logged it (NOT NULL)
    # Which board this effort was sourced from. "M" = overall board (default);
    # "F" = women's board. An existing row is flipped to "F" when the effort
    # also appears on the women's board (the overall-board INSERT usually wins
    # the dedup race first). NULL = legacy rows before gender tracking.
    gender = models.TextField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["segment", "athlete", "effort_id"],
                name="uniq_effortlog_seg_ath_effort",
            )
        ]
        indexes = [
            models.Index(
                fields=["segment", "observed_at"],
                name="idx_effort_log_seg",
            ),
            models.Index(
                fields=["segment", "athlete"],
                name="idx_effort_log_athlete",
            ),
        ]

    def __str__(self):
        return f"EffortLog(seg={self.segment_id}, ath={self.athlete_id}, t={self.elapsed_time})"


# ---------------------------------------------------------------------------
# Effort (unmanaged — backed by the SQL view)
# ---------------------------------------------------------------------------

class Effort(models.Model):
    """Read-only model over the `efforts` SQL view.

    The view is unmanaged (managed=False) so Django never touches its DDL.
    It is created by a RunSQL in the initial migration.

    Ranking: Strava-style competition — tied elapsed_times share a rank and
    the next distinct time skips tied-out places (1, 1, 3). rank is NULL when
    elapsed_time is NULL.

    Primary key note: the view has no single-column natural key. Django 5.2
    CompositePrimaryKey is used (segment_id, athlete_id). This is correct for
    SELECT; never UPDATE or INSERT through this model.
    """

    pk = models.CompositePrimaryKey("segment_id", "athlete_id")

    segment = models.ForeignKey(
        Segment, on_delete=models.DO_NOTHING, db_column="segment_id", related_name="+"
    )
    athlete = models.ForeignKey(
        Athlete, on_delete=models.DO_NOTHING, db_column="athlete_id", related_name="+"
    )
    rank = models.IntegerField(null=True, blank=True)
    elapsed_time = models.IntegerField(null=True, blank=True)
    avg_speed = models.FloatField(null=True, blank=True)
    avg_watts = models.FloatField(null=True, blank=True)
    avg_hr = models.FloatField(null=True, blank=True)
    effort_id = models.TextField(null=True, blank=True)
    activity_id = models.TextField(null=True, blank=True)
    start_date_local = models.TextField(null=True, blank=True)
    first_seen = models.TextField(null=True, blank=True)
    last_seen = models.TextField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "efforts"

    def __str__(self):
        return f"Effort(seg={self.segment_id}, ath={self.athlete_id}, rank={self.rank})"
