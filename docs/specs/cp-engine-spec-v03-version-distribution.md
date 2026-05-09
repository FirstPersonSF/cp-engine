---
Project: Context Protocol Engine
Provenance: Architectural change proposal | 2026-05-09
Filename: cp-engine-spec-v03-version-distribution.md
Author: Drew + Claude (for Tony's review)
Status: PROPOSAL — not yet accepted
---

# CP Engine Spec v03 — Version Distribution

> Single change to v02: define how cp-engine versions reach end users (humans and runners). Same vocabulary, same modes, same sync semantics, same framework/tenant split — different release and update mechanics. Drafted while answering "what happens when Tony installs the plugin" and finding the honest answer is "four separate things, manually kept in sync." Tony — please review and either accept, push back, or refactor.

## Why this surfaced

Right now (v0.5.2) a single release of cp-engine touches **four separate places** where a version lives, all of which can drift out of sync:

| # | Location | What's pinned | How it currently updates |
|---|---|---|---|
| 1 | Tony's machine — Claude Code plugin | slash command markdown (`plugin/`) | `/plugin update cp-engine` (manual) |
| 2 | Tony's machine — `cp` CLI | Python entry point | `uv tool install --force --from <path>` (manual) |
| 3 | Tenant repo — `.cp-engine.toml` | `[engine].version` constraint (e.g. `~= 0.5`) | Edit + commit (manual, deliberate) |
| 4 | GitHub Actions runner — `sync.yml` | `pip install ...@v0.5.2` (hardcoded tag) | Edit workflow file + commit (manual) |

A release today means: tag the engine repo, bump `marketplace.json`, ask every user to run `/plugin update` AND `uv tool install --force`, and edit each tenant's `sync.yml` to point at the new tag. v0.4.1's `EngineVersionMismatch` catches CLI-vs-tenant skew, but nothing catches plugin-vs-CLI skew or tenant-vs-runner skew.

This will not survive a second user. The first time Tony runs `/cp-summarize` and gets a `EngineVersionMismatch` because he forgot one of the two install commands, the system stops feeling like a tool and starts feeling like a maintenance burden.

## Three options considered

1. **Documentation only** — write an "Updating cp-engine" section, list the commands. Rejected: relies on humans remembering. v0.4.1's hard-fail check is already a partial admission that humans don't remember.

2. **Lockstep release script + version-check hook** — codify the four-place bump as a script; add a `SessionStart` hook that auto-installs the matching CLI. Reduces release work, makes plugin updates feel atomic, but adds network ops to session startup.

3. **Single install path (plugin owns the CLI)** — the plugin shipping is the install. CLI becomes an implementation detail of the plugin. Cleanest UX, biggest change to the install model.

This proposal is **option 2 with parts of option 3**: lockstep release, plugin-owned auto-install, but the CLI stays separately reusable for cron jobs and CI.

## The change in one paragraph

Make `pyproject.toml`'s `[project].version` the **single source of truth** for the engine version. Add a release script that propagates that version to `plugin.json`, `marketplace.json`, the CHANGELOG header, and a git tag, in one command. Add a `SessionStart` hook in the plugin that compares the installed `cp` CLI version to the version baked into the plugin and runs `uv tool install --force --from git+...@v<version>` if they don't match. Switch the runner workflow to install from the engine ref recorded in the tenant's `.cp-engine.toml` (resolve `~= 0.5` against the engine repo's tags at workflow run time), so bumping the tenant pin is sufficient — no workflow edit needed per release.

End state: a release is `./scripts/release.sh <version>`. Tony updates by running `/plugin update cp-engine` and the CLI follows automatically. Tenants opt into a new minor version by editing one line in `.cp-engine.toml`, and the runner picks it up on the next cron tick.

## What changes

### 1. Single source of truth for version

`pyproject.toml`'s `[project].version` is canonical. All other places read from it (at build time) or are bumped by the release script (at release time):

| File | How it's kept in sync |
|---|---|
| `pyproject.toml` | source of truth |
| `src/cp_engine/__init__.py` `__version__` | `importlib.metadata.version("cp-engine")` at runtime, no hardcoded string |
| `plugin/plugin.json` `version` | bumped by release script |
| `.claude-plugin/marketplace.json` `version` (top-level + per-plugin) | bumped by release script |
| `CHANGELOG.md` heading | release script enforces a `## v<X> — <date>` section exists before tagging |
| Git tag `v<X>` | created by release script |

### 2. Release script

`scripts/release.sh <new-version>`:

1. Validate working tree is clean.
2. Validate CHANGELOG has a `## v<new-version>` heading.
3. Update `pyproject.toml`, `plugin.json`, `marketplace.json` to the new version.
4. Run `pytest` — abort on failure.
5. Run `python -m build` — verify the package builds.
6. `git commit -am "v<new-version>: <one-line summary from CHANGELOG>"`.
7. `git tag v<new-version>`.
8. `git push && git push --tags`.
9. Print: "Released v<new-version>. Run `uv tool install --force --from . cp-engine` to update local CLI."

### 3. SessionStart hook (plugin-owned auto-install)

A new hook `plugin/hooks/sync-cli-version.sh` runs on `SessionStart`:

```sh
#!/usr/bin/env bash
# Plugin's expected CLI version is recorded in plugin.json. Compare to
# the installed `cp --version`. If missing or stale, install/upgrade.

PLUGIN_VERSION=$(jq -r .version "${CLAUDE_PLUGIN_ROOT}/plugin.json")
INSTALLED_VERSION=$(cp --version 2>/dev/null | awk '{print $NF}' || echo "missing")

if [ "$PLUGIN_VERSION" != "$INSTALLED_VERSION" ]; then
    echo "[cp-engine] CLI version drift: plugin=${PLUGIN_VERSION} installed=${INSTALLED_VERSION}. Updating..."
    uv tool install --force \
        --from "git+https://github.com/FirstPersonSF/cp-engine.git@v${PLUGIN_VERSION}" \
        cp-engine
fi
```

Properties:
- **Fast on the happy path.** Two subshell calls (~50ms total) when versions match. Network is only touched when an update is needed.
- **Loud on failure.** If `uv tool install` fails (offline, auth, network), the hook prints the error but doesn't block session start. The next `/cp-summarize` will fail with the existing `EngineVersionMismatch` and tell the user what to do.
- **No surprise installs.** The version installed is always the one matching the plugin the user explicitly chose to update to — not "latest." User stays in control of *when* to update; the hook just makes the two installs atomic.

### 4. Runner installs from tenant pin

`sync.yml`'s install step changes from:

```yaml
- name: Install cp-engine
  run: pip install "git+https://github.com/FirstPersonSF/cp-engine.git@v0.5.2"
```

to:

```yaml
- name: Install cp-engine
  run: |
    PIN=$(python -c "import tomllib; print(tomllib.load(open('.cp-engine.toml','rb'))['engine']['version'])")
    pip install "cp-engine ${PIN}" --extra-index-url <git-resolver>
    # OR, until cp-engine is on PyPI:
    LATEST_TAG=$(git ls-remote --tags --sort=-v:refname \
      https://github.com/FirstPersonSF/cp-engine.git \
      | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    # resolve $PIN against $LATEST_TAG, install from git
    pip install "git+https://github.com/FirstPersonSF/cp-engine.git@${RESOLVED}"
```

The exact resolution step is messier without PyPI; folding it into a small `cp resolve-engine-pin` helper keeps the workflow YAML clean.

### 5. Tenant pin discipline (unchanged)

The tenant's `[engine].version` constraint is still manually edited. This is **not** a regression — it's the right place for human judgment ("does this tenant want v0.6 yet?"). The change is that bumping the pin is now sufficient on its own; the runner picks it up on the next cron tick without a workflow edit.

## What stays the same

- The framework / tenant split (v02).
- The four kinds of artifact, the four principles, the sync semantics.
- `EngineVersionMismatch` and the existing runtime check — still the safety net for tenant-vs-CLI skew.
- The `cp` CLI is still independently usable outside the plugin (cron, CI, manual `cp sync`).
- `marketplace.json`'s `git-subdir` source — the plugin still ships from `plugin/` on `main`.

## Migration

For existing tenants (just `cp` for now):

1. Cut a v0.6.0 release using the new script (validates the script).
2. Update `cp/.github/workflows/sync.yml` to the tenant-pin-driven install (one-time edit).
3. Bump `cp/.cp-engine.toml`'s `[engine].version` to `~= 0.6` when ready.
4. Existing local installs continue to work; the SessionStart hook only kicks in on next plugin update.

For new users (Tony):

1. Install the plugin: `/plugin install cp-engine@cp-engine`.
2. First `SessionStart` after install detects no `cp` CLI and runs `uv tool install`. Done.

The README's "One-time per-machine setup" loses two steps.

## Open questions for Tony

1. **Is the SessionStart auto-install acceptable?** It runs on every Claude Code session start in a project where the plugin is loaded. Happy-path is ~50ms; update-path can be 2–5 seconds. Alternative: only install if the user explicitly runs a `/cp-engine-update` command, but that puts us back in "humans must remember" territory.

2. **Should the plugin block on install failure?** Current draft: hook prints error, doesn't block. Stricter alternative: hook hard-fails session start with a clear message. Stricter is safer (prevents "cp command not found" later) but more annoying when offline.

3. **How do we handle pre-PyPI?** The runner resolution helper is uglier without PyPI. Two paths: (a) publish cp-engine to PyPI (requires a maintenance commitment), or (b) keep the git-resolver helper indefinitely. Probably (a) once cp-engine settles, but not blocking.

4. **What about CLI-only users (cron, scripts) who never use the plugin?** They install the CLI manually with the version they choose. The SessionStart hook only affects users coming through Claude Code. Two install paths, same package — fine, but worth naming explicitly so we don't confuse ourselves later.

5. **Is this v0.6 or v1.0?** The version-distribution mechanics are infrastructure, not user-facing features. v0.6 fits the existing minor-bump cadence. But "we now have a real release process" might be a v1.0 story. Defer until we see what else lands in the v0.6 window.
