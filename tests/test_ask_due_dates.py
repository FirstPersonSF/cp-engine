# tests/test_ask_due_dates.py — due dates on open asks (#70)
from pathlib import Path

from cp_engine.sprints import _ask_who_and_by, parse_sprint_file


_FM = ("---\n"
       "Project: ggl-5168 — Playbooks\n"
       "Sprint: 2026-W29\n"
       "PriorSprint: 2026-W28\n"
       "---\n")


def _sprint(tmp_path: Path, asks_block: str) -> Path:
    p = tmp_path / "ggl-5168.md"
    p.write_text(
        _FM + "## Client communication\n\n### Open asks\n" + asks_block + "\n",
        encoding="utf-8",
    )
    return p


def test_ask_parses_by_date(tmp_path):
    p = _sprint(tmp_path,
                "- [open · 2026-07-11 · Janet · by 2026-07-30] schedule Jen "
                "Moyse (UX) or drop")
    ask = parse_sprint_file(p).client_open_asks[0]
    assert ask.who == "Janet"
    assert ask.by == "2026-07-30"
    assert ask.status == "open"


def test_ask_without_by_unchanged(tmp_path):
    p = _sprint(tmp_path, "- [open · 2026-07-11 · Janet] send the deck")
    ask = parse_sprint_file(p).client_open_asks[0]
    assert ask.who == "Janet" and ask.by is None


def test_ask_by_without_who(tmp_path):
    p = _sprint(tmp_path, "- [open · 2026-07-11 · by 2026-07-30] confirm room")
    ask = parse_sprint_file(p).client_open_asks[0]
    assert ask.who is None and ask.by == "2026-07-30"


def test_who_and_by_helper_is_order_tolerant():
    assert _ask_who_and_by(["Janet", "by 2026-07-30"]) == ("Janet", "2026-07-30")
    assert _ask_who_and_by(["by 2026-07-30", "Janet"]) == ("Janet", "2026-07-30")
    assert _ask_who_and_by(["by then"]) == ("by then", None)  # not a date → who
    assert _ask_who_and_by([]) == (None, None)


def test_carry_forward_preserves_by(tmp_path):
    from cp_engine.sprints import compute_carry_forward

    p = _sprint(tmp_path,
                "- [open · 2026-07-11 · Janet · by 2026-07-30] schedule Jen\n"
                "- [answered · 2026-07-11 · Bo · by 2026-07-20] old one")
    cf = compute_carry_forward(p)
    assert len(cf.asks) == 1  # only open asks roll
    assert cf.asks[0].by == "2026-07-30"


def test_carry_forward_region_parses_by(tmp_path):
    p = tmp_path / "s.md"
    p.write_text(
        _FM +
        "<!-- cp-engine:start carry-forward -->\n"
        "## Carried over from 2026-W28\n"
        "- [ask · 2026-07-11 · Janet · by 2026-07-30] schedule Jen\n"
        "<!-- cp-engine:end carry-forward -->\n",
        encoding="utf-8",
    )
    sf = parse_sprint_file(p)
    assert sf.carry_forward.asks[0].by == "2026-07-30"
    assert sf.carry_forward.asks[0].who == "Janet"


def test_ingest_write_ask_accepts_due_alias(tmp_path):
    from cp_engine.ingest import _write_ask

    p = _sprint(tmp_path, "- _<placeholder>_")
    ok = _write_ask("ggl-5168",
                    {"text": "confirm attendees", "who": "Janet",
                     "due": "2026-07-30"}, p)
    assert ok
    body = p.read_text(encoding="utf-8")
    assert "· Janet · by 2026-07-30]" in body
