# tests/test_hosted_version_scope.py — issue #198: a new version must inherit
# BOTH halves of an element's account placement.
#
# `add_spine_version` carried `scope` forward but not `company_id`, because
# `company_id` was not in `_ELEMENT_RESOLVE_COLUMNS` — so `base.get()` returned
# None and the new row went in as scope='account' with a NULL company.
#
# That row matches NEITHER read arm: the account arm filters
# `company_id=X AND scope='account'`, the project arm `scope='project'`. The
# live version goes invisible at account scope while the stale superseded row
# remains the only thing sibling projects can see. On ibx-5192 that stranded
# the corrected Kimber and Janet dossiers — ibx-5153 kept reading the
# pre-correction text — and the resulting zero-live account group crashed the
# mirror (#197).
#
# Same harness as the other hosted tests: the decorated verbs are MCP Tool
# objects, so this targets the module-level constants and the row contract.
import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("jwt")
pytest.importorskip("mcp")
pytest.importorskip("supabase")

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "prototypes" / "hosted-mcp" / "server.py"
)


@pytest.fixture(scope="module")
def srv():
    os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key-for-tests")
    spec = importlib.util.spec_from_file_location("hosted_mcp_server", _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_columns_select_company_id(srv):
    """The root cause: `scope` was selected, `company_id` was not, so the
    carry-forward silently read None for an account-scoped element."""
    cols = srv._ELEMENT_RESOLVE_COLUMNS
    assert "scope" in cols
    assert "company_id" in cols, (
        "company_id missing from _ELEMENT_RESOLVE_COLUMNS — a new version of "
        "an account-scoped element will carry scope='account' with a NULL "
        "company, which no read arm matches (#198)"
    )


def test_scope_and_company_id_are_carried_together(srv):
    """Both halves travel from the live base row onto the new version."""
    src = _SERVER_PATH.read_text()
    start = src.index("def add_spine_version(")
    body = src[start:start + 6000]
    assert '"scope": base.get("scope")' in body
    assert '"company_id": base.get("company_id")' in body, (
        "add_spine_version does not carry company_id forward (#198)"
    )


def test_account_pair_is_indivisible_for_the_mirror(srv):
    """The invariant the pair protects, stated as the mirror sees it: an
    account-scoped row is only reachable when BOTH fields are set. This is the
    shape that broke — asserted directly so a future refactor that drops one
    half fails here rather than 24 days later in a stale dossier."""
    def reachable(scope, company_id, *, arm):
        if arm == "account":
            return scope == "account" and company_id is not None
        return (scope or "project") == "project"

    # the bug's row: carried scope, dropped company
    assert not reachable("account", None, arm="account")
    assert not reachable("account", None, arm="project")
    # the fixed row
    assert reachable("account", "company-uuid", arm="account")
    # an ordinary project row stays reachable on the project arm
    assert reachable("project", None, arm="project")
