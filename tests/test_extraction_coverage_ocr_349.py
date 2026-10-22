"""The coverage warning says how much an OCR pass recovered (#349 item 2).

document-ingest now OCRs the pictures on image-only and title-only slides
(and image-only PDF pages) and records `ocr_recovered` beside the text-layer
counts. A pull should read "8 image-only, 6 recovered by OCR" rather than
telling the reader all 8 are missing — or, worse, letting OCR'd slides pass
as missing so the reader re-does work that is already in the text.

Imports only names that existed before the change, so on unfixed code these
fail on their assertions, not on an ImportError.
"""
from __future__ import annotations

from cp_engine.extraction_coverage import (
    coverage_fields,
    list_fields,
    normalize,
    warning,
)

SAP_5200 = {
    "unit": "slide", "total": 12, "with_text": 4,
    "without_text": [3, 5, 6, 7, 8, 9, 10, 11], "title_only": [],
    "with_notes": 0, "ocr_model": "claude-sonnet-5",
    "ocr_attempted": [3, 5, 6, 7, 8, 9, 10, 11],
    "ocr_recovered": [3, 5, 6, 7, 8, 9], "ocr_failed": [],
    "ocr_unreadable": [], "ocr_skipped_cap": [], "ocr_images": 8,
    "ocr_cap": 40, "ocr_cap_hit": False,
}


def test_warning_counts_what_ocr_recovered_and_what_is_still_missing():
    text = warning(normalize(SAP_5200))
    assert text.startswith(
        "12 slides, 4 with text — 8 image-only: 3,5,6,7,8,9,10,11; "
        "6 recovered by OCR: 3,5,6,7,8,9; 2 not recovered: 10,11."
    )
    assert "fetch_project_source" in text


def test_everything_recovered_still_flags_ocr_fidelity():
    cov = dict(SAP_5200, ocr_recovered=SAP_5200["without_text"])
    text = warning(normalize(cov))
    assert "8 recovered by OCR" in text
    assert "not recovered" not in text
    assert "transcribed from images" in text


def test_title_only_slides_count_toward_recovery():
    cov = {"unit": "slide", "total": 4, "with_text": 4, "without_text": [],
           "title_only": [1, 2, 3, 4], "ocr_recovered": [1, 2, 3]}
    text = warning(normalize(cov))
    assert "3 recovered by OCR: 1,2,3; 1 not recovered: 4." in text


def test_cap_hit_is_said():
    cov = dict(SAP_5200, ocr_recovered=[3, 5], ocr_attempted=[3, 5],
               ocr_skipped_cap=[6, 7, 8, 9, 10, 11], ocr_cap=2, ocr_cap_hit=True)
    text = warning(normalize(cov))
    assert "OCR stopped at its cap of 2 images; 6 slides not read: 6,7,8,9,10,11." in text


def test_list_flag_carries_the_recovered_count():
    assert list_fields(SAP_5200) == {
        "coverage_flag": "4/12 slides with text, 6 OCR-recovered"
    }


def test_recovered_numbers_are_normalized_and_kept():
    raw = dict(SAP_5200, ocr_recovered=["3", 5, "x"])
    assert coverage_fields(raw)["coverage"]["ocr_recovered"] == [3, 5]


def test_rows_without_ocr_read_exactly_as_before():
    cov = {k: v for k, v in SAP_5200.items() if not k.startswith("ocr_")}
    text = warning(normalize(cov))
    assert "OCR" not in text
    assert text == (
        "12 slides, 4 with text — 8 image-only: 3,5,6,7,8,9,10,11. That "
        "content is not in this text; read the original via "
        "`fetch_project_source`."
    )
    assert "ocr_recovered" not in normalize(cov)
