from datetime import date

from cp_engine.execution_status import derive_status


# --- Task 4.1: derive_status -------------------------------------------------

def test_done_when_bar_toggled_done():
    bars = [{"start_week": 0, "duration": 7, "done": True, "item_type": "activity"}]
    assert derive_status(bars, has_live_substance=True, recent_activity=False,
                         start_date="2026-04-27", today=date(2026, 6, 18)).status == "done"


def test_active_when_bar_overlaps_today():
    bars = [{"start_week": 6, "duration": 3, "done": False, "item_type": "activity"}]
    s = derive_status(bars, has_live_substance=True, recent_activity=False,
                      start_date="2026-04-27", today=date(2026, 6, 18))
    assert s.status == "active"


def test_next_when_future_bar_no_substance():
    bars = [{"start_week": 10, "duration": 2, "done": False, "item_type": "activity"}]
    s = derive_status(bars, has_live_substance=False, recent_activity=False,
                      start_date="2026-04-27", today=date(2026, 6, 18))
    assert s.status == "next"


def test_flag_when_past_bar_no_substance_not_done():
    bars = [{"start_week": 2, "duration": 2, "done": False, "item_type": "activity"}]
    s = derive_status(bars, has_live_substance=False, recent_activity=False,
                      start_date="2026-04-27", today=date(2026, 6, 18))
    assert s.status == "flag"
    assert "nothing captured" in s.basis.lower()


def test_proposed_done_when_past_bar_with_substance():
    bars = [{"start_week": 2, "duration": 2, "done": False, "item_type": "activity"}]
    s = derive_status(bars, has_live_substance=True, recent_activity=False,
                      start_date="2026-04-27", today=date(2026, 6, 18))
    assert s.status == "done?"


def test_no_start_date_degrades_to_relative():
    bars = [{"start_week": 6, "duration": 3, "done": False, "item_type": "activity"}]
    s = derive_status(bars, has_live_substance=True, recent_activity=True,
                      start_date=None, today=date(2026, 6, 18))
    assert s.status == "active"


def test_no_bars_no_substance_is_next():
    # an item with no schedule bar and no substance is simply upcoming/unstarted
    s = derive_status([], has_live_substance=False, recent_activity=False,
                      start_date="2026-04-27", today=date(2026, 6, 18))
    assert s.status == "next"
