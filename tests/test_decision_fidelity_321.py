"""#321 — auto-ingest records deliberations as decisions and keeps a
reversed decision at its first value.

Fixture wording is taken from real tenant bullets: the SOC 2 vendor
"leaning toward Thoropass" (09-22, recorded as a decision), and the
ibx-5153 09-15 "only present negative-framed versions" decision that the
same meeting reversed.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tests.test_ingest import _scaffold_minimal_sprint_file
from tests.test_plan_from_transcript import _make_tenant_config, _stub_claude_response

PLAN = """
transcript:
  source: fathom
  path: /tmp/t.txt
projects:
  ibx-5153:
    decisions:
      - text: "Partners leaning toward Thoropass over Vanta as SOC 2 compliance vendor."
        date: "2026-09-22"
      - text: "For the 'Your AI can't' direction, only present negative-framed versions (drop the positive flips)."
        date: "2026-09-15"
      - text: "Tagline locked as 'Infoblox, the truth AI depends on'."
        date: "2026-09-15"
      - text: "For the 'Your AI can't' direction, present negative-framed versions with their positive flips alongside."
        date: "2026-09-15"
      - text: "Deck ships Thursday."
        date: "2026-09-15"
        earlier_position: "Deck ships Wednesday."
"""


def _generate(tmp_path, monkeypatch):
    from cp_engine.plan_from_transcript import generate_plan

    _stub_claude_response(monkeypatch, PLAN)
    t = tmp_path / "t.txt"
    t.write_text("00:00:01 - Drew Fiero\n  hi\n")
    return generate_plan(
        config=_make_tenant_config(tmp_path),
        project_code="ibx-5153",
        transcript_path=t,
        api_key="stub",
    ).plan["projects"]["ibx-5153"]


def test_a_deliberation_becomes_an_open_question(tmp_path, monkeypatch):
    block = _generate(tmp_path, monkeypatch)
    decided = [d["text"] for d in block["decisions"]]
    assert not any("leaning toward" in t for t in decided)
    assert [q["text"] for q in block["open-questions"]] == [
        "Partners leaning toward Thoropass over Vanta as SOC 2 compliance vendor."
    ]


def test_a_restated_decision_keeps_its_last_value_and_says_so(tmp_path, monkeypatch):
    block = _generate(tmp_path, monkeypatch)
    ai_cant = [d["text"] for d in block["decisions"] if "Your AI can't" in d["text"]]
    assert len(ai_cant) == 1
    assert ai_cant[0].startswith(
        "For the 'Your AI can't' direction, present negative-framed versions with"
    )
    assert "revised in-meeting; earlier:" in ai_cant[0]
    assert "only present negative-framed" in ai_cant[0]
    # Unrelated decisions are untouched.
    assert any(d["text"].startswith("Tagline locked") for d in block["decisions"])


def test_earlier_position_is_rendered_on_the_bullet(tmp_path, monkeypatch):
    block = _generate(tmp_path, monkeypatch)
    deck = [d for d in block["decisions"] if d["text"].startswith("Deck ships")][0]
    assert deck["text"] == (
        'Deck ships Thursday. (revised in-meeting; earlier: "Deck ships Wednesday.")'
    )
    assert "earlier_position" not in deck


def test_open_question_lands_beside_decisions_not_in_them(tmp_path):
    from cp_engine.ingest import execute_plan
    from cp_engine.sprints import parse_sprint_file

    path = tmp_path / "sprints" / "2026-W39" / "ibx-5153.md"
    _scaffold_minimal_sprint_file(path, "ibx-5153")
    result = execute_plan(
        {
            "projects": {
                "ibx-5153": {
                    "open-questions": [
                        {"text": "Thoropass or Vanta for SOC 2?", "date": "2026-09-22"}
                    ],
                    "decisions": [{"text": "Deck ships Thursday.", "date": "2026-09-22"}],
                }
            }
        },
        tenant_root=tmp_path,
        today=date(2026, 9, 22),
        week_iso="2026-W39",
    )
    assert result.errors == []
    body = path.read_text()
    decisions_at = body.index("### Decisions")
    questions_at = body.index("### Open questions")
    q_line = next(l for l in body.splitlines() if "Thoropass or Vanta" in l)
    assert q_line.startswith("- [open question · 2026-09-22] Thoropass or Vanta for SOC 2?")
    assert body.index(q_line) > questions_at > decisions_at
    # The decision parser (agendas, prep) never sees the question.
    parsed = parse_sprint_file(path)
    texts = [d.text for d in parsed.decisions]
    assert any("Deck ships Thursday" in t for t in texts)
    assert not any("Thoropass" in t for t in texts)


def test_unsettled_account_decision_goes_to_the_node_as_a_question():
    from cp_engine.ingest_fidelity import apply_decision_fidelity

    plan = {
        "projects": {},
        "account_decisions": [
            {"text": "Invoice routing through Brandon going forward.", "code": "ggl-5216-google"},
            {"text": "Still debating whether to move Go Safety to T&M.", "code": "ggl-5216-google"},
        ],
    }
    apply_decision_fidelity(plan)
    assert [d["text"] for d in plan["account_decisions"]] == [
        "Invoice routing through Brandon going forward."
    ]
    assert plan["projects"]["ggl-5216-google"]["open-questions"][0]["text"].startswith(
        "Still debating whether"
    )
