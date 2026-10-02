"""One source-title resolver, two modes — parity across every path.

Five paths turn a caller's source title into ONE `rag_assets` row:

  * `pick_source`            — attach (add/remove_element_source, add_spine_document)
  * `_resolve_source_asset`  — curation writes (rename, archive, set_status)
  * `fetch_source`           — fetch_project_source / compare_project_sources
  * `pull_document_comments` — pull_document_comments
  * `pull_source`            — the engine's chunk read (cxp spine, spine_recover)

They run here on ONE corpus and one set of keys. The table is the contract;
`OLD` records what each path did before the resolver was unified (kept so the
behaviour change is reviewable in one diff), `NEW` what it does now.

Outcome codes: an asset id (`U1`…) = resolved to that asset; `A1+A2` = the
chunks of several assets merged under one result (pull_source only);
`AMBIG` = refused with candidates; `NONE` = no match.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from cp_engine import project_sources as ps

P, C = "proj-1", "co-1"


def _u(n: int) -> str:
    return str(uuid.UUID(int=n))


_CORPUS = [
    # id, title, scope, status
    ("U1", "IBX 5192 Deck Review", "project", "active"),
    ("U2", "IBX 5192 Deck review", "project", "active"),
    ("U3", "Statement of Work v02", "project", "active"),
    ("U4", "Statement of Work v01", "project", "archived"),
    ("U5", "Brand Brief", "account", "active"),
    ("U6", "Weekly Sync", "project", "active"),
    ("U7", "Weekly Sync", "project", "active"),
    ("U8", "Kickoff Notes", "project", "archived"),
]
_ID = {name: _u(i + 1) for i, (name, *_r) in enumerate(_CORPUS)}
_NAME = {v: k for k, v in _ID.items()}


def _rows():
    out = []
    for name, title, scope, status in _CORPUS:
        out.append({
            "id": _ID[name], "title": title, "scope": scope, "status": status,
            "project_id": P if scope == "project" else None,
            "company_id": C if scope == "account" else None,
            "source_type": "docx", "created_at": "2026-09-01T00:00:00Z",
            "source_provider": "drive", "source_file_id": _ID[name],
            "source_path": None, "url": None, "prev_asset_id": None,
        })
    return out


class _Q:
    def __init__(self, client, table):
        self._c, self._t, self._f = client, table, []

    def select(self, *_a, **_k):
        return self

    def eq(self, k, v):
        self._f.append(lambda r, k=k, v=v: r.get(k) == v)
        return self

    def in_(self, k, vals):
        vals = list(vals)
        self._f.append(lambda r, k=k, vals=vals: r.get(k) in vals)
        return self

    def or_(self, expr):
        # owner_filter: "project_id.eq.<id>"
        col, _op, val = expr.split(".", 2)
        self._f.append(lambda r, col=col, val=val: r.get(col) == val)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def range(self, *_a, **_k):
        return self

    def execute(self):
        if self._t == "asset_chunks":
            rows = [{"asset_id": r["id"]} for r in self._c.assets]
        else:
            rows = self._c.assets
        return SimpleNamespace(data=[r for r in rows if all(f(r) for f in self._f)])


class _Client:
    def __init__(self):
        self.assets = _rows()

    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, params):
        assert name == "read_scoped_asset_chunks"
        rows = [
            {"text": r["id"], "title": r["title"], "scope": r["scope"],
             "citation_url": None, "chunk_index": 0, "page": None}
            for r in self.assets
            if r["status"] == "active" and (
                (r["scope"] == "project" and r["project_id"] == params["p_project_id"])
                or (r["scope"] == "account" and r["company_id"] == params["p_company_id"]))
        ]
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=rows))


def _code(row_or_id) -> str:
    return _NAME.get(row_or_id, row_or_id)


def run_pick_source(key, _tmp):
    pool = [r for r in _rows() if r["status"] == "active"]
    row, _note = ps.pick_source(pool, key)
    if row is not None:
        return _code(row["id"])
    return "AMBIG" if _note and _note.get("candidates") else "NONE"


def run_resolve_source_asset(key, _tmp):
    out = ps._resolve_source_asset(_Client(), P, key)
    if out is None:
        return "NONE"
    if "candidates" in out:
        return "AMBIG"
    return _code(out["id"])


def run_fetch_source(key, tmp, monkeypatch=None):
    out = ps.fetch_source(_Client(), P, key, tmp, company_id=C)
    if "error" in out:
        return "AMBIG" if out.get("candidates") else "NONE"
    return _code(out["source_file_id"])


def run_pull_document_comments(key, tmp):
    out = ps.pull_document_comments(_Client(), P, key, tmp, company_id=C) \
        if _comments_takes_company() else ps.pull_document_comments(_Client(), P, key, tmp)
    if "error" in out:
        return "AMBIG" if out.get("candidates") else "NONE"
    if "comments" not in out:
        return "AMBIG" if "ambiguous" in (out.get("note") or "") else "NONE"
    return _code(out["comments"][0]["file_id"])


def _comments_takes_company() -> bool:
    import inspect

    return "company_id" in inspect.signature(ps.pull_document_comments).parameters


def run_pull_source(key, _tmp):
    out = ps.pull_source(_Client(), P, C, key)
    if out.get("chunks"):
        return "+".join(sorted(_code(t) for t in out["chunks"]))
    if out.get("candidates") or "ambiguous" in (out.get("note") or ""):
        return "AMBIG"
    return "NONE"


PATHS = {
    "pick": run_pick_source,
    "curate": run_resolve_source_asset,
    "fetch": run_fetch_source,
    "comments": run_pull_document_comments,
    "pull": run_pull_source,
}

KEYS = [
    "IBX 5192 Deck Review",     # case-exact one of two case variants
    "ibx 5192 deck review",     # case-insensitive match of both, no case-exact
    "Deck",                     # substring of two distinct titles
    "Statement of Work",        # substring: active v02 + archived v01
    "Statement of Work v01",    # exact title of an ARCHIVED row only
    "brand brief",              # account-scoped doc, mis-cased
    "Weekly Sync",              # two assets share the exact title
    "Kickoff Notes",            # archived-only document
    "UUID:U3",                  # a rag_asset id
    "Nope",                     # nothing
    " Statement of Work v02 ",  # surrounding whitespace
    "work v02",                 # unique substring
]

# What each path did BEFORE unification (v0.131.2), recorded by running this
# file against that code.
OLD = {
    "pick": ["U1", "AMBIG", "AMBIG", "U3", "NONE", "U5", "AMBIG", "NONE", "U3", "NONE", "U3", "U3"],
    "curate": ["U1", "NONE", "NONE", "NONE", "NONE", "NONE", "AMBIG", "NONE", "U3", "NONE", "NONE", "NONE"],
    "fetch": ["U1", "U1", "U1", "U3", "U4", "U5", "U6", "U8", "NONE", "NONE", "NONE", "U3"],
    "comments": ["U1", "U1", "AMBIG", "AMBIG", "U4", "NONE", "U6", "U8", "NONE", "NONE", "NONE", "U3"],
    "pull": ["U1+U2", "U1+U2", "U1+U2", "U3", "NONE", "U5", "U6+U7", "NONE", "NONE", "NONE", "NONE", "U3"],
}

# After: ONE resolver (`resolve_source`), two modes. Reads (`fetch`,
# `comments`, `pull`, and the attach `pick`) use mode="lookup"; curation
# writes use mode="strict". Every read pool is active rows of the workstream
# plus its company's account-scoped rows.
NEW = {
    "pick": ["U1", "AMBIG", "AMBIG", "U3", "NONE", "U5", "AMBIG", "NONE", "U3", "NONE", "U3", "U3"],
    "curate": ["U1", "NONE", "NONE", "NONE", "NONE", "NONE", "AMBIG", "NONE", "U3", "NONE", "U3", "NONE"],
    "fetch": ["U1", "AMBIG", "AMBIG", "U3", "NONE", "U5", "AMBIG", "NONE", "U3", "NONE", "U3", "U3"],
    "comments": ["U1", "AMBIG", "AMBIG", "U3", "NONE", "U5", "AMBIG", "NONE", "U3", "NONE", "U3", "U3"],
    "pull": ["U1", "AMBIG", "AMBIG", "U3", "NONE", "U5", "U6+U7", "NONE", "NONE", "NONE", "U3", "U3"],
}


def _key(k: str) -> str:
    return _ID[k[5:]] if k.startswith("UUID:") else k


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, tmp_path):
    def fake_download(file_ref, dest_dir, *a, **k):
        p = Path(dest_dir) / "f.docx"
        p.write_bytes(b"PK")
        return p

    monkeypatch.setattr(ps, "download_file", fake_download, raising=False)
    monkeypatch.setattr(ps, "_drive_comments", lambda fid: [{"file_id": fid}], raising=False)


@pytest.mark.parametrize("path", list(PATHS))
def test_every_path_on_the_shared_corpus(path, tmp_path):
    got = [PATHS[path](_key(k), tmp_path) for k in KEYS]
    assert dict(zip(KEYS, got)) == dict(zip(KEYS, NEW[path]))


def test_reads_never_silently_pick_one_of_several_matches(tmp_path):
    """The invariant behind the table: for every read path, a key that
    matches several assets at its winning rung returns candidates."""
    for path in ("fetch", "comments", "pick"):
        assert PATHS[path]("Deck", tmp_path) == "AMBIG"
        assert PATHS[path]("ibx 5192 deck review", tmp_path) == "AMBIG"


def test_one_resolver_backs_every_path():
    import inspect

    src = inspect.getsource(ps)
    assert "def resolve_source(" in src
    for fn in (ps.fetch_source, ps.pull_document_comments, ps.pull_source,
               ps._resolve_source_asset, ps.pick_source):
        assert "resolve_source(" in inspect.getsource(fn), fn.__name__
