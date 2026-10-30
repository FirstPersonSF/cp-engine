#!/usr/bin/env bash
# session-register.sh — add (or, with --remove, drop) this Claude Code
# session in the tenant clone's session registry (cp-engine #394).
#
# The registry lets a wrap-up say "1 other cp session is active" and lets
# `cxp session list` show who is working where. The hook JSON (session_id,
# cwd) arrives on stdin and is passed straight to `cxp session register`.
#
# Silent no-op outside a tenant, or when `cxp` is missing or too old to have
# `session register`. Never fails the session: always exit 0.

set -u

payload="$(cat 2>/dev/null || true)"

dir="$PWD"
root=""
while [ "$dir" != "/" ] && [ -n "$dir" ]; do
    if [ -f "$dir/.cp-engine.toml" ]; then root="$dir"; break; fi
    dir=$(dirname "$dir")
done
[ -z "$root" ] && exit 0
command -v cxp >/dev/null 2>&1 || exit 0

printf '%s' "$payload" | cxp session register "$@" 2>/dev/null || true
exit 0
