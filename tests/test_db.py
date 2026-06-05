"""Small DB helpers — kv store and add_segment tri-state."""

from __future__ import annotations

import pytest

from segments import pipeline
from segments.models import Kv, Segment

pytestmark = pytest.mark.django_db


def test_kv_set_then_get_roundtrip():
    assert pipeline.kv_get("x") is None
    pipeline.kv_set("x", "hello")
    assert pipeline.kv_get("x") == "hello"
    pipeline.kv_set("x", "world")
    assert pipeline.kv_get("x") == "world"


def test_kv_set_none_deletes():
    pipeline.kv_set("x", "hello")
    pipeline.kv_set("x", None)
    assert pipeline.kv_get("x") is None
    # And the row is actually gone, not just blank.
    assert not Kv.objects.filter(key="x").exists()


def test_add_segment_outcomes():
    """add_segment differentiates added / updated / unchanged so the CLI can
    report what happened per pair."""
    assert pipeline.add_segment(100, "gravel") == "added"
    assert pipeline.add_segment(100, "gravel") == "unchanged"
    assert pipeline.add_segment(100, "road") == "updated"
    seg = Segment.objects.get(pk=100)
    assert seg.discipline == "road"


def test_unimported_segment_ids():
    pipeline.add_segment(1, "road")
    pipeline.add_segment(2, "gravel")
    # Mark one as imported.
    Segment.objects.filter(pk=1).update(fetched_at="2026-01-01")
    assert pipeline.unimported_segment_ids() == [2]
