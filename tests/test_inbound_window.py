"""The cp.md inbound strip keeps its heading's promises (cp-engine #328, #329).

#328 — "Recent inbound (last 4 weeks)" showed ONE week: sync handed the strip
builder the current sprint week's files only, so a bullet recorded three weeks
ago had already vanished from the strip that promised four. The same was true
of "Recent decisions (last 4 weeks)".

#329 — #323 reconciled new-source announcements against the live source list
by TITLE, so a source renamed with ``rename_project_source`` (same asset row,
new title) dropped out as if deleted. Announcements now carry the asset id
(outside the hashed text), and a title-only legacy announcement is resolved
through the supersede chain before it is dropped.

Bullets are written with the REAL writers (``announce_new_sources``,
``_write_inbound``) so the bytes under test are the bytes production writes.
"""

from __future__ import annotations

import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from cp_engine.aggregators import (
    PROJECT_STRIP_CAP,
    LiveSources,
    aggregate_project_strips,
)
from cp_engine.ingest import _content_hash, _write_inbound, announce_new_sources
from cp_engine.render import render_project_strip_bodies
from cp_engine.sprints import current_sprint_week_iso, parse_sprint_file

GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "sprints" / "scaffold-engagement.md"
CODE = "peb-5100"
TODAY = date(2026, 5, 20)


def _week_of(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _sprint(tmp_path: Path, week: str = "2026-W21") -> Path:
    """The scaffold golden relabelled as ``week``'s file."""
    p = tmp_path / "sprints" / week / f"{CODE}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = re.sub(r"^Sprint: .*$", f"Sprint: {week}", GOLDEN.read_text(), count=1, flags=re.M)
    p.write_text(body)
    return p


def _inbound(path: Path, text: str, d: date, who: str = "Maria") -> None:
    assert _write_inbound(CODE, {"text": text, "date": d.isoformat(), "who": who},
                          path, today=d)


# ──────────────────────────────────────────────────────────────────────
#  #328 — the window is four weeks, not one
# ──────────────────────────────────────────────────────────────────────


def test_sync_strip_shows_a_bullet_from_three_weeks_ago_not_five(tmp_path: Path) -> None:
    """Through ``sync_tenant``, on a Wednesday (the sprint label has already
    rolled forward to next week): an inbound bullet recorded in the sprint
    file of the week three weeks back appears in the cp.md strip; one in the
    file five weeks back does not. Before #328 sync read only the current
    week's file and the three-week-old bullet was missing."""
    from cp_engine import sync_tenant

    from tests.test_spine_sync import _FakeClient
    from tests.test_sync import make_config
    from tests.test_sync_spine_integration import _make_engagement, _SpineBackend

    config = make_config(tmp_path)
    code = "ggl-5168"
    now = datetime(2026, 5, 20, 9, 0)  # a Wednesday
    backend = lambda _: _SpineBackend(  # noqa: E731
        (_make_engagement(code, mc2_id=None),), _FakeClient())
    sync_tenant(config, backend_factory=backend, now=now)

    current = tmp_path / "sprints" / current_sprint_week_iso(now) / f"{code}.md"
    assert current.is_file()
    three_ago = now.date() - timedelta(days=21)
    five_ago = now.date() - timedelta(days=35)
    for d, text in ((three_ago, "Three weeks back"), (five_ago, "Five weeks back")):
        week = _week_of(d)
        p = tmp_path / "sprints" / week / f"{code}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(current, p)
        p.write_text(re.sub(r"^Sprint: .*$", f"Sprint: {week}", p.read_text(),
                            count=1, flags=re.M))
        assert _write_inbound(code, {"text": text, "date": d.isoformat(), "who": "Ann"},
                              p, today=d)

    sync_tenant(config, backend_factory=backend, now=now)
    body = (tmp_path / "1p/google" / code / "cp.md").read_text()
    strip = body[body.index("<!-- cp-engine:start inbound-strip -->"):
                 body.index("<!-- cp-engine:end inbound-strip -->")]
    assert "Three weeks back" in strip
    assert "Five weeks back" not in strip


def test_window_dedupes_across_weeks_newest_first(tmp_path: Path) -> None:
    """A bullet recorded in two weeks (same cp:hash) appears once, and the
    strip reads newest first by bullet date whatever file it came from.
    Decisions share the heading's promise and get the same treatment."""
    old = _sprint(tmp_path, "2026-W19")
    cur = _sprint(tmp_path, "2026-W21")
    _inbound(old, "Oldest note", date(2026, 5, 5))
    _inbound(old, "Restated note", date(2026, 5, 6))
    _inbound(cur, "Restated note", date(2026, 5, 6))  # same text → same hash
    _inbound(cur, "Newest note", date(2026, 5, 19))
    strips = aggregate_project_strips(
        CODE, (parse_sprint_file(cur),), TODAY,
        window_files=(parse_sprint_file(old),),
    )
    texts = [ib.text.split(" <!--")[0] for ib in strips.inbound]
    assert texts == ["Newest note", "Restated note", "Oldest note"]


def test_window_caps_and_names_the_remainder(tmp_path: Path) -> None:
    cur = _sprint(tmp_path)
    for n in range(PROJECT_STRIP_CAP + 3):
        _inbound(cur, f"Note {n:02d}", date(2026, 5, 1) + timedelta(days=n % 19))
    strips = aggregate_project_strips(CODE, (parse_sprint_file(cur),), TODAY)
    assert len(strips.inbound) == PROJECT_STRIP_CAP
    assert strips.inbound_overflow == 3
    assert "- _+3 older — see sprint files._" in render_project_strip_bodies(strips)["inbound-strip"]


# ──────────────────────────────────────────────────────────────────────
#  #329 — renamed is not gone
# ──────────────────────────────────────────────────────────────────────

ASSET = "0b0e8f4e-1111-4c1c-9a57-5e2f3a4b5c6d"
SUCCESSOR = "7d1c2b3a-2222-4c1c-9a57-5e2f3a4b5c6d"


def _announce(path: Path, title: str, asset_id: str | None = ASSET) -> None:
    asset = {"title": title, "created_at": "2026-05-18T10:00:00Z", "source_type": "doc"}
    if asset_id:
        asset["id"] = asset_id
    assert announce_new_sources(CODE, path, [asset], today=TODAY) == 1


def _strip(path: Path, live: LiveSources | None) -> list[str]:
    sf = parse_sprint_file(path)
    return [ib.text for ib in aggregate_project_strips(CODE, (sf,), TODAY, live_sources=live).inbound]


def test_announcement_carries_the_id_without_changing_its_hash(tmp_path: Path) -> None:
    """The id rides AFTER the cp:hash marker; the hash is still the hash of
    the pre-#329 text, so a file announced by an older engine is recognised
    and the source is not announced a second time."""
    path = _sprint(tmp_path)
    _announce(path, "Brief v03")
    line = next(ln for ln in path.read_text().splitlines() if "Brief v03" in ln)
    text = "**New source ingested:** Brief v03 (doc) — full text via `pull_project_source`."
    assert f"<!-- cp:hash={_content_hash(CODE, 'record-inbound', text)} -->" in line
    assert line.endswith(f"<!-- cp:asset={ASSET} -->")

    legacy = _sprint(tmp_path, "2026-W20")
    _announce(legacy, "Brief v03", asset_id=None)  # what a pre-#329 engine wrote
    asset = {"id": ASSET, "title": "Brief v03", "created_at": "2026-05-18T10:00:00Z",
             "source_type": "doc"}
    assert announce_new_sources(CODE, legacy, [asset], today=TODAY) == 0


def test_renamed_source_stays_under_its_current_title(tmp_path: Path) -> None:
    """``rename_project_source`` retitles the SAME row; the carried id still
    resolves, and the strip names the current title."""
    path = _sprint(tmp_path)
    _announce(path, "Brief v03")
    texts = _strip(path, LiveSources(by_id={ASSET: "Brief v03 (client-approved)"}))
    assert len(texts) == 1
    assert texts[0].startswith(
        "**New source ingested:** Brief v03 (client-approved) (renamed from “Brief v03”) (doc)")


def test_deleted_source_drops(tmp_path: Path) -> None:
    path = _sprint(tmp_path)
    _announce(path, "Brief v03")
    _announce(path, "Old deck", asset_id=None)
    lineage = ({"id": "gone-1", "title": "Old deck", "prev_asset_id": None,
                "supersedes_asset_id": None},)
    live = LiveSources(by_id={"other": "Something else"}, lineage=lineage)
    assert _strip(path, live) == []


def test_legacy_bullet_follows_the_supersede_chain(tmp_path: Path) -> None:
    """A title-only (pre-#329) announcement whose asset was replaced by a
    differently-titled successor resolves through ``supersedes_asset_id`` and
    stays, under the successor's title — unless the successor is announced
    in the strip already, which then represents the source."""
    path = _sprint(tmp_path)
    _announce(path, "Deck draft", asset_id=None)
    lineage = (
        {"id": ASSET, "title": "Deck draft", "prev_asset_id": None, "supersedes_asset_id": None},
        {"id": SUCCESSOR, "title": "Deck v2", "prev_asset_id": None, "supersedes_asset_id": ASSET},
    )
    live = LiveSources(by_id={SUCCESSOR: "Deck v2"}, lineage=lineage)
    texts = _strip(path, live)
    assert len(texts) == 1 and "Deck v2 (renamed from “Deck draft”)" in texts[0]

    _announce(path, "Deck v2", asset_id=SUCCESSOR)
    texts = _strip(path, live)
    assert len(texts) == 1 and texts[0].startswith("**New source ingested:** Deck v2 (doc)")


def test_store_unreachable_leaves_the_strip_unchanged(tmp_path: Path) -> None:
    path = _sprint(tmp_path)
    _announce(path, "Brief v03")
    _announce(path, "Old deck", asset_id=None)
    assert len(_strip(path, None)) == 2


def test_sync_keeps_a_renamed_source_and_batches_one_lineage_query(tmp_path: Path, monkeypatch) -> None:
    """Through ``sync_tenant``: a source renamed in MC-2 stays in the strip
    under its new title with NO lineage query (the id resolves it); a source
    gone from the live list triggers exactly one batched lineage query and
    drops; a failing lineage query leaves the title rule in force; and a
    failed manifest pass keeps every announcement."""
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
    asset = lambda i, t: {"id": i, "title": t, "source_type": "doc",  # noqa: E731
                          "created_at": "2026-05-19T10:00:00Z", "file_hash": i}
    live: dict = {"assets": [asset(ASSET, "Brief v03"), asset(SUCCESSOR, "Old deck")]}
    calls: list[list[str]] = []

    def fake_write(client, project_dir, *a, **k):
        if live["assets"] is None:
            raise RuntimeError("MC-2 unreachable")
        return list(live["assets"])

    def fake_lineage(client, project_ids):
        calls.append(list(project_ids))
        if live.get("lineage_fails"):
            raise RuntimeError("lineage down")
        return {"rag-proj-uuid": []}

    monkeypatch.setattr(asset_ingest, "resolve_project_folders_by_id",
                        lambda client, mc_project_id: _stub_folders())
    monkeypatch.setattr(project_sources, "write_sources_manifest", fake_write)
    monkeypatch.setattr(project_sources, "fetch_asset_lineage", fake_lineage)
    backend = lambda _: _SpineBackend(  # noqa: E731
        (_make_engagement(code, mc2_id="uuid-ggl-5168"),), _FakeClient())
    cp_path = tmp_path / "1p/google" / code / "cp.md"

    def strip() -> str:
        body = cp_path.read_text()
        return body[body.index("<!-- cp-engine:start inbound-strip -->"):
                    body.index("<!-- cp-engine:end inbound-strip -->")]

    sync_tenant(config, backend_factory=backend, now=now)
    sync_tenant(config, backend_factory=backend, now=now)
    assert "Brief v03 (doc)" in strip() and "Old deck (doc)" in strip()

    live["assets"] = [asset(ASSET, "Brief v04"), asset(SUCCESSOR, "Old deck")]  # renamed
    sync_tenant(config, backend_factory=backend, now=now)
    assert "Brief v04 (renamed from “Brief v03”)" in strip()
    assert calls == []  # the carried id resolved it — no query

    live["assets"] = [asset(ASSET, "Brief v04")]  # the deck is gone
    sync_tenant(config, backend_factory=backend, now=now)
    assert "Old deck" not in strip() and "Brief v04" in strip()
    assert calls == [["rag-proj-uuid"]]  # one batched query

    live["lineage_fails"] = True
    sync_tenant(config, backend_factory=backend, now=now)
    assert "Old deck" not in strip() and "Brief v04" in strip()

    live["assets"] = None  # manifest pass fails: keep every announcement
    sync_tenant(config, backend_factory=backend, now=now)
    assert "Old deck (doc)" in strip() and "Brief v03 (doc)" in strip()


def test_lineage_query_is_one_batched_select_of_named_columns() -> None:
    """``fetch_asset_lineage`` is the one extra query: a single select across
    every project that needs it, scalar columns only — never ``*`` or
    ``meta`` — grouped back per project."""
    from cp_engine import mc2_db
    from cp_engine.project_sources import fetch_asset_lineage

    calls: list[tuple] = []

    class _Q:
        def __init__(self):
            self.ops = []

        def select(self, cols):
            self.ops.append(("select", cols))
            return self

        def in_(self, col, vals):
            self.ops.append(("in_", col, tuple(vals)))
            return self

        def execute(self):
            calls.append(tuple(self.ops))
            return type("R", (), {"data": [
                {"id": "a", "project_id": "p1", "title": "A"},
                {"id": "b", "project_id": "p2", "title": "B"},
            ]})()

    class _C:
        def table(self, name):
            assert name == "rag_assets"
            return _Q()

    out = fetch_asset_lineage(_C(), ["p1", "p2"])
    assert len(calls) == 1
    (select, cols), (in_, col, vals) = calls[0]
    assert cols == mc2_db.RAG_ASSET_LINEAGE_COLUMNS
    assert "*" not in cols and "meta" not in cols
    assert (in_, col, vals) == ("in_", "project_id", ("p1", "p2"))
    assert set(out) == {"p1", "p2"}
    assert fetch_asset_lineage(_C(), []) == {} and len(calls) == 1
