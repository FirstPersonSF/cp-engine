#!/usr/bin/env bash
# session-end.sh — SessionEnd: drop this Claude Code session from the tenant
# clone's session registry (cp-engine #394). Never fails: exit 0.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
bash "$here/session-register.sh" --remove || true
exit 0
