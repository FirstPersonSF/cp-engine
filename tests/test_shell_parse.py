from pathlib import Path

from cp_engine.shell import ShellElement, parse_element


def test_parse_element_reads_frontmatter_and_body(tmp_path: Path) -> None:
    f = tmp_path / "positioning-narrative.md"
    f.write_text(
        "---\n"
        "id: ibx-5153/deliverable/positioning-narrative\n"
        "project: ibx-5153\n"
        "layer: Deliverables\n"
        "type: positioning-narrative\n"
        "title: IBX leadership positioning narrative\n"
        "stage: revised\n"
        "depends_on: [ibx-5153/deliverable/unf]\n"
        "serves: [ibx-5153/deliverable/positioning-narrative]\n"
        "status: active\n"
        "last_touched: 2026-06-13\n"
        "target_date: 2026-06-19\n"
        "---\n"
        "The positioning narrative body.\n",
        encoding="utf-8",
    )

    el = parse_element(f)

    assert isinstance(el, ShellElement)
    assert el.id == "ibx-5153/deliverable/positioning-narrative"
    assert el.layer == "Deliverables"
    assert el.type == "positioning-narrative"
    assert el.stage == "revised"
    assert el.status == "active"
    assert el.depends_on == ("ibx-5153/deliverable/unf",)
    assert el.serves == ("ibx-5153/deliverable/positioning-narrative",)
    assert el.target_date == "2026-06-19"
    assert el.last_touched == "2026-06-13"
    assert "positioning narrative body" in el.body


def test_parse_element_defaults_missing_optional_fields(tmp_path: Path) -> None:
    f = tmp_path / "carol-framework.md"
    f.write_text(
        "---\n"
        "id: ibx-5153/research/carol-framework\n"
        "project: ibx-5153\n"
        "layer: Research\n"
        "title: Carol framework deck\n"
        "status: active\n"
        "last_touched: 2026-06-11\n"
        "serves: [ibx-5153/deliverable/positioning-narrative]\n"
        "---\n"
        "notes\n",
        encoding="utf-8",
    )

    el = parse_element(f)

    assert el.type is None
    assert el.stage is None
    assert el.fidelity is None
    assert el.target_date is None
    assert el.depends_on == ()
    assert el.serves == ("ibx-5153/deliverable/positioning-narrative",)
