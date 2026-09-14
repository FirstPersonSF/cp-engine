# tests/test_resolver_parity.py — #243: the two resolvers must agree.
#
# There are two project-code resolvers in this repo, by design:
#
#   * `cp_engine.mc2_db._resolve_project_id` — the stdio/CLI/webhook path.
#   * `resolve_project_id` in `prototypes/hosted-mcp/server.py` — the hosted
#     path, which deliberately does NOT import cp_engine. Its Dockerfile COPYs
#     exactly `server.py` + `observability.py` and its requirements.txt does
#     not list cp-engine, so the container stays small and independently
#     deployable. That convention is load-bearing, not stylistic.
#
# The cost of that split is drift, which the hosted docstring itself warns
# about ("a hosted server needs one resolver all clients share, or every tool
# re-invents this"). It has already bitten twice:
#
#   #236 — the hosted resolver reached engagements only through the spine, so
#          a project with zero spine rows was invisible. sap-5198 (the tenant's
#          largest engagement) had 11 commitments cp-sources listed and
#          cp-hosted said did not exist.
#   #243 — PR #242 then taught the HOSTED resolver to accept a bare UUID while
#          the engine one still could not, so for a while the two servers
#          disagreed about what identifies a project.
#
# Unifying the code is a bigger change (a third dependency-light package, or
# pulling all of cp-engine into the container). Until that happens, this file
# is the cheap insurance: it drives BOTH resolvers with the same fake client
# over the same tenant and asserts identical answers. If someone teaches one a
# new trick, this fails until they teach the other.
import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("jwt")
pytest.importorskip("mcp")
pytest.importorskip("supabase")

from cp_engine.mc2_db import _resolve_project_id as engine_resolve  # noqa: E402

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "prototypes" / "hosted-mcp" / "server.py"
)


@pytest.fixture(scope="module")
def hosted_resolve():
    os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key-for-tests")
    spec = importlib.util.spec_from_file_location("hosted_mcp_server", _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.resolve_project_id


# ---------------------------------------------------------------- fake client


class _Query:
    """The subset of the postgrest builder both resolvers use."""

    def __init__(self, rows, table, log):
        self._rows, self._table, self._log = rows, table, log
        self._filters = []

    def select(self, _cols):
        return self

    def limit(self, _n):
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def ilike(self, col, val):
        self._filters.append(("ilike", col, val))
        return self

    def like(self, col, val):
        self._filters.append(("like", col, val))
        return self

    def _match(self, row):
        for op, col, val in self._filters:
            actual = row.get(col)
            if op == "eq":
                if actual != val:
                    return False
            else:  # ilike / like — only the trailing-% form is used
                pat, a = str(val), str(actual or "")
                if op == "ilike":
                    pat, a = pat.lower(), a.lower()
                if pat.endswith("%"):
                    if not a.startswith(pat[:-1]):
                        return False
                elif a != pat:
                    return False
        return True

    def execute(self):
        self._log.append((self._table, tuple(self._filters)))
        return type("R", (), {"data": [r for r in self._rows if self._match(r)]})()


class _FakeClient:
    def __init__(self, tables):
        self.tables, self.log = tables, []

    def table(self, name):
        return _Query(self.tables.get(name, []), name, self.log)


_SAP_ID = "11111111-2222-3333-4444-555555555555"
_GGL_ID = "4e39be45-6ee4-4fcb-bc66-36c4d290996c"
_INIT_ID = "99999999-8888-7777-6666-555555555555"


def _tenant(with_spine: bool = False):
    return {
        "projects": [
            {
                "id": _SAP_ID,
                "code": "SAP-2027-ad-videos",
                "full_job_name": "SAP 5198 2027 Ad Videos",
                "company_id": "co-sap",
                "number": 5198,
            },
            {
                "id": _GGL_ID,
                "code": "GGL-evacuation-video-refresh",
                "full_job_name": "GGL 5179 Evacuation Video Refresh",
                "company_id": "co-ggl",
                "number": 5179,
            },
        ],
        "companies": [
            {"id": "co-sap", "code": "SAP"},
            {"id": "co-ggl", "code": "GGL"},
        ],
        "initiatives": [{"id": _INIT_ID, "code": "storyos"}],
        "spine_substance": (
            [{"project_code": "sap-5198-2027-ad-videos", "project_id": _SAP_ID}]
            if with_spine
            else []
        ),
    }


# Every identifier shape a caller might hold, and what both resolvers owe.
_CASES = [
    ("dir-slug", "sap-5198-2027-ad-videos", _SAP_ID),
    ("short code", "sap-5198", _SAP_ID),
    ("raw MC-2 code", "SAP-2027-ad-videos", _SAP_ID),
    ("display name", "SAP 5198 2027 Ad Videos", _SAP_ID),
    ("project uuid", _SAP_ID, _SAP_ID),
    ("second project uuid", _GGL_ID, _GGL_ID),
    ("initiative code", "storyos", _INIT_ID),
    ("initiative uuid", _INIT_ID, _INIT_ID),
    ("unknown code", "nope-9999", None),
    ("unknown words", "not a project at all", None),
    ("uuid-ish but malformed", "1111-2222", None),
]


@pytest.mark.parametrize("label,code,expected", _CASES, ids=[c[0] for c in _CASES])
@pytest.mark.parametrize("with_spine", [False, True], ids=["no-spine", "with-spine"])
def test_both_resolvers_agree(hosted_resolve, label, code, expected, with_spine):
    """Same tenant, same code, same answer — from both resolvers.

    Parameterised over an empty and a populated spine because that is the axis
    #236 turned on: the hosted resolver used to answer correctly only when the
    spine happened to carry the row.
    """
    engine_answer = engine_resolve(_FakeClient(_tenant(with_spine)), code)
    hosted_answer = hosted_resolve(_FakeClient(_tenant(with_spine)), code)
    assert engine_answer == expected, f"engine resolver wrong for {label}"
    assert hosted_answer == expected, f"hosted resolver wrong for {label}"
    assert engine_answer == hosted_answer, (
        f"RESOLVER DRIFT on {label}: engine={engine_answer} hosted={hosted_answer}. "
        "The two resolvers must stay behaviourally identical — see #243."
    )


def test_neither_resolver_sends_a_malformed_uuid_to_the_db(hosted_resolve):
    """A uuid-ish string is treated as a code, never as an id filter.

    Filtering a uuid column with a malformed string ERRORS in Postgres rather
    than returning no rows, so the parse guard is load-bearing on both sides.
    """
    for resolve in (engine_resolve, hosted_resolve):
        client = _FakeClient(_tenant())
        assert resolve(client, "1111-2222") is None
        id_filters = [f for _, fs in client.log for f in fs if f[1] == "id"]
        assert id_filters == [], f"{resolve} filtered on id with a malformed uuid"


def test_uuid_short_circuits_the_company_prefix_scan(hosted_resolve):
    """A UUID must not fall through to the `ilike(code, '<hex>-%')` scan.

    Without the short-circuit, both resolvers split a uuid on its first `-` and
    scan for a company prefix that can never match — a wasted table scan on the
    way to failing.
    """
    unknown_uuid = "deadbeef-0000-1111-2222-333344445555"
    for resolve in (engine_resolve, hosted_resolve):
        client = _FakeClient(_tenant())
        assert resolve(client, unknown_uuid) is None
        scans = [
            (t, fs)
            for t, fs in client.log
            for op, col, _ in fs
            if op == "ilike" and col == "code"
        ]
        assert scans == [], f"{resolve} ran a company-prefix scan for a uuid"
