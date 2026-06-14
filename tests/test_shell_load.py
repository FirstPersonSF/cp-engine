from pathlib import Path

from cp_engine.shell import LAYER_IMPORTANCE, LAYERS, load_shell


def _write(p: Path, **fm) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines += ["---", "body"]
    p.write_text("\n".join(lines), encoding="utf-8")


def test_load_shell_collects_all_layer_dirs(tmp_path: Path) -> None:
    shell = tmp_path / "shell"
    _write(
        shell / "Deliverables" / "pos.md",
        id="ibx-5153/deliverable/pos",
        project="ibx-5153",
        layer="Deliverables",
        title="Positioning",
        status="active",
        last_touched="2026-06-13",
    )
    _write(
        shell / "Research" / "carol.md",
        id="ibx-5153/research/carol",
        project="ibx-5153",
        layer="Research",
        title="Carol deck",
        status="active",
        last_touched="2026-06-11",
    )

    elements = load_shell(tmp_path)

    ids = {e.id for e in elements}
    assert ids == {"ibx-5153/deliverable/pos", "ibx-5153/research/carol"}


def test_load_shell_returns_empty_when_no_shell_dir(tmp_path: Path) -> None:
    assert load_shell(tmp_path) == ()


def test_load_shell_orders_by_layers_then_sorted_within(tmp_path: Path) -> None:
    # Drafts (LAYERS index 6) precedes Deliverables (index 7), but alpha order
    # would put "Deliverables" before "Drafts" — so this pins LAYERS order, not
    # alpha order. Two files in Drafts prove sorted-within-layer.
    shell = tmp_path / "shell"
    _write(
        shell / "Deliverables" / "pos.md",
        id="ibx-5153/deliverable/pos",
        project="ibx-5153",
        layer="Deliverables",
        title="Positioning",
        status="active",
        last_touched="2026-06-13",
    )
    _write(
        shell / "Drafts" / "b.md",
        id="ibx-5153/draft/b",
        project="ibx-5153",
        layer="Drafts",
        title="Draft B",
        status="active",
        last_touched="2026-06-13",
    )
    _write(
        shell / "Drafts" / "a.md",
        id="ibx-5153/draft/a",
        project="ibx-5153",
        layer="Drafts",
        title="Draft A",
        status="active",
        last_touched="2026-06-13",
    )

    elements = load_shell(tmp_path)

    assert [e.id for e in elements] == [
        "ibx-5153/draft/a",
        "ibx-5153/draft/b",
        "ibx-5153/deliverable/pos",
    ]


def test_layer_importance_covers_all_layers() -> None:
    for layer in LAYERS:
        assert layer in LAYER_IMPORTANCE, f"missing weight for {layer}"
        assert 0.0 < LAYER_IMPORTANCE[layer] <= 1.0
