# Releasing cp-engine

One command does the whole release:

```bash
scripts/release.py 0.131.0
```

Before it runs, draft `## v0.131.0 — <date>` at the top of `CHANGELOG.md`,
push, and wait for the `tests` workflow to go green. The CI gate refuses a
release unless a green run tested the code being released.

## What it does

1. **Preflight.** Clean tree, on `main`, version strictly higher, changelog
   section on top, tag free locally and on origin, CI gate green.
2. **Bump, test, build, commit.** Rewrites the six version mirrors
   (`pyproject.toml`, `__init__.py`, `plugin.json`, `marketplace.json`, the
   webhook pin, the hosted `SERVER_VERSION`), runs pytest, builds, and commits.
   If anything fails before the commit (red tests, a failed build, Ctrl-C),
   every bumped file and `uv.lock` goes back byte-for-byte, so the next
   attempt starts from a clean tree (#346).
3. **Tag and push** main + `v<version>`.
4. **Post-release steps** (`scripts/post_release.py`). Each step can be run
   again safely: a finished step reports `already …`. Each prints one line,
   and if it fails it prints the manual command that finishes the job.

| Step | Does | Verified by |
|---|---|---|
| `local-install` | `uv tool install --force --reinstall --from <clone> cp-engine` | `cxp --version` |
| `plugins` | `claude plugin marketplace update cp-engine`, then `claude plugin update` for **every** entry in `~/.claude/plugins/installed_plugins.json`: user scope, plus each project scope, run from inside its project | the registry lists the new version for every entry |
| `hosted-deploy` | `prototypes/hosted-mcp/deploy.sh -m "cp-engine v<v>"` | `https://cp.mc-2.1p.is/health` → `server_version == hosted-cp/<v>` |
| `webhook-verify` | nothing to run: the webhook deploys itself from main | `https://cp-engine-production.up.railway.app/health` → `cp_engine_version == <v>` |
| `tenant-pin` | **minor bumps only**: sets `[engine] version = "~= X.Y"` in each tenant, commits just that file, and pushes with the tenant's `.github/scripts/push-with-rebase.sh` | origin already carries the pin (checked first) |
| `mc2-pin` | bumps mc-2's cp-engine pin, plus every shared `1p-component-library` pin, to cp-engine's revisions at the tag (`test_shared_pin_parity`). Runs mc-2's backend gate the way `backend-tests.yml` does, then commits and pushes to mc-2 **main** (= DEV) | `https://api-dev-9400.up.railway.app/api/version` reports the pushed commit or a later one |

`plugins` and `tenant-pin` depend on `local-install`. If it fails, they are
reported `blocked` and are not run. Steps that do not depend on it still run.
At the end a summary table prints and, if anything failed, a resume line:

```
[post] resume: scripts/release.py --resume-post 0.131.0 --only local-install --only plugins
```

## Flags

| Flag | Effect |
|---|---|
| `--skip STEP` | Skip a post step (repeatable). |
| `--only STEP` | Run only these post steps (repeatable). A dependency you did not select is assumed already done. |
| `--no-post` | Stop after the push. |
| `--resume-post VERSION` | Run the post steps alone for a tag already on origin. With no previous version to compare, `X.Y.0` counts as the minor bump. |
| `--tenant PATH` | Tenant clone(s) for `tenant-pin` (repeatable). |
| `--mc2-repo PATH` | The mc-2 clone (default `$MC2_REPO`, else the cp-engine clone's sibling `mc-2`). |
| `--mc2-python PATH` | The Python for mc-2's gate (default: discovered, see below). |
| `--poll-timeout SECS` | How long to wait for each deploy to report the new version (default 900). |
| `--dry-run` | Run preflight and list the post steps that would run. |

`scripts/post_release.py VERSION [--previous X.Y.Z] [flags]` is the same
pipeline without the release in front of it.

## How tenants are found

In order:

1. `--tenant PATH`, or `CP_RELEASE_TENANTS` (a list separated by `:`).
2. Otherwise, any sibling of the cp-engine clone whose `.cp-engine.toml` has
   both `[tenant]` and `[engine]`. mc-2's parity test relies on the same
   sibling layout. The tenant above the clone's `.cp-link` target is added too.

If nothing is found, the step fails and tells you to pass `--tenant`. The pin
rules come from `cp_engine.health.raise_pin_floor`, the same function
`cxp sync` uses:

- the pin is never lowered;
- `version_lock = true` is honoured;
- a compound constraint someone wrote by hand is left alone.

A tenant that is not on `main`, or whose `.cp-engine.toml` has uncommitted
edits, is refused rather than committed around.

## mc-2

- **Isolated.** The bump happens in a temporary `git worktree` of
  `origin/main`, so your mc-2 checkout and its uncommitted work are never
  touched. The worktree has no `backend/.env`, just like CI.
- **The venv.** The gate uses mc-2's own venv, never a global install and
  never a new venv. It picks the venv under `backend/venv`, `venv`,
  `backend/.venv` or `.venv` whose Python matches the workflow's
  `python-version`. On Drew's machine that is `backend/venv` (3.13). The venv
  is brought up to the new pins: `pip install -r requirements.txt` as CI
  does, then a `--force-reinstall --no-deps` of the git pins. pip skips a git
  dependency whose version string did not change
  (`test_installed_pins_match`).
- **The gate.** The command comes from `backend-tests.yml` itself, with its
  `--ignore` list and working directory, so the two cannot drift.
  `CP_ENGINE_PATH` points at this clone so the parity guard reads the
  released pins.
- **A red gate commits nothing.**
- **PROD is never automated.** Production deploys from mc-2's `release`
  branch. The pipeline refuses any push refspec other than
  `HEAD:refs/heads/main`. It prints the promotion command for a person to run
  when Drew says so:

  ```bash
  git -C ../mc-2 push origin origin/main:refs/heads/release
  ```

## Credentials

The hosted deploy needs the Railway CLI to be logged in, which it is locally.
Git pushes use your normal git credentials. Command output is redacted before
it is printed: credentials in URLs, `gh*_`/`github_pat_` tokens, and
`token=`/`secret=`/`password=` values.

## After a release

There is no local MCP server to restart (the `cxp mcp` server was retired in
step 5b). A hosted change reaches sessions only after `deploy.sh`; a session
connected before the deploy keeps its old tool list until `/mcp` reconnects.
