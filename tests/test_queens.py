"""Queens of Shutesbury — women's leaderboard tests.

Covers:
  - _refresh_one stores women's board rows with gender="F"
  - A pre-existing overall-board EffortLog row is flipped to "F" when the same
    effort appears on the women's board (dedup race flip)
  - The Athlete row is also flipped to "F"
  - F is sticky: a subsequent overall-board fetch does NOT downgrade to "M"
  - Queens re-rank: women subset with tied times gets competition ranks 1,1,3
    and full points each; overall board ranks differ
  - export writes both data.json and data-queens.json; kings payload is
    unaffected; queens payload contains only F athletes
"""

from __future__ import annotations

import json
import pathlib

import pytest

from segments import pipeline
from segments import scoring
from segments.models import Athlete, EffortLog, Effort
from segments.export import export_data_json, DATA_JSON, DATA_QUEENS_JSON

from conftest import FakeStravaClient, effort, insert_segment

pytestmark = pytest.mark.django_db


# === helpers =================================================================


def _make_client(seg_id: int, overall_rows, following_rows=None,
                 women_rows=None):
    return FakeStravaClient(
        overall={seg_id: overall_rows},
        following={seg_id: following_rows or []},
        women={seg_id: women_rows or []},
    )


# === gender storage on _refresh_one =========================================


def test_women_rows_stored_with_gender_f(seed_segment):
    """Women's board rows come through with gender='F' on EffortLog."""
    seed_segment(1)
    alice = effort(201, 100, name="Alice", effort_id="e201")
    client = _make_client(1, overall_rows=[], women_rows=[alice])
    assert pipeline._refresh_one(client, 1, depth=1) is True

    logs = list(EffortLog.objects.filter(segment_id=1, athlete_id=201))
    assert len(logs) == 1
    assert logs[0].gender == "F"
    assert Athlete.objects.get(pk=201).gender == "F"


def test_overall_row_flipped_to_f_when_on_womens_board(seed_segment):
    """The overall-board INSERT usually wins the dedup race, leaving the row
    stamped 'M'. After the women's fetch, the same (segment, athlete, effort_id)
    row must be flipped to 'F', and the Athlete too."""
    seed_segment(1)
    # Same effort appears on both boards (identical effort_id).
    row = effort(201, 100, name="Alice", effort_id="e201")
    client = _make_client(1, overall_rows=[row], women_rows=[row])
    assert pipeline._refresh_one(client, 1, depth=1) is True

    # Only one row in the log (dedup), and it must be flipped to F.
    logs = list(EffortLog.objects.filter(segment_id=1, athlete_id=201))
    assert len(logs) == 1
    assert logs[0].gender == "F", "row must be flipped to F by the women's fetch"
    assert Athlete.objects.get(pk=201).gender == "F"


def test_gender_f_is_sticky_overall_does_not_downgrade(seed_segment):
    """After a women's fetch stamps gender='F', a subsequent overall-board fetch
    must not overwrite the Athlete or EffortLog back to 'M'."""
    seed_segment(1)
    row = effort(201, 100, name="Alice", effort_id="e201")
    # First refresh: alice on both boards → F
    client1 = _make_client(1, overall_rows=[row], women_rows=[row])
    pipeline._refresh_one(client1, 1, depth=1)
    assert Athlete.objects.get(pk=201).gender == "F"

    # Second refresh: alice only on overall board (women's board now empty).
    # effort_id differs so a new EffortLog row is created — but Athlete must stay F.
    row2 = effort(201, 100, name="Alice", effort_id="e201-v2")
    client2 = _make_client(1, overall_rows=[row2], women_rows=[])
    pipeline._refresh_one(client2, 1, depth=1)

    ath = Athlete.objects.get(pk=201)
    assert ath.gender == "F", "F must not be downgraded to M by an overall-board fetch"


# === queens re-rank ==========================================================


def test_queens_rerank_tied_times(seed_segment):
    """Women subset with tied times should get competition ranks 1,1,3 and the
    same full points as kings-rank-1 — just re-ranked among women."""
    seed_segment(2, difficulty=100.0, total_efforts=1000)  # Cat 3, depth 8

    # Three overall efforts; alice and carol are women.
    alice = effort(201, 35, name="Alice", effort_id="e201")
    bob = effort(202, 35, name="Bob", effort_id="e202")    # overall rank 1 (tie with alice)
    carol = effort(203, 39, name="Carol", effort_id="e203")

    # Alice and Bob win overall (tied 1,1,3); Carol is 3rd.
    client = _make_client(
        2,
        overall_rows=[alice, bob, carol],
        women_rows=[alice, carol],        # women's board: alice(35) carol(39)
    )
    # Add a third woman with the same time as alice to exercise 1,1,3 among women.
    diane = effort(204, 35, name="Diane", effort_id="e204")
    client.women[2].append(diane)

    pipeline._refresh_one(client, 2, depth=1)

    # Confirm women athletes are stamped F.
    for aid in (201, 203, 204):
        assert Athlete.objects.get(pk=aid).gender == "F"
    assert Athlete.objects.get(pk=202).gender == "M"

    # Re-rank in Python as export does.
    women_aids = [201, 203, 204]
    # Use the Effort view for elapsed times.
    times = {e.athlete_id: e.elapsed_time
             for e in Effort.objects.filter(segment_id=2, athlete_id__in=women_aids)}
    ranks = pipeline.competition_ranks(times)
    # Alice(35) and Diane(35) share rank 1; Carol(39) is rank 3.
    assert ranks[201] == 1
    assert ranks[204] == 1
    assert ranks[203] == 3

    # Points: cat 3 depth 8 — rank-1 = 9, rank-3 = 2.
    cat = scoring.segment_category(100.0, False)
    depth = scoring.effort_depth(1000)
    pts = {aid: scoring.points_for_rank(r, cat, depth, 100.0)
           for aid, r in ranks.items()}
    assert pts[201] == 9
    assert pts[204] == 9
    assert pts[203] == 2


# === export ==================================================================


def test_export_kings_unaffected_queens_f_only(seed_segment, tmp_path, monkeypatch):
    """export_data_json writes two files. Kings payload includes all athletes;
    queens payload contains only F-gendered athletes."""
    seed_segment(3, difficulty=100.0, total_efforts=1000)

    alice = effort(301, 60, name="Alice", effort_id="e301")
    bob = effort(302, 70, name="Bob", effort_id="e302")
    client = _make_client(3, overall_rows=[alice, bob], women_rows=[alice])
    pipeline._refresh_one(client, 3, depth=1)

    # Redirect output files to tmp_path so we don't clobber real data.
    monkeypatch.setattr("segments.export.DATA_JSON",
                        tmp_path / "data.json")
    monkeypatch.setattr("segments.export.DATA_QUEENS_JSON",
                        tmp_path / "data-queens.json")
    monkeypatch.setattr("segments.export.WEB_DIR", tmp_path)

    export_data_json()

    kings = json.loads((tmp_path / "data.json").read_text())
    queens = json.loads((tmp_path / "data-queens.json").read_text())

    # Both files have the expected top-level keys.
    for payload in (kings, queens):
        assert "generated_at" in payload
        assert "segments" in payload
        assert "filtered" in payload
        assert "boundary" in payload

    # Find our test segment in each payload.
    def seg_by_id(payload, sid):
        return next((s for s in payload["segments"] if s["id"] == sid), None)

    ks = seg_by_id(kings, 3)
    qs = seg_by_id(queens, 3)
    assert ks is not None, "kings must include segment 3"
    assert qs is not None, "queens must include segment 3"

    # Kings: both Alice and Bob.
    kings_aids = {e["athlete_id"] for e in ks["efforts"]}
    assert kings_aids == {301, 302}

    # Queens: only Alice (F).
    queens_aids = {e["athlete_id"] for e in qs["efforts"]}
    assert queens_aids == {301}

    # Queens leader is Alice.
    assert qs["leader"]["name"] == "Alice"

    # Bob (M) not in queens.
    assert 302 not in queens_aids


def test_export_queens_empty_when_no_women(seed_segment, tmp_path, monkeypatch):
    """Queens payload efforts lists are empty when no F athletes exist."""
    seed_segment(4)
    bob = effort(401, 60, name="Bob", effort_id="e401")
    client = _make_client(4, overall_rows=[bob], women_rows=[])
    pipeline._refresh_one(client, 4, depth=1)

    monkeypatch.setattr("segments.export.DATA_JSON",
                        tmp_path / "data.json")
    monkeypatch.setattr("segments.export.DATA_QUEENS_JSON",
                        tmp_path / "data-queens.json")
    monkeypatch.setattr("segments.export.WEB_DIR", tmp_path)

    export_data_json()

    queens = json.loads((tmp_path / "data-queens.json").read_text())
    seg = next((s for s in queens["segments"] if s["id"] == 4), None)
    assert seg is not None
    assert seg["efforts"] == []
    assert seg["leader"] is None
