"""The vendored `cp_engine` must not drift from the real one (#283).

WHY IT EXISTS. The container ships `server.py` and `observability.py` only, so
the four wrap-up verbs' call-time `from cp_engine.<module> import ...` raised
ModuleNotFoundError in production. They REGISTERED — `/health` reported 57
tools — and then every invocation failed. Three of the four verbs were dead on
arrival for the hosted teammates #280 built them for.

Vendoring fixes that and introduces the classic second problem: two copies of a
rule drift, which is the #172/#178 lesson the shared `run_all_lints` existed to
avoid in the first place. These tests make the copies provably identical to
their sources, so the fix cannot quietly become the next defect.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE / "cp_engine" if (_HERE / "cp_engine").is_dir() else _HERE / "vendor" / "cp_engine"
_SRC = _HERE.parents[1] / "src" / "cp_engine"

# Copied whole — byte-for-byte, no excerpting.
# Every file here that is a COPY rather than a shim. Derived from the tree in
# the fixture below rather than hand-listed: `card_class.py` and
# `exec_summary_lint.py` were copied verbatim and omitted from this tuple when
# it was hand-maintained, so they could have drifted from source silently —
# found while writing an audit brief, not by the test.
_VERBATIM = (
    "spine_lint.py",
    "commitments_sweep.py",
    "seal_sweep.py",
    "word_count_lint.py",
    "card_class.py",
    "exec_summary_lint.py",
)


@pytest.mark.parametrize("name", _VERBATIM)
def test_vendored_module_is_byte_identical_to_source(name: str) -> None:
    """A verbatim copy is the only kind that cannot drift silently.

    If a lint gains a check in `src/` and the copy does not, the hosted server
    reports a clean project the CLI would flag — and nothing says so.
    """
    vendored = (_VENDOR / name).read_text(encoding="utf-8")
    source = (_SRC / name).read_text(encoding="utf-8")
    assert vendored == source, (
        f"vendor/cp_engine/{name} has drifted from src/cp_engine/{name} — "
        "re-copy it; the hosted verbs and the CLI must run the same checks"
    )


def _class_source(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    return ast.get_source_segment(src, node)


def _func_source(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(src, node)


def test_vendored_tables_matches_the_real_class() -> None:
    """`Tables` is copied WHOLE rather than excerpted.

    A hand-picked subset is how two spellings of one table name start, and a
    query against the wrong name fails at runtime, not at import.
    """
    assert _class_source(_VENDOR / "mc2_db.py", "Tables") == _class_source(
        _SRC / "mc2_db.py", "Tables"
    ), "vendored Tables has drifted from cp_engine.mc2_db.Tables"


def test_vendored_ttl_clock_matches_the_real_one() -> None:
    """THE POLICY NUMBER. An undated commitment expires at 14 days.

    Two spellings of that would mean the CLI and the hosted server disagree
    about when someone's work lapses — a wrong answer a human acts on, not a
    crash anyone would notice.
    """
    assert _func_source(_VENDOR / "dates_loop.py", "_ttl_bucket") == _func_source(
        _SRC / "dates_loop.py", "_ttl_bucket"
    ), "vendored _ttl_bucket has drifted"

    import re

    def _const(path: Path, name: str) -> str:
        m = re.search(rf"^{name} = .*$", path.read_text(encoding="utf-8"), re.M)
        assert m, f"{name} not found in {path.name}"
        return m.group(0)

    for const in ("_EXPIRE_AFTER_DAYS", "_EXPIRE_WARN_AFTER_DAYS"):
        assert _const(_VENDOR / "dates_loop.py", const) == _const(
            _SRC / "dates_loop.py", const
        ), f"vendored {const} has drifted"


def test_the_vendor_closure_EXECUTES_with_no_cp_engine_installed(tmp_path) -> None:
    """THE CONTROL THAT ACTUALLY CATCHES #283.

    An earlier version of this test resolved import NAMES against the vendor
    tree and passed while production was still broken — because the real
    breakage was NESTED imports: `run_all_lints` imports
    `cp_engine.project_sources` inside the function body, and a
    name-resolution check never runs the body.

    So this RUNS the code, in a subprocess whose `sys.path` carries the vendor
    tree and nothing else. A function-level import of a module nobody vendored
    fails here, the way it failed in the container.
    """
    import subprocess
    import sys

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(_VENDOR.parent)!r})\n"
        "from cp_engine.spine_lint import lint_spine_rows, lint_curation\n"
        "from cp_engine.word_count_lint import contributors\n"
        "from cp_engine.seal_sweep import build_rounds\n"
        "from cp_engine.exec_summary_lint import lint_exec_summary\n"
        "import cp_engine.project_sources as PS\n"
        "import cp_engine.commitments as C\n"
        "rows = [{'est_item_id': '_authored/x', 'framing': 'X', 'status': 'live',\n"
        "  'layer': 'Brief', 'binding': 'unbound', 'important': True,\n"
        "  'version_label': 'v1', 'project_id': 'p1', 'version_date': '2026-09-01',\n"
        "  'note': '', 'serves': None, 'placement': None, 'sources': [],\n"
        "  'origin': None, 'scope': None, 'company_id': None, 'phase': None}]\n"
        # A fake PostgREST client: enough chaining to let `run_all_lints`
        # execute its REAL query construction. The AttributeError that shipped
        # after the import fix — `mc2_db.SPINE_LINT_COLUMNS` missing from the
        # shim — lives in that construction, and no import-level check reaches
        # it. Constants are read as the query is built, not as it is sent.
        "class _Q:\n"
        "    def __init__(s, rows): s._rows = rows\n"
        "    def select(s, *a, **k): return s\n"
        "    def eq(s, *a, **k): return s\n"
        "    def in_(s, *a, **k): return s\n"
        "    def order(s, *a, **k): return s\n"
        "    def limit(s, *a, **k): return s\n"
        "    def execute(s): return type('R', (), {'data': s._rows})()\n"
        "class _C:\n"
        "    def __init__(s, rows): s._rows = rows\n"
        "    def table(s, name): return _Q(s._rows)\n"
        "    def schema(s, name): return s\n"
        "from cp_engine.spine_lint import run_all_lints\n"
        "run_all_lints(_C(rows), ['x'], cp_md_text='## Exec Summary\\n')\n"
        "list(lint_spine_rows(rows))\n"
        "list(lint_curation(rows, today=None))\n"
        "PS._one_live_per_element(rows)\n"
        "contributors('word ' * 2600)\n"
        "lint_exec_summary('<!-- cp-engine:start exec-summary -->\\n"
        "**Status:** x\\n<!-- cp-engine:end exec-summary -->')\n"
        "build_rounds(rows, [])\n"
        "C.resolve_commitment_owner\n"
        "print('VENDOR_OK')\n",
        encoding="utf-8",
    )

    r = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),  # nowhere near the repo, so no src/ sneaks onto the path
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
    )
    assert "VENDOR_OK" in r.stdout, (
        "the vendored closure does not execute standalone — the container will "
        "raise at CALL time while the tool still registers:\n" + r.stderr[-2000:]
    )


def test_the_dockerfile_actually_ships_the_vendor_tree() -> None:
    """Vendoring that never reaches the image is the bug with extra steps."""
    dockerfile = (_HERE / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY vendor" in dockerfile, (
        "the Dockerfile does not COPY vendor — the container will have no "
        "cp_engine and the wrap-up verbs will raise at call time"
    )


def test_every_mc2_db_constant_is_vendored_not_just_the_ones_in_use() -> None:
    """Picking constants one at a time is how this shipped broken twice.

    The first shim carried `Tables` and nothing else; `run_all_lints` reads
    `mc2_db.SPINE_LINT_COLUMNS` while BUILDING its query, so it passed every
    import check and raised AttributeError on the first real call — one deploy
    after the import fix.

    These column lists are also the tenant's NEVER-`SELECT *` rule in code. A
    constant missing here becomes a wrong query, not an obvious crash.
    """
    src_consts = {
        m.group(1)
        for m in __import__("re").finditer(
            r"^([A-Z][A-Z_]*) = ", (_SRC / "mc2_db.py").read_text(encoding="utf-8"), __import__("re").M
        )
    }
    ven_consts = {
        m.group(1)
        for m in __import__("re").finditer(
            r"^([A-Z][A-Z_]*) = ", (_VENDOR / "mc2_db.py").read_text(encoding="utf-8"), __import__("re").M
        )
    }
    missing = src_consts - ven_consts
    assert not missing, (
        f"mc2_db constants not vendored: {sorted(missing)} — a verb reading one "
        "raises AttributeError at call time while the tool still registers"
    )

    # PRESENCE IS NOT ENOUGH — the VALUES must match too. #284 dropped
    # `is_default` from `EST_PROJECT_COLUMNS`; the vendored copy kept the old
    # spelling and this test passed, because the name was still there. A stale
    # column list does not raise: it sends a query selecting a column that no
    # longer exists, which is a 42703 the caller swallows.
    import re as _re

    def _consts(path):
        out = {}
        for m in _re.finditer(
            r"^([A-Z][A-Z_]*) = (.+?)$", path.read_text(encoding="utf-8"), _re.M
        ):
            out[m.group(1)] = m.group(2).strip()
        return out

    src_vals, ven_vals = _consts(_SRC / "mc2_db.py"), _consts(_VENDOR / "mc2_db.py")
    drifted = {
        k: (src_vals[k], ven_vals[k])
        for k in src_vals.keys() & ven_vals.keys()
        if src_vals[k] != ven_vals[k]
    }
    assert not drifted, f"vendored mc2_db constants have drifted in VALUE: {drifted}"


def test_every_verbatim_copy_is_actually_checked() -> None:
    """THE LIST CANNOT BE HAND-MAINTAINED.

    `_VERBATIM` was written by hand and omitted two files — `card_class.py`
    and `exec_summary_lint.py` are byte copies of their sources and nothing
    compared them, so either could have drifted silently. Found while writing
    an audit brief, which is a worse way to find it than a test.

    A vendored file is a COPY if it carries no VENDORED header; every copy
    must appear in `_VERBATIM`.
    """
    copies = set()
    for path in sorted(_VENDOR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        head = path.read_text(encoding="utf-8")[:200]
        if "VENDORED" not in head:
            copies.add(path.name)

    unchecked = copies - set(_VERBATIM)
    assert not unchecked, (
        f"{sorted(unchecked)} are verbatim copies that no drift test compares "
        "against source — they can diverge silently, which is the exact "
        "failure vendoring introduces"
    )
