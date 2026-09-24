"""`cp_engine.codes` — the one parser for workstream codes (#301, phase 3.2).

Five spellings of one identity must parse to the same company + number,
and everything that is not a workstream code must be rejected: before this
module five regexes disagreed (the transcript planner rejected every real
slug code; the tag parser needed exactly three letters, which ``1pi`` is
not; the dir matcher needed four digits).
"""

from __future__ import annotations

import pytest

from cp_engine.codes import ParsedCode, canonical_code, code_number, parse_code


@pytest.mark.parametrize(
    "text,company,number,slug",
    [
        ("ggl-5136", "ggl", 5136, None),
        ("GGL 5136 Go Safety", "ggl", 5136, "go-safety"),
        ("GGL-5136", "ggl", 5136, None),
        ("ggl-5136-go-safety-website", "ggl", 5136, "go-safety-website"),
        ("1pi-9005-mission-control", "1pi", 9005, "mission-control"),
    ],
)
def test_the_five_shapes(text, company, number, slug):
    assert parse_code(text) == ParsedCode(company=company, number=number, slug=slug)


def test_display_name_punctuation_and_glued_prefix():
    """Fathom tags carry the display form verbatim — slashes, glued prefix."""
    assert parse_code("GGL 5136 go/safety website") == ParsedCode("ggl", 5136, "go-safety-website")
    assert parse_code("IBX5167 DDI Platform") == ParsedCode("ibx", 5167, "ddi-platform")
    assert parse_code("  tel-5163  ") == ParsedCode("tel", 5163, None)


def test_number_is_the_first_all_digit_segment_not_a_year_in_the_slug():
    assert parse_code("sap-5171-vision-update-2026") == ParsedCode("sap", 5171, "vision-update-2026")


@pytest.mark.parametrize(
    "junk",
    [
        None,
        "",
        "   ",
        "storyos",
        "mission-control",
        "untagged",
        "cp",
        "1p-component-library",
        "mc-2",  # a job number is at least three digits
        "2026-05",  # a company code carries a letter
        "ggl",
        "ggl-XX",
        "Some Random Meeting Title",
    ],
)
def test_rejects_anything_that_is_not_a_workstream_code(junk):
    assert parse_code(junk) is None
    assert code_number(junk) is None


def test_canonical_code_round_trips():
    assert canonical_code(parse_code("GGL 5136 Go Safety Website")) == "ggl-5136-go-safety-website"
    assert canonical_code(parse_code("ggl-5136")) == "ggl-5136"
    assert canonical_code(parse_code("1PI 9005 Mission Control")) == "1pi-9005-mission-control"
    assert parse_code("ggl-5136-go-safety-website").short == "ggl-5136"


def test_code_number_is_the_thin_wrapper_the_call_sites_want():
    assert code_number("ggl-5136-go-safety-website") == 5136
    assert code_number("1pi-9005-mission-control") == 9005
