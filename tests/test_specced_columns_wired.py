"""A specced column must have a WRITER and a READER in the code (#315).

Three pieces of the spine model were specced, migrated, and structurally unable
to run, and nothing noticed for weeks:

  * `card_kind` (mig 140) — read by `card_class.classify`, written by nothing
    until #246, and never by the hosted create path until #315.
  * `actor` (mig 126) — written by hosted `set_spine_element`, read by nothing:
    every appearance was a PASSTHROUGH (`"actor": row.get("actor")`) echoing
    the column into a listing. Echoing is transport, not consumption.
  * the Lens status term — its inputs (`stage`, `depends_on`) have no column on
    the substance path at all, so it was the constant 1.0.

A migration adds a column in mc-2; a writer and a reader land (or don't) here.
Nothing tied the two together, so this does: every column below must have at
least one WRITE site and at least one non-passthrough READ site in the code
that runs — `src/cp_engine` and the hosted server. Tests, the vendored copies
and docs do not count; a column that only a test reads is still inert.

WHAT COUNTS (AST, not grep — a grep for "actor" matches the webhook's unrelated
commit-trailer `actor` and would have passed):

    write  a dict literal with the column as a key, or `x["col"] = ...`
           (excluding the passthrough `"col": y.get("col")`)
    read   `x.get("col")` / `x["col"]` in load position, or `obj.col`
           (excluding the same passthrough)

ADDING A COLUMN. A new `spine_substance` column the engine is meant to own goes
in `ENGINE_OWNED` with the migration that added it. A column another repo owns
end to end goes in `OWNED_ELSEWHERE` with WHERE its writer and reader live —
the exemption has to name them, or it is just a place to hide an inert column.

    python -m pytest tests/test_specced_columns_wired.py -v
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

# spine_substance columns the engine owns — each needs a writer AND a reader
# here. `why` is the spec that made it load-bearing.
ENGINE_OWNED: dict[str, str] = {
    "card_kind": "mig 140/185 — card_class.classify; route_queue; weekly_sort",
    "actor": "mig 126 — spec v04 §1 authority precedence",
    "lifetime": "mig 140 — weekly_sort / route_propose",
    "placement": "mig 070 — structural item/context split",
    "origin": "mig 074 — write-path provenance",
    "important": "element-level importance flag",
    "archived": "soft-retire flag",
    "scope": "#198 account-scoped elements (pairs with company_id)",
    "company_id": "#198 — travels with scope",
    "field_states": "#180 confirmed-mark",
    "confirmed_by": "#180 confirmed-mark",
    "confirmed_at": "#180 confirmed-mark",
    "review_flags": "sweep drift flags",
}

# Columns another repository writes AND reads. Named, not waved through.
OWNED_ELSEWHERE: dict[str, str] = {
    "agreement_id": (
        "mc-2 mig 159 — written by mc-2 backend/src/routers/agreements.py, "
        "read by backend/src/lib/spine_outline.py + SowAgreementPanel.tsx"
    ),
}


def _sources() -> list[Path]:
    files = sorted((_ROOT / "src" / "cp_engine").rglob("*.py"))
    files.append(_ROOT / "prototypes" / "hosted-mcp" / "server.py")
    return [f for f in files if f.exists()]


def _const_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _read_key(node: ast.AST) -> str | None:
    """The column a `x.get("c")` / `x["c"]` expression reads, else None."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and node.args):
        return _const_key(node.args[0])
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        return _const_key(node.slice)
    return None


def scan(
    columns: set[str], files: list[Path], root: Path = _ROOT
) -> dict[str, dict[str, list[str]]]:
    """``{column: {"write": [site…], "read": [site…]}}`` over ``files``."""
    out = {c: {"write": [], "read": []} for c in columns}
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        rel = f.relative_to(root)
        passthrough: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    col = _const_key(k) if k is not None else None
                    if col not in out:
                        continue
                    if _read_key(v) == col:
                        passthrough.add(id(v))
                        continue
                    out[col]["write"].append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Subscript) and isinstance(t.ctx, ast.Store):
                        col = _const_key(t.slice)
                        if col not in out:
                            continue
                        if _read_key(node.value) == col:
                            passthrough.add(id(node.value))
                            continue
                        out[col]["write"].append(f"{rel}:{node.lineno}")
        for node in ast.walk(tree):
            if id(node) in passthrough:
                continue
            col = _read_key(node)
            if col in out:
                out[col]["read"].append(f"{rel}:{node.lineno}")
            elif (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                  and node.attr in out and node.attr != "get"):
                out[node.attr]["read"].append(f"{rel}:{node.lineno}")
    return out


@pytest.fixture(scope="module")
def sites():
    return scan(set(ENGINE_OWNED), _sources())


@pytest.mark.parametrize("column", sorted(ENGINE_OWNED))
def test_specced_column_has_a_writer(column, sites):
    assert sites[column]["write"], (
        f"spine_substance.{column} ({ENGINE_OWNED[column]}) has no writer in "
        "src/cp_engine or the hosted server — every row carries the default, "
        "and whatever reads it is reading a constant."
    )


@pytest.mark.parametrize("column", sorted(ENGINE_OWNED))
def test_specced_column_has_a_reader(column, sites):
    assert sites[column]["read"], (
        f"spine_substance.{column} ({ENGINE_OWNED[column]}) has no reader in "
        "src/cp_engine or the hosted server beyond passthrough echoes — it is "
        "written and never consulted. Wire a consumer, or remove it from the spec."
    )


def test_the_two_lists_do_not_overlap():
    assert not set(ENGINE_OWNED) & set(OWNED_ELSEWHERE)


def test_an_exempt_column_is_really_absent_here():
    """An exemption is for a column this repo does not touch. If cp-engine
    starts writing or reading it, it is engine-owned and must move up."""
    touched = {
        c: s for c, s in scan(set(OWNED_ELSEWHERE), _sources()).items()
        if s["write"] or s["read"]
    }
    assert not touched, f"move to ENGINE_OWNED: {touched}"


# ── the scanner must be able to FAIL (control) ────────────────────────────


def test_scanner_treats_a_passthrough_echo_as_neither(tmp_path):
    """The pre-#315 shape of `actor`: written by a patch, echoed by listings,
    consulted by nothing. The scanner must report no reader for it."""
    f = tmp_path / "m.py"
    f.write_text(
        "def listing(r):\n"
        "    return {'actor': r.get('actor')}\n"
        "def pull(row, out):\n"
        "    out['actor'] = row.get('actor')\n"
        "def set_it(patch, actor):\n"
        "    patch['actor'] = actor.strip()\n",
        encoding="utf-8",
    )
    got = scan({"actor"}, [f], tmp_path)["actor"]
    assert got["write"] and not got["read"], got


def test_scanner_sees_a_real_read(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(
        "def decide(r):\n"
        "    return r.get('actor') == 'partner'\n",
        encoding="utf-8",
    )
    assert scan({"actor"}, [f], tmp_path)["actor"]["read"]
