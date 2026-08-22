#!/usr/bin/env bash
# sync-cli-version.sh — keep the `cxp` CLI in lockstep with the plugin.
#
# Reads the version baked into the plugin's plugin.json and compares it
# to the installed `cxp --version`. If they don't match (or `cxp` is
# missing), runs `uv tool install --force --from
# git+https://github.com/FirstPersonSF/cp-engine.git@v<version> cp-engine`.
#
# The CLI was named `cp` before 2026-08-22. That shadowed /bin/cp on PATH
# (~/.local/bin precedes /bin), so every bare `cp` in a shell ran this CLI
# instead of the copy command. Current `uv` prunes the orphaned `cp` script
# on upgrade, but older uv and pip installs do not — so this hook also
# prunes it defensively (see "Stale `cp` shim" below).
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

# ── Tenant deferral (arch-phase-3, issue #28) ────────────────────────
# Inside a cp tenant, the TENANT PIN (.cp-engine.toml [engine].version)
# is the single truth source for the cp CLI version, enforced by the
# tenant's own SessionStart hook (check-cp-engine-version.py, installed
# by `cxp sync`). Two hooks keyed off different truths (plugin version
# vs tenant pin) can disagree and fight over the installed CLI — so
# when a session starts anywhere under a tenant root, this hook defers
# entirely. Outside a tenant there is no pin, and plugin-version
# matching below remains the right (only) behavior.
_dir="$PWD"
while [ "$_dir" != "/" ] && [ -n "$_dir" ]; do
    if [ -f "$_dir/.cp-engine.toml" ]; then
        exit 0
    fi
    _dir=$(dirname "$_dir")
done

# ── Marketplace-clone self-update (the once-and-for-all downgrade fix) ──
# This hook's truth source is the plugin.json in the marketplace clone at
# ~/.claude/plugins/marketplaces/cp-engine — but Claude Code does not
# reliably refresh that clone, so after every release it goes stale and
# the old behavior "helpfully" downgraded a newer installed CLI to match
# it. Fix both sides: (a) refresh the clone here, so its version is
# current; (b) below, NEVER move the CLI backwards regardless.
# Safety: only reset a checkout that is genuinely the marketplace clone
# (toplevel under ~/.claude/plugins/) — never a dev working tree.
_toplevel=$(git -C "${CLAUDE_PLUGIN_ROOT}" rev-parse --show-toplevel 2>/dev/null || true)
case "$_toplevel" in
    "$HOME/.claude/plugins/"*)
        if git -C "$_toplevel" fetch --quiet origin main 2>/dev/null; then
            git -C "$_toplevel" reset --hard --quiet origin/main 2>/dev/null || true
        fi
        # Offline / fetch failure is fine: the no-downgrade guard below
        # makes a stale clone harmless.
        ;;
esac

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

# ── Stale `cp` shim (rename cleanup, 2026-08-22) ─────────────────────
# Until v0.102.0 this package installed its entry point as `cp`, which
# shadowed /bin/cp on PATH. Current `uv` removes the orphaned script on
# upgrade (verified 2026-08-22), but older uv and pip installs leave it,
# and a surviving shim silently reinstates the exact bug this rename
# exists to fix. Cheap to run, so we prune unconditionally.
#
# Deliberately narrow: we unlink ONLY a symlink that resolves into a uv
# tools directory for cp-engine. A regular file, a symlink pointing
# anywhere else, or the real /bin/cp is never touched.
prune_stale_cp_shim() {
    _shim="$HOME/.local/bin/cp"

    # Must exist AND be a symlink. A regular file here is not ours.
    [ -L "$_shim" ] || return 0

    # Resolve one hop; readlink -f is unavailable on stock macOS bash.
    _target=$(readlink "$_shim" 2>/dev/null) || return 0

    case "$_target" in
        *"/uv/tools/cp-engine/"*)
            if rm -f "$_shim" 2>/dev/null; then
                echo "[cp-engine] removed stale 'cp' shim at ${_shim} — /bin/cp is no longer shadowed."
            else
                echo "[cp-engine] could not remove stale 'cp' shim at ${_shim}; remove it manually so /bin/cp works." >&2
            fi
            ;;
        *)
            # Someone else's `cp`. Leave it alone.
            ;;
    esac
}

# Runs on EVERY invocation, before the version checks below. A user whose
# CLI is already current takes an early `exit 0` at the version comparison,
# so pruning only after an install would strand the shim on exactly the
# machines that need no upgrade.
prune_stale_cp_shim

# `cxp --version` prints "cxp, version X.Y.Z" (Click default). Awk the last
# field. If `cxp` isn't on PATH, INSTALLED_VERSION stays empty and we treat
# as drift. Must NOT probe `cp`: post-rename that resolves to /bin/cp, which
# exits non-zero, so every session would see phantom drift and reinstall.
INSTALLED_VERSION=$(cxp --version 2>/dev/null | awk '{print $NF}' || true)

if [ "$PLUGIN_VERSION" = "$INSTALLED_VERSION" ]; then
    exit 0
fi

# NEVER downgrade. If the installed CLI is NEWER than the plugin says
# (stale clone that couldn't refresh, or a dev install ahead of the last
# release), leave it alone. sort -V gives semantic version ordering; the
# highest of the two being the installed version means we're ahead.
if [ -n "$INSTALLED_VERSION" ]; then
    _highest=$(printf '%s\n%s\n' "$PLUGIN_VERSION" "$INSTALLED_VERSION" | sort -V | tail -1)
    if [ "$_highest" = "$INSTALLED_VERSION" ]; then
        # Installed >= plugin (and != from the check above) → ahead. No-op.
        exit 0
    fi
fi

# Drift detected. Tell the user what we're doing before the network call —
# `uv tool install` can take a few seconds and silent latency is worse
# than a one-line "we're updating cp".
if [ -z "$INSTALLED_VERSION" ]; then
    echo "[cp-engine] cxp CLI not installed — installing v${PLUGIN_VERSION}..."
else
    echo "[cp-engine] cxp CLI version drift: plugin=${PLUGIN_VERSION} installed=${INSTALLED_VERSION}. Updating..."
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "[cp-engine] uv not found on PATH; cannot auto-install. Install uv (https://docs.astral.sh/uv/) and re-run, or install manually:" >&2
    echo "[cp-engine]   pip install 'cp-engine @ git+${REPO_URL}@v${PLUGIN_VERSION}'" >&2
    exit 0
fi

if uv tool install --force \
    --from "git+${REPO_URL}@v${PLUGIN_VERSION}" \
    cp-engine >/dev/null 2>&1; then
    echo "[cp-engine] cxp CLI updated to v${PLUGIN_VERSION}."
else
    # Don't block the session — the next /cp-summarize will fail loud
    # with EngineVersionMismatch and tell the user what to fix.
    echo "[cp-engine] auto-install of cxp v${PLUGIN_VERSION} failed (offline? auth?). The next /cp-summarize will fail with EngineVersionMismatch — re-run this manually:" >&2
    echo "[cp-engine]   uv tool install --force --from 'git+${REPO_URL}@v${PLUGIN_VERSION}' cp-engine" >&2
fi

exit 0
