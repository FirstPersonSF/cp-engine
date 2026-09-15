"""The hosted `capture_session` verb and its identity contract (#247).

`prototypes/hosted-mcp/server.py` deliberately does not import cp_engine, so
these tests lift the pure pieces out by AST — the same convention as
test_hosted_wrap_bundle.py.

WHAT IS ACTUALLY AT RISK HERE, and why each test exists:

1. **The author must not be settable by this hop.** A session file names its
   author, and that name is the capture's whole provenance value. mc-2 derives
   it from the verified JWT; if this hop ever started sending a `user` field,
   the name would become a claim rather than a fact — and nothing downstream
   would notice, because a session file looks identical either way.

2. **The summary must never reach the audit table as prose.** The hosted audit
   sanitizer is allow-list based precisely so "a future tool that takes a new
   free-text param cannot silently start writing user content" into it.
   `summary` is exactly that param.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_HOSTED = (
    Path(__file__).resolve().parent.parent
    / "prototypes" / "hosted-mcp" / "server.py"
)


@pytest.fixture(scope="module")
def source() -> str:
    return _HOSTED.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tree(source: str):
    return ast.parse(source)


def _func(tree, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in the hosted server")


class TestTheVerbExists:
    def test_capture_session_is_a_registered_tool(self, tree):
        fn = _func(tree, "capture_session")
        decorators = [ast.unparse(d) for d in fn.decorator_list]
        assert any("mcp_server.tool" in d for d in decorators), decorators

    def test_it_takes_no_user_parameter(self, tree):
        """The caller cannot name the author — that is the point."""
        fn = _func(tree, "capture_session")
        args = [a.arg for a in fn.args.args]
        assert "user" not in args
        assert args[:2] == ["project_code", "summary"]


class TestIdentityIsNotSentByThisHop:
    def test_the_mc2_payload_omits_user(self, tree):
        """mc-2 derives the name from the token. If this hop ever sends one,
        the author becomes a claim instead of a fact."""
        fn = _func(tree, "call_mc2_capture_session")
        src = ast.unparse(fn)
        assert '"user"' not in src and "'user'" not in src

    def test_it_forwards_the_callers_own_token(self, tree):
        fn = _func(tree, "call_mc2_capture_session")
        src = ast.unparse(fn)
        assert "caller_jwt()" in src
        assert "Authorization" in src

    def test_when_is_only_sent_when_provided(self, tree):
        """An absent `when` must not become a null the upstream has to special-
        case — the webhook defaults it to now."""
        fn = _func(tree, "call_mc2_capture_session")
        src = ast.unparse(fn)
        assert "if when:" in src


class TestTheSummaryNeverLandsInTheAuditTable:
    def test_summary_is_in_the_redacted_set(self, source):
        line = next(
            l for l in source.splitlines()
            if l.startswith("_AUDIT_REDACTED_ARGS")
        )
        assert '"summary"' in line, line

    def test_summary_is_not_in_the_safe_set(self, source):
        """Safe args are recorded VERBATIM. A session narrative must never be."""
        start = source.index("_AUDIT_SAFE_ARGS = {")
        end = source.index("}", start)
        assert '"summary"' not in source[start:end]

    def test_the_audit_call_passes_summary_for_redaction(self, tree):
        """Passing it (rather than omitting it) is what produces `summary_len`
        — a useful fact — while the sanitizer strips the prose."""
        fn = _func(tree, "capture_session")
        src = ast.unparse(fn)
        assert "'summary': summary" in src or '"summary": summary' in src


class TestItRefusesAnEmptyCapture:
    def test_a_short_summary_is_rejected_before_the_network(self, tree):
        """An empty capture is worse than none: it advances the Last-session
        line while saying nothing, so it reads as a record that isn't there."""
        fn = _func(tree, "capture_session")
        src = ast.unparse(fn)
        assert "len(summary) < 20" in src
        # The guard must precede the call, or the refusal costs a round trip
        # and — worse — could still have written.
        assert src.index("len(summary) < 20") < src.index("call_mc2_capture_session")
