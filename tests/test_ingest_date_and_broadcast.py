"""Two failures from the 1p sprint-planning ingest of 2026-09-15.

Both were found by running the real thing, not by review, and neither failed
loudly enough to stop the run.

1. `'datetime.date' object has no attribute 'strip'` — six writes lost across
   ibx-5153 and sap-5174 (two decisions, two risks, a milestone) while the run
   reported partial success. YAML parses an unquoted `date: 2026-09-15` as a
   `datetime.date`, and every field reader assumed a string.

2. The SAME SIX asks written into all 18 projects — 108 bullets where 6
   belonged, a Teleflex ask filed on the Google 5136 sprint file. The plan was
   structurally valid; the model had put a global action-item list under every
   project, using `asks` (routed) and `record-ask` (broadcast) as if they were
   different fields. Nothing looked for it.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from cp_engine.ingest import _as_text, _warn_on_broadcast


# ── 1. the date crash ─────────────────────────────────────────────────


class TestDateCoercion:
    def test_an_unquoted_yaml_date_does_not_crash(self):
        """The exact shape that lost six writes."""
        assert _as_text(date(2026, 9, 15)) == "2026-09-15"

    def test_a_datetime_renders_iso(self):
        assert _as_text(datetime(2026, 9, 15, 16, 39)).startswith("2026-09-15")

    def test_a_string_is_stripped_as_before(self):
        assert _as_text("  2026-09-15  ") == "2026-09-15"

    def test_none_is_empty(self):
        assert _as_text(None) == ""

    def test_a_number_does_not_crash(self):
        """A plan that is 99% right should not lose a bullet over a type."""
        assert _as_text(42) == "42"

    def test_the_real_yaml_path(self):
        """Prove the crash source, not just the fix: unquoted YAML gives a date."""
        import yaml

        parsed = yaml.safe_load("date: 2026-09-15")
        assert isinstance(parsed["date"], date)
        # The old code did `(item.get("date") or "").strip()` — this is why.
        try:
            parsed["date"].strip()
            raise AssertionError("expected AttributeError")
        except AttributeError:
            pass
        assert _as_text(parsed["date"]) == "2026-09-15"


# ── 2. the broadcast ──────────────────────────────────────────────────


def _plan(projects: dict) -> dict:
    return {"projects": projects}


class TestBroadcastDetection:
    def test_the_reported_case_warns(self, caplog):
        """Six identical asks across 18 projects — 108 bullets for 6 items."""
        asks = [{"text": f"ask {i}", "who": "Drew"} for i in range(6)]
        plan = _plan({f"proj-{n}": {"record-ask": list(asks)} for n in range(18)})
        with caplog.at_level(logging.WARNING):
            _warn_on_broadcast(plan)
        assert "BROADCAST" in caplog.text
        assert "18 projects" in caplog.text

    def test_it_does_not_refuse_the_plan(self, caplog):
        """A broadcast is a judgement call — warn, never discard.

        Discarding would cost more than it saves; the rest of the routing is
        usually right. Same lesson as the account_summary coercion.
        """
        asks = [{"text": "x"}]
        plan = _plan({f"p{n}": {"record-ask": list(asks)} for n in range(5)})
        with caplog.at_level(logging.WARNING):
            assert _warn_on_broadcast(plan) is None
        assert plan["projects"]["p0"]["record-ask"] == asks

    def test_genuinely_routed_content_is_silent(self, caplog):
        """The same run routed asks/risks/decisions correctly — no false alarm."""
        plan = _plan({
            "sap-5198": {"asks": [{"text": "a"}, {"text": "b"}, {"text": "c"}]},
            "tel-5113": {"asks": [{"text": "d"}, {"text": "e"}]},
            "ggl-5136": {"decisions": [{"text": "f"}]},
            "slt-5195": {"risks": [{"text": "g"}]},
        })
        with caplog.at_level(logging.WARNING):
            _warn_on_broadcast(plan)
        assert "BROADCAST" not in caplog.text

    def test_two_projects_sharing_an_item_is_not_a_broadcast(self, caplog):
        """A joint decision on two projects is legitimate."""
        shared = [{"text": "joint decision"}]
        plan = _plan({
            "a": {"decisions": list(shared)},
            "b": {"decisions": list(shared)},
            "c": {"decisions": [{"text": "different"}]},
        })
        with caplog.at_level(logging.WARNING):
            _warn_on_broadcast(plan)
        assert "BROADCAST" not in caplog.text

    def test_shorthand_and_canonical_are_counted_together(self, caplog):
        """`asks` and `record-ask` are one verb — the alias split hid this."""
        shared = [{"text": "same"}]
        plan = _plan({
            "a": {"asks": list(shared)},
            "b": {"record-ask": list(shared)},
            "c": {"asks": list(shared)},
        })
        with caplog.at_level(logging.WARNING):
            _warn_on_broadcast(plan)
        assert "BROADCAST" in caplog.text

    def test_a_small_plan_is_never_flagged(self, caplog):
        plan = _plan({"a": {"asks": [{"text": "x"}]}, "b": {"asks": [{"text": "x"}]}})
        with caplog.at_level(logging.WARNING):
            _warn_on_broadcast(plan)
        assert "BROADCAST" not in caplog.text

    def test_a_malformed_plan_does_not_raise(self):
        """Never break an ingest from the advisory path."""
        assert _warn_on_broadcast({"projects": "not a dict"}) is None
        assert _warn_on_broadcast({}) is None
