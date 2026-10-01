"""webhook/cron/provision.py — never run in tests against Railway; these pin
what it WOULD do: the variables (a reference for the secret, never a value),
the upload layout the Dockerfile needs, plan-only touching nothing, and that
its schedules agree with the trigger and the route."""
from __future__ import annotations

import importlib.util
import io
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_CRON = Path(__file__).resolve().parent.parent / "webhook" / "cron"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"cron_{name}", _CRON / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


provision = _load("provision")
trigger = _load("trigger")

_WEBHOOK = _CRON.parent
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))


def test_secret_is_a_reference_and_verify_adds_the_dry_run_pair():
    v = provision.trigger_variables("sync", "0 14,22 * * *", verify=False)
    assert v == {"WEBHOOK_HMAC_SECRET": "${{cp-engine.WEBHOOK_HMAC_SECRET}}",
                 "CP_WEBHOOK_URL": "https://${{cp-engine.RAILWAY_PUBLIC_DOMAIN}}",
                 "CRON_JOB": "sync", "CRON_SCHEDULE": "0 14,22 * * *"}
    vv = provision.trigger_variables("draft-summaries", "17 12,13 * * 1", verify=True)
    assert vv["CRON_DRY_RUN"] == "1" and vv["CRON_SLOT"] == "17 12 * * 1"


def test_upload_has_the_dockerfile_at_root_and_the_trigger_where_it_copies_from(tmp_path):
    up = provision.build_upload(tmp_path / "u")
    assert sorted(str(p.relative_to(up)) for p in up.rglob("*") if p.is_file()) == \
        ["Dockerfile", "webhook/cron/trigger.py"]
    assert "COPY webhook/cron/trigger.py" in (up / "Dockerfile").read_text()


def test_plan_only_makes_no_mutation(monkeypatch):
    calls = []

    def fake_gql(query, variables=None):
        calls.append(query)
        assert "mutation" not in query
        return {"project": {"environments": {"edges": [{"node": {"id": "e", "name": "production"}}]},
                            "services": {"edges": [{"node": {"id": "w", "name": "cp-engine"}}]}}}

    monkeypatch.setattr(provision, "gql", fake_gql)
    monkeypatch.setattr(provision.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not run railway")))
    out = io.StringIO()
    assert provision.provision("sync", apply=False, verify=False, out=out) == 0
    assert "plan only" in out.getvalue() and len(calls) == 1


def test_every_job_schedule_derives_slots_the_route_accepts():
    from routers import cron as cron_router

    assert set(provision.JOBS) == set(cron_router.JOBS)
    for job, (_name, schedule) in provision.JOBS.items():
        times, weekdays = trigger.parse_schedule(schedule)
        day = datetime(2026, 10, 5, tzinfo=UTC)  # a Monday
        for h, m in times:
            slot = trigger.derive_slot(schedule, day.replace(hour=h, minute=m) + timedelta(minutes=3),
                                       timedelta(minutes=45))
            pattern = cron_router._SLOT_RE if job == "health" else cron_router._SLOT_DOW_RE
            assert pattern.match(slot), (job, slot)
