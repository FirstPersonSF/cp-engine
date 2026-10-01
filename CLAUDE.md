# cp-engine

**If you are a session a person pointed here to install, update, or explain
cp, read [`docs/install.md`](docs/install.md) first and follow it.** It is
written for you, the session, not for the person; it names what they can be
assumed to know (what cp is for, nothing about how it is built).

## What this is, in the user's words

cp is First Person and Canonic's shared, current picture of every workstream
(client jobs, accounts, programs, internal initiatives). **The spine** is the
memory, kept in Mission Control; **the tenant** (`FirstPersonSF/cp`) is the
shared notebook, one folder per workstream; **the sync** keeps the notebook
current. This repo is the engine behind all three.

## How a person uses it

Conversationally: they open Claude Code in their cp tenant folder and say what
they want ("update ggl-5168", "wrap up"). The session does the rest. A person
should have to remember nothing else.

## Install, verify, upgrade, uninstall

[`docs/install.md`](docs/install.md). Verify is `cxp doctor`; the install
records itself with `cxp record-install`. Do not improvise an install from the
rest of this repo.

## If you are here to change the engine

- Conventions: [`docs/conventions/`](docs/conventions/) — start with
  [`self-describing-repo.md`](docs/conventions/self-describing-repo.md), which
  includes the reader test every user-facing string must pass.
- Spec: [`docs/specs/cp-engine-spec-v02.md`](docs/specs/cp-engine-spec-v02.md).
- Releases: always `scripts/release.py <version>` — it bumps every version
  file, tags, pushes, then runs every post-release step (CLI + plugins, hosted
  deploy, webhook check, tenant pin, mc-2 pin) and verifies each.
  `--resume-post <version>` finishes a partial run. mc-2 PROD promotion is
  never automated. See [`docs/releasing.md`](docs/releasing.md).
- Tenant `CLAUDE.md` files are generated from
  `src/cp_engine/templates/CLAUDE.md.j2`; change the template, never a tenant copy.
- Tests: `pytest` from the repo root, with this checkout's `src` first on the
  path (an editable install elsewhere will otherwise be imported instead).
