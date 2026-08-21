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


# ---------------------------------------------------------------------------
# issue #212 — the count was unactionable on its own.
#
# The CLI installs no logging handler, so every record this counter saw was
# counted and then discarded by Python's last-resort behaviour. The summary
# still said "N warnings logged above — some content may not have been
# written", naming nothing and pointing at output that did not exist. The only
# way to learn what happened was to re-run the whole tenant pass and diff.
# The counter now retains the formatted text so the CLI can name it.


def test_retains_the_message_text():
    counter = _WarningCounter()
    pkg = logging.getLogger("cp_engine")
    pkg.addHandler(counter)
    try:
        logging.getLogger("cp_engine.spine_substance_sync").warning(
            "authored-live shield (#113): element abc123 disk v6 claims live"
        )
    finally:
        pkg.removeHandler(counter)

    assert counter.count == 1
    assert len(counter.messages) == 1
    # The module name is part of the message — "which subsystem" is most of
    # what makes one of these actionable.
    assert "spine_substance_sync" in counter.messages[0]
    assert "authored-live shield" in counter.messages[0]


def test_interpolates_lazy_log_args():
    """`logger.warning("x %s", y)` must be rendered, not stored raw."""
    counter = _WarningCounter()
    pkg = logging.getLogger("cp_engine")
    pkg.addHandler(counter)
    try:
        logging.getLogger("cp_engine.sync").warning("stranded %s in %s", "elem-9", "ggl")
    finally:
        pkg.removeHandler(counter)

    assert "stranded elem-9 in ggl" in counter.messages[0]


def test_retention_is_capped_but_the_count_is_not():
    """A pathological run must not balloon the summary or hold every string."""
    counter = _WarningCounter()
    pkg = logging.getLogger("cp_engine")
    pkg.addHandler(counter)
    try:
        for i in range(_WarningCounter.MAX_RETAINED + 15):
            logging.getLogger("cp_engine.sync").warning("warning %s", i)
    finally:
        pkg.removeHandler(counter)

    assert counter.count == _WarningCounter.MAX_RETAINED + 15, "count stays exact"
    assert len(counter.messages) == _WarningCounter.MAX_RETAINED, "detail is bounded"


def test_result_carries_the_messages(tmp_path, monkeypatch):
    """The texts must reach SyncResult — the CLI reads them from there."""
    from cp_engine.sync import SyncResult

    result = SyncResult(
        projects_seen=1,
        files_written=(),
        files_deactivated=(),
        no_op=True,
        warnings=1,
        warning_messages=("[cp_engine.sync] something was stranded",),
    )
    assert result.warning_messages == ("[cp_engine.sync] something was stranded",)


def test_result_defaults_to_no_messages():
    """Back-compat: constructing without the new field must still work."""
    from cp_engine.sync import SyncResult

    result = SyncResult(
        projects_seen=0, files_written=(), files_deactivated=(), no_op=True
    )
    assert result.warning_messages == ()
    assert result.warnings == 0


def test_cli_names_the_warning_instead_of_alluding_to_it():
    """#212 end-to-end: the summary must print the text, not point at nothing.

    Before: "1 warning logged above — some content may not have been written",
    with nothing above it and no way to learn what happened.
    """
    from pathlib import Path
    from unittest.mock import patch

    from click.testing import CliRunner

    from cp_engine.cli_cmds.core import sync
    from cp_engine.sync import SyncResult

    result = SyncResult(
        projects_seen=37,
        files_written=(),
        files_deactivated=(),
        no_op=True,
        warnings=1,
        warning_messages=(
            "[cp_engine.spine_substance_sync] authored-live shield (#113): "
            "element e94d0a03 disk v6 claims live",
        ),
    )

    class _Cfg:
        root = Path("/tmp")

    with (
        patch("cp_engine.cli_cmds.core.load", return_value=_Cfg()),
        patch("cp_engine.cli_cmds.core.sync_tenant", return_value=result),
    ):
        out = CliRunner().invoke(sync, []).output

    assert "authored-live shield" in out, "the actual warning must be printed"
    assert "spine_substance_sync" in out, "naming the subsystem is the point"
    assert "logged above" not in out, "must not point at output that isn't there"


def test_cli_reports_truncation_rather_than_hiding_it():
    """When the retention cap bites, say how many are not shown."""
    from pathlib import Path
    from unittest.mock import patch

    from click.testing import CliRunner

    from cp_engine.cli_cmds.core import sync
    from cp_engine.sync import SyncResult

    result = SyncResult(
        projects_seen=5,
        files_written=(),
        files_deactivated=(),
        no_op=True,
        warnings=25,
        warning_messages=tuple(f"[cp_engine.sync] w{i}" for i in range(20)),
    )

    class _Cfg:
        root = Path("/tmp")

    with (
        patch("cp_engine.cli_cmds.core.load", return_value=_Cfg()),
        patch("cp_engine.cli_cmds.core.sync_tenant", return_value=result),
    ):
        out = CliRunner().invoke(sync, []).output

    assert "and 5 more" in out, "silent truncation reads as full coverage"


def test_counted_but_unretained_warnings_do_not_dangle():
    """A count with no retained text must not print '… and N more' alone.

    An older SyncResult (or a counter that could not retain) yields warnings
    with an empty message tuple. Emitting a bare continuation line there would
    repeat exactly the fault #212 fixes: text referring to output that is not
    on screen.
    """
    from pathlib import Path
    from unittest.mock import patch

    from click.testing import CliRunner

    from cp_engine.cli_cmds.core import sync
    from cp_engine.sync import SyncResult

    result = SyncResult(
        projects_seen=3,
        files_written=(),
        files_deactivated=(),
        no_op=True,
        warnings=2,
    )

    class _Cfg:
        root = Path("/tmp")

    with (
        patch("cp_engine.cli_cmds.core.load", return_value=_Cfg()),
        patch("cp_engine.cli_cmds.core.sync_tenant", return_value=result),
    ):
        out = CliRunner().invoke(sync, []).output

    assert "and 2 more" not in out, "a continuation line with nothing above it"
    assert "detail unavailable" in out, "say the detail is missing, plainly"
