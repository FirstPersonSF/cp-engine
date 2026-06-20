from scripts.full_job_name_id_map import build_id_map


def test_build_id_map_old_to_new():
    rows = [{"number": 5192, "full_job_name": "IBX 5192 Platform Sales Readiness Summit",
             "companies": {"code": "IBX"}}]
    assert build_id_map(rows) == {"ibx-5192": "ibx-5192-platform-sales-readiness-summit"}


def test_build_id_map_skips_unchanged():
    # old == new (full_job_name empty -> fallback to ibx-5192 == old) -> skip
    rows = [{"number": 5192, "full_job_name": "", "companies": {"code": "IBX"}}]
    assert build_id_map(rows) == {}


def test_build_id_map_skips_rows_without_company_or_number():
    assert build_id_map([{"number": None, "full_job_name": "X", "companies": {}}]) == {}


def test_build_id_map_multiple():
    rows = [
      {"number": 5168, "full_job_name": "GGL 5168 Activation", "companies": {"code": "GGL"}},
      {"number": 5136, "full_job_name": "GGL 5136 go/safety website", "companies": {"code": "GGL"}},
    ]
    assert build_id_map(rows) == {
      "ggl-5168": "ggl-5168-activation",
      "ggl-5136": "ggl-5136-go-safety-website",
    }
