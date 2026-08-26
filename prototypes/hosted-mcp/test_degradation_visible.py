"""Degradation must be visible in the response, not just in a log line.

Audit of 2026-08-26, prompted by the tenant tree sitting nine days stale
beside a live database with nothing in the response saying so. The choice to
keep serving on failure is almost always right; the bug is that the degraded
response is shaped exactly like a healthy one, so the caller cannot tell
"nothing matched" from "the backend was down".

These are source-shape assertions, not behavioral tests. The paths they guard
need a live Supabase + a stale git remote to exercise for real, so what is
checkable here is that the code still *reports* what it could not do. That is
the property that regressed, and it is the one worth pinning.

    python -m pytest prototypes/hosted-mcp/test_degradation_visible.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = (Path(__file__).parent / "server.py").read_text()


def body_of(func: str) -> str:
    """Source of one top-level def, up to the next one."""
    start = SRC.index(f"def {func}(")
    rest = SRC[start:]
    nxt = re.search(r"\n(?:@mcp_server\.tool\(\)\n)?def ", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest


# ── reads: which commit, and is it current ───────────────────────────

def test_tree_provenance_reports_head_and_staleness():
    fn = body_of("tree_provenance")
    assert '"tree_head"' in fn
    # `tree_head` alone is not enough: a frozen mirror still returns a
    # plausible SHA forever. `tree_stale` is the field a caller branches on.
    assert '"tree_stale"' in fn
    assert '"tree_error"' in fn


def test_tree_root_records_refresh_outcome():
    fn = body_of("tree_root")
    assert 'refresh_ok"] = False' in fn, "a failed refresh must be recorded"
    assert 'refresh_ok"] = True' in fn, "a good refresh must clear the flag"


@pytest.mark.parametrize("reader", ["read_project_file", "get_project_state"])
def test_both_tree_readers_carry_provenance(reader: str):
    """The first fix reached only one of the two. get_project_state is the
    orientation call and the more dangerous omission."""
    assert "tree_provenance()" in body_of(reader), f"{reader} serves files blind"


# ── reads: empty-because-nothing vs empty-because-broken ─────────────

def test_spine_annotation_failure_is_reported():
    """`absorbed_into` gates a `continue`, so a failed edge read un-hides every
    sealed element. Silence there reads as "nothing was ever sealed"."""
    fn = body_of("list_spine_elements")
    assert "annotations_available" in fn
    assert "annotations_error" in fn


def test_semantic_search_reports_a_dead_spine_lookup():
    """`spine_context: []` on failure is byte-identical to a genuine no-match,
    and silently regresses the caller to pre-mig-146 chunk-only search."""
    fn = body_of("semantic_search")
    assert "spine_available" in fn
    assert "spine_error" in fn


# ── writes: counted iterations are not counted effects ───────────────

def test_reorder_counts_matched_rows_not_loop_passes():
    """The spine_steps UPDATE policy matches only source='auto' AND
    review='proposed', so human steps refuse renumbering with a 0-row 200 —
    not an exception. Counting passes reports a reorder that did not happen."""
    fn = body_of("reorder_spine_step")
    assert "if result.data:" in fn, "must branch on matched rows"
    assert "refused" in fn
    # The verb rejects partial reorders on input; it must not silently create
    # one on output.
    assert "PARTIALLY" in fn or "partially" in fn


def test_densify_is_verified_and_scoped():
    fn = body_of("remove_spine_step")
    assert "densify_refused" in fn
    # Every other step write carries these guards; this one had drifted.
    assert fn.count('.eq("project_id"') >= 2
    assert fn.count('.eq("est_item_id"') >= 2


def test_auto_step_retitle_is_verified():
    fn = body_of("upsert_auto_step")
    assert '"updated": retitled' in fn, "must not assert updated:True blindly"


# ── the private key ──────────────────────────────────────────────────

def test_ssh_key_is_written_once():
    """tree_root() calls this on every invocation; a fresh mkdtemp each time
    left 0600 key copies accumulating on a long-lived container."""
    fn = body_of("tree_ssh_env")
    assert 'ssh_key_path' in fn, "key path must be cached"
    # Strip the docstring — it names mkdtemp when explaining the leak.
    code = fn[fn.index('"""', fn.index('"""') + 3) + 3:]
    assert code.count("mkdtemp") == 1, "the key must be written on one path only"
    assert "if cached and Path(cached).exists():" in code


# ── the convention itself ────────────────────────────────────────────

def test_no_bare_pass_on_a_swallowed_read():
    """`except Exception: pass` is the shape this whole audit is about — it
    keeps serving and records nothing, not even a log line. New ones should
    carry at minimum a log call; ideally a response field.

    Not a blanket ban: this asserts the count does not GROW past what the
    2026-08-26 audit left behind.
    """
    bare = len(re.findall(r"except Exception:\s*#[^\n]*\n\s*pass\b", SRC))
    bare += len(re.findall(r"except Exception:\s*\n\s*pass\b", SRC))
    assert bare <= 6, (
        f"{bare} bare swallow-and-continue sites (was 6 at the 2026-08-26 "
        "audit) — a new one should report what it could not do"
    )


# ── observability: the operator-facing half ──────────────────────────

def test_swallowed_failures_route_to_an_alert():
    """A log line nobody reads is not observability.

    The tenant tree was frozen for nine days while `tenant tree pull failed`
    fired correctly on every single read. Response-level provenance (above)
    serves a caller who looks; `capture()` serves an operator who isn't
    looking. Both halves are needed.
    """
    for func, area in [
        ("tree_root", "tree_refresh"),
        ("list_spine_elements", "spine_annotations"),
        ("semantic_search", "spine_context_lookup"),
        ("audit", "audit_log_write"),
    ]:
        fn = body_of(func)
        assert "observability.capture(" in fn, f"{func} swallows without alerting"
        assert area in fn, f"{func} should tag area={area}"


def test_sentry_is_dsn_gated_and_says_which():
    """Alerting that is silently off is worse than none — an operator would
    assume coverage. main() must state the mode."""
    obs = (Path(__file__).parent / "observability.py").read_text()
    assert 'os.environ.get("SENTRY_DSN")' in obs
    assert "if not dsn:\n        return False" in obs
    main = body_of("main")
    assert "error alerting" in main
    assert "DISABLED" in main


def test_correlation_middleware_is_registered():
    assert "middleware=[observability.correlation_middleware]" in SRC


def test_observability_ships_in_the_image():
    """server.py imports it at module scope; the Dockerfile copied only
    server.py, so this would have crashed the container at startup."""
    dockerfile = (Path(__file__).parent / "Dockerfile").read_text()
    assert "observability.py" in dockerfile
    reqs = (Path(__file__).parent / "requirements.txt").read_text()
    assert "sentry-sdk" in reqs


def test_capture_never_raises():
    """Called from except blocks — if it could raise it would convert a
    degraded path into a broken one."""
    obs = (Path(__file__).parent / "observability.py").read_text()
    capture = obs[obs.index("def capture("):obs.index("# DIFF 3")]
    assert "except Exception:" in capture
    assert "pass" in capture
