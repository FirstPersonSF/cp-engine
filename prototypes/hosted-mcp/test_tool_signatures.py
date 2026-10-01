"""`/cp-tools` signatures must be the servers' real ones, and a misnamed
argument must fail the call (#318).

WHAT BROKE. The catalog documented `create_spine_element(code, label, type,
body?, serves?)`; the hosted server takes `project_code, framing, body,
layer`. The MCP SDK validates arguments through a pydantic model whose default
is `extra="ignore"`, so the `type` a caller copied out of the catalog was
dropped without a word and the `layer` default applied: decisions filed into
layer Note, three times over five days. The validation error for the
REQUIRED names was the only documentation anyone got.

WHY THESE TESTS. A command file is prose the model EXECUTES — a stale
parameter name does not raise, it produces a plausible, wrong call. So:

1. Every signature in the catalog is parsed and checked against the tools
   the server actually REGISTERS (the hosted `mcp_server`, read through the
   SDK's own registry — the schema a client sees, not a hand-kept list). The
   stdio `cp-sources` server it was also checked against was retired in
   architecture plan step 5b.
2. Every hosted tool refuses an undeclared argument, through the same
   `call_tool` path a client's request takes.
3. The element-identifier aliases (`key` / `element_id`) work on the two
   verbs that disagreed, and refuse to pick between two different values.

    python -m pytest prototypes/hosted-mcp/test_tool_signatures.py -v
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

_CATALOG = Path(__file__).resolve().parents[2] / "plugin" / "commands" / "cp-tools.md"

# `name(args)` in backticks. Example CALLS (`pull_project_source("<code>", …)`,
# `set_spine_element(..., serves=[...])`) carry quotes, placeholders or an
# ellipsis and are not signatures.
_SIG = re.compile(r"`([a-z_]+)\(([^`]*)\)`", re.S)
_NOT_A_SIGNATURE = re.compile(r"[\"'<]|\.\.\.")


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    yield mod
    mp.undo()


def _registry(mcp) -> dict[str, tuple[set[str], set[str]]]:
    """{tool: (all params, required params)} from the SDK's registered schema."""
    out = {}
    for tool in mcp._tool_manager.list_tools():
        props = set((tool.parameters.get("properties") or {}).keys())
        out[tool.name] = (props, set(tool.parameters.get("required") or []))
    return out


@pytest.fixture(scope="module")
def registries(server) -> dict[str, dict[str, tuple[set[str], set[str]]]]:
    return {"cp-hosted": _registry(server.mcp_server)}


def _signatures() -> list[tuple[str, list[str]]]:
    text = _CATALOG.read_text(encoding="utf-8")
    sigs = []
    for name, raw in _SIG.findall(text):
        if _NOT_A_SIGNATURE.search(raw):
            continue
        params = []
        for part in raw.split(","):
            part = part.strip().split("=", 1)[0].strip()
            part = part.rstrip("?").replace("[]", "").strip()
            if part:
                params.append(part)
        sigs.append((name, params))
    return sigs


def _matches(params: list[str], tool: tuple[set[str], set[str]]) -> bool:
    """A signature describes a tool when every name it gives exists on the
    tool AND it gives every name the tool requires."""
    names, required = tool
    return set(params) <= names and required <= set(params)


def test_the_catalog_has_signatures_to_check():
    """Guard the parser: if it silently matched nothing, every test below
    would pass on an empty loop."""
    names = {n for n, _ in _signatures()}
    assert {"create_spine_element", "set_spine_element", "pull_spine_element",
            "create_commitment", "create_note"} <= names


def test_every_documented_verb_is_registered_somewhere(registries):
    """A verb that was renamed or never shipped cannot be documented."""
    known = set().union(*(set(r) for r in registries.values()))
    missing = sorted({n for n, _ in _signatures() if n not in known})
    assert not missing, f"/cp-tools documents verbs no server registers: {missing}"


def test_every_signature_matches_a_server_that_registers_it(registries):
    """The #318 check proper: every documented parameter name exists on the
    tool (and every required one is documented) on at least one server
    carrying that verb. `create_spine_element(code, label, type, ...)` fails
    here on `code`, `label` and `type`."""
    bad = []
    for name, params in _signatures():
        carriers = {s: r[name] for s, r in registries.items() if name in r}
        if carriers and not any(_matches(params, t) for t in carriers.values()):
            bad.append(
                f"{name}({', '.join(params)}) — "
                + "; ".join(f"{s} takes {sorted(t[0])}" for s, t in carriers.items())
            )
    assert not bad, "catalog signatures that match no server:\n  " + "\n  ".join(bad)


@pytest.mark.parametrize("srv", ["cp-hosted"])
def test_each_server_gets_a_correct_signature_for_its_verbs(registries, srv):
    """Every documented verb has at least one signature that works on the
    server — not only a retired server's form."""
    sigs = _signatures()
    documented = {n for n, _ in sigs}
    reg = registries[srv]
    bad = sorted(
        name for name in documented & set(reg)
        if not any(_matches(p, reg[name]) for n, p in sigs if n == name)
    )
    assert not bad, f"no catalog signature matches {srv}'s form of: {bad}"


# ── unknown arguments ─────────────────────────────────────────────────


def test_every_hosted_tool_advertises_that_it_takes_no_extra_arguments(server):
    """The schema is what a client reads before calling; without
    `additionalProperties: false` it cannot know a stray name will fail."""
    loose = [t.name for t in server.mcp_server._tool_manager.list_tools()
             if t.parameters.get("additionalProperties") is not False]
    assert not loose, f"tools whose schema allows undeclared arguments: {loose}"


def test_a_misnamed_argument_fails_the_call_instead_of_being_dropped(server, monkeypatch):
    """The exact #318 call, through the SDK's `call_tool` — the path a
    client's request takes, so this exercises the real validation, not the
    Python function. It must fail naming `type`, and the tool body must
    never run (a dropped arg that still wrote would be the old defect)."""
    ran = []
    monkeypatch.setattr(server, "user_client", lambda: ran.append(1))
    with pytest.raises(Exception) as exc:
        asyncio.run(server.mcp_server.call_tool("create_spine_element", {
            "project_code": "slt-5196", "framing": "Use the blue palette",
            "body": "Decided 09-21.", "type": "decision",
        }))
    assert "type" in str(exc.value) and "Extra inputs are not permitted" in str(exc.value)
    assert not ran


# ── element-identifier aliases ────────────────────────────────────────


class _Rows:
    """Just enough PostgREST for `pull_spine_element` to find one row."""

    def __init__(self, rows):
        self._rows = rows
        self._eq = []

    def table(self, _name):
        self._eq = []
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def execute(self):
        return SimpleNamespace(data=[
            r for r in self._rows if all(r.get(c) == v for c, v in self._eq)
        ])


@pytest.fixture
def one_element(server, monkeypatch):
    row = {"est_item_id": "_authored/brief", "status": "live", "body": "b",
           "project_code": "slt-5196-brand-campaign-26", "version_date": "2026-09-01"}
    monkeypatch.setattr(server, "user_client", lambda: _Rows([row]))
    monkeypatch.setattr(server, "caller_subject", lambda: "u")
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    return row


@pytest.mark.parametrize("arg", ["key", "element_id"])
def test_pull_spine_element_takes_either_name(server, one_element, arg):
    out = server.pull_spine_element(**{arg: "_authored/brief"})
    assert "error" not in out, out
    assert out["slug"] == "_authored/brief"


def test_two_different_identifiers_are_refused_not_picked(server, one_element):
    out = server.pull_spine_element(key="_authored/brief", element_id="_authored/other")
    assert "aliases" in out["error"]


def test_no_identifier_is_an_error(server, one_element):
    assert "element is required" in server.pull_spine_element()["error"]


def test_set_spine_element_takes_element_id(server, monkeypatch):
    """Resolution is the verb's own; this pins that the alias reaches it."""
    seen = []
    monkeypatch.setattr(server, "user_client", lambda: object())
    monkeypatch.setattr(server, "resolve_write_scope",
                        lambda c, code: {"id": "p", "kind": "project", "project_code": code})

    def fake_resolve(_client, _pid, key):
        seen.append(key)
        return None, [], None

    monkeypatch.setattr(server, "resolve_element_versions", fake_resolve)
    server.set_spine_element("slt-5196", element_id="_authored/brief", important=True)
    assert seen == ["_authored/brief"]


# ── add_spine_version takes `key` too ─────────────────────────────────


@pytest.fixture
def version_seen(server, monkeypatch):
    """Stops `add_spine_version` right after element resolution, recording
    the identifier that reached it."""
    seen = []
    monkeypatch.setattr(server, "user_client", lambda: object())
    monkeypatch.setattr(server, "caller_subject", lambda: "u")
    monkeypatch.setattr(server, "resolve_write_scope",
                        lambda c, code: {"id": "p", "kind": "project", "project_code": code})

    def fake_resolve(_client, _pid, key):
        seen.append(key)
        return None, [], None

    monkeypatch.setattr(server, "resolve_element_versions", fake_resolve)
    return seen


@pytest.mark.parametrize("arg", ["key", "element_id"])
def test_add_spine_version_takes_either_name(server, version_seen, arg):
    server.add_spine_version("slt-5196", body="v2 text", **{arg: "_authored/brief"})
    assert version_seen == ["_authored/brief"]


def test_add_spine_version_keeps_element_id_first_positionally(server, version_seen):
    """Existing positional callers — `(code, element_id, body)` — keep working."""
    server.add_spine_version("slt-5196", "_authored/brief", "v2 text")
    assert version_seen == ["_authored/brief"]


def test_add_spine_version_refuses_two_different_identifiers(server, version_seen):
    out = server.add_spine_version("slt-5196", element_id="_authored/a",
                                   body="v2", key="_authored/b")
    assert "aliases" in out["error"] and not version_seen


def test_add_spine_version_still_requires_a_body(server, version_seen):
    """`body` gained a default only so `key` could follow it; empty is refused."""
    assert server.add_spine_version("slt-5196", key="_authored/brief")["error"] == "body is required"
