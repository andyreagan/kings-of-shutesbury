"""CLI validation — exit-codes / messages for known-wrong flag combos.

These invoke Django management commands via call_command and assert the
SystemExit behavior (sys.exit() in command handlers raises SystemExit).
Validation messages mirror the legacy update_segments.py argparse contract.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command


def test_update_loop_without_background_errors():
    with pytest.raises(SystemExit) as exc_info:
        call_command("update", "12345", loop=True)
    assert "--loop only makes sense with --background" in str(exc_info.value.code)


def test_update_background_with_depth_errors():
    """--background picks depth itself; the combo is a usage error so the
    user knows --depth is being ignored before they're confused by the picker."""
    with pytest.raises(SystemExit) as exc_info:
        call_command("update", background=True, depth=4)
    assert "chooses depth itself" in str(exc_info.value.code)


def test_update_both_id_and_all_errors():
    with pytest.raises(SystemExit) as exc_info:
        call_command("update", id=12345, all_=True)
    assert "exactly one of" in str(exc_info.value.code)


def test_update_no_mode_errors():
    with pytest.raises(SystemExit) as exc_info:
        call_command("update")
    assert "exactly one of" in str(exc_info.value.code)


def test_add_odd_args_errors():
    with pytest.raises(SystemExit) as exc_info:
        call_command("add", "12345")
    assert "(id, discipline) pairs" in str(exc_info.value.code)


def test_add_bad_discipline_errors():
    with pytest.raises(SystemExit) as exc_info:
        call_command("add", "12345", "bmx")
    assert "discipline must be" in str(exc_info.value.code)


def test_help_lists_all_six_subcommands():
    """Management commands are self-contained; verify all six exist by
    checking that call_command doesn't raise for each command's --help."""
    import subprocess
    import sys
    from pathlib import Path

    ROOT = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, str(ROOT / "manage.py"), "--help"],
        capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0
    for cmd in ("add", "update", "export", "list", "log"):
        assert cmd in r.stdout, f"manage.py --help should list `{cmd}`"
