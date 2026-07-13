#!/usr/bin/env bash
# SessionStart tenant-freshness gate (cp-engine #80).
#
# The cp tenant accumulates ~20 commits/day from the daily cron sync,
# auto-ingest, and teammates' session captures — a clone that isn't pulled
# daily WILL diverge. A forced `git pull` here would be wrong: in exactly
# the state that needs it most (dirty tree + divergence) `git pull` fails.
# So this is a GATE, not a pull:
#
#   1. Not inside a cp tenant → silent no-op (exit 0).
#   2. `git fetch` always (cheap, conflict-free; offline degrades to a note).
#   3. Clean tree + strictly behind (no divergence) → `git pull --ff-only`,
#      one quiet line.
#   4. Otherwise → LOUD warning into session context with behind/ahead/dirty
#      counts and the reconcile recipe, so the session sees it BEFORE edits.
#
# Never blocks session start: every failure path exits 0 with at most a note.

set -u

# ── 1. Walk up from cwd for the tenant config ─────────────────────────
dir="$PWD"
root=""
while [ "$dir" != "/" ]; do
    if [ -f "$dir/.cp-engine.toml" ]; then
        root="$dir"
        break
    fi
    dir=$(dirname "$dir")
done
[ -z "$root" ] && exit 0

cd "$root" 2>/dev/null || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# ── 2. Fetch (bounded by the hooks.json timeout; low-speed guard for
#       flaky networks so we degrade instead of eating the whole budget) ──
if ! git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=10 fetch --quiet origin 2>/dev/null; then
    echo "[cp] tenant freshness: could not fetch origin (offline?) — working from local state; pull when back online."
    exit 0
fi

upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null) || exit 0

behind=$(git rev-list --count "HEAD..$upstream" 2>/dev/null || echo 0)
ahead=$(git rev-list --count "$upstream..HEAD" 2>/dev/null || echo 0)
dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')

# Current (or ahead-only, the about-to-push state): nothing to gate.
# A dirty-but-current tree is normal mid-work state — not this hook's business.
[ "$behind" -eq 0 ] && exit 0

# ── 3. Safe to fast-forward: clean tree, strictly behind ──────────────
if [ "$dirty" -eq 0 ] && [ "$ahead" -eq 0 ]; then
    if git pull --ff-only --quiet 2>/dev/null; then
        echo "[cp] tenant fast-forwarded $behind commit(s) from origin."
        exit 0
    fi
fi

# ── 4. Can't safely pull: warn loudly before any edits happen ─────────
echo "⚠ [cp] TENANT CLONE STALE: $behind commit(s) behind / $ahead ahead of $upstream; $dirty uncommitted file(s)."
echo "  Reconcile BEFORE editing: commit or discard local changes, then \`git pull --rebase\`."
echo "  Stale generated mirrors (cp.md, _sources.md, sprint files) are usually safe to discard — sync regenerates them."
exit 0
