"""Raw-SQL placeholder regression: pytest runs with DEBUG=False, which skips
Django's debug-SQL formatting (`sql % params`) — the exact path that crashes
on sqlite-style `?` placeholders. `?` works fine against the driver, so the
bug ONLY fires in live runs (DEBUG used to be True; it's False now, but raw
SQL must still be `%s`-clean). These tests re-run every raw-SQL path under
override_settings(DEBUG=True) so a stray `?` fails CI instead of a 2am tick.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from segments import pipeline

pytestmark = pytest.mark.django_db


@override_settings(DEBUG=True)
def test_background_pick_formats_under_debug(seed_segment):
    seed_segment(1, efforts_fetched_at=None)        # phase A candidate
    sid, depth, phase = pipeline._background_pick()
    assert sid == 1


@override_settings(DEBUG=True)
def test_store_efforts_f_flip_formats_under_debug(seed_segment):
    from tests.conftest import effort
    seed_segment(2)
    rows = [effort(101, 100), effort(102, 110)]
    pipeline._store_efforts(2, rows, "2026-06-06T00:00:00+00:00")          # M
    pipeline._store_efforts(2, rows, "2026-06-06T00:00:00+00:00", gender="F")


@override_settings(DEBUG=True)
def test_changelog_formats_under_debug(seed_segment, capsys):
    from tests.conftest import effort
    seed_segment(3)
    pipeline._store_efforts(3, [effort(101, 100)], "2026-06-06T00:00:00+00:00")
    pipeline.print_changelog("2026-06-05T00:00:00+00:00")
    assert "CHANGELOG" in capsys.readouterr().out
