"""The install payload is the repo's instruction to a cold agent (#296 §4.6, #297).

These are rot guards, not prose review: the entry file a cold Claude Code
session reads first must name the payload, and every `cxp` command the payload
tells an agent to run must exist — a payload that names a command the engine
does not ship is the September failure (an agent improvising) with a link.
"""

from __future__ import annotations

import re
from pathlib import Path

from cp_engine.cli import main

REPO = Path(__file__).resolve().parents[1]
PAYLOAD = REPO / "docs" / "install.md"
CONVENTION = REPO / "docs" / "conventions" / "self-describing-repo.md"


def test_entry_files_point_a_cold_session_at_the_payload():
    claude_md = (REPO / "CLAUDE.md").read_text()
    assert "docs/install.md" in "\n".join(claude_md.splitlines()[:8]), "named in the first screen"
    readme_top = "\n".join((REPO / "README.md").read_text().splitlines()[:25])
    assert "docs/install.md" in readme_top


def test_payload_names_its_reader_and_answers_the_four_questions():
    text = PAYLOAD.read_text()
    assert text.split("---", 2)[2].lstrip().startswith("# ")
    assert "**Reader.**" in text[:1500]
    for heading in ("## 1. What cp is", "## 2. How a person uses it", "## 4. Install",
                    "## 5. What a correct install looks like", "## 6. Upgrade", "## 7. Uninstall"):
        assert heading in text, heading
    assert "cxp record-install --installer agent" in text
    assert "cxp doctor" in text


def test_every_cxp_command_in_the_payload_exists():
    text = PAYLOAD.read_text()
    used = set(re.findall(r"\bcxp ([a-z][a-z-]+)", text))
    assert used, "the payload drives the engine through cxp"
    missing = sorted(u for u in used if u not in main.commands)
    assert missing == [], f"payload names commands the engine does not ship: {missing}"


def test_convention_carries_the_reader_test_as_a_required_check():
    text = CONVENTION.read_text()
    assert "## Required pre-write check: the reader test" in text
    assert "reacting to something" in text
