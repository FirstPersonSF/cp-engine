from cp_engine.shell import ShellElement, derive_status, render_sweep
from datetime import date
from pathlib import Path


def _el(eid, layer, **o):
    d = dict(id=eid, project="p", layer=layer, title=eid, status="active",
             last_touched="2026-06-13", path=Path("/x.md"), body="")
    d.update(o)
    return ShellElement(**d)


def test_server_of_final_deliverable_demotes_to_reference():
    deliv = _el("p/deliverable/d1", "Deliverables", stage="final", status="final",
                serves=("p/deliverable/d1",))
    note = _el("p/clientfeedback/f1", "ClientFeedback",
               serves=("p/deliverable/d1",))
    by_id = {e.id: e for e in (deliv, note)}
    assert derive_status(note, by_id) == "reference"


def test_server_of_blocked_deliverable_goes_dormant():
    # d2 depends on d1 which is not final → d2 is blocked → its servers dormant.
    d1 = _el("p/deliverable/d1", "Deliverables", stage="revised", status="active")
    d2 = _el("p/deliverable/d2", "Deliverables", stage="first", status="active",
             depends_on=("p/deliverable/d1",), serves=("p/deliverable/d2",))
    note = _el("p/research/r1", "Research", serves=("p/deliverable/d2",))
    by_id = {e.id: e for e in (d1, d2, note)}
    assert derive_status(note, by_id) == "dormant"


def test_framing_layer_never_demotes():
    brief = _el("p/brief/b1", "Brief")
    assert derive_status(brief, {brief.id: brief}) == "active"


def test_server_of_active_unblocked_deliverable_stays_active():
    d1 = _el("p/deliverable/d1", "Deliverables", stage="revised", status="active")
    note = _el("p/research/r1", "Research", serves=("p/deliverable/d1",))
    by_id = {e.id: e for e in (d1, note)}
    assert derive_status(note, by_id) == "active"


def test_derived_demotion_affects_sweep_ordering():
    """A note serving a final deliverable must rank below an equally-fresh,
    same-layer note serving an active deliverable — derivation drives order."""
    today = date(2026, 6, 13)
    d_final = _el("p/deliverable/dfin", "Deliverables", stage="final",
                  status="final")
    d_active = _el("p/deliverable/dact", "Deliverables", stage="revised",
                   status="active")
    note_final = _el("p/research/r_final", "Research",
                     serves=("p/deliverable/dfin",))
    note_active = _el("p/research/r_active", "Research",
                      serves=("p/deliverable/dact",))
    elements = (d_final, d_active, note_final, note_active)
    out = render_sweep("p", elements, today=today)
    lines = out.splitlines()
    pos_active = next(i for i, ln in enumerate(lines) if "r_active" in ln)
    pos_final = next(i for i, ln in enumerate(lines) if "r_final" in ln)
    assert pos_active < pos_final


def test_explicit_reference_status_is_never_promoted():
    # Frontmatter floor: an explicitly-reference element stays reference even
    # though its served deliverable is active+unblocked (derivation only demotes).
    d1 = _el("p/deliverable/d1", "Deliverables", stage="revised", status="active")
    note = _el("p/research/r1", "Research", status="reference",
               serves=("p/deliverable/d1",))
    by_id = {e.id: e for e in (d1, note)}
    assert derive_status(note, by_id) == "reference"


def test_dangling_dependency_blocks_deliverable():
    # A deliverable whose depends_on points at an id not in the graph is treated
    # as blocked (conservative) → its servers go dormant.
    d2 = _el("p/deliverable/d2", "Deliverables", stage="first", status="active",
             depends_on=("p/deliverable/MISSING",), serves=("p/deliverable/d2",))
    note = _el("p/research/r1", "Research", serves=("p/deliverable/d2",))
    by_id = {e.id: e for e in (d2, note)}
    assert derive_status(note, by_id) == "dormant"


def test_warmest_wins_active_plus_blocked():
    # Warmest-wins (the IBX sweep fix): an element serving an active-unblocked
    # deliverable AND a blocked one stays "active" — was "dormant" under the old
    # coldest-wins logic. The liveliest consumer keeps it live.
    d_active = _el("p/deliverable/dact", "Deliverables", stage="revised",
                   status="active")
    d_unfinished_dep = _el("p/deliverable/dx", "Deliverables", stage="revised",
                        status="active")
    d_blocked = _el("p/deliverable/dblk", "Deliverables", stage="first",
                    status="active", depends_on=("p/deliverable/dx",))
    note = _el("p/research/r1", "Research",
               serves=("p/deliverable/dact", "p/deliverable/dblk"))
    by_id = {e.id: e for e in (d_active, d_unfinished_dep, d_blocked, note)}
    assert derive_status(note, by_id) == "active"


def test_warmest_wins_active_plus_final():
    # Serving active-unblocked + final → "active" (live consumer wins over shipped).
    d_active = _el("p/deliverable/dact", "Deliverables", stage="revised",
                   status="active")
    d_final = _el("p/deliverable/dfin", "Deliverables", stage="final",
                  status="final")
    note = _el("p/research/r1", "Research",
               serves=("p/deliverable/dact", "p/deliverable/dfin"))
    by_id = {e.id: e for e in (d_active, d_final, note)}
    assert derive_status(note, by_id) == "active"


def test_warmest_wins_blocked_plus_final():
    # Serving only blocked + final (no active-unblocked) → "reference":
    # shipped (citable) beats stalled.
    d_unfinished_dep = _el("p/deliverable/dx", "Deliverables", stage="revised",
                        status="active")
    d_blocked = _el("p/deliverable/dblk", "Deliverables", stage="first",
                    status="active", depends_on=("p/deliverable/dx",))
    d_final = _el("p/deliverable/dfin", "Deliverables", stage="final",
                  status="final")
    note = _el("p/research/r1", "Research",
               serves=("p/deliverable/dblk", "p/deliverable/dfin"))
    by_id = {e.id: e for e in (d_unfinished_dep, d_blocked, d_final, note)}
    assert derive_status(note, by_id) == "reference"


def test_warmest_wins_all_blocked():
    # Serving only blocked deliverables → "dormant" (every consumer stalled).
    d_dep = _el("p/deliverable/dx", "Deliverables", stage="revised",
                status="active")
    d_blocked1 = _el("p/deliverable/db1", "Deliverables", stage="first",
                     status="active", depends_on=("p/deliverable/dx",))
    d_blocked2 = _el("p/deliverable/db2", "Deliverables", stage="first",
                     status="active", depends_on=("p/deliverable/dx",))
    note = _el("p/research/r1", "Research",
               serves=("p/deliverable/db1", "p/deliverable/db2"))
    by_id = {e.id: e for e in (d_dep, d_blocked1, d_blocked2, note)}
    assert derive_status(note, by_id) == "dormant"


def test_sweep_marks_graph_derived_reference():
    # A note whose frontmatter status is "active" but which serves a FINAL
    # deliverable → derives to reference → the rendered marker must reflect
    # that derived status, not the raw frontmatter status.
    deliv = _el("p/deliverable/d1", "Deliverables", stage="final", status="final")
    note = _el("p/research/r1", "Research", status="active",
               serves=("p/deliverable/d1",), last_touched="2026-06-13")
    out = render_sweep("p", (deliv, note), today=date(2026, 6, 13))
    # render_sweep prints "<title>  ·<layer>" then suffix on the same line;
    # _el sets title=eid, so the note's title is "p/research/r1".
    note_line = next(ln for ln in out.splitlines()
                     if "p/research/r1" in ln and "serves:" not in ln)
    assert "(reference)" in note_line


def test_retrospective_layer_never_demotes():
    # Retrospective is framing/living: even serving only a blocked deliverable
    # it must stay active (it's append-only history, not deliverable-bound work).
    d1 = _el("p/deliverable/d1", "Deliverables", stage="revised", status="active")
    d2 = _el("p/deliverable/d2", "Deliverables", stage="first", status="active",
             depends_on=("p/deliverable/d1",), serves=("p/deliverable/d2",))
    retro = _el("p/retrospective/meeting-history", "Retrospective",
                serves=("p/deliverable/d2",))
    by_id = {e.id: e for e in (d1, d2, retro)}
    assert derive_status(retro, by_id) == "active"
