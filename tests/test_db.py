"""Small DB helpers."""

from __future__ import annotations

import db


def test_kv_set_then_get_roundtrip(conn):
    assert db.kv_get(conn, "x") is None
    db.kv_set(conn, "x", "hello")
    assert db.kv_get(conn, "x") == "hello"
    db.kv_set(conn, "x", "world")
    assert db.kv_get(conn, "x") == "world"


def test_kv_set_none_deletes(conn):
    db.kv_set(conn, "x", "hello")
    db.kv_set(conn, "x", None)
    assert db.kv_get(conn, "x") is None
    # And the row is actually gone, not just blank.
    n = conn.execute("SELECT COUNT(*) AS n FROM kv WHERE key = 'x'").fetchone()["n"]
    assert n == 0


def test_add_segment_outcomes(conn):
    """add_segment differentiates added / updated / unchanged so the CLI can
    report what happened per pair."""
    assert db.add_segment(conn, 100, "gravel") == "added"
    assert db.add_segment(conn, 100, "gravel") == "unchanged"
    assert db.add_segment(conn, 100, "road") == "updated"
    row = conn.execute(
        "SELECT discipline FROM segments WHERE id = 100").fetchone()
    assert row["discipline"] == "road"


def test_unimported_segment_ids(conn):
    db.add_segment(conn, 1, "road")
    db.add_segment(conn, 2, "gravel")
    # Mark one as imported.
    conn.execute("UPDATE segments SET fetched_at = '2026-01-01' WHERE id = 1")
    conn.commit()
    assert db.unimported_segment_ids(conn) == [2]
