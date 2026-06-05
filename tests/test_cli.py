"""CLI validation — exit-codes / messages for known-wrong flag combos.

These call into the argparse handlers via the public entry point and assert
the SystemExit-with-message behavior, so refactors of the flags don't
silently break the contract documented in the README.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "update_segments.py"), *argv],
        capture_output=True, text=True, cwd=ROOT)


def test_update_loop_without_background_errors():
    r = _run("update", "12345", "--loop")
    assert r.returncode != 0
    assert "--loop only makes sense with --background" in (r.stdout + r.stderr)


def test_update_background_with_depth_errors():
    """--background picks depth itself; the combo is a usage error so the
    user knows --depth is being ignored before they're confused by the picker."""
    r = _run("update", "--background", "--depth", "4")
    assert r.returncode != 0
    assert "chooses depth itself" in (r.stdout + r.stderr)


def test_update_both_id_and_all_errors():
    r = _run("update", "12345", "--all")
    assert r.returncode != 0
    assert "exactly one of" in (r.stdout + r.stderr)


def test_update_no_mode_errors():
    r = _run("update")
    assert r.returncode != 0
    assert "exactly one of" in (r.stdout + r.stderr)


def test_add_odd_args_errors():
    r = _run("add", "12345")
    assert r.returncode != 0
    assert "(id, discipline) pairs" in (r.stdout + r.stderr)


def test_add_bad_discipline_errors():
    r = _run("add", "12345", "bmx")
    assert r.returncode != 0
    assert "discipline must be" in (r.stdout + r.stderr)


def test_help_lists_all_six_subcommands():
    r = _run("--help")
    assert r.returncode == 0
    for cmd in ("add", "import", "update", "export", "list", "log"):
        assert cmd in r.stdout, f"top-level help should advertise `{cmd}`"
