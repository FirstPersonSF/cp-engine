"""Tests for cp_engine.close_out — the `cp close` checklist logic (pure)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from cp_engine.close_out import (
    CloseChecklistInputs,
    CloseElement,
    build_close_checklist,
    element_from_row,
    find_close_workdir,
    is_active_like,
    promotion_candidates,
    synthesis_targets,
    thin_stubs,
)
from cp_engine.spine import SpineDirNotFound

_TODAY = date(2026, 7, 26)


def _el(key: str, layer: str, *, body_len: int = 500, serves=(), scope=None):
    return CloseElement(
        key=key, title=key.replace("_", " "), layer=layer,
        body_len=body_len, serves=tuple(serves), scope=scope,
    )


# ── selectors ─────────────────────────────────────────────────────────


def test_synthesis_targets_are_synthesis_and_output_layers() -> None:
    els = [
        _el("_authored/synth-1", "Synthesis"),
        _el("_authored/deck-v04", "Output"),
        _el("_authored/email-1", "Email"),
    ]
    assert [e.key for e in synthesis_targets(els)] == [
        "_authored/synth-1", "_authored/deck-v04",
    ]


def test_thin_stubs_require_short_body_and_no_serves() -> None:
    els = [
        _el("stub", "Note", body_len=50),                       # candidate
        _el("short-but-serving", "Note", body_len=50, serves=("d1",)),
        _el("long", "Note", body_len=900),
        _el("thin-brief", "Brief", body_len=10),                # standing: exempt
        _el("thin-sow", "Agreement", body_len=10),              # standing: exempt
    ]
    assert [e.key for e in thin_stubs(els)] == ["stub"]


def test_promotion_candidates_skip_already_account_scoped() -> None:
    els = [
        _el("dossier", "Stakeholders"),
        _el("client-voice", "Synthesis"),
        _el("promoted", "Stakeholders", scope="account"),
        _el("email", "Email"),
    ]
    assert [e.key for e in promotion_candidates(els)] == [
        "dossier", "client-voice",
    ]


def test_element_from_row_maps_substance_columns() -> None:
    el = element_from_row(
        {
            "id": "tel-5144/x/v2",
            "est_item_id": "_authored/synth-1",
            "framing": "What we learned",
            "layer": "Synthesis",
            "body": "  body text  ",
            "serves": ["d1"],
            "scope": "account",
        }
    )
    assert el.key == "_authored/synth-1"
    assert el.title == "What we learned"
    assert el.body_len == len("body text")
    assert el.serves == ("d1",)
    assert el.scope == "account"


# ── active-like gate ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "status", "active"),
    [
        ("project", "Open", True),
        ("project", "Deal", True),
        ("project", "Holding", False),
        ("project", "Closed", False),
        ("initiative", "Active", True),
        ("initiative", "Done", False),
        ("repo", "Active", True),
        ("repo", "Inactive", False),
    ],
)
def test_is_active_like(kind: str, status: str, active: bool) -> None:
    assert is_active_like(kind, status) is active


# ── workdir resolution incl. inactive bins ────────────────────────────


def _mk(root: Path, rel: str) -> Path:
    d = root / rel
    d.mkdir(parents=True)
    (d / "cp.md").write_text("# stub\n", encoding="utf-8")
    return d


def test_find_close_workdir_live_dir(tmp_path: Path) -> None:
    d = _mk(tmp_path, "1p/teleflex/tel-5144-urolift-3-naming")
    assert find_close_workdir(tmp_path, "tel-5144") == (d, False)


def test_find_close_workdir_account_inactive_bin(tmp_path: Path) -> None:
    _mk(tmp_path, "1p/teleflex/tel-5149-other")  # keeps the account dir live
    d = _mk(tmp_path, "1p/teleflex/inactive/tel-5144-urolift-3-naming")
    assert find_close_workdir(tmp_path, "tel-5144") == (d, True)


def test_find_close_workdir_scope_inactive_whole_account(tmp_path: Path) -> None:
    d = _mk(tmp_path, "1p/inactive/oldco/old-1111-legacy")
    assert find_close_workdir(tmp_path, "old-1111") == (d, True)


def test_find_close_workdir_missing_raises(tmp_path: Path) -> None:
    (tmp_path / "1p").mkdir()
    with pytest.raises(SpineDirNotFound):
        find_close_workdir(tmp_path, "nope-9999")


# ── checklist rendering ───────────────────────────────────────────────


def _inputs(**kw) -> CloseChecklistInputs:
    base = dict(
        code="tel-5144",
        status_label="Closed (project)",
        elements=[
            _el("_authored/synth-1", "Synthesis"),
            _el("_authored/stub-1", "Note", body_len=40),
            _el("_authored/dossier", "Stakeholders"),
        ],
        commitments=[
            {
                "description": "Send final files",
                "direction": "us_to_them",
                "owner_name": "Drew",
                "due_date": "2026-07-30",
            }
        ],
        workdir_root_files=["cp.md", "notes.md"],
    )
    base.update(kw)
    return CloseChecklistInputs(**base)


def test_checklist_carries_all_six_sections_and_real_data() -> None:
    text = build_close_checklist(_inputs(), today=_TODAY)
    assert "# Close-out ritual — tel-5144" in text
    assert "## 1 · Final synthesis spine element" in text
    assert "`_authored/synth-1`" in text  # derives_from target
    assert "## 2 · Exec Summary → terminal form" in text
    assert "**Objective (past tense):**" in text
    assert "## 3 · Open commitments — resolve or drop" in text
    assert "Send final files" in text and "due: 2026-07-30" in text
    assert "## 4 · Thin-stub retire candidates — VERIFY PER CARD" in text
    assert "`_authored/stub-1`" in text and "40 chars" in text
    assert "## 5 · Account-promotion candidates" in text
    assert "`_authored/dossier`" in text
    assert "## 6 · Working-dir hygiene" in text
    assert "- `notes.md`" in text
    assert "close-out-tel-5144-2026-07-26.md" in text  # anchor block filename


def test_checklist_empty_states() -> None:
    text = build_close_checklist(
        _inputs(elements=[], commitments=[], workdir_root_files=[]),
        today=_TODAY,
    )
    assert "no live Synthesis/Output elements found" in text
    assert "No open commitments — verified live." in text
    assert "No thin-stub candidates found." in text
    assert "No promotion candidates" in text


def test_checklist_commitments_unavailable_is_flagged_not_verified() -> None:
    text = build_close_checklist(_inputs(commitments=None), today=_TODAY)
    assert "commitments unavailable" in text
    assert "No open commitments — verified live." not in text
