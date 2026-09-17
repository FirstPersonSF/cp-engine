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


def test_the_vendor_tree_covers_every_cp_engine_import_the_server_makes() -> None:
    """THE CONTROL THAT WOULD HAVE CAUGHT #283.

    Reads the `from cp_engine...` imports out of `server.py` and resolves each
    against the VENDOR tree alone — no `src/` on the path, the way the
    container sees it. A verb importing a module nobody vendored fails here
    instead of in production.
    """
    import importlib.util

    src = (_HERE / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    needed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("cp_engine"):
            needed.add(node.module)
            if node.module == "cp_engine":
                needed.update(f"cp_engine.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            needed.update(a.name for a in node.names if a.name.startswith("cp_engine"))

    assert needed, "no cp_engine imports found — has the server stopped using them?"

    missing = []
    for mod in sorted(needed):
        rel = Path(mod.replace(".", "/"))
        if not ((_VENDOR.parent / rel).with_suffix(".py").exists()
                or (_VENDOR.parent / rel / "__init__.py").exists()):
            missing.append(mod)
    assert not missing, (
        f"server.py imports {missing} but the vendor tree does not carry them — "
        "they will raise ModuleNotFoundError in the container while the tool "
        "still registers, so /health will look healthy and every call will fail"
    )


def test_the_dockerfile_actually_ships_the_vendor_tree() -> None:
    """Vendoring that never reaches the image is the bug with extra steps."""
    dockerfile = (_HERE / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY vendor" in dockerfile, (
        "the Dockerfile does not COPY vendor — the container will have no "
        "cp_engine and the wrap-up verbs will raise at call time"
    )
