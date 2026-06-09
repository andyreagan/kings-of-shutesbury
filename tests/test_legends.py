"""Legends of Shutesbury — segment-completion leaderboard tests.

Covers:
  - export writes data-legends.json alongside data.json / data-queens.json
  - both genders are included (it's a completion board, not a gendered one)
  - completion is the set of segments a rider has any effort on, counted from the
    FULL effort set (not the 100-cap shipped in data.json)
  - segment metadata carries discipline so the frontend can filter
  - rank-NULL (timeless) efforts still count as "completed"
  - default sort is most-segments-first, then name
"""

from __future__ import annotations

import json

import pytest

from segments import pipeline
from segments.export import (export_data_json, DATA_JSON, DATA_QUEENS_JSON,
                             DATA_LEGENDS_JSON)

from conftest import effort

pytestmark = pytest.mark.django_db


def _redirect(tmp_path, monkeypatch):
    monkeypatch.setattr("segments.export.DATA_JSON", tmp_path / "data.json")
    monkeypatch.setattr("segments.export.DATA_QUEENS_JSON",
                        tmp_path / "data-queens.json")
    monkeypatch.setattr("segments.export.DATA_LEGENDS_JSON",
                        tmp_path / "data-legends.json")
    monkeypatch.setattr("segments.export.WEB_DIR", tmp_path)


def _legends(tmp_path):
    return json.loads((tmp_path / "data-legends.json").read_text())


def _by_id(legends, aid):
    return next((l for l in legends["legends"] if l["athlete_id"] == aid), None)


def test_export_writes_legends_file(seed_segment, tmp_path, monkeypatch):
    seed_segment(1, discipline="road")
    client = pipeline_client(1, [effort(101, 60, name="Alice", effort_id="e1")])
    pipeline._refresh_one(client, 1, depth=1)
    _redirect(tmp_path, monkeypatch)

    export_data_json()

    legends = _legends(tmp_path)
    assert set(legends) == {"generated_at", "segments", "legends"}
    seg = legends["segments"][0]
    assert seg["discipline"] == "road"
    # Coverage fields ride along so the frontend can show how trustworthy the
    # completion is (breadth + recency).
    assert seg["captured"] == 1                      # one captured athlete
    assert "total_athletes" in seg
    assert "efforts_fetched_at" in seg
    assert _by_id(legends, 101)["segs"] == [1]


def test_both_genders_included(seed_segment, tmp_path, monkeypatch):
    """A women's-board rider and a men's-board rider both appear — completion is
    not gendered."""
    seed_segment(1, discipline="road")
    alice = effort(101, 60, name="Alice", effort_id="e1")
    bob = effort(102, 70, name="Bob", effort_id="e2")
    from conftest import FakeStravaClient
    client = FakeStravaClient(overall={1: [alice, bob]}, women={1: [alice]})
    pipeline._refresh_one(client, 1, depth=1)
    _redirect(tmp_path, monkeypatch)

    export_data_json()
    legends = _legends(tmp_path)
    ids = {l["athlete_id"] for l in legends["legends"]}
    assert ids == {101, 102}
    assert _by_id(legends, 101)["gender"] == "F"
    assert _by_id(legends, 102)["gender"] == "M"


def test_completion_counts_across_segments_and_sort(seed_segment, tmp_path,
                                                     monkeypatch):
    """Alice does 2 of 2 segments, Bob does 1 — Alice sorts first."""
    seed_segment(1, discipline="road")
    seed_segment(2, discipline="gravel")
    alice = effort(101, 60, name="Alice", effort_id="ea")
    bob = effort(102, 70, name="Bob", effort_id="eb")
    from conftest import FakeStravaClient
    client = FakeStravaClient(overall={1: [alice, bob], 2: [alice]})
    pipeline._refresh_one(client, 1, depth=1)
    pipeline._refresh_one(client, 2, depth=1)
    _redirect(tmp_path, monkeypatch)

    export_data_json()
    legends = _legends(tmp_path)
    assert [l["athlete_id"] for l in legends["legends"]] == [101, 102]
    assert _by_id(legends, 101)["segs"] == [1, 2]
    assert _by_id(legends, 102)["segs"] == [1]


def test_timeless_effort_counts_as_completed(seed_segment, tmp_path, monkeypatch):
    """A rank-NULL (timeless) effort still counts as completion. The `efforts`
    view rank is NULL when elapsed_time is NULL; the legends board must still
    credit the rider with having done the segment."""
    from segments.models import Athlete, EffortLog
    seed_segment(1, discipline="mtb")
    Athlete.objects.create(id=101, name="Ghost")
    EffortLog.objects.create(segment_id=1, athlete_id=101, elapsed_time=None,
                             effort_id="eg", observed_at="2026-06-01T00:00:00+00:00")
    _redirect(tmp_path, monkeypatch)

    export_data_json()
    legends = _legends(tmp_path)
    assert _by_id(legends, 101)["segs"] == [1]


# Small helper to build a one-segment overall client.
def pipeline_client(seg_id, overall_rows):
    from conftest import FakeStravaClient
    return FakeStravaClient(overall={seg_id: overall_rows})
