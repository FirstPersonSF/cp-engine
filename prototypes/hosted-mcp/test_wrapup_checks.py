"""The wrap-up checks, reachable from a hosted session (#280).

WHY THESE VERBS EXIST. A hosted session had `capture_project_state` — the verb
that WRITES an Exec Summary — and none of the checks that say whether the write
was any good. `spine-lint`, `commitments-sweep` and `seal-sweep` were CLI-only,
so a teammate working through cp-hosted could refresh a summary and had no way
to ask whether the project's spine was sound, what was owed, or what needed
sealing.

Measured on ggl-5136, 2026-09-17: its Exec Summary carried a current `Status`
and a 09-15 stamp while `Next up` listed three deadlines from July and named a
collaborator the Status line said had left. A stamp-only refresh is worse than
none, because the staleness check reads the stamp.

THE CONSTRAINT WAS NEVER TECHNICAL. `spine_lint`, `seal_sweep` and
`commitments_sweep` contain ZERO `Path`/`read_text`/`open()` references between
them — they are pure functions over rows. Only the ASSEMBLY (which tables,
which columns, the one-live-per-element discipline) lived in the CLI command
bodies. `run_all_lints` was extracted so both surfaces share it rather than
drifting, which is the #172/#178 lesson.

These tests assert the contract at the seam — serialization shape and the
shared-assembly guarantee — rather than booting a server.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def server(monkeypatch):
    """The module, with the env it refuses to import without.

    Same shape as `test_health.py`: the server exits at import when its
    credentials are absent, which is correct for a deployed service and means
    every test that touches it needs stub values.
    """
    for k, v in {
        "MC2_API_BASE": "http://example.invalid",
        "SUPABASE_URL": "http://example.invalid",
        "SUPABASE_ANON_KEY": "x",
    }.items():
        monkeypatch.setenv(k, v)
    import server as mod

    return mod


# ──────────────────────────────────────────────────────────────────────
#  The shared assembly — one implementation, two callers
# ──────────────────────────────────────────────────────────────────────


def test_run_all_lints_is_the_single_assembly():
    """THE POINT OF #280. The CLI must not carry its own copy.

    Two copies of "which tables, which columns, which checks" drift — that is
    what #172 (`Output` vs `Deliverables`) and #178 cost. The CLI command body
    should call `run_all_lints`, not rebuild the query.
    """
    from pathlib import Path

    cli = Path(__file__).resolve().parents[2] / "src/cp_engine/cli_cmds/spine.py"
    src = cli.read_text(encoding="utf-8")
    assert "run_all_lints(client, codes" in src, (
        "the CLI should delegate to the shared assembly"
    )
    # The old inline query must be gone, or it is still a second copy.
    assert src.count("SPINE_LINT_COLUMNS") == 0, (
        "the CLI still builds its own spine-lint query"
    )


def test_the_lint_functions_touch_no_filesystem():
    """The premise these verbs rest on, asserted rather than assumed.

    If a check ever grows a `Path` or an `open()`, it stops being hostable and
    this package quietly becomes CLI-only again — with no test to notice.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src/cp_engine"
    for module in ("spine_lint.py", "seal_sweep.py", "commitments_sweep.py"):
        src = (root / module).read_text(encoding="utf-8")
        # `run_all_lints` takes cp.md TEXT, never a path — that is the seam.
        for banned in ("read_text(", "open(", "Path("):
            assert banned not in src, (
                f"{module} gained {banned!r} — it is no longer a pure "
                "row-level function, and the hosted verb wrapping it will "
                "fail or silently degrade"
            )


# ──────────────────────────────────────────────────────────────────────
#  Serialization — dataclasses out, JSON in
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _Row:
    id: str = "c1"
    description: str = "Send the estimate"
    owner: str = "Drew"
    source_kind: str = "meeting_ingest"
    due_date: date | None = None
    date_status: str = "undated"
    age_days: int = 13
    ttl: str | None = "warn"


def test_a_commitment_row_serializes_its_expiry_clock(server):
    """`ttl` and `undated` are the fields the wrap-up ritual acts on.

    An UNDATED commitment expires at 14 days. A serializer that dropped `ttl`
    would turn "expires tomorrow" into an ordinary open row.
    """
    out = server._sweep_row_dict(_Row())
    assert out["undated"] is True
    assert out["ttl"] == "warn"
    assert out["age_days"] == 13
    assert out["due_date"] is None


def test_a_dated_commitment_serializes_an_iso_string(server):
    """JSON has no date type; the caller must still be able to sort."""
    out = server._sweep_row_dict(_Row(due_date=date(2026, 9, 17), date_status="due"))
    assert out["due_date"] == "2026-09-17"
    assert out["undated"] is False


@dataclass
class _Cand:
    est_item_id: str = "_authored/brief"
    framing: str = "Inputs & Briefing"
    layer: str = "Brief"
    kinds: list | None = None
    via: str = ""

    def __post_init__(self):
        if self.kinds is None:
            self.kinds = ["reference"]


@dataclass
class _Round:
    est_item_id: str = "_authored/deck"
    framing: str = "The deck"
    version_label: str = "v3"
    version_date: date | None = date(2026, 9, 1)
    candidates: list | None = None
    already_absorbed: int = 2

    def __post_init__(self):
        if self.candidates is None:
            self.candidates = [_Cand(), _Cand(via="_authored/workshop")]


def test_a_seal_round_preserves_via_per_candidate(server):
    """`via` is the difference between direct and indirect evidence.

    An indirect candidate reached the deliverable THROUGH an activity — real,
    because that is how routing works, but weaker: the human bound the source
    to an activity, not to this deliverable. A caller deciding what to seal has
    to see which is which, or it asserts provenance that did not happen.
    """
    out = server._round_dict(_Round())
    assert out["version_date"] == "2026-09-01"
    assert out["already_absorbed"] == 2
    vias = [c["via"] for c in out["candidates"]]
    assert "" in vias and "_authored/workshop" in vias


def test_a_round_with_no_candidates_is_not_an_error(server):
    """A shipped deliverable that consumed nothing traceable is a real state —
    it reports an empty list, not a failure."""
    out = server._round_dict(_Round(candidates=[], version_date=None))
    assert out["candidates"] == []
    assert out["version_date"] is None


# ──────────────────────────────────────────────────────────────────────
#  Tier 2 — word-count REPORTING, not rotation
# ──────────────────────────────────────────────────────────────────────


def test_word_count_matches_the_cli_on_the_same_text():
    """The hosted verb must not become a second implementation.

    Both surfaces call `lint_word_count(text, label)`; the only difference is
    where the text came from. Measured on the live ibx-5153 cp.md, 2026-09-17:
    3,242 words, both paths.
    """
    from cp_engine.word_count_lint import lint_word_count

    text = "word " * 2600
    findings = lint_word_count(text, "x/cp.md")
    assert findings, "2,600 words should trip the 2,500 audit threshold"
    assert "2,600 words" in findings[0]


def test_word_count_is_silent_under_the_threshold():
    """A clean file produces nothing — the discipline is warn-only and a
    warning on every project would be noise, not signal."""
    from cp_engine.word_count_lint import lint_word_count

    assert lint_word_count("word " * 100, "x/cp.md") == []


def test_the_hosted_verb_reports_and_does_not_rotate(server):
    """THE BOUNDARY. Rotation is a WRITE — two files in one commit — and this
    server holds no write access by construction.

    A verb that quietly rotated would be the write-deploy-key shortcut arriving
    through the back door, which #280 argues against on attribution grounds.
    """
    import inspect

    src = inspect.getsource(server.word_count_check)
    assert "REPORTING ONLY" in src
    for banned in ("write_text", "_commit_with_message", "rotate"):
        assert banned not in src, (
            f"word_count_check gained {banned!r} — it must report, not write"
        )


def test_every_tool_is_defined_before_the_main_guard():
    """THE PRODUCTION CONTROL. A verb after `if __name__ == "__main__"` is
    dead code in the container and invisible in a dev import.

    Measured 2026-09-17: four verbs appended to the end of this file passed
    every local test and deployed cleanly, and `/health` still reported the
    old tool count. In the container the process runs AS `__main__`, so
    execution enters the guard, calls `main()`, blocks serving requests, and
    NEVER reaches a definition below it. The test suite imported the module as
    `server` — where the guard is false and the whole file executes — so the
    one condition that mattered was the one nothing exercised.

    Two deploys were spent looking for a stale tarball and a missing
    dependency before the line numbers were compared.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parent / "server.py").read_text(encoding="utf-8")
    lines = src.split("\n")
    guard = next(
        i for i, l in enumerate(lines) if l.startswith('if __name__ == "__main__"')
    )
    after = [
        (i + 1, l)
        for i, l in enumerate(lines[guard:], start=guard)
        if l.startswith("@mcp_server.tool()")
    ]
    assert not after, (
        f"{len(after)} tool(s) declared after the __main__ guard at line "
        f"{guard + 1} — they will not register in the container: "
        f"{[n for n, _ in after]}"
    )


# ──────────────────────────────────────────────────────────────────────
#  Discoverability — the instructions are the only unprompted surface
# ──────────────────────────────────────────────────────────────────────


def test_the_server_instructions_point_at_the_tenant_protocol(server):
    """AVAILABLE IS NOT DISCOVERABLE.

    A Claude Code session gets the tenant protocol from the plugin and the
    checkout. A session reaching this server from the Claude app has NEITHER —
    plugins do not reach that client, so `/cp-wrapup` and the trigger table it
    lives in are simply absent.

    Measured 2026-09-17: `CLAUDE.md` was reachable through `read_project_file`
    and mentioned NOWHERE in server.py, so a hosted caller had to already know
    the path to find the protocol. `instructions` is the one surface every
    session sees before any tool call, which makes it the only place that
    closes the gap without a plugin.
    """
    text = server.mcp_server.instructions
    assert "CLAUDE.md" in text, (
        "the instructions do not name the tenant protocol file — a hosted "
        "session has no other way to learn it exists"
    )
    assert "read_project_file" in text, (
        "the instructions name the file but not the verb that reads it"
    )


def test_the_instructions_carry_the_partial_refresh_warning(server):
    """The ggl-5136 lesson, on the surface that reaches every session.

    A status-only refresh advances the `· updated` stamp while the rest goes
    stale — and the staleness check reads that stamp, so it is worse than not
    refreshing at all. This is the single most costly thing a hosted session
    can get wrong, and it must not depend on the caller having read CLAUDE.md.
    """
    text = server.mcp_server.instructions
    assert "status" in text and "stale" in text, (
        "the instructions omit the partial-refresh warning"
    )


def test_the_instructions_state_the_real_size_of_the_write_surface(server):
    """It says what it does, and the NUMBER has to be true.

    Two wrong versions preceded this one. "Read-only prototype" was false and
    taught callers not to expect the verbs that make a wrap-up durable.
    "Mostly read, but not read-only" replaced a false claim with a vague one
    and cited two writers when there are seventeen — implying a smaller blast
    radius than the server has, so `create_commitment` or `set_spine_element`
    could read as safe.

    The count is asserted against the AST rather than trusted, because a
    hardcoded number in prose rots the first time somebody adds a verb.
    """
    import ast
    import re
    from pathlib import Path

    text = server.mcp_server.instructions
    assert "Read-only prototype" not in text, (
        "instructions claim read-only; 17 tools write"
    )

    src = (Path(__file__).resolve().parent / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    writers = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        is_tool = any(
            isinstance((d.func if isinstance(d, ast.Call) else d), ast.Attribute)
            and (d.func if isinstance(d, ast.Call) else d).attr == "tool"
            for d in node.decorator_list
        )
        if not is_tool:
            continue
        body = ast.dump(node)
        if any(k in body for k in ("'insert'", "'update'", "'upsert'", "'delete'", "call_mc2_")):
            writers.add(node.name)

    stated = re.search(r"(\d+)\s+OF THEM WRITE", text)
    assert stated, "the instructions no longer state how many tools write"
    assert int(stated.group(1)) == len(writers), (
        f"instructions say {stated.group(1)} tools write; the server has "
        f"{len(writers)} — update the text: {sorted(writers)}"
    )


# ──────────────────────────────────────────────────────────────────────
#  #285 — registration is not execution
# ──────────────────────────────────────────────────────────────────────


def test_the_probe_executes_every_cp_engine_import_the_file_makes(server):
    """THE GAP #285 NAMES. `tool_count` proves registration, not callability.

    Three verbs raised on every call for a day while `/health` reported
    `healthy, 57 tools`. The probe must cover every `cp_engine` import in
    `server.py` — a probe that samples would have reported healthy, because
    the one verb that looked fine (`word_count_check`) was the one that
    returned before reaching its import.
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parent / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("cp_engine"):
            if node.module == "cp_engine":
                imported.update(f"cp_engine.{a.name}" for a in node.names)
            else:
                imported.add(node.module)

    ok, rows = server.dependency_probe()
    probed = {r["dep"].split(":")[0] for r in rows}
    missing = imported - probed
    assert not missing, (
        f"server.py imports {sorted(missing)} but the probe never exercises "
        "them — they can break in the container while /health reports healthy"
    )


def test_the_probe_reaches_the_constants_not_just_the_imports(server):
    """The THIRD costume of the same bug.

    `mc2_db.SPINE_LINT_COLUMNS` is read while BUILDING a query — import-clean,
    AttributeError on first call. That shipped one deploy after the import fix
    because the check then in place only resolved module names.
    """
    _, rows = server.dependency_probe()
    deps = {r["dep"] for r in rows}
    assert "cp_engine.mc2_db:SPINE_LINT_COLUMNS" in deps
    assert "cp_engine.mc2_db:Tables" in deps


def test_the_probe_reports_rather_than_raises(server):
    """A probe that throws takes `/health` down with it, turning a diagnostic
    into an outage. Every failure is caught and reported as a row."""
    import inspect

    src = inspect.getsource(server.dependency_probe)
    assert "except Exception" in src
    ok, rows = server.dependency_probe()
    assert isinstance(ok, bool) and isinstance(rows, list)
    assert all("dep" in r and "ok" in r for r in rows)


def test_the_build_fingerprint_covers_the_vendor_tree(server):
    """`commit` is "unknown" on a `railway up` deploy, so the fingerprint is
    the only answer to "is the container what the repo is".

    It must hash `vendor/` as well as `server.py` — #283's fix lived entirely
    in `vendor/`, so a fingerprint over server.py alone would not have moved
    across the deploy that fixed it.
    """
    import inspect

    src = inspect.getsource(server.build_fingerprint)
    assert "vendor" in src, "the fingerprint ignores the vendored closure"
    first = server.build_fingerprint()
    assert isinstance(first, str) and len(first) == 12
    assert first == server.build_fingerprint(), "fingerprint must be stable"


def test_health_degrades_rather_than_lying(server):
    """`status` must not say "healthy" when the verbs cannot run.

    Railway's probe still gets a 200 — the process IS up — but a human reading
    the payload has to be able to tell the difference, which is the entire
    complaint in #285.
    """
    import ast
    import inspect

    # Assert on the CODE, not the source text: "degraded" also appears in the
    # docstring, so a substring check passes against a handler hardcoded to
    # "healthy" — which is the exact defect this test exists to catch.
    tree = ast.parse(inspect.getsource(server.health).lstrip())
    literals = {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    docstring = ast.get_docstring(tree.body[0]) or ""
    literals.discard(docstring)
    assert "degraded" in literals, (
        "/health never yields the string 'degraded' outside its docstring — "
        "it reports healthy whatever the dependency probe says"
    )
    src = inspect.getsource(server.health)
    assert "deps_ok" in src and "build" in src


# ──────────────────────────────────────────────────────────────────────
#  set_commitment_date — the disposition that had no verb
# ──────────────────────────────────────────────────────────────────────


def test_dating_a_commitment_has_a_verb(server):
    """THE GAP. Every other move had one: create, resolve, drop, route, list,
    sweep. Dating did not — so `commitments-sweep` flagged a row as needing a
    date and offered no way to give it one. The only dispositions were to
    close it or let the TTL expire it."""
    assert hasattr(server, "set_commitment_date")


def test_it_refuses_an_unparseable_date(server):
    """An invented deadline is worse than an undated row, which at least flags
    itself as needing one."""
    for bad in ("next Thursday", "2026-13-01", "09/24/2026", ""):
        out = server.set_commitment_date("x", "y", bad)
        assert "error" in out and "ISO" in out["error"], bad


def test_a_caller_cannot_stamp_slipped(server):
    """`slipped` is the dates loop's verdict on a PAST-DUE row, not a caller's
    to assert. Letting one be set by hand would put a judgement in the column
    the loop uses to make that judgement."""
    out = server.set_commitment_date("x", "y", "2026-09-24", date_status="slipped")
    assert "error" in out
    assert "dates loop" in out["error"]


def test_it_goes_through_mc2_rather_than_writing_the_column(server):
    """THE REASON THIS IS NOT A DIRECT WRITE.

    A due_date change must reset `posted_count` to 0 and return `date_status`
    to `proposed`: the loop promotes proposed → agreed after two posts at an
    UNCHANGED date. Writing the column directly leaves a stale count against a
    new date, so a row can auto-ratify a date nobody posted twice. mc-2's PATCH
    owns that rule; restating it here would be a second copy of a
    ratification rule.
    """
    import inspect

    src = inspect.getsource(server.call_mc2_set_commitment_date)
    assert "/api/commitments/" in src
    assert "httpx.patch" in src
    # It must not reimplement the reset. The precise claim is that the helper
    # never ASSIGNS posted_count — reading it back off mc-2's response is fine
    # and useful; setting it here would be a second copy of a ratification
    # rule, which is how two systems come to disagree about what was agreed.
    import ast

    tree = ast.parse(src.lstrip())
    assigned: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Subscript) and isinstance(tgt.slice, ast.Constant):
                    assigned.add(str(tgt.slice.value))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            for kw in node.keywords:
                if kw.arg:
                    assigned.add(kw.arg)
    assert "posted_count" not in assigned, (
        "the helper assigns posted_count — that reset rule belongs to mc-2's "
        "PATCH handler, which owns the ratification contract"
    )


def test_it_reuses_the_shared_matcher(server):
    """One matching truth across the commitment verbs. A second `key`
    resolution order would make the same string mean different rows in
    `resolve_commitment` and here."""
    import inspect

    src = inspect.getsource(server.set_commitment_date)
    assert "_match_open_commitment" in src
    assert "_fetch_open_commitments" in src
