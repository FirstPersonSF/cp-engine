"""Tests for `cp_engine.sync_mc2` row-transformation logic.

These don't hit Supabase. They test the pure transformation of row dicts
(as Supabase would return them via PostgREST embeds) into ProjectStates.

v0.2 covers two source streams: engagement projects and standalone repos.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

import cp_engine.sync_mc2 as sync_mod
from cp_engine import mc2_db
from cp_engine.config import SyncConfig, TenantConfig
from cp_engine.sync import BackendUnavailable
from cp_engine.sync_mc2 import (
    _engagement_canonical_id,
    _engagement_row_is_valid,
    _engagement_row_to_state,
    _load_supabase_creds,
    _parse_iso,
    _parse_linked_repos,
    _parse_numeric,
    _repo_row_is_valid,
    _repo_row_to_state,
)


# ──────────────────────────────────────────────────────────────────────
#  Engagement canonical ID
# ──────────────────────────────────────────────────────────────────────


def _eng_row(**kw):
    base = {
        "number": 5192,
        "full_job_name": "IBX 5192 Platform Sales Readiness Summit",
        "companies": {"code": "IBX"},
    }
    base.update(kw)
    return base


def test_canonical_id_from_full_job_name() -> None:
    assert _engagement_canonical_id(_eng_row()) == "ibx-5192-platform-sales-readiness-summit"


def test_canonical_id_bare_number_code_still_clean() -> None:
    assert (
        _engagement_canonical_id(
            _eng_row(
                number=5136,
                full_job_name="GGL 5136 go/safety website",
                companies={"code": "GGL"},
            )
        )
        == "ggl-5136-go-safety-website"
    )


def test_canonical_id_falls_back_when_full_job_name_empty() -> None:
    assert _engagement_canonical_id(_eng_row(full_job_name="")) == "ibx-5192"
    assert _engagement_canonical_id(_eng_row(full_job_name=None)) == "ibx-5192"


def test_canonical_id_fallback_no_company() -> None:
    assert _engagement_canonical_id({"number": 9, "full_job_name": "", "companies": {}}) == "9"


def test_engagement_canonical_id_with_company_prefix() -> None:
    assert _engagement_canonical_id({"number": 5188, "companies": {"code": "GGL"}}) == "ggl-5188"
    assert _engagement_canonical_id({"number": 5168, "companies": {"code": "ibx"}}) == "ibx-5168"
    assert _engagement_canonical_id({"number": 5176, "companies": {"code": "Snt"}}) == "snt-5176"


def test_engagement_canonical_id_lowercases_prefix() -> None:
    assert _engagement_canonical_id({"number": 5188, "companies": {"code": "HEX"}}) == "hex-5188"


def test_engagement_canonical_id_falls_back_to_number_when_no_company() -> None:
    assert _engagement_canonical_id({"number": 5026, "companies": None}) == "5026"
    assert _engagement_canonical_id({"number": 5026}) == "5026"


def test_engagement_canonical_id_falls_back_when_company_has_no_code() -> None:
    assert _engagement_canonical_id({"number": 5099, "companies": {"code": ""}}) == "5099"
    assert _engagement_canonical_id({"number": 5099, "companies": {"code": None}}) == "5099"


def test_engagement_canonical_id_strips_whitespace_in_prefix() -> None:
    assert _engagement_canonical_id({"number": 1, "companies": {"code": "  GGL  "}}) == "ggl-1"


# ──────────────────────────────────────────────────────────────────────
#  Engagement row → ProjectState
# ──────────────────────────────────────────────────────────────────────


def test_engagement_row_to_state_happy_path() -> None:
    row = {
        "number": 5168,
        "companies": {"code": "GGL", "name": "Google", "kind": "client"},
        "full_job_name": "GGL 5168 Playbooks",
        "name": "Playbooks (Activation)",
        "mc_status": "Open",
        "account_manager": "Drew Fiero",
        "is_internal": False,
        "deal_stage": "Won",
        "budget": "150000",
        "updated_at": "2026-05-07T16:14:34.123456+00:00",
    }
    state = _engagement_row_to_state(row)

    assert state.code == "ggl-5168-playbooks"
    assert state.source == "engagement"
    assert state.company_kind == "client"
    assert state.company_code == "GGL"
    assert state.company_name == "Google"
    assert state.name == "GGL 5168 Playbooks"
    assert state.status == "Open"
    assert state.owner == "Drew Fiero"
    assert state.is_internal is False
    assert state.deal_stage == "Won"
    assert state.budget == 150000.0
    assert state.last_touched == datetime(
        2026, 5, 7, 16, 14, 34, 123456, tzinfo=timezone.utc
    )


def test_engagement_row_to_state_legacy_row_without_company() -> None:
    row = {
        "number": 5026,
        "companies": None,
        "full_job_name": None,
        "name": "SentinelOne 5107",
        "mc_status": "Deal",
        "account_manager": None,
        "is_internal": False,
        "deal_stage": None,
        "budget": None,
        "updated_at": None,
    }
    state = _engagement_row_to_state(row)
    assert state.code == "5026"
    assert state.source == "engagement"
    assert state.company_kind == "client"  # fallback
    assert state.company_code is None


def test_engagement_row_to_state_falls_back_to_name_when_full_job_name_missing() -> None:
    row = {
        "number": 1111,
        "companies": {"code": "GGL", "kind": "client"},
        "full_job_name": None,
        "name": "Just the project name",
        "mc_status": "Open",
        "account_manager": None,
        "is_internal": False,
        "updated_at": None,
    }
    state = _engagement_row_to_state(row)
    assert state.name == "Just the project name"


def test_engagement_row_to_state_settles_for_empty_name_when_both_null() -> None:
    row = {
        "number": 2222,
        "companies": {"code": "GGL", "kind": "client"},
        "full_job_name": None,
        "name": None,
        "mc_status": "Open",
        "is_internal": False,
        "updated_at": None,
    }
    state = _engagement_row_to_state(row)
    assert state.name == ""


def test_engagement_row_to_state_internal_flag_coerces_to_bool() -> None:
    row = {
        "number": 1,
        "companies": {"code": "X", "kind": "client"},
        "full_job_name": "X",
        "name": "X",
        "mc_status": "Open",
        "is_internal": 1,
        "updated_at": None,
    }
    assert _engagement_row_to_state(row).is_internal is True


# ──────────────────────────────────────────────────────────────────────
#  Engagement validation guard
# ──────────────────────────────────────────────────────────────────────


def test_engagement_row_is_valid_rejects_missing_number() -> None:
    assert not _engagement_row_is_valid({"number": None, "mc_status": "Open"})
    assert not _engagement_row_is_valid({"mc_status": "Open"})


def test_engagement_row_is_valid_rejects_old_vocab_status() -> None:
    assert not _engagement_row_is_valid({"number": 1, "mc_status": "Active"})
    assert not _engagement_row_is_valid({"number": 1, "mc_status": "Complete"})


def test_engagement_row_is_valid_rejects_unknown_status() -> None:
    assert not _engagement_row_is_valid({"number": 1, "mc_status": "Floating"})


def test_engagement_row_is_valid_accepts_all_canonical_statuses() -> None:
    for status in ("Deal", "Open", "Holding", "Closed", "Archived"):
        assert _engagement_row_is_valid({"number": 1, "mc_status": status}), status


# ──────────────────────────────────────────────────────────────────────
#  Repo row → ProjectState
# ──────────────────────────────────────────────────────────────────────


def test_repo_row_to_state_happy_path_fpsf() -> None:
    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "repo_name": "mc-2",
        "status": "Active",
        "description": "Mission Control codebase",
        "owner": "Drew",
        "updated_at": "2026-05-08T16:00:00+00:00",
        "github_orgs": {"name": "FirstPersonSF"},
        "companies": {"code": "1PI", "name": "First Person", "kind": "self-fpsf"},
    }
    state = _repo_row_to_state(row)

    assert state.code == "mc-2"
    assert state.name == "mc-2"
    assert state.source == "repo"
    assert state.company_kind == "self-fpsf"
    assert state.company_code == "1PI"
    assert state.company_name == "First Person"
    assert state.status == "Active"
    assert state.is_internal is False  # repos don't carry this flag
    assert state.owner == "Drew"
    assert state.github_org == "FirstPersonSF"
    assert state.repo_name == "mc-2"
    assert state.description == "Mission Control codebase"


def test_repo_row_to_state_canonic_kind() -> None:
    row = {
        "id": "x",
        "repo_name": "storyos",
        "status": "Active",
        "description": "The main repo for storyos",
        "owner": "Drew + Tony",
        "updated_at": None,
        "github_orgs": {"name": "Canonic-OS"},
        "companies": {"code": "CNC", "name": "Canonic", "kind": "self-canonic"},
    }
    state = _repo_row_to_state(row)
    assert state.company_kind == "self-canonic"
    assert state.github_org == "Canonic-OS"


def test_repo_row_to_state_holding_status() -> None:
    row = {
        "id": "x",
        "repo_name": "old-thing",
        "status": "Holding",
        "description": None,
        "owner": None,
        "updated_at": None,
        "github_orgs": {"name": "FirstPersonSF"},
        "companies": {"code": "1PI", "kind": "self-fpsf"},
    }
    state = _repo_row_to_state(row)
    assert state.status == "Holding"


# ──────────────────────────────────────────────────────────────────────
#  Repo validation guard
# ──────────────────────────────────────────────────────────────────────


def test_repo_row_is_valid_rejects_missing_repo_name() -> None:
    assert not _repo_row_is_valid({"repo_name": "", "status": "Active",
                                    "github_orgs": {"name": "X"}, "companies": {"code": "Y"}})
    assert not _repo_row_is_valid({"status": "Active",
                                    "github_orgs": {"name": "X"}, "companies": {"code": "Y"}})


def test_repo_row_is_valid_rejects_unknown_status() -> None:
    row = {
        "repo_name": "x",
        "status": "Floating",
        "github_orgs": {"name": "X"},
        "companies": {"code": "Y"},
    }
    assert not _repo_row_is_valid(row)


def test_repo_row_is_valid_accepts_all_repo_statuses() -> None:
    for status in ("Active", "Holding", "Inactive"):
        row = {
            "repo_name": "x",
            "status": status,
            "github_orgs": {"name": "X"},
            "companies": {"code": "Y"},
        }
        assert _repo_row_is_valid(row), status


def test_repo_row_is_valid_rejects_missing_org_or_company() -> None:
    """Defensive: even though SELECT uses inner joins, defend against empty embeds."""
    assert not _repo_row_is_valid({
        "repo_name": "x", "status": "Active",
        "github_orgs": None, "companies": {"code": "Y"},
    })
    assert not _repo_row_is_valid({
        "repo_name": "x", "status": "Active",
        "github_orgs": {"name": "X"}, "companies": None,
    })


# ──────────────────────────────────────────────────────────────────────
#  Numeric parsing (budget)
# ──────────────────────────────────────────────────────────────────────


def test_parse_numeric_handles_string_and_float() -> None:
    assert _parse_numeric("150000") == 150000.0
    assert _parse_numeric(150000.0) == 150000.0
    assert _parse_numeric(150000) == 150000.0


def test_parse_numeric_returns_none_for_falsy_or_unparseable() -> None:
    assert _parse_numeric(None) is None
    assert _parse_numeric("") is None
    assert _parse_numeric("not-a-number") is None


# ──────────────────────────────────────────────────────────────────────
#  Timestamp parsing
# ──────────────────────────────────────────────────────────────────────


def test_parse_iso_with_microseconds_and_offset() -> None:
    dt = _parse_iso("2026-05-07T16:14:34.123456+00:00")
    assert dt == datetime(2026, 5, 7, 16, 14, 34, 123456, tzinfo=timezone.utc)


def test_parse_iso_with_seconds_only() -> None:
    dt = _parse_iso("2026-05-07T16:14:34+00:00")
    assert dt is not None
    assert dt.tzinfo is timezone.utc


def test_parse_iso_returns_none_for_falsy() -> None:
    assert _parse_iso(None) is None
    assert _parse_iso("") is None


def test_parse_iso_assumes_utc_for_naive() -> None:
    dt = _parse_iso("2026-05-07T16:14:34")
    assert dt is not None
    assert dt.tzinfo is timezone.utc


# ──────────────────────────────────────────────────────────────────────
#  Supabase credential loading
# ──────────────────────────────────────────────────────────────────────


def _make_config(tmp_path: Path, mc2_clone: Path | None = None) -> TenantConfig:
    local_repos: dict[str, Path] = {}
    if mc2_clone is not None:
        local_repos["mc-2"] = mc2_clone
    return TenantConfig(
        name="test",
        display="Test",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"),
        projects=(),
        root=tmp_path,
        local_repos=MappingProxyType(local_repos),
    )


def test_load_supabase_creds_uses_environment_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://env.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "env-key")
    config = _make_config(tmp_path)
    assert _load_supabase_creds(config) == ("https://env.supabase.co", "env-key")


def test_load_supabase_creds_falls_back_to_mc2_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    clone = tmp_path / "mc-2"
    (clone / "backend").mkdir(parents=True)
    (clone / "backend" / ".env").write_text(
        '# leading comment\n'
        'SUPABASE_URL="https://file.supabase.co"\n'
        "SUPABASE_SERVICE_KEY=file-key\n"
        "OTHER_VAR=ignored\n"
    )
    config = _make_config(tmp_path, mc2_clone=clone)
    assert _load_supabase_creds(config) == ("https://file.supabase.co", "file-key")
    assert "Loaded SUPABASE_* from" in capsys.readouterr().err


def _write_local_toml(tmp_path: Path, body: str) -> None:
    (tmp_path / ".cp-engine.local.toml").write_text(body)


def test_load_supabase_creds_resolves_op_references(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `[supabase]` block of `op://` refs resolves via the `op` CLI, between
    the environment and the mc-2 dotenv fallback."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    _write_local_toml(
        tmp_path,
        "[supabase]\n"
        'url_ref = "op://cp-engine/MC-2 Supabase/url"\n'
        'service_key_ref = "op://cp-engine/MC-2 Supabase/service_key"\n',
    )
    resolved = {
        "op://cp-engine/MC-2 Supabase/url": "https://op.supabase.co",
        "op://cp-engine/MC-2 Supabase/service_key": "op-key",
    }
    monkeypatch.setattr(mc2_db, "_op_read", lambda ref: resolved[ref])

    config = _make_config(tmp_path)
    assert _load_supabase_creds(config) == ("https://op.supabase.co", "op-key")
    assert "1Password" in capsys.readouterr().err


def test_load_supabase_creds_env_wins_over_op_references(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Environment variables still take precedence; `op` is never invoked."""
    monkeypatch.setenv("SUPABASE_URL", "https://env.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "env-key")
    _write_local_toml(
        tmp_path,
        "[supabase]\n"
        'url_ref = "op://v/i/url"\n'
        'service_key_ref = "op://v/i/key"\n',
    )

    def _boom(ref):  # pragma: no cover - asserts op is not called
        raise AssertionError("op must not run when env is present")

    monkeypatch.setattr(mc2_db, "_op_read", _boom)
    config = _make_config(tmp_path)
    assert _load_supabase_creds(config) == ("https://env.supabase.co", "env-key")


def test_load_supabase_creds_op_failure_raises_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A configured ref that fails to resolve raises loudly — it never silently
    falls through to the dotenv path (the empty-result-masquerade failure mode)."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    _write_local_toml(
        tmp_path,
        "[supabase]\n"
        'url_ref = "op://v/i/url"\n'
        'service_key_ref = "op://v/i/key"\n',
    )

    def _boom(ref):
        raise RuntimeError("op not signed in")

    monkeypatch.setattr(mc2_db, "_op_read", _boom)
    config = _make_config(tmp_path)
    with pytest.raises(BackendUnavailable) as exc:
        _load_supabase_creds(config)
    msg = str(exc.value)
    assert "1Password" in msg and "op not signed in" in msg


def test_load_supabase_creds_partial_op_block_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Half a `[supabase]` block (only one ref) is a misconfig → raise, naming
    the missing key, rather than silently resolving one and dropping the other."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    _write_local_toml(
        tmp_path,
        "[supabase]\nurl_ref = \"op://v/i/url\"\n",  # missing service_key_ref
    )
    monkeypatch.setattr(mc2_db, "_op_read", lambda ref: "x")
    config = _make_config(tmp_path)
    with pytest.raises(BackendUnavailable) as exc:
        _load_supabase_creds(config)
    assert "service_key_ref" in str(exc.value)


def test_load_supabase_creds_raises_with_paths_when_neither_source_has_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    clone = tmp_path / "mc-2"
    (clone / "backend").mkdir(parents=True)
    config = _make_config(tmp_path, mc2_clone=clone)
    with pytest.raises(BackendUnavailable) as exc:
        _load_supabase_creds(config)
    msg = str(exc.value)
    assert "environment" in msg
    assert str(clone / "backend" / ".env") in msg


def test_load_supabase_creds_message_notes_missing_clone_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    config = _make_config(tmp_path)  # no mc-2 in local_repos
    with pytest.raises(BackendUnavailable) as exc:
        _load_supabase_creds(config)
    assert "no MC-2 clone configured" in str(exc.value)


# ──────────────────────────────────────────────────────────────────────
#  Linked repos (engagement → repos.project_id join)
# ──────────────────────────────────────────────────────────────────────


def test_parse_linked_repos_happy_path() -> None:
    payload = [
        {
            "repo_name": "ggl-5136-events-calendar",
            "status": "Active",
            "description": "Firebase calendar app for the EHS team",
            "github_orgs": {"name": "FirstPersonSF"},
        }
    ]
    repos = _parse_linked_repos(payload)
    assert len(repos) == 1
    assert repos[0].repo_name == "ggl-5136-events-calendar"
    assert repos[0].github_org == "FirstPersonSF"
    assert repos[0].status == "Active"
    assert repos[0].description == "Firebase calendar app for the EHS team"


def test_parse_linked_repos_filters_inactive() -> None:
    payload = [
        {"repo_name": "live", "status": "Active", "github_orgs": {"name": "Org"}},
        {"repo_name": "old", "status": "Inactive", "github_orgs": {"name": "Org"}},
    ]
    repos = _parse_linked_repos(payload)
    assert [r.repo_name for r in repos] == ["live"]


def test_parse_linked_repos_skips_rows_missing_org_or_name() -> None:
    payload = [
        {"repo_name": "good", "status": "Active", "github_orgs": {"name": "Org"}},
        {"repo_name": "", "status": "Active", "github_orgs": {"name": "Org"}},  # blank name
        {"repo_name": "no-org", "status": "Active", "github_orgs": {"name": ""}},  # blank org
        {"repo_name": "no-org-key", "status": "Active"},  # missing github_orgs
    ]
    repos = _parse_linked_repos(payload)
    assert [r.repo_name for r in repos] == ["good"]


def test_parse_linked_repos_sorts_by_name() -> None:
    payload = [
        {"repo_name": "zebra", "status": "Active", "github_orgs": {"name": "Org"}},
        {"repo_name": "apple", "status": "Active", "github_orgs": {"name": "Org"}},
        {"repo_name": "mango", "status": "Active", "github_orgs": {"name": "Org"}},
    ]
    repos = _parse_linked_repos(payload)
    assert [r.repo_name for r in repos] == ["apple", "mango", "zebra"]


def test_parse_linked_repos_empty_or_invalid_payload() -> None:
    assert _parse_linked_repos(None) == ()
    assert _parse_linked_repos([]) == ()
    assert _parse_linked_repos("not a list") == ()
    assert _parse_linked_repos([None, "garbage", 42]) == ()


def test_parse_linked_repos_defaults_status_to_active_when_missing() -> None:
    # MC-2 should always send status, but be defensive — assume Active rather than drop.
    payload = [{"repo_name": "x", "github_orgs": {"name": "Org"}}]
    repos = _parse_linked_repos(payload)
    assert len(repos) == 1
    assert repos[0].status == "Active"


def test_engagement_row_to_state_populates_linked_repos() -> None:
    row = {
        "number": 5136,
        "companies": {"code": "GGL", "name": "Google", "kind": "client"},
        "full_job_name": "GGL 5136 go/safety website",
        "name": "go/safety",
        "mc_status": "Open",
        "account_manager": "Brandon Grande",
        "is_internal": False,
        "deal_stage": "Won",
        "budget": "0",
        "updated_at": "2026-05-07T16:14:34+00:00",
        "repos": [
            {
                "repo_name": "ggl-5136-events-calendar",
                "status": "Active",
                "description": "Firebase calendar app",
                "github_orgs": {"name": "FirstPersonSF"},
            }
        ],
    }
    state = _engagement_row_to_state(row)
    assert state.code == "ggl-5136-go-safety-website"
    assert len(state.linked_repos) == 1
    assert state.linked_repos[0].repo_name == "ggl-5136-events-calendar"


def test_engagement_row_to_state_empty_linked_repos_when_no_join() -> None:
    row = {
        "number": 5188,
        "companies": {"code": "GGL", "name": "Google", "kind": "client"},
        "full_job_name": "GGL 5188 Calendar",
        "name": "Calendar",
        "mc_status": "Open",
        "is_internal": False,
        "updated_at": "2026-05-07T16:14:34+00:00",
        # no `repos` key — tests that absent join doesn't crash
    }
    state = _engagement_row_to_state(row)
    assert state.linked_repos == ()
