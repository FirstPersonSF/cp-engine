"""Smoke test for `cp parse-sprint <path> --json`.

Invokes the CLI via `python -m cp_engine.cli` so the test doesn't depend
on the `cp` script being on PATH. The fixture is the minimum content
`parse_sprint_file` needs to round-trip: frontmatter + an H1 with a
date range. Engine-managed regions are omitted on purpose — the parser
falls back to defaults, and that's part of the contract being exercised.
"""

from __future__ import annotations

import json
import subprocess
import sys

_FIXTURE_SPRINT_MARKDOWN = (
    "---\n"
    "Project: peb — Pebble Foods\n"
    "Sprint: 2026-W19\n"
    "PriorSprint: 2026-W18\n"
    "---\n"
    "# peb — Pebble Foods · Sprint W19 (May 11 – May 17, 2026)\n"
)


def test_cp_parse_sprint_emits_valid_json(tmp_path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(_FIXTURE_SPRINT_MARKDOWN)
    result = subprocess.run(
        [sys.executable, "-m", "cp_engine.cli", "parse-sprint", str(f), "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    parsed = json.loads(result.stdout)
    assert parsed["project_code"] == "peb"
    assert parsed["week_iso"] == "2026-W19"
    assert isinstance(parsed["client_open_asks"], list)
