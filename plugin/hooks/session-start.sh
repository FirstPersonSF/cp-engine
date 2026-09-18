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
# Cost of the order, stated: the two used to run in parallel under 30s/25s;
# they now run in series under one budget, and the network-bound freshness
# fetch is the one that gets cut if the budget blows. Inside a tenant the
# first script is sub-second (observe, then defer), so the common case is
# fine; outside a tenant it may fetch and install first.
#
# SessionStart hooks receive JSON on stdin. Drain it once here — the same
# convention the tenant hook uses — so neither child inherits an unread pipe.
# Neither child may fail session start: `|| true` and a final exit 0. The
# syntax of all three scripts is gated by tests/test_session_start_hooks.py
# (`bash -n`), because `|| true` would otherwise hide a broken child forever.

set -u

cat >/dev/null 2>&1 || true

here="$(cd "$(dirname "$0")" && pwd)"

bash "$here/sync-cli-version.sh" || true
bash "$here/tenant-freshness.sh" || true

exit 0
