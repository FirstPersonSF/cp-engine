#!/usr/bin/env bash
# sync-cli-version.sh — keep the `cp` CLI in lockstep with the plugin.
#
# Reads the version baked into the plugin's plugin.json and compares it
# to the installed `cp --version`. If they don't match (or `cp` is
# missing), runs `uv tool install --force --from
# git+https://github.com/FirstPersonSF/cp-engine.git@v<version> cp-engine`.
#
# Policy decisions (per docs/specs/cp-engine-spec-v03-version-distribution.md):
# - Print errors but never block session start. The next /cp-summarize
#   will fail loud with EngineVersionMismatch, which is the existing
#   safety net for tenant-vs-CLI skew.
# - Never auto-install "latest". Always pin to the version in plugin.json,
#   so the user stays in control of when to update.
# - Fast on the happy path: two subshell calls when versions match.
#
# Hook contract: stdin is JSON from Claude Code (ignored). stdout/stderr
# show in the transcript when the hook actually does work.

set -uo pipefail

PLUGIN_JSON="${CLAUDE_PLUGIN_ROOT}/plugin.json"
REPO_URL="https://github.com/FirstPersonSF/cp-engine.git"

if [ ! -f "$PLUGIN_JSON" ]; then
    # Plugin is malformed; nothing we can do. Stay silent — surfacing
    # this on every session would be noise the user can't act on.
    exit 0
fi

# Read plugin version. jq is the dependable parser, but fall back to a
# grep/sed pair so a missing jq doesn't break the user's session.
if command -v jq >/dev/null 2>&1; then
    PLUGIN_VERSION=$(jq -r '.version // empty' "$PLUGIN_JSON")
else
    PLUGIN_VERSION=$(grep -E '"version"' "$PLUGIN_JSON" \
        | head -1 \
        | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')
fi

if [ -z "$PLUGIN_VERSION" ]; then
    exit 0
fi

# `cp --version` prints "cp, version X.Y.Z" (Click default). Awk the last field.
# If `cp` isn't on PATH, INSTALLED_VERSION stays empty and we treat as drift.
INSTALLED_VERSION=$(cp --version 2>/dev/null | awk '{print $NF}' || true)

if [ "$PLUGIN_VERSION" = "$INSTALLED_VERSION" ]; then
    exit 0
fi

# Drift detected. Tell the user what we're doing before the network call —
# `uv tool install` can take a few seconds and silent latency is worse
# than a one-line "we're updating cp".
if [ -z "$INSTALLED_VERSION" ]; then
    echo "[cp-engine] cp CLI not installed — installing v${PLUGIN_VERSION}..."
else
    echo "[cp-engine] cp CLI version drift: plugin=${PLUGIN_VERSION} installed=${INSTALLED_VERSION}. Updating..."
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "[cp-engine] uv not found on PATH; cannot auto-install. Install uv (https://docs.astral.sh/uv/) and re-run, or install manually:" >&2
    echo "[cp-engine]   pip install 'cp-engine @ git+${REPO_URL}@v${PLUGIN_VERSION}'" >&2
    exit 0
fi

if uv tool install --force \
    --from "git+${REPO_URL}@v${PLUGIN_VERSION}" \
    cp-engine >/dev/null 2>&1; then
    echo "[cp-engine] cp CLI updated to v${PLUGIN_VERSION}."
else
    # Don't block the session — the next /cp-summarize will fail loud
    # with EngineVersionMismatch and tell the user what to fix.
    echo "[cp-engine] auto-install of cp v${PLUGIN_VERSION} failed (offline? auth?). The next /cp-summarize will fail with EngineVersionMismatch — re-run this manually:" >&2
    echo "[cp-engine]   uv tool install --force --from 'git+${REPO_URL}@v${PLUGIN_VERSION}' cp-engine" >&2
fi

exit 0
