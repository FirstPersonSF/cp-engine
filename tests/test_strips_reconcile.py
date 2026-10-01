"""Engine strips reconcile against current truth (cp-engine #323).

Three defects, one shape — a strip that records an event and never re-checks it:

a. ``inbound-strip`` kept listing new-source announcements for sources since
   archived, superseded or deleted in MC-2.
b. The cp.md ``current-sprint`` header counted a different set of asks than the
   list beneath it ("Open client asks (0)" above a non-empty list), and closing
   an ask never moved the count.
c. Snooze hid an item from the attention digest while every other surface kept
   rendering it, because no contract said what snooze affects. The contract now
   lives in ``cp_engine.snooze``; each surface it names has a test here.

Sprint files are built from the committed scaffold golden and mutated with the
REAL writers (``_write_ask``, ``_write_snooze``, ``announce_new_sources`` …), so
the bullets under test are the bytes production writes, not a hand-typed guess.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from cp_engine.aggregators import (
    aggregate_project_strips,
    carry_forward_rollup,
    normalize_source_title,
    open_client_asks,
)
from cp_engine.ingest import (
    _write_ask,
    _write_close_ask,
    _write_inbound,
    _write_risk,
    _write_snooze,
    announce_new_sources,
)
from cp_engine.render import render_project_strip_bodies
from cp_engine.snooze import is_snoozed, snoozed_until
from cp_engine.sprints import (
    compute_carry_forward,
    ensure_sprint_file,
    parse_sprint_file,
    render_current_sprint_block,
    render_sprint_index,
)

from tests.test_sprints import _fixture_project

GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "sprints" / "scaffold-engagement.md"
CODE = "peb-5100"
TODAY = date(2026, 5, 20)
LINK = "../../sprints/2026-W20/peb-5100.md"


def _sprint(tmp_path: Path, week: str = "2026-W20") -> Path:
    """The scaffold golden on disk: empty own sections, one carried ask
    (`Volume forecast from ops team`, asked 2026-05-04) and one carried
    escalated risk."""
    p = tmp_path / "sprints" / week / f"{CODE}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    # Step 4a: the engine no longer renders carried asks, but files written
    # before it still hold `[ask · …]` rows in `carry-forward`, and every
    # surface still reads them until that week is re-rendered. Seed that
    # legacy shape so these reader tests keep covering it.
    body = re.sub(
        r"(## Carried over from [^\n]*\n)",
        r"\1- [ask · 2026-05-04 · Maria] Volume forecast from ops team\n",
        GOLDEN.read_text(), count=1,
    )
    p.write_text(body)
    return p


def _hash_of(path: Path, needle: str) -> str:
    for line in path.read_text().splitlines():
        if needle in line:
            m = re.search(r"cp:hash=([0-9a-f]{8})", line)
            assert m, line
            return m.group(1)
    raise AssertionError(f"{needle!r} not in {path}")


def _asks_block(block: str) -> list[str]:
    """The bullet lines under the current-sprint asks header."""
    lines = block.splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith("**Open client asks**"))
    out = []
    for ln in lines[i + 1:]:
        if not ln.startswith("- "):
            break
        out.append(ln)
    return out


def _header_count(block: str, label: str) -> str:
    m = re.search(rf"\*\*{re.escape(label)}\*\* \(([^)]*)\):", block)
    assert m, block
    return m.group(1)


# ──────────────────────────────────────────────────────────────────────
#  (b) one definition of "open asks" — count and list agree
# ──────────────────────────────────────────────────────────────────────


def test_current_sprint_count_includes_the_carried_ask_it_lists(tmp_path: Path) -> None:
    """The live ggl-5197 shape: the only open ask was carried from last week.
    The header counted the own section (0) while the list fell back to the
    carried ask — "Open client asks (0)" above a one-item list."""
    sf = parse_sprint_file(_sprint(tmp_path))
    block = render_current_sprint_block(sf, link_path=LINK, today=TODAY)
    listed = _asks_block(block)
    assert len(listed) == 1 and "Volume forecast" in listed[0]
    assert _header_count(block, "Open client asks") == "1"


def test_closed_ask_leaves_the_count(tmp_path: Path) -> None:
    """Resolutions never flowed back: the header counted every bullet in
    `### Open asks`, so closing one left the number where it was."""
    path = _sprint(tmp_path)
    _write_ask(CODE, {"text": "Brand guide PDF", "who": "Maria", "date": "2026-05-18"}, path)
    _write_close_ask(CODE, {"hash": _hash_of(path, "Brand guide PDF")}, path)
    sf = parse_sprint_file(path)
    assert [a.status for a in sf.client_open_asks] == ["closed"]  # the record keeps it
    block = render_current_sprint_block(sf, link_path=LINK, today=TODAY)
    assert _header_count(block, "Open client asks") == "1"  # the carried one only
    assert "Brand guide PDF" not in block


def test_resolution_in_the_owning_week_flows_to_the_next_weeks_count(tmp_path: Path) -> None:
    """End to end through the real renderer. Before step 4a an ask written in
    W19 was carried into W20 and only a close in W19 dropped it. Asks now live
    in MC-2: W19's bullet never carries, so W20 counts nothing from it (the
    MC-2 import + render path is tests/test_asks_mc2.py)."""
    root = tmp_path / "sprints"

    def render(week: str, prior: str | None, start: str, end: str) -> Path:
        return ensure_sprint_file(
            project=_fixture_project(code=CODE),
            sprint_root=root, week_iso=week, week_label=week[-3:],
            week_start=start, week_end=end, prior_sprint=prior,
            last_sprint_hours_line=None, sessions_this_week=0,
            last_session_date=None, last_session_who=None, last_session_summary=None,
            recent_commits=(), open_issues=(),
        )

    w19 = render("2026-W19", None, "2026-05-04", "2026-05-10")
    _write_ask(CODE, {"text": "Signed SOW", "who": "Sam", "date": "2026-05-06"}, w19)
    w20 = render("2026-W20", "2026-W19", "2026-05-11", "2026-05-17")
    block = render_current_sprint_block(parse_sprint_file(w20), link_path=LINK, today=TODAY)
    assert _header_count(block, "Open client asks") == "0"
    assert "Signed SOW" not in block

    _write_close_ask(CODE, {"hash": _hash_of(w19, "Signed SOW")}, w19)
    render("2026-W20", "2026-W19", "2026-05-11", "2026-05-17")
    block = render_current_sprint_block(parse_sprint_file(w20), link_path=LINK, today=TODAY)
    assert _header_count(block, "Open client asks") == "0"
    assert "Signed SOW" not in block


def test_open_asks_strip_lists_the_carried_ask(tmp_path: Path) -> None:
    """The open-asks strip read only the own section and said "No open asks"
    beside a current-sprint block listing one."""
    sf = parse_sprint_file(_sprint(tmp_path))
    body = render_project_strip_bodies(aggregate_project_strips(CODE, (sf,), TODAY))["open-asks-strip"]
    assert "Volume forecast from ops team" in body
    assert "No open asks" not in body
    assert "16d stale" in body


def test_rollup_stale_asks_include_the_carried_ask(tmp_path: Path) -> None:
    """master-cp.md's agenda rollup read only the own section — so it missed
    the carried asks, which are by construction the stale ones."""
    sf = parse_sprint_file(_sprint(tmp_path))
    rollup = carry_forward_rollup((sf,), TODAY)
    assert [a["text"] for a in rollup["stale_asks"]] == ["Volume forecast from ops team"]


def test_every_ask_surface_counts_the_same_set(tmp_path: Path) -> None:
    """Consistency across surfaces: current-sprint header, sprint-index row and
    open-asks strip all agree, with an own ask, a closed ask and a restated
    carried ask (counted once) in the file."""
    path = _sprint(tmp_path)
    _write_ask(CODE, {"text": "Brand guide PDF", "who": "Maria", "date": "2026-05-18"}, path)
    _write_ask(CODE, {"text": "Old ask", "who": "Maria", "date": "2026-05-18"}, path)
    _write_close_ask(CODE, {"hash": _hash_of(path, "Old ask")}, path)
    # A hand-restated copy of the carried ask (no hash) must not double-count.
    body = path.read_text().replace(
        "### Inbound",
        "- [open · 2026-05-04 · Maria] Volume forecast from ops team\n\n### Inbound",
        1,
    )
    path.write_text(body)
    sf = parse_sprint_file(path)
    expected = len(open_client_asks(sf))
    assert expected == 2
    block = render_current_sprint_block(sf, link_path=LINK, today=TODAY)
    assert _header_count(block, "Open client asks") == str(expected)
    assert len(_asks_block(block)) == expected
    index = render_sprint_index(week_iso="2026-W20", week_dates="x", sprint_files=[sf])
    assert f"| `{CODE}` | " in index and f" | {expected} | " in index
    strip = aggregate_project_strips(CODE, (sf,), TODAY)
    assert len(strip.open_asks) == expected


# ──────────────────────────────────────────────────────────────────────
#  (a) inbound strip reconciles against the live source store
# ──────────────────────────────────────────────────────────────────────


def _announce(path: Path, titles: list[str], *, comments: int = 0) -> None:
    assets = [
        {"title": t, "created_at": "2026-05-18T10:00:00Z", "source_type": "doc",
         "comment_count": comments}
        for t in titles
    ]
    assert announce_new_sources(CODE, path, assets, today=TODAY) == len(titles)


def _inbound_texts(strips) -> list[str]:
    return [ib.text for ib in strips.inbound]


def test_inbound_strip_drops_announcements_for_sources_no_longer_live(tmp_path: Path) -> None:
    path = _sprint(tmp_path)
    _announce(path, ["Brief v03", "Brief v03 - Copy", "COPY ME template (draft)"])
    _write_inbound(CODE, {"text": "Maria confirmed the shoot", "who": "Maria",
                          "date": "2026-05-18"}, path, today=TODAY)
    sf = parse_sprint_file(path)
    live = frozenset({normalize_source_title("Brief v03")})
    texts = _inbound_texts(aggregate_project_strips(CODE, (sf,), TODAY, live_source_titles=live))
    assert any("Brief v03 (doc)" in t for t in texts)
    assert not any("- Copy" in t for t in texts)
    assert not any("COPY ME" in t for t in texts)
    assert any("Maria confirmed the shoot" in t for t in texts)  # hand-written: never filtered
    # The record is untouched — the sprint file still says it was ingested.
    assert "Brief v03 - Copy" in path.read_text()


def test_inbound_strip_matches_titles_with_parens_and_comment_counts(tmp_path: Path) -> None:
    """Titles are parsed back out of the writer's own sentence; a title that
    contains parentheses, or an announcement carrying the reviewer-comments
    clause, must still match its live source."""
    path = _sprint(tmp_path)
    _announce(path, ["Deck (Round 2)  final"], comments=4)
    sf = parse_sprint_file(path)
    live = frozenset({normalize_source_title("Deck (Round 2)  final")})
    assert len(aggregate_project_strips(CODE, (sf,), TODAY, live_source_titles=live).inbound) == 1
    assert len(aggregate_project_strips(CODE, (sf,), TODAY, live_source_titles=frozenset()).inbound) == 0


def test_inbound_strip_unchanged_when_the_store_was_unreachable(tmp_path: Path) -> None:
    """``None`` = no live list this run. An unreachable MC-2 must never blank
    the strip."""
    path = _sprint(tmp_path)
    _announce(path, ["Brief v03", "Brief v03 - Copy"])
    sf = parse_sprint_file(path)
    assert len(aggregate_project_strips(CODE, (sf,), TODAY, live_source_titles=None).inbound) == 2


def test_sync_reconciles_the_inbound_strip_against_the_manifest(tmp_path: Path, monkeypatch) -> None:
    """Through ``sync_tenant``: sync 1 announces two sources; by sync 2 one
    has been archived (absent from ``list_sources``) and the cp.md strip drops
    it; on sync 3 the manifest pass fails and the strip keeps everything."""
    import cp_engine.asset_ingest as asset_ingest
    import cp_engine.project_sources as project_sources
    from cp_engine import sync_tenant

    from tests.test_spine_sync import _FakeClient
    from tests.test_sync import make_config
    from tests.test_sync_sources_manifest import _stub_folders
    from tests.test_sync_spine_integration import _make_engagement, _SpineBackend

    config = make_config(tmp_path)
    code = "ggl-5168"
    now = datetime(2026, 5, 20, 9, 0)
    asset = lambda t: {"id": t, "title": t, "source_type": "doc",  # noqa: E731
                       "created_at": "2026-05-19T10:00:00Z", "file_hash": t}
    live: dict = {"assets": [asset("Brief v03"), asset("Brief v03 - Copy")]}

    def fake_write(client, project_dir, *a, **k):
        if live["assets"] is None:
            raise RuntimeError("MC-2 unreachable")
        return list(live["assets"])

    monkeypatch.setattr(asset_ingest, "resolve_project_folders_by_id",
                        lambda client, mc_project_id: _stub_folders())
    monkeypatch.setattr(project_sources, "write_sources_manifest", fake_write)
    backend = lambda _: _SpineBackend(  # noqa: E731
        (_make_engagement(code, mc2_id="uuid-ggl-5168"),), _FakeClient())
    cp_path = tmp_path / "1p/google" / code / "cp.md"

    sync_tenant(config, backend_factory=backend, now=now)
    sync_tenant(config, backend_factory=backend, now=now)  # strips see the announcements
    strip = _cp_inbound(cp_path)
    assert "Brief v03 (doc)" in strip and "Brief v03 - Copy" in strip

    live["assets"] = [asset("Brief v03")]  # the copy was archived
    sync_tenant(config, backend_factory=backend, now=now)
    strip = _cp_inbound(cp_path)
    assert "Brief v03 (doc)" in strip
    assert "Brief v03 - Copy" not in strip

    live["assets"] = None  # manifest pass fails: keep every announcement
    sync_tenant(config, backend_factory=backend, now=now)
    assert "Brief v03 - Copy" in _cp_inbound(cp_path)


def _cp_inbound(cp_path: Path) -> str:
    body = cp_path.read_text()
    start = body.index("<!-- cp-engine:start inbound-strip -->")
    end = body.index("<!-- cp-engine:end inbound-strip -->")
    return body[start:end]


# ──────────────────────────────────────────────────────────────────────
#  (c) the snooze contract, surface by surface
# ──────────────────────────────────────────────────────────────────────


def _snoozed_file(tmp_path: Path, until: str = "2026-06-01") -> Path:
    """Own section: one live ask, one snoozed ask (listed FIRST, so a
    renderer that ignores snooze previews it first); one snoozed escalated
    risk. Plus the scaffold's carried ask and carried risk."""
    path = _sprint(tmp_path)
    _write_ask(CODE, {"text": "Snoozed ask", "who": "Maria", "date": "2026-05-01"}, path)
    _write_ask(CODE, {"text": "Live ask", "who": "Sam", "date": "2026-05-01"}, path)
    _write_snooze(CODE, {"hash": _hash_of(path, "Snoozed ask"), "until": until}, path,
                  bullet_kind="ask")
    _write_risk(CODE, {"text": "Snoozed risk", "severity": "escalated",
                       "category": "scope", "date": "2026-05-18"}, path)
    _write_snooze(CODE, {"hash": _hash_of(path, "Snoozed risk"), "until": until}, path,
                  bullet_kind="risk")
    return path


def test_snooze_window_resurfaces_on_the_until_date() -> None:
    text = "x <!-- cp:snoozed-until=2026-06-01 --> <!-- cp:hash=aaaaaaaa -->"
    assert is_snoozed(text, date(2026, 5, 31))
    assert not is_snoozed(text, date(2026, 6, 1))
    assert snoozed_until("x <!-- cp:snoozed-until=2026-13-45 -->") is None  # typo ≠ crash


def test_current_sprint_marks_snoozed_items_and_previews_live_ones_first(tmp_path: Path) -> None:
    """Summary strip: a snoozed ask/risk stays counted, is marked, and never
    takes a preview slot ahead of a live item. It used to render exactly like
    a live one while the digest hid it (#323)."""
    sf = parse_sprint_file(_snoozed_file(tmp_path))
    block = render_current_sprint_block(sf, link_path=LINK, today=TODAY)
    asks = _asks_block(block)
    assert _header_count(block, "Open client asks") == "3 · 1 snoozed"
    assert "Live ask" in asks[0] and "Volume forecast" in asks[1]
    assert asks[2].endswith("(snoozed until 2026-06-01)") and "Snoozed ask" in asks[2]
    assert "cp:snoozed-until" not in block  # lifted into the visible label
    assert _header_count(block, "Active risks") == "2 · 1 snoozed"
    risk_lines = [ln for ln in block.splitlines() if "risk" in ln.lower() and ln.startswith("- ")]
    assert any(ln.startswith("- Snoozed risk") and "(snoozed until 2026-06-01)" in ln
               for ln in risk_lines)


def test_current_sprint_expired_snooze_renders_live(tmp_path: Path) -> None:
    sf = parse_sprint_file(_snoozed_file(tmp_path, until="2026-05-20"))
    block = render_current_sprint_block(sf, link_path=LINK, today=TODAY)
    # An expired marker is inert: no label, no header note, normal ordering.
    assert "(snoozed until" not in block
    assert _header_count(block, "Open client asks") == "3"
    assert "Snoozed ask" in _asks_block(block)[0]


def test_open_asks_strip_marks_snoozed_and_drops_its_stale_flag(tmp_path: Path) -> None:
    sf = parse_sprint_file(_snoozed_file(tmp_path))
    body = render_project_strip_bodies(aggregate_project_strips(CODE, (sf,), TODAY))["open-asks-strip"]
    lines = [ln for ln in body.splitlines() if ln.startswith("- ")]
    snoozed = [ln for ln in lines if "Snoozed ask" in ln]
    assert len(snoozed) == 1
    assert snoozed[0].endswith("(snoozed until 2026-06-01)")
    assert "stale" not in snoozed[0] and "cp:snoozed-until" not in snoozed[0]
    assert lines[-1] == snoozed[0]  # after the live ones
    assert any("Live ask" in ln and "19d stale" in ln for ln in lines)


def test_rollup_omits_snoozed_stale_asks_and_escalated_risks(tmp_path: Path) -> None:
    """Escalation surface (master agenda, sprint-facts-strip counts, subtree
    carry-forward): a snoozed item is left out until its date."""
    sf = parse_sprint_file(_snoozed_file(tmp_path))
    rollup = carry_forward_rollup((sf,), TODAY)
    stale = [a["text"] for a in rollup["stale_asks"]]
    assert not any("Snoozed ask" in t for t in stale)
    assert any("Live ask" in t for t in stale)
    assert not any("Snoozed risk" in r["text"] for r in rollup["escalated_risks"])
    # …and back once the snooze lapses.
    later = carry_forward_rollup((sf,), date(2026, 6, 1))
    assert any("Snoozed ask" in a["text"] for a in later["stale_asks"])
    assert any("Snoozed risk" in r["text"] for r in later["escalated_risks"])


def test_prep_agenda_aged_asks_omit_snoozed(tmp_path: Path) -> None:
    from cp_engine.agenda import build_project_block

    sf = parse_sprint_file(_snoozed_file(tmp_path))
    block = build_project_block(
        _fixture_project(code=CODE), tenant_root=tmp_path, sprint_files=(sf,),
        weekly_decisions=(), today=TODAY,
    )
    texts = [a["text"] for a in block.open_asks_aged]
    assert any("Live ask" in t for t in texts)
    assert not any("Snoozed ask" in t for t in texts)


def test_prep_bundle_marks_snoozed_in_table_and_omits_it_from_urgent(tmp_path: Path) -> None:
    from cp_engine import prep_planning

    path = tmp_path / "s.md"
    path.write_text(
        "## Client communication\n### Open asks\n"
        "- [open · 2026-05-01 · Maria · by 2026-05-10] Snoozed ask "
        "<!-- cp:hash=aaaaaaaa -->\n"
        "- [open · 2026-05-01 · Sam · by 2026-05-10] Live ask <!-- cp:hash=bbbbbbbb -->\n"
        "\n## Dependencies & risks\n\n"
        "- [escalated · scope · 2026-05-18] Snoozed risk <!-- cp:hash=cccccccc -->\n"
    )
    _write_snooze(CODE, {"hash": "aaaaaaaa", "until": "2026-06-01"}, path, bullet_kind="ask")
    _write_snooze(CODE, {"hash": "cccccccc", "until": "2026-06-01"}, path, bullet_kind="risk")

    asks = prep_planning._parse_sprint_open_asks(path, TODAY)
    by_text = {a["text"]: a for a in asks}
    assert by_text["Snoozed ask"]["snoozed_until"] == "2026-06-01"  # marker lifted out
    assert by_text["Live ask"]["snoozed_until"] == ""

    flags = prep_planning._detect_urgent(
        _fixture_project(code=CODE), (), asks, today=TODAY,
        sprint_file_body=path.read_text(),
    )
    flagged = " | ".join(f["text"] for f in flags)
    assert "Live ask" in flagged
    assert "Snoozed ask" not in flagged
    assert "Snoozed risk" not in flagged

    block = prep_planning.ProjectPlanningBlock(
        project=_fixture_project(code=CODE), exec_summary=None, milestones=(),
        client_asks=(), sprint_open_asks=asks, urgent=(), fetch_error=None,
    )
    table = "\n".join(prep_planning._render_commitments_table(block))
    assert "Snoozed ask _(sprint file)_ (snoozed until 2026-06-01)" in table
    assert "Live ask _(sprint file)_ |" in table


def test_record_keeps_the_marker_and_the_carried_copy_stays_marked(tmp_path: Path) -> None:
    """The sprint file is the record: carry-forward reproduces the bullet with
    its marker, so next week's summary still knows the item is snoozed."""
    w19 = _snoozed_file(tmp_path, until="2026-06-01")
    # Step 4a: asks no longer carry; a snooze survives the MC-2 re-render by
    # hash instead (tests/test_asks_mc2.py). A carried-shape line that is
    # still on disk keeps rendering marked.
    assert compute_carry_forward(w19).asks == ()
    snoozed = next(ln for ln in w19.read_text().splitlines() if "Snoozed ask" in ln)
    text = snoozed.split("] ", 1)[1]

    w20 = _sprint(tmp_path, week="2026-W21")
    body = w20.read_text().replace(
        "- [ask · 2026-05-04 · Maria] Volume forecast from ops team",
        f"- [ask · 2026-05-01 · Maria] {text}",
    )
    w20.write_text(body)
    block = render_current_sprint_block(parse_sprint_file(w20), link_path=LINK, today=TODAY)
    assert "Snoozed ask" in block and "(snoozed until 2026-06-01)" in block
