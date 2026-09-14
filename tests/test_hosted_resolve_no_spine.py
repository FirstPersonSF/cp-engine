# tests/test_hosted_resolve_no_spine.py — issue #236: a project with ZERO
# spine rows must still resolve on cp-hosted.
#
# `resolve_project_id` resolved an engagement only through
# `spine_substance.project_code` (branches 1 and 3 cover initiatives and the raw
# MC-2 code, neither of which applies). So a project with no spine rows was
# invisible to every hosted verb — and it failed in the worst direction: a
# MATURE project has spine rows and resolves, while a NEW project has none, and
# a new project is exactly where the unsettled commitments and the first spine
# card live.
#
# Two live instances. `sap-5198` (SAP 5198 2027 Ad Videos, the tenant's largest
# engagement at $425k) had 11 open commitments that `cp-sources` listed happily
# and `cp-hosted` claimed did not exist. `ggl-5179` (a deal held for a Nov/Dec
# pitch) could not receive its first spine card at all — and the raw MC-2 UUID
# did not resolve either, which is what prompted the UUID branch.
#
# Same harness as the other hosted tests: load the prototype by path and drive
# the resolver with a fake client that records which tables were hit.
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


# ---------------------------------------------------------------- fake client


class _Query:
    """Records filters and returns whatever the table's rows match.

    Only the subset of the postgrest builder the resolver actually uses:
    select / eq / ilike / like / limit / execute.
    """

    def __init__(self, rows, table, log):
        self._rows = rows
        self._table = table
        self._log = log
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
            elif op == "ilike":
                pat = str(val).lower()
                a = str(actual or "").lower()
                if pat.endswith("%"):
                    if not a.startswith(pat[:-1]):
                        return False
                elif a != pat:
                    return False
            elif op == "like":
                pat = str(val)
                if pat.endswith("%"):
                    if not str(actual or "").startswith(pat[:-1]):
                        return False
                elif str(actual or "") != pat:
                    return False
        return True

    def execute(self):
        self._log.append((self._table, tuple(self._filters)))
        return type("R", (), {"data": [r for r in self._rows if self._match(r)]})()


class _FakeClient:
    def __init__(self, tables):
        self.tables = tables
        self.log = []

    def table(self, name):
        return _Query(self.tables.get(name, []), name, self.log)


# `sap-5198`: real row, real display name, code that is NEITHER the dir-slug
# NOR the short form — and ZERO spine rows. This is the live shape from #236.
_SAP_ID = "11111111-2222-3333-4444-555555555555"
_GGL_ID = "4e39be45-6ee4-4fcb-bc66-36c4d290996c"


def _tenant():
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
        # The whole point: no spine rows anywhere.
        "spine_substance": [],
        "initiatives": [],
    }


# ------------------------------------------------------------------- the tests


def test_resolves_by_dir_slug_with_zero_spine_rows(srv):
    """The canonical on-disk id resolves without any spine row to lean on."""
    client = _FakeClient(_tenant())
    assert srv.resolve_project_id(client, "sap-5198-2027-ad-videos") == _SAP_ID


def test_resolves_by_short_code_with_zero_spine_rows(srv):
    """`<prefix>-<number>` resolves via the companies/number join.

    The prefix-match on `spine_substance` used to be the only thing covering
    this shape, so it died with an empty spine.
    """
    client = _FakeClient(_tenant())
    assert srv.resolve_project_id(client, "sap-5198") == _SAP_ID


def test_resolves_by_raw_uuid(srv):
    """A bare `projects.id` resolves — the identifier `cp.md`'s `MC-id:` carries.

    Tony's addition on #236: the UUID is the one string that can never be
    ambiguous across the three naming forms, and it did not resolve either.
    """
    client = _FakeClient(_tenant())
    assert srv.resolve_project_id(client, _GGL_ID) == _GGL_ID


def test_resolves_by_display_name(srv):
    """The raw `full_job_name`, which is what Fathom stores in project_tags."""
    client = _FakeClient(_tenant())
    assert srv.resolve_project_id(client, "GGL 5179 Evacuation Video Refresh") == _GGL_ID


def test_spine_remains_the_fast_path(srv):
    """A project WITH spine rows still resolves off the spine, before `projects`.

    The fix must not turn one indexed lookup into a company-prefix scan for the
    common case.
    """
    tables = _tenant()
    tables["spine_substance"] = [
        {"project_code": "sap-5198-2027-ad-videos", "project_id": _SAP_ID}
    ]
    client = _FakeClient(tables)
    assert srv.resolve_project_id(client, "sap-5198-2027-ad-videos") == _SAP_ID
    hit_tables = [t for t, _ in client.log]
    assert "spine_substance" in hit_tables
    # `projects` is never consulted when the spine answers.
    assert "projects" not in hit_tables


def test_unknown_code_still_returns_none(srv):
    """A genuinely absent project resolves to None rather than raising."""
    client = _FakeClient(_tenant())
    assert srv.resolve_project_id(client, "nope-9999") is None
    assert srv.resolve_project_id(client, "not-a-code-at-all") is None


def test_malformed_uuid_never_reaches_the_db_as_a_uuid_filter(srv):
    """A code that merely looks uuid-ish is treated as a code, not an id.

    Filtering a uuid column with a malformed string errors in Postgres rather
    than missing, so the parse guard matters.
    """
    client = _FakeClient(_tenant())
    assert srv.resolve_project_id(client, "1111-2222") is None
    id_filters = [
        f for _, filters in client.log for f in filters if f[1] == "id"
    ]
    assert id_filters == []


def test_initiative_still_resolves_first(srv):
    """Initiatives keep their branch — unchanged by the engagement fix."""
    tables = _tenant()
    tables["initiatives"] = [{"id": "init-1", "code": "storyos"}]
    client = _FakeClient(tables)
    assert srv.resolve_project_id(client, "storyos") == "init-1"
