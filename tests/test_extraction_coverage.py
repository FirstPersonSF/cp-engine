"""`extraction_coverage` helpers: what a pull says about partial ingests (#349)."""
from __future__ import annotations

import json

from cp_engine.extraction_coverage import (
    coverage_fields,
    list_fields,
    normalize,
    warning,
)


def _slides(total, with_text, without=(), title_only=()):
    return {"unit": "slide", "total": total, "with_text": with_text,
            "without_text": list(without), "title_only": list(title_only)}


def test_absent_or_malformed_coverage_says_nothing():
    for raw in (None, {}, "not json", {"total": "x", "with_text": 1}, [1, 2]):
        assert coverage_fields(raw) == {}
        assert list_fields(raw) == {}


def test_json_string_is_accepted():
    # PostgREST may hand a jsonb projection back as text through some paths.
    cov = normalize(json.dumps(_slides(3, 3)))
    assert cov["total"] == 3 and cov["with_text"] == 3


def test_complete_coverage_has_no_warning_or_flag():
    out = coverage_fields(_slides(5, 5))
    assert out["coverage"]["total"] == 5
    assert "coverage_warning" not in out
    assert list_fields(_slides(5, 5)) == {}


def test_title_only_slides_are_partial_even_when_every_slide_has_text():
    # sap-5200: every slide kept its title; every body was an image.
    cov = _slides(4, 4, title_only=[1, 2, 3, 4])
    text = warning(normalize(cov))
    assert text.startswith("4 slides, 4 with text; 4 slides with only a title over images: 1,2,3,4")
    assert list_fields(cov) == {"coverage_flag": "4/4 slides with text, 4 title-only"}


def test_long_lists_are_capped_in_the_warning_but_kept_in_coverage():
    missing = list(range(2, 41))
    out = coverage_fields(_slides(40, 1, without=missing))
    assert out["coverage"]["without_text"] == missing
    assert "39 image-only: 2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,…" in out["coverage_warning"]


def test_pages_are_said_without_text_not_image_only():
    cov = {"unit": "page", "total": 1, "with_text": 0, "without_text": [1],
           "method": "text"}
    assert warning(normalize(cov)).startswith("1 page, 0 with text — 1 without text: 1.")
