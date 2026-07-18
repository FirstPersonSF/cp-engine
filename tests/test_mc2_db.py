"""Tests for `cp_engine.mc2_db` — the single MC-2 access layer (arch-phase-3).

Three groups:
- `load_supabase_creds` precedence (env → op:// → dotenv; config=None
  env-only; SUPABASE_KEY alias). The op://-tier behaviors keep their
  original coverage in test_sync_mc2.py (retargeted to mc2_db).
- `get_client` construction, caching, and the required=False fail-soft.
- The registry-enforcement grep: no `.table("...")` string literal may
  exist outside mc2_db — this is issue #27's "one grep finds every MC-2
  table reference" done-when, made permanent.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from cp_engine import mc2_db
from cp_engine.sync import BackendUnavailable


def _make_config(root: Path, mc2_clone: Path | None = None) -> SimpleNamespace:
    """The minimal TenantConfig surface the resolver reads: root + local_repos."""
    repos = {"mc-2": mc2_clone} if mc2_clone is not None else {}
    return SimpleNamespace(root=root, local_repos=MappingProxyType(repos))


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY"):
        monkeypatch.delenv(var, raising=False)


# ──────────────────────────────────────────────────────────────────────
#  load_supabase_creds
# ──────────────────────────────────────────────────────────────────────


def test_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://env.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "env-key")
    assert mc2_db.load_supabase_creds(None) == ("https://env.supabase.co", "env-key")


def test_supabase_key_env_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """SUPABASE_KEY is a last-resort alias for the service key (the legacy
    name cli.py's digest-link helper honored before consolidation)."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", "https://env.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "legacy-key")
    assert mc2_db.load_supabase_creds(None) == ("https://env.supabase.co", "legacy-key")


def test_service_key_beats_legacy_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", "https://env.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key")
    monkeypatch.setenv("SUPABASE_KEY", "legacy-key")
    assert mc2_db.load_supabase_creds(None)[1] == "service-key"


def test_no_config_is_env_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """config=None (webhook context) never falls through to op:// or dotenv."""
    _clear_env(monkeypatch)
    with pytest.raises(BackendUnavailable) as exc:
        mc2_db.load_supabase_creds(None)
    assert "environment only" in str(exc.value)


def test_dotenv_tier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    clone = tmp_path / "mc-2"
    (clone / "backend").mkdir(parents=True)
    (clone / "backend" / ".env").write_text(
        'SUPABASE_URL="https://file.supabase.co"\nSUPABASE_SERVICE_KEY=file-key\n'
    )
    config = _make_config(tmp_path, mc2_clone=clone)
    assert mc2_db.load_supabase_creds(config) == (
        "https://file.supabase.co",
        "file-key",
    )


def test_op_tier_between_env_and_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch)
    (tmp_path / ".cp-engine.local.toml").write_text(
        "[supabase]\nurl_ref = \"op://v/i/url\"\nservice_key_ref = \"op://v/i/key\"\n"
    )
    resolved = {"op://v/i/url": "https://op.supabase.co", "op://v/i/key": "op-key"}
    monkeypatch.setattr(mc2_db, "_op_read", lambda ref: resolved[ref])
    # A dotenv that would ALSO resolve proves op:// wins over it.
    clone = tmp_path / "mc-2"
    (clone / "backend").mkdir(parents=True)
    (clone / "backend" / ".env").write_text(
        "SUPABASE_URL=https://file.supabase.co\nSUPABASE_SERVICE_KEY=file-key\n"
    )
    config = _make_config(tmp_path, mc2_clone=clone)
    assert mc2_db.load_supabase_creds(config) == ("https://op.supabase.co", "op-key")


def test_missing_everywhere_raises_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch)
    config = _make_config(tmp_path)
    with pytest.raises(BackendUnavailable):
        mc2_db.load_supabase_creds(config)


# ──────────────────────────────────────────────────────────────────────
#  get_client
# ──────────────────────────────────────────────────────────────────────


def test_get_client_builds_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://env.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "env-key")
    built = []

    def fake_create(url: str, key: str) -> object:
        built.append((url, key))
        return object()

    monkeypatch.setattr("supabase.create_client", fake_create)
    a = mc2_db.get_client()
    b = mc2_db.get_client()
    assert a is b
    assert built == [("https://env.supabase.co", "env-key")]


def test_get_client_cache_is_per_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("supabase.create_client", lambda url, key: object())
    a = mc2_db.get_client(url="https://a.supabase.co", key="ka")
    b = mc2_db.get_client(url="https://b.supabase.co", key="kb")
    a2 = mc2_db.get_client(url="https://a.supabase.co", key="ka")
    assert a is a2
    assert a is not b


def test_get_client_explicit_creds_bypass_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)  # resolution would raise; injection must not
    monkeypatch.setattr("supabase.create_client", lambda url, key: (url, key))
    assert mc2_db.get_client(url="https://x", key="k") == ("https://x", "k")


def test_get_client_required_false_returns_none_without_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    assert mc2_db.get_client(None, required=False) is None


def test_get_client_required_true_raises_without_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    with pytest.raises(BackendUnavailable):
        mc2_db.get_client(None)


def test_get_client_required_false_swallows_construction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(url: str, key: str) -> object:
        raise RuntimeError("bad url")

    monkeypatch.setattr("supabase.create_client", boom)
    assert mc2_db.get_client(url="https://x", key="k", required=False) is None


def test_get_client_resolves_creds_once_per_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeat calls must NOT re-run resolution (op:// tier = subprocesses)."""
    monkeypatch.setattr("supabase.create_client", lambda url, key: object())
    calls = []

    def fake_resolve(config):
        calls.append(config)
        return ("https://env.supabase.co", "env-key")

    monkeypatch.setattr(mc2_db, "load_supabase_creds", fake_resolve)
    a = mc2_db.get_client(None)
    b = mc2_db.get_client(None)
    assert a is b
    assert len(calls) == 1


def test_reset_client_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("supabase.create_client", lambda url, key: object())
    a = mc2_db.get_client(url="https://x", key="k")
    mc2_db.reset_client_cache()
    b = mc2_db.get_client(url="https://x", key="k")
    assert a is not b


# ──────────────────────────────────────────────────────────────────────
#  Row mappers
# ──────────────────────────────────────────────────────────────────────


def test_spine_substance_row_tolerates_narrow_shapes() -> None:
    row = mc2_db.SpineSubstanceRow.from_row(
        {"est_item_id": 42, "status": "live", "version_date": "2026-05-01"}
    )
    assert row.est_item_id == "42"  # TEXT in the DB; coerced for comparisons
    assert row.status == "live"
    assert row.version_date == "2026-05-01"
    assert row.body is None and row.important is False


def test_spine_substance_row_ignores_unknown_keys() -> None:
    row = mc2_db.SpineSubstanceRow.from_row(
        {"est_item_id": "x", "status": "live", "cached_messages": "..."}
    )
    assert row.est_item_id == "x"
    assert row.status == "live"


def test_rag_asset_row_maps_list_shape() -> None:
    row = mc2_db.RagAssetRow.from_row(
        {"id": "a", "title": "Brief", "source_type": "gdoc", "status": "active"}
    )
    assert (row.id, row.title, row.source_type) == ("a", "Brief", "gdoc")


# ──────────────────────────────────────────────────────────────────────
#  Registry enforcement — issue #27's done-when, made permanent
# ──────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Full-text scan (\s* spans newlines): catches single OR double quotes,
# f-/r-prefixes, and formatter-wrapped calls where the string lands on the
# next line. Deliberately no comment-skipping — a table literal in a comment
# is worth flagging too.
_TABLE_LITERAL = re.compile(r'\.table\(\s*[frbu]{0,2}["\']')


def _scan_dirs() -> list[Path]:
    return [
        p
        for d in ("src/cp_engine", "webhook")
        for p in (_REPO_ROOT / d).rglob("*.py")
        if p.name != "mc2_db.py"
    ]


def test_no_table_string_literals_outside_registry() -> None:
    """Every `.table(...)` call must go through `Tables` (or a constant
    assigned from it). A raw string literal here means a new table
    reference was added outside the registry — add it to
    `mc2_db.Tables` instead."""
    offenders: list[str] = []
    for path in _scan_dirs():
        text = path.read_text()
        for match in _TABLE_LITERAL.finditer(text):
            lineno = text.count("\n", 0, match.start()) + 1
            line = text.splitlines()[lineno - 1].strip()
            offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line}")
    assert not offenders, (
        "raw .table(\"...\") literals bypass the mc2_db.Tables registry:\n"
        + "\n".join(offenders)
    )


def test_registry_covers_known_tables() -> None:
    """The registry names every table the survey found (drift alarm both ways)."""
    expected_public = {
        "projects", "repos", "initiatives", "companies", "github_orgs",
        "sprint_allocations", "fathom_meetings", "auto_ingest_runs",
        "asset_ingest_runs", "rag_assets", "asset_chunks",
        "clickup_task_proposals",
        "commitments", "app_config",
        "spine_substance", "spine_context", "spine_elements",
        "spine_snapshots", "spine_inbox", "spine_promote_runs",
        "spine_relations",
    }
    names = {
        v
        for k, v in vars(mc2_db.Tables).items()
        if not k.startswith("_") and not k.startswith("EST_")
    }
    assert names == expected_public
