"""Managed-region guard off Claude Code (architecture plan step 2).

THE DEFECT. Text written inside an engine-managed region by anything other
than the engine — a person in an editor, a webhook route, an LLM plan verb
that appended in the wrong place (#263) — was replaced by the next render with
no trace. The only guard was a Claude Code PreToolUse hook. The one existing
check (#320, carry-forward only) warned but kept nothing.

CONTROLS. ``test_hand_edit_in_sprint_facts_is_preserved_and_named`` and
``test_hand_edit_in_master_cp_region_survives_cxp_sync`` were run against
origin/main before this change and FAIL there (no quarantine file, no
warning): the edit vanished silently. See the step-2 report for the run.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cp_engine.render import splice_managed_region
from tests.test_sprints import _cf_kwargs, _cf_setup
from tests.test_sync import FakeBackend, make_config, make_state

# The two CONTROL tests touch nothing below but the render/sync entry points
# and these literals, so they can run unchanged against origin/main.
_QUARANTINE = Path("exceptions") / "region-edits"
_REGION_RE = re.compile(
    r"<!-- cp-engine:start (?P<n>[\w-]+) -->(?P<b>.*?)<!-- cp-engine:end (?P=n) -->", re.S
)

try:
    from cp_engine import region_guard
except ImportError:  # origin/main, for the control run
    region_guard = None

needs_guard = pytest.mark.skipif(region_guard is None, reason="pre-step-2 engine")


def _tenant(tmp_path: Path) -> Path:
    (tmp_path / ".cp-engine.toml").write_text("# fixture\n")
    return tmp_path


def _quarantined(root: Path) -> list[Path]:
    return sorted((root / _QUARANTINE).glob("*.md"))


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# ── digest primitives ──────────────────────────────────────────────────────


@needs_guard
def test_splice_stamps_and_recognises_its_own_write() -> None:
    body = "<!-- cp-engine:start facts -->\nold\n<!-- cp-engine:end facts -->\n"
    out = splice_managed_region(body, "facts", "- a\n- b  ")
    inner = region_guard.regions(out)["facts"]
    assert region_guard.provenance(inner) == "engine"
    # The body is written byte-for-byte (a markdown hard break survives).
    assert "- b  \n<!-- cp-engine:digest " in out


@needs_guard
def test_foreign_line_flips_provenance_but_trailing_whitespace_does_not() -> None:
    out = splice_managed_region(
        "<!-- cp-engine:start facts -->\n<!-- cp-engine:end facts -->\n", "facts", "- a\n- b"
    )
    trimmed = out.replace("- a\n", "- a   \n")
    assert region_guard.provenance(region_guard.regions(trimmed)["facts"]) == "engine"
    edited = out.replace("- b\n", "- b\n- typed by hand\n")
    assert region_guard.provenance(region_guard.regions(edited)["facts"]) == "foreign"
    legacy = "<!-- cp-engine:start facts -->\n- a\n<!-- cp-engine:end facts -->\n"
    assert region_guard.provenance(region_guard.regions(legacy)["facts"]) == "unstamped"


@needs_guard
def test_exec_summary_is_authored_never_stamped_never_guarded(tmp_path, caplog) -> None:
    root = _tenant(tmp_path)
    f = root / "cp.md"
    body = "<!-- cp-engine:start exec-summary -->\n**Status:** x\n<!-- cp-engine:end exec-summary -->\n"
    f.write_text(body)
    with caplog.at_level(logging.WARNING):
        out = splice_managed_region(body, "exec-summary", "**Status:** y", source=f)
    assert "cp-engine:digest" not in out
    assert _quarantined(root) == []
    assert _warnings(caplog) == []


@needs_guard
def test_engine_rerender_of_its_own_region_is_quiet(tmp_path, caplog) -> None:
    """Data drift is not a hand edit: re-rendering a region the engine wrote
    with DIFFERENT content must neither warn nor quarantine."""
    root = _tenant(tmp_path)
    f = root / "x.md"
    first = splice_managed_region(
        "<!-- cp-engine:start facts -->\n<!-- cp-engine:end facts -->\n", "facts", "- 3 sessions"
    )
    with caplog.at_level(logging.WARNING):
        splice_managed_region(first, "facts", "- 4 sessions", source=f)
    assert _quarantined(root) == []
    assert _warnings(caplog) == []


@needs_guard
def test_unstamped_legacy_region_is_not_accused(tmp_path, caplog) -> None:
    root = _tenant(tmp_path)
    legacy = "<!-- cp-engine:start facts -->\n- anything\n<!-- cp-engine:end facts -->\n"
    with caplog.at_level(logging.WARNING):
        out = splice_managed_region(legacy, "facts", "- new", source=root / "x.md")
    assert _warnings(caplog) == []
    assert region_guard.provenance(region_guard.regions(out)["facts"]) == "engine"


# ── cxp render (sprint files) ──────────────────────────────────────────────


def test_hand_edit_in_sprint_facts_is_preserved_and_named(tmp_path, caplog) -> None:
    """CONTROL (fails on origin/main): a hand line inside `sprint-facts` — a
    region the #320 check never covered — is quarantined and named."""
    from cp_engine.sprints import ensure_sprint_file

    root = _tenant(tmp_path)
    _prior, cur = _cf_setup(root)
    rendered = cur.read_text()
    end = "<!-- cp-engine:end sprint-facts -->"
    cur.write_text(rendered.replace(end, "| Hand fact | Morgan PTO 09-08 |\n" + end, 1))

    with caplog.at_level(logging.WARNING, logger="cp_engine"):
        ensure_sprint_file(**_cf_kwargs(root, "2026-W20", "2026-W19"))

    assert "Morgan PTO" not in cur.read_text()  # the region is still derived
    q = _quarantined(root)
    assert len(q) == 1, q
    text = q[0].read_text()
    assert "Morgan PTO 09-08" in text
    assert "sprints/2026-W20/peb.md" in text and "`sprint-facts`" in text
    msgs = _warnings(caplog)
    assert len(msgs) == 1 and "sprint-facts" in msgs[0] and "Morgan PTO" in msgs[0], msgs
    assert str(q[0].relative_to(root)) in msgs[0]


@needs_guard
def test_carry_forward_hand_edit_is_now_preserved_too(tmp_path, caplog) -> None:
    """#320 warned; it now also keeps the line (one warning, not two)."""
    from cp_engine.sprints import ensure_sprint_file

    root = _tenant(tmp_path)
    _prior, cur = _cf_setup(root)
    end = "<!-- cp-engine:end carry-forward -->"
    cur.write_text(cur.read_text().replace(
        end, "- [milestone · 2026-09-08] Morgan on PTO\n" + end, 1
    ))
    with caplog.at_level(logging.WARNING, logger="cp_engine"):
        ensure_sprint_file(**_cf_kwargs(root, "2026-W20", "2026-W19"))
    q = _quarantined(root)
    assert len(q) == 1 and "Morgan on PTO" in q[0].read_text()
    assert len(_warnings(caplog)) == 1


@needs_guard
def test_rerender_with_nothing_foreign_writes_no_quarantine(tmp_path) -> None:
    from cp_engine.sprints import ensure_sprint_file

    root = _tenant(tmp_path)
    _prior, cur = _cf_setup(root)
    before = cur.read_text()
    ensure_sprint_file(**_cf_kwargs(root, "2026-W20", "2026-W19"))
    assert cur.read_text() == before  # idempotent with digests
    assert _quarantined(root) == []


# ── cxp sync (master-cp.md) ────────────────────────────────────────────────


def test_hand_edit_in_master_cp_region_survives_cxp_sync(tmp_path, caplog) -> None:
    """CONTROL (fails on origin/main): sync replaced a hand line inside a
    master-cp region with no trace."""
    from cp_engine.sync import sync_tenant

    root = _tenant(tmp_path)
    config = make_config(root)
    fake = FakeBackend((make_state(),))
    now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
    sync_tenant(config, backend_factory=lambda _: fake, now=now)
    master = root / "master-cp.md"
    text = master.read_text()
    names = [m.group("n") for m in _REGION_RE.finditer(text)
             if m.group("n") not in ("exec-summary", "last-sync-timestamp")]
    target = names[0]
    end = f"<!-- cp-engine:end {target} -->"
    master.write_text(text.replace(end, "- NOTE TYPED BY TONY\n" + end, 1))

    with caplog.at_level(logging.WARNING, logger="cp_engine"):
        sync_tenant(config, backend_factory=lambda _: fake, now=now)

    assert "NOTE TYPED BY TONY" not in master.read_text()
    q = _quarantined(root)
    assert len(q) == 1 and "NOTE TYPED BY TONY" in q[0].read_text(), q
    assert any("NOTE TYPED BY TONY" in m and target in m for m in _warnings(caplog))


# ── cxp write-region (the sanctioned escape hatch) ─────────────────────────


@needs_guard
def test_write_region_preserves_what_it_overwrites(tmp_path) -> None:
    from click.testing import CliRunner

    from cp_engine.cli_cmds.core import write_region_cmd

    root = _tenant(tmp_path)
    f = root / "cp.md"
    f.write_text(splice_managed_region(
        "<!-- cp-engine:start facts -->\n<!-- cp-engine:end facts -->\n", "facts", "- a"
    ).replace("- a\n", "- a\n- hand\n"))
    res = CliRunner().invoke(write_region_cmd, [str(f), "facts", "--body", "- b"])
    assert res.exit_code == 0, res.output
    q = _quarantined(root)
    assert len(q) == 1 and "- hand" in q[0].read_text()
