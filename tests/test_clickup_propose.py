"""Tests for webhook/clickup_propose.py."""
from __future__ import annotations

import sys
from pathlib import Path

# webhook/ is a sibling of src/; not on the import path by default.
_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from clickup_propose import _build_proposal_row
from cp_engine.ingest import _content_hash


def test_build_proposal_row_includes_cp_ask_hash():
    """The hash carries cp ask identity through to ClickUp via the proposal row."""
    item = {
        "description": "Confirm ISCI code",
        "assignee": {"email": "drew@firstperson.is", "name": "Drew"},
        "recording_playback_url": "https://example/play",
    }
    project = {
        "id": "p1",
        "clickup_list_id": "L1",
        "code": "ggl-5168",
    }
    row = _build_proposal_row(meeting_id="m1", project=project, item=item)
    expected = _content_hash("ggl-5168", "record-ask", "Confirm ISCI code")
    assert row["cp_ask_hash"] == expected
    # Existing fields preserved.
    assert row["meeting_id"] == "m1"
    assert row["project_id"] == "p1"
    assert row["clickup_list_id"] == "L1"
    assert row["description"] == "Confirm ISCI code"
    assert row["assignee_email"] == "drew@firstperson.is"
    assert row["recording_playback_url"] == "https://example/play"
    assert row["status"] == "pending"


def test_build_proposal_row_strips_description_before_hashing():
    """Whitespace on the description must not change the hash — it'd break round-trip."""
    item = {"description": "  Send Q3 invoice  ", "assignee": {}}
    project = {"id": "p1", "clickup_list_id": "L1", "code": "ggl-5168"}
    row = _build_proposal_row(meeting_id="m1", project=project, item=item)
    # The description stored matches what Task 1.1's helper writes into the
    # sprint file (stripped), and the hash matches that stripped value.
    assert row["description"] == "Send Q3 invoice"
    assert row["cp_ask_hash"] == _content_hash("ggl-5168", "record-ask", "Send Q3 invoice")


def test_build_proposal_row_handles_missing_assignee():
    """Missing or empty assignee dict yields assignee_email=None, no AttributeError."""
    item = {"description": "Some ask"}
    project = {"id": "p1", "clickup_list_id": "L1", "code": "ggl-5168"}
    row = _build_proposal_row(meeting_id="m1", project=project, item=item)
    assert row["assignee_email"] is None


def test_build_proposal_row_project_owner_sets_project_id():
    """A project-owned ask writes project_id and leaves initiative_id null —
    satisfying migration 081's exactly-one-owner CHECK."""
    item = {"description": "Some ask"}
    project = {"id": "p1", "clickup_list_id": "L1", "code": "ggl-5168", "kind": "project"}
    row = _build_proposal_row(meeting_id="m1", project=project, item=item)
    assert row["project_id"] == "p1"
    assert row.get("initiative_id") is None


def test_build_proposal_row_initiative_owner_sets_initiative_id():
    """An initiative-owned ask writes initiative_id and leaves project_id null —
    otherwise the FK→projects insert would crash for an initiative."""
    item = {"description": "Ship the spine UI"}
    project = {
        "id": "i1",
        "clickup_list_id": "L9",
        "code": "mission-control",
        "kind": "initiative",
    }
    row = _build_proposal_row(meeting_id="m1", project=project, item=item)
    assert row["initiative_id"] == "i1"
    assert row.get("project_id") is None
    # The hash recipe still keys off the owner code.
    assert row["cp_ask_hash"] == _content_hash(
        "mission-control", "record-ask", "Ship the spine UI"
    )
