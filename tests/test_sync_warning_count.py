# tests/test_sync_warning_count.py — issue #197, second half: a sync that
# degraded must not look identical to a clean one.
#
# Sync is best-effort in a dozen places by design — one malformed element
# should not cost the other 36 projects their sync. The cost was that the
# outcome line read "Synced 37 projects." and exit 0 whether or not anything
# had been stranded, so a company's whole stakeholder mirror could go missing
# in silence. The count travels to the summary.
import logging

from cp_engine.sync import _WarningCounter


def test_counts_warnings_from_any_cp_engine_module():
    counter = _WarningCounter()
    pkg = logging.getLogger("cp_engine")
    pkg.addHandler(counter)
    try:
        logging.getLogger("cp_engine.spine_substance_sync").warning("stranded")
        logging.getLogger("cp_engine.ingest").warning("also stranded")
    finally:
        pkg.removeHandler(counter)
    assert counter.count == 2


def test_info_and_debug_do_not_count():
    counter = _WarningCounter()
    pkg = logging.getLogger("cp_engine")
    pkg.setLevel(logging.DEBUG)
    pkg.addHandler(counter)
    try:
        logging.getLogger("cp_engine.sync").info("routine")
        logging.getLogger("cp_engine.sync").debug("noisy")
        logging.getLogger("cp_engine.sync").warning("real")
    finally:
        pkg.removeHandler(counter)
    assert counter.count == 1, "only WARNING+ should count"


def test_each_warning_counts_once():
    """`Handler.handle()` calls `emit()` — counting in both double-counts."""
    counter = _WarningCounter()
    pkg = logging.getLogger("cp_engine")
    pkg.addHandler(counter)
    try:
        logging.getLogger("cp_engine.sync").warning("once")
    finally:
        pkg.removeHandler(counter)
    assert counter.count == 1


def test_errors_count_too():
    counter = _WarningCounter()
    pkg = logging.getLogger("cp_engine")
    pkg.addHandler(counter)
    try:
        logging.getLogger("cp_engine.sync").error("worse than a warning")
    finally:
        pkg.removeHandler(counter)
    assert counter.count == 1


def test_routine_summary_truncation_is_not_a_warning(caplog):
    """#197: the warning count is only useful if warnings mean something.

    The 120-char master-CP cell cap is a designed constraint that most project
    summaries exceed — a real tenant-wide sync tripped it 23 times, hiding the
    ONE warning that mattered (a stranded spine element). Routine behavior
    belongs at DEBUG so the count stays a signal.
    """
    from cp_engine.summary import enforce_summary_cap

    with caplog.at_level(logging.WARNING, logger="cp_engine"):
        out = enforce_summary_cap("x" * 400)
    assert out.endswith("…") and len(out) <= 120, "still truncates"
    assert not caplog.records, "routine truncation must not log at WARNING"
