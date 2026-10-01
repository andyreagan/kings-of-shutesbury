#!/bin/sh
# Weekly publish: export → commit → push. Pages deploys on push (see
# .github/workflows/pages.yml). Run by launchd on Sunday evenings — see README,
# "Running in the background".
set -eu
cd "$(dirname "$0")"
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%S+00:00) [publish] $*"; }

# Don't snapshot strava.db while a background tick is mid-write. A tick is 3
# requests (well under a minute); give it up to 5 minutes anyway.
i=0
while pgrep -f 'manage.py update --background' >/dev/null; do
  i=$((i + 1))
  if [ "$i" -gt 30 ]; then log "updater still running after 5 min, giving up"; exit 1; fi
  sleep 10
done

uv run manage.py export

git add strava.db web/data.json web/data-queens.json web/data-legends.json
if git diff --cached --quiet; then
  log "nothing to publish"
  exit 0
fi

git commit -qm "Weekly refresh: data + site $(date +%F)"
git push -q origin main
log "pushed $(git rev-parse --short HEAD)"
