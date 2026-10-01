"""scripts/step5_reject_inbox_cards.py: a dry run writes nothing; --apply
dismisses only the cards still `proposed`."""
from __future__ import annotations

import sys
from pathlib import Path

from tests._spine_fake import FakeClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import step5_reject_inbox_cards as rej  # noqa: E402


def _client() -> FakeClient:
    rows = [
        {"id": f"ibx-5153/inbox/m{i}", "project_code": "ibx-5153", "source_ref": f"m{i}",
         "status": "proposed", "created_at": f"2026-09-{i + 10:02d}T12:00:00Z",
         "raw_distillation": "x" * 50}
        for i in range(12)
    ]
    rows.append({"id": "sap-5174/inbox/a", "project_code": "sap-5174", "source_ref": "a",
                 "status": "promoted", "created_at": "2026-09-01T00:00:00Z"})
    return FakeClient(spine_inbox=rows)


def test_dry_run_reports_and_writes_nothing(monkeypatch, capsys):
    client = _client()
    monkeypatch.setattr(rej, "mc2_client", lambda tenant: client)
    assert rej.main(["--tenant", "/nowhere"]) == 0
    out = capsys.readouterr().out
    assert "status='proposed': 12" in out
    assert "ibx-5153" in out and "dry run" in out
    assert sum(1 for line in out.splitlines() if "/inbox/" in line) == 10
    assert all(r["status"] != "dismissed" for r in client.store["spine_inbox"])


def test_apply_dismisses_only_proposed(monkeypatch):
    client = _client()
    monkeypatch.setattr(rej, "mc2_client", lambda tenant: client)
    assert rej.main(["--tenant", "/nowhere", "--apply"]) == 0
    statuses = {r["id"]: r["status"] for r in client.store["spine_inbox"]}
    assert statuses.pop("sap-5174/inbox/a") == "promoted"
    assert set(statuses.values()) == {"dismissed"}
