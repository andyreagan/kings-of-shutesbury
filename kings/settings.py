"""Minimal Django settings for Kings of Shutesbury.

This project never serves HTTP — Django is used only for ORM access to
strava.db and schema management via migrations.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "insecure-local-only"  # noqa: S105 — not a secret; no HTTP server
DEBUG = True

# No HTTP serving — omit ROOT_URLCONF; add a trivial one if django check demands it.
ROOT_URLCONF = "kings.urls"

INSTALLED_APPS = [
    "segments",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "strava.db",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
