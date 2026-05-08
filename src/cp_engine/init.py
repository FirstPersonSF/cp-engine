"""Interactive `cp init` flow.

Walks a new collaborator through populating `.cp-engine.local.toml`.
Run from inside a tenant repo that already has a committed `.cp-engine.toml`.

Spec v02 §5.2. Design notes:

- **CWD-only.** Run from the tenant root; we don't walk upward.
- **Only-prompt-for-new.** On re-init, only ask about projects that are
  in `.cp-engine.toml` but missing from `.cp-engine.local.toml`. Existing
  entries (including intentional skips) are preserved.
- **Strict path validation.** Per-project, up to 3 re-prompts on a bad path,
  then we abort with guidance for that project. Matches config.py's
  fail-loud philosophy: never write a config we know is broken.
- **Round-trip safe.** Uses tomlkit so manual edits and comments in
  `.cp-engine.local.toml` survive re-init.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import IO, Callable

import tomlkit
from tomlkit import TOMLDocument

from cp_engine import __version__ as ENGINE_VERSION
from cp_engine.config import (
    COMMITTED_FILENAME,
    LOCAL_FILENAME,
    CommittedConfigMissing,
    _load_committed,
)


class InitAborted(Exception):
    """User aborted (Ctrl-C, or too many bad-path retries)."""


# A prompt callable so we can inject a fake one in tests.
PromptFn = Callable[[str], str]
MAX_PATH_RETRIES = 3


def run_init(
    tenant_root: Path,
    *,
    prompt: PromptFn | None = None,
    out: IO[str] | None = None,
    interactive: bool = True,
) -> Path:
    """Run interactive init in `tenant_root`. Returns path to the local file.

    Args:
        tenant_root: directory that holds `.cp-engine.toml`. CWD by default
            in the CLI; tests pass an explicit tmp_path.
        prompt: function taking a prompt string, returning the user's
            response. Defaults to `input` for the real CLI; tests inject
            a deterministic stand-in.
        out: stream to write progress messages to. Defaults to stdout.
        interactive: if False, don't prompt at all — just touch a fresh
            local file with empty entries for every committed project.
            Useful for CI bootstrap or running from automation.
    """
    out = out or sys.stdout
    prompt = prompt or input

    tenant_root = tenant_root.resolve()

    # Use config.py's _load_committed for parsing — keeps the schema
    # validation in one place. Translates filesystem absence into
    # the engine's typed error.
    try:
        committed = _load_committed(tenant_root)
    except CommittedConfigMissing as exc:
        # Re-raise with the exact same error so the CLI handler sees the
        # public type. (Exception class is already public.)
        raise

    print(
        f"cp-engine v{ENGINE_VERSION} — initializing local config for tenant: "
        f"{committed['name']}",
        file=out,
    )
    print(file=out)

    # Load existing local file if present, else start a fresh document.
    local_path = tenant_root / LOCAL_FILENAME
    doc = _load_or_create_local_doc(local_path)
    repos_table = doc.setdefault("repos", tomlkit.table())

    committed_codes = [p["code"] for p in committed["projects"]]
    already_configured = set(repos_table.keys())
    needs_prompting = [c for c in committed_codes if c not in already_configured]

    if not needs_prompting:
        print(
            f"All {len(committed_codes)} projects already have entries in "
            f"{LOCAL_FILENAME}. Nothing to do.",
            file=out,
        )
        return local_path

    if already_configured:
        print(
            f"Found {len(already_configured)} project(s) already configured; "
            f"prompting for {len(needs_prompting)} new one(s).",
            file=out,
        )
    else:
        print(f"Found {len(committed_codes)} project(s) in {COMMITTED_FILENAME}.", file=out)
    print(file=out)

    # github = "owner/repo" — index by code for the prompts.
    github_by_code = {p["code"]: p["github"] for p in committed["projects"]}

    for code in needs_prompting:
        github = github_by_code[code]
        if not interactive:
            # Non-interactive: write empty (skip) for every project.
            repos_table[code] = ""
            print(f"  {code} ({github}) — non-interactive, marked as skip", file=out)
            continue
        try:
            value = _prompt_for_path(code, github, prompt, out)
        except InitAborted:
            # Save what we have so far before re-raising — partial progress
            # is better than losing typed paths to a Ctrl-C halfway through.
            _write_local_doc(local_path, doc)
            raise
        repos_table[code] = value

    _write_local_doc(local_path, doc)

    print(file=out)
    print(f"Wrote {local_path.relative_to(tenant_root)}.", file=out)
    print("`cp sync` will pick up these paths.", file=out)
    return local_path


# ──────────────────────────────────────────────────────────────────────
#  Internals
# ──────────────────────────────────────────────────────────────────────


def _load_or_create_local_doc(local_path: Path) -> TOMLDocument:
    """Load an existing local file (round-trip safe) or seed a fresh document."""
    if local_path.exists():
        return tomlkit.parse(local_path.read_text())
    doc = tomlkit.document()
    doc.add(tomlkit.comment("cp-engine local config — gitignored, per-machine."))
    doc.add(
        tomlkit.comment(
            "Edit by hand or re-run `cp init` after adding projects to .cp-engine.toml."
        )
    )
    doc.add(tomlkit.nl())
    return doc


def _write_local_doc(local_path: Path, doc: TOMLDocument) -> None:
    local_path.write_text(tomlkit.dumps(doc))


def _prompt_for_path(
    code: str,
    github: str,
    prompt: PromptFn,
    out: IO[str],
) -> str:
    """Ask the user for a local path; validate strictly; allow `[skip]` empty.

    Re-prompts up to MAX_PATH_RETRIES times on a non-existent or non-directory
    path before raising InitAborted (which the caller treats as "leave this
    project unconfigured and bail").
    """
    print(f"  {code} ({github})", file=out)

    for attempt in range(1, MAX_PATH_RETRIES + 1):
        raw = prompt(f"  Local path? [skip]: ").strip()

        if raw == "":
            print(f"    skipped (no local access)", file=out)
            return ""

        candidate = Path(raw).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            print(
                f"    Path does not exist: {candidate}  "
                f"(attempt {attempt}/{MAX_PATH_RETRIES})",
                file=out,
            )
            continue

        if not resolved.is_dir():
            print(
                f"    Not a directory: {resolved}  "
                f"(attempt {attempt}/{MAX_PATH_RETRIES})",
                file=out,
            )
            continue

        # Store the user's typed value, not the resolved one — preserves
        # symlink semantics and lets future filesystem moves work without
        # re-init (config.load() resolves at read time).
        print(f"    OK: {resolved}", file=out)
        return raw

    raise InitAborted(
        f"Too many invalid paths for {code}. Aborting; rerun `cp init` after "
        f"locating the repo, or edit {LOCAL_FILENAME} directly."
    )
