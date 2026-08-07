"""Dual-read-surface parity fence (cp-engine #138, option 3).

The #138 ratchet gave every WRITE exactly one home (the hosted server).
Six READ verbs deliberately remain on BOTH servers — stdio for
headless/cron runs where interactively-authenticated connectors are
absent, hosted for identity-carrying sessions — and dual surfaces drift
silently (the `tier` facet had to be written twice on 2026-08-07; the
smoke test once missed three shipped verbs for a day).

This fence converts silent drift into a loud error. It AST-parses both
servers (no imports — the hosted module wants env at call time) and
compares every verb that exists on both against the checked-in contract
below. Changing a dual verb's signature on either side now REQUIRES
editing this file in the same commit — divergence becomes an explicit,
reviewed act, never an accident.

The contract records today's KNOWN, intentional divergence (different
resolution models predate the fence: stdio pulls by (code, title/key),
hosted by direct id). New divergence should be justified in the updated
entry's comment — and if the surfaces keep growing, the real fix is
extracting shared read cores into 1p-component-library (the
spine-authoring / note-dm-format precedent), at which point this fence
becomes a tripwire for forgetting to use them.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_STDIO = _ROOT / "src/cp_engine/mcp_server.py"
_HOSTED = _ROOT / "prototypes/hosted-mcp/server.py"

# verb -> (stdio params, hosted params). Order matters (positional callers).
DUAL_READ_CONTRACT: dict[str, tuple[list[str], list[str]]] = {
    "list_commitments": (
        ["project_code", "status"],
        ["project_code", "status"],  # backfilled to parity when the fence landed
    ),
    "list_project_meetings": (
        ["project_code"],
        ["project_code"],
    ),
    "list_project_sources": (
        ["project_code"],
        ["project_code"],
    ),
    "list_spine_elements": (
        # stdio: orientation filters; hosted: lifecycle awareness. `tier` is
        # the one facet deliberately mirrored on both (#158 gap 5).
        ["project_code", "layer", "scope", "binding", "compact", "tier"],
        ["project_code", "include_absorbed", "tier"],
    ),
    "pull_project_source": (
        # Divergent by design history: stdio resolves by title within a code
        # (+ relevance query); hosted takes the asset id from its own listing.
        ["project_code", "doc_title", "query"],
        ["asset_id", "max_chars"],
    ),
    "pull_spine_element": (
        # stdio: key = est_item_id or framing substring; hosted: direct id
        # with optional code scope.
        ["project_code", "key"],
        ["element_id", "project_code"],
    ),
}


def _tool_params(path: Path, decorator_markers: tuple[str, ...]) -> dict[str, list[str]]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        decos = [ast.get_source_segment(src, d) or "" for d in node.decorator_list]
        if not any(m in d for m in decorator_markers for d in decos):
            continue
        a = node.args
        out[node.name] = [p.arg for p in a.posonlyargs + a.args + a.kwonlyargs]
    return out


def test_dual_read_surface_matches_contract() -> None:
    stdio = _tool_params(_STDIO, ("_tool",))
    hosted = _tool_params(_HOSTED, ("mcp_server.tool",))
    dual = sorted(set(stdio) & set(hosted))

    assert dual == sorted(DUAL_READ_CONTRACT), (
        "The set of verbs existing on BOTH servers changed. A verb should "
        "exist twice only when it is a read the headless/stdio path needs — "
        f"update DUAL_READ_CONTRACT deliberately.\n  now dual: {dual}\n"
        f"  contract: {sorted(DUAL_READ_CONTRACT)}"
    )

    for verb, (want_stdio, want_hosted) in DUAL_READ_CONTRACT.items():
        assert stdio[verb] == want_stdio, (
            f"stdio {verb} signature drifted from the parity contract.\n"
            f"  contract: {want_stdio}\n  actual:   {stdio[verb]}\n"
            "If intentional, mirror-or-justify on hosted and update "
            "DUAL_READ_CONTRACT in this commit."
        )
        assert hosted[verb] == want_hosted, (
            f"hosted {verb} signature drifted from the parity contract.\n"
            f"  contract: {want_hosted}\n  actual:   {hosted[verb]}\n"
            "If intentional, mirror-or-justify on stdio and update "
            "DUAL_READ_CONTRACT in this commit."
        )


def test_no_write_verb_exists_twice() -> None:
    """The #143/#138 ratchet invariant, mechanized: every dual verb must be
    in the read contract — a write appearing on both servers is the exact
    regression the ratchet exists to prevent."""
    stdio = _tool_params(_STDIO, ("_tool",))
    hosted = _tool_params(_HOSTED, ("mcp_server.tool",))
    unexpected = sorted((set(stdio) & set(hosted)) - set(DUAL_READ_CONTRACT))
    assert not unexpected, (
        f"verbs now exist on BOTH servers outside the read contract: "
        f"{unexpected} — writes live on hosted ONLY."
    )
