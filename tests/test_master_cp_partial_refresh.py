# tests/test_master_cp_partial_refresh.py — the #251 marker in master-cp.md
"""master-cp.md flags a summary whose stamp outruns its state fields.

The detector (`exec_summary_freshness`) dates fields by `git blame`, so it can
only answer from a full history. The index is rendered both locally (full
clone) and by the tenant's daily CI sync, which has historically checked out
shallow. Two properties are pinned here:

1. From a full history, a Status-only refresh renders `⚠️ _partial refresh_`
   in the project's row (and a full refresh does not).
2. From a SHALLOW clone of the very same history, the sync renders exactly
   what it rendered before this marker existed — byte for byte. No false
   positive, and no other difference a flip between the two could cause.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cp_engine import sync_tenant
from cp_engine.render import EXEC_SUMMARY_END, EXEC_SUMMARY_START
from cp_engine.sync import find_working_dir
from tests.test_sync import FakeBackend, make_config, make_state

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)

MARKER = "⚠️ _partial refresh_"
_CODE = "ggl-5188-calendar-maintenance"
_NOW = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)

_JULY = dict(
    status="Widgets built; waiting on Tony's spec.",
    where="Calendar/cards/tables widgets built and working.",
    next_up="Drew: widget updates per Tony's spec by Fri 7/17.",
    blockers="Tony's spec (due Tue) gates the build.",
)


def _region(stamp: str, status: str, where: str, next_up: str,
            blockers: str) -> str:
    return (
        "# ggl-5188 Calendar maintenance\n\n"
        f"{EXEC_SUMMARY_START}\n"
        f"## Exec Summary  ·  updated {stamp}\n\n"
        "**Objective:** Keep the Go Safety calendar widgets current.\n"
        f"**Status:** {status}\n\n"
        f"**Where it stands:**\n- {where}\n\n"
        f"**Next up:**\n- {next_up}\n\n"
        f"**Blockers:**\n- {blockers}\n"
        f"{EXEC_SUMMARY_END}\n"
    )


def _git(repo: Path, *args: str, when: str | None = None) -> None:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    })
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                   capture_output=True)


def _sync(root: Path) -> str:
    fake = FakeBackend((make_state(code=_CODE, name="Calendar maintenance"),))
    sync_tenant(make_config(root), backend_factory=lambda _: fake, now=_NOW)
    return (root / "master-cp.md").read_text(encoding="utf-8")


def _tenant_with_history(root: Path, *, full_refresh: bool) -> Path:
    """Scaffold via a first sync, author the summary in July, then write it
    again on 09-15 — Status only, or every field."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _sync(root)
    wd = find_working_dir(root, _CODE, None)
    assert wd is not None
    cp_md = wd / "cp.md"
    cp_md.write_text(_region("2026-07-14", **_JULY), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "july", when="2026-07-14T12:00:00-07:00")
    if full_refresh:
        september = dict(
            status="Now folded into ggl-5197's $130K scope.",
            where="Calendar work moved to ggl-5197.",
            next_up="Close this job out once 5197's SOW signs.",
            blockers="None.",
        )
    else:
        september = dict(_JULY, status="Now folded into ggl-5197's $130K scope.")
    cp_md.write_text(_region("2026-09-15", **september), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "sept", when="2026-09-15T12:00:00-07:00")
    return root


def _row(master: str) -> str:
    rows = [ln for ln in master.splitlines() if f"`{_CODE}`" in ln]
    assert rows, "project row missing from master-cp.md"
    return "\n".join(rows)


def test_status_only_refresh_is_marked_in_master_cp(tmp_path):
    """CONTROL: fails against the pre-#251-follow-up engine, which never
    renders the marker."""
    root = _tenant_with_history(tmp_path / "t", full_refresh=False)
    master = _sync(root)
    assert MARKER in _row(master)
    assert master.count(MARKER) == 1


def test_full_refresh_is_not_marked(tmp_path):
    root = _tenant_with_history(tmp_path / "t", full_refresh=True)
    assert MARKER not in _sync(root)


def test_no_git_is_not_marked(tmp_path):
    root = tmp_path / "t"
    root.mkdir()
    _sync(root)
    wd = find_working_dir(root, _CODE, None)
    september = dict(_JULY, status="Now folded into ggl-5197's $130K scope.")
    (wd / "cp.md").write_text(_region("2026-09-15", **september),
                              encoding="utf-8")
    assert MARKER not in _sync(root)


def test_shallow_clone_renders_exactly_as_before(tmp_path, monkeypatch):
    """The same partial-refresh history, seen through a --depth 1 clone,
    must render byte-identical to the engine without this feature."""
    import cp_engine.sync as sync_mod

    src = _tenant_with_history(tmp_path / "src", full_refresh=False)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{src}",
                    str(clone)], check=True, capture_output=True)
    shallow = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"], cwd=clone,
        capture_output=True, text=True, check=True).stdout.strip()
    assert shallow == "true"

    shallow_master = _sync(clone)

    # "Today's output": the same full-history tenant with the derivation
    # switched off, which is what the engine rendered before the marker.
    # (raising=False so this also runs against an engine without the hook.)
    monkeypatch.setattr(sync_mod, "_derive_partial_refresh_days",
                        lambda *_a, **_k: None, raising=False)
    before = _sync(src)

    assert MARKER not in shallow_master
    assert shallow_master == before
