"""PreToolUse guard for engine-managed regions (#205).

The guard turns `CLAUDE.md`'s "MUST NOT be edited" instruction into something
the client enforces. Two properties matter most and are pinned here:

- it BLOCKS (exit 2) an edit landing inside a cp-engine:start/end region
- it FAILS OPEN (exit 0) on every unexpected condition

Fail-open is the load-bearing one: a guard that blocks because it couldn't
parse its own input would make the tenant un-editable the moment a payload
shape changed.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src" / "cp_engine" / "hooks" / "guard-engine-regions.py"
)

_DOC = """---
Project: demo
---

Human prose above.

<!-- cp-engine:start project-facts -->
ENGINE OWNED LINE
<!-- cp-engine:end project-facts -->

Human prose below.
"""


def _load():
    spec = importlib.util.spec_from_file_location("guard", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(event: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
    )


def _edit(path: Path, old: str) -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(path), "old_string": old, "new_string": "X"
        },
    }


def test_blocks_edit_inside_region(tmp_path: Path):
    f = tmp_path / "cp.md"
    f.write_text(_DOC)
    result = _run(_edit(f, "ENGINE OWNED LINE"))
    assert result.returncode == 2
    assert "project-facts" in result.stderr
    # the message must name the escape hatch, not just refuse
    assert "cp write-region" in result.stderr


def test_allows_edit_outside_region(tmp_path: Path):
    f = tmp_path / "cp.md"
    f.write_text(_DOC)
    assert _run(_edit(f, "Human prose below.")).returncode == 0


def test_blocks_write_that_drops_a_region(tmp_path: Path):
    f = tmp_path / "cp.md"
    f.write_text(_DOC)
    event = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(f), "content": "wiped"},
    }
    assert _run(event).returncode == 2


def test_allows_write_that_preserves_regions(tmp_path: Path):
    f = tmp_path / "cp.md"
    f.write_text(_DOC)
    event = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(f), "content": _DOC + "\nappended\n"},
    }
    assert _run(event).returncode == 0


def test_multiedit_blocks_when_any_edit_hits_a_region(tmp_path: Path):
    f = tmp_path / "cp.md"
    f.write_text(_DOC)
    event = {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": str(f),
            "edits": [
                {"old_string": "Human prose below.", "new_string": "ok"},
                {"old_string": "ENGINE OWNED LINE", "new_string": "bad"},
            ],
        },
    }
    assert _run(event).returncode == 2


def test_unguarded_tools_pass_through(tmp_path: Path):
    f = tmp_path / "cp.md"
    f.write_text(_DOC)
    event = {"tool_name": "Read", "tool_input": {"file_path": str(f)}}
    assert _run(event).returncode == 0


def test_file_without_regions_is_unaffected(tmp_path: Path):
    f = tmp_path / "notes.md"
    f.write_text("just prose, no markers\n")
    assert _run(_edit(f, "just prose")).returncode == 0


def test_fails_open_on_missing_file(tmp_path: Path):
    assert _run(_edit(tmp_path / "nope.md", "anything")).returncode == 0


def test_fails_open_on_malformed_json():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input="not json at all",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_fails_open_on_unexpected_schema():
    assert _run({"tool_name": "Edit", "tool_input": "not-a-dict"}).returncode == 0
    assert _run({}).returncode == 0


def test_unterminated_region_is_not_treated_as_a_region(tmp_path: Path):
    """A start marker with no matching end protects nothing.

    Half-written markers must not silently freeze the rest of a file.
    """
    mod = _load()
    assert mod._regions("<!-- cp-engine:start orphan -->\nbody\n") == []
