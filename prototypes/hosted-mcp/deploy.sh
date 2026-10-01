#!/usr/bin/env bash
# Deploy the hosted-cp MCP server to Railway — ONE command, from anywhere:
#
#   prototypes/hosted-mcp/deploy.sh -m "<what changed>"
#   prototypes/hosted-mcp/deploy.sh --build-local      # docker build, no deploy
#
# WHY A SCRIPT (architecture plan step 1). The image installs cp-engine from
# the same checkout as server.py, so the build context must hold the repo's
# `pyproject.toml`, `README.md` and `src/` as well as this directory. The
# service has no GitHub connection, so the context is whatever `railway up`
# uploads. Uploading the whole repo root would ship .venv and every test; a
# root-level railway.toml would be read by other Railway services built from
# this repo. So this stages exactly the paths the Dockerfile COPYs, from
# `git archive HEAD` — the engine and the server are the SAME commit, and that
# commit is written to BUILD_COMMIT for /health and whoami to report.
#
# A working tree with uncommitted changes under src/, pyproject.toml or this
# directory is refused: the deploy would not be the commit it claims to be.
# Commit first (a branch is fine), or pass --allow-dirty to stage the working
# tree instead (BUILD_COMMIT then ends in "-dirty").
#
# `railway up` returning only means the build was QUEUED. Poll until the newest
# deployment is terminal before believing it shipped:
#   railway deployment list --service hosted-mcp --json
set -euo pipefail

PROJECT=9d463e04-7f42-455b-884a-c7718dc6f6f2      # Mission Control
ENVIRONMENT=6db33527-d0f9-4a3d-98a6-65b27385afa6  # production
SERVICE=42ca79bc-c416-45cd-93ce-77ecee3c6179      # hosted-mcp

message=""
mode=deploy
allow_dirty=0
while [ $# -gt 0 ]; do
  case "$1" in
    -m|--message) message="$2"; shift 2 ;;
    --build-local) mode=build; shift ;;
    --stage-only) mode=stage; shift ;;
    --allow-dirty) allow_dirty=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "deploy.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

root="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
cd "$root"

# The Dockerfile's COPY sources, minus BUILD_COMMIT (written below).
paths=(pyproject.toml README.md src
       prototypes/hosted-mcp/Dockerfile prototypes/hosted-mcp/railway.toml
       prototypes/hosted-mcp/requirements.txt prototypes/hosted-mcp/server.py
       prototypes/hosted-mcp/observability.py)

commit="$(git rev-parse --short=12 HEAD)"
dirty="$(git status --porcelain -- "${paths[@]}")"
if [ -n "$dirty" ] && [ "$allow_dirty" -ne 1 ]; then
  echo "deploy.sh: uncommitted changes in deployed paths — commit first, or pass --allow-dirty:" >&2
  echo "$dirty" >&2
  exit 1
fi

stage="$(mktemp -d "${TMPDIR:-/tmp}/hosted-cp-stage.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
if [ -n "$dirty" ]; then
  # --allow-dirty: the working tree as it stands (tracked + untracked, minus
  # ignored files), so what you tested locally is what ships.
  git ls-files -z --cached --others --exclude-standard -- "${paths[@]}" \
    | while IFS= read -r -d '' f; do
        [ -f "$f" ] || continue
        mkdir -p "$stage/$(dirname "$f")"
        cp "$f" "$stage/$f"
      done
  commit="$commit-dirty"
else
  git archive HEAD -- "${paths[@]}" | tar -x -C "$stage"
fi
printf '%s\n' "$commit" > "$stage/prototypes/hosted-mcp/BUILD_COMMIT"
# Railway reads railway.toml at the ROOT of the upload; its dockerfilePath is
# relative to that root.
cp "$stage/prototypes/hosted-mcp/railway.toml" "$stage/railway.toml"

case "$mode" in
  stage)
    trap - EXIT
    echo "$stage"
    ;;
  build)
    tag="hosted-cp:$commit"
    if [ -z "${GH_TOKEN:-}" ]; then
      GH_TOKEN="$(gh auth token)"
    fi
    docker build --build-arg gh_token="$GH_TOKEN" \
      -f "$stage/prototypes/hosted-mcp/Dockerfile" -t "$tag" "$stage"
    echo "built $tag"
    ;;
  deploy)
    [ -n "$message" ] || message="hosted-cp $commit"
    railway up "$stage" --path-as-root --detach -m "$message" \
      --project "$PROJECT" --environment "$ENVIRONMENT" --service "$SERVICE"
    echo "queued $commit — poll: railway deployment list --service hosted-mcp --json"
    ;;
esac
