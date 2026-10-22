"""A hand-marked closed ask in an OLDER week closes its open commitment (#354).

The defect: ``sync_sprint_asks`` honoured a ``[done · …]`` bullet only in the
CURRENT week. In an earlier week it hit ``if hb.status != OPEN or _match(hb):
continue`` and was neither closed nor reported, and weeks before the carry
cutoff were never read — so a person's "done" and MC-2's "open" diverged with
no surface naming it. Measured on the tenant 2026-10-22: 0 such pairs today
(90 closed ask bullets; 87 never imported, 2 done, 1 expired), so this pins
the contract rather than repairing live drift.

Authority (``asks.py`` module + ``sync_sprint_asks`` docstring, step 2): a
closed hand bullet closes its open commitment. The same rule now reaches
earlier weeks, newest statement first, and never closes a commitment created
after the week that marked it done (a later re-raise is a new life).

CONTROLS: each test FAILS on the pre-fix engine on its assertion.
"""

from __future__ import annotations

import logging

from cp_engine.asks import ask_hash
from tests.test_asks_mc2 import CODE, FakeMC2, _render, _row, _sync, _type_ask

TEXT = "Approve the pop-up copy"


def _status(client) -> list[str]:
    return [r["status"] for r in client.commitments]


def test_done_in_an_earlier_week_closes_the_open_commitment(tmp_path):
    client = FakeMC2([_row(TEXT, created_at="2026-09-10T12:00:00+00:00")])
    w38 = _render(tmp_path, "2026-W38", None)
    _type_ask(w38, f"- [done · 2026-09-15 · Rena] {TEXT}")
    before = w38.read_text()
    _render(tmp_path, "2026-W39", "2026-W38")
    _render(tmp_path, "2026-W40", "2026-W39")
    _sync(client, tmp_path, "2026-W40")
    assert _status(client) == ["done"]
    assert w38.read_text() == before  # history is not edited


def test_close_reaches_weeks_before_the_carry_cutoff(tmp_path):
    client = FakeMC2([_row(TEXT, created_at="2026-07-01T12:00:00+00:00")])
    w30 = _render(tmp_path, "2026-W30", None)
    _type_ask(w30, f"- [closed · 2026-07-21 · Rena] {TEXT}")
    _render(tmp_path, "2026-W40", "2026-W30")
    _sync(client, tmp_path, "2026-W40")
    assert _status(client) == ["done"]


def test_struck_through_ask_reads_as_done(tmp_path):
    client = FakeMC2([_row(TEXT, created_at="2026-09-10T12:00:00+00:00")])
    w39 = _render(tmp_path, "2026-W39", None)
    _type_ask(w39, f"- [open · 2026-09-22 · Rena] ~~{TEXT}~~")
    _render(tmp_path, "2026-W40", "2026-W39")
    _sync(client, tmp_path, "2026-W40")
    assert _status(client) == ["done"]


def test_done_bullet_carrying_the_rendered_hash_still_closes(tmp_path):
    """The current week's region renders the open row with its hash; that view
    is not a newer human statement and must not shadow the older done mark."""
    h = ask_hash(CODE, TEXT)
    client = FakeMC2([_row(TEXT, cp_hash=h, created_at="2026-09-10T12:00:00+00:00")])
    w39 = _render(tmp_path, "2026-W39", None)
    _type_ask(w39, f"- [done · 2026-09-10 · Rena] {TEXT} <!-- cp:hash={h} -->")
    w40 = _render(tmp_path, "2026-W40", "2026-W39")
    _sync(client, tmp_path, "2026-W40")  # renders the open row into W40's region
    assert _status(client) == ["done"]
    assert TEXT not in w40.read_text().split("cp-engine:start open-asks")[1].split(
        "cp-engine:end open-asks")[0]


def test_a_commitment_raised_after_the_done_week_is_left_open(tmp_path):
    client = FakeMC2([_row(TEXT, created_at="2026-09-29T12:00:00+00:00")])
    w38 = _render(tmp_path, "2026-W38", None)
    _type_ask(w38, f"- [done · 2026-09-15 · Rena] {TEXT}")
    _render(tmp_path, "2026-W40", "2026-W38")
    _sync(client, tmp_path, "2026-W40")
    assert _status(client) == ["open"]


def test_a_newer_open_statement_wins_over_an_older_done(tmp_path):
    client = FakeMC2([_row(TEXT, created_at="2026-09-01T12:00:00+00:00")])
    w37 = _render(tmp_path, "2026-W37", None)
    _type_ask(w37, f"- [done · 2026-09-08 · Rena] {TEXT}")
    w39 = _render(tmp_path, "2026-W39", "2026-W37")
    _type_ask(w39, f"- [open · 2026-09-22 · Rena] {TEXT}")
    _render(tmp_path, "2026-W40", "2026-W39")
    _sync(client, tmp_path, "2026-W40")
    assert _status(client) == ["open"]


def test_resolve_is_idempotent_and_logged(tmp_path, caplog, monkeypatch):
    import cp_engine.asks as asks

    client = FakeMC2([_row(TEXT, created_at="2026-09-10T12:00:00+00:00")])
    w38 = _render(tmp_path, "2026-W38", None)
    _type_ask(w38, f"- [done · 2026-09-15 · Rena] {TEXT}")
    _render(tmp_path, "2026-W40", "2026-W38")
    calls: list[str] = []
    real = asks._close
    monkeypatch.setattr(asks, "_close",
                        lambda c, i, o: (calls.append(i), real(c, i, o)))
    with caplog.at_level(logging.INFO):
        _sync(client, tmp_path, "2026-W40")
        _sync(client, tmp_path, "2026-W40")
    assert _status(client) == ["done"]
    assert len(calls) == 1
    assert any("2026-W38" in m and "resolved" in m for m in caplog.messages)
