#!/usr/bin/env bash
# session-start.sh — the plugin's ONE SessionStart entry (#296).
#
# Claude Code runs every hook registered under one matcher IN PARALLEL, with
# no ordering guarantee (hooks reference, verified 2026-09-18). Two entries
# therefore meant two lines in whichever order won the race — and any plan
# for "one consolidated line" across them was impossible by construction.
# One entry, two scripts in a fixed order, is the honest version.
#
# Order: CLI-version observe/sync first (it may print the install-health
# warning), then the tenant-clone freshness gate. Each script still owns its
# own silence — a healthy session prints nothing from either.
#
# Neither script reads stdin (the SessionStart JSON); </dev/null makes that
# explicit. Neither may fail session start: `|| true` and a final exit 0.

set -u

here="$(cd "$(dirname "$0")" && pwd)"

bash "$here/sync-cli-version.sh" </dev/null || true
bash "$here/tenant-freshness.sh" </dev/null || true

exit 0
