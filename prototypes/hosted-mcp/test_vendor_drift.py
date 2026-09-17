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
_VERBATIM = ("spine_lint.py", "commitments_sweep.py", "seal_sweep.py", "word_count_lint.py")


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
