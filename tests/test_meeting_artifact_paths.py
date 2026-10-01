"""Tests for webhook/meeting_artifact.write_meeting_artifacts filenames (#307).

The artifact pair is keyed on date + title slug, but a meeting's identity is
its id. Two meetings on one day with the same title (Zoom's default
"Impromptu Zoom Meeting") must land in separate files, while a re-tag of the
same meeting still overwrites its own pair in place.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# webhook/ is a sibling of src/; not on the import path by default.
_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import meeting_artifact  # noqa: E402


@pytest.fixture
def project_dir(tmp_path, monkeypatch) -> Path:
    pdir = tmp_path / "clients" / "slt-5196"
    pdir.mkdir(parents=True)
    monkeypatch.setattr(meeting_artifact, "_find_project_dir", lambda root, code: pdir)
    return pdir


def _meeting(meeting_id: str, title: str = "Impromptu Zoom Meeting") -> dict:
    return {"id": meeting_id, "title": title, "meeting_date": "2026-09-29T15:00:00Z"}


def _write(tmp_path: Path, meeting: dict, transcript: str) -> list[Path]:
    return meeting_artifact.write_meeting_artifacts(
        tenant_root=tmp_path,
        meeting=meeting,
        transcript_text=transcript,
        project_codes=["slt-5196"],
    )


def test_same_day_same_title_meetings_do_not_overwrite(tmp_path, project_dir):
    first = _write(tmp_path, _meeting("c727646e-1111-4000-8000-000000000000"), "Herzog transcript")
    second = _write(tmp_path, _meeting("781333e0-2222-4000-8000-000000000000"), "Person transcript")

    meetings = project_dir / "meetings"
    assert sorted(p.name for p in meetings.iterdir()) == [
        "2026-09-29-impromptu-zoom-meeting-781333e0.md",
        "2026-09-29-impromptu-zoom-meeting-781333e0.txt",
        "2026-09-29-impromptu-zoom-meeting.md",
        "2026-09-29-impromptu-zoom-meeting.txt",
    ]
    assert first != second

    # Both transcripts survive, and each .md links its own .txt.
    for paths, transcript, meeting_id in (
        (first, "Herzog transcript", "c727646e"),
        (second, "Person transcript", "781333e0"),
    ):
        md, txt = paths
        assert txt.read_text() == transcript
        md_text = md.read_text()
        assert f"Meeting-ID: {meeting_id}" in md_text
        assert f"Filename: {md.name}" in md_text
        assert f"[`{txt.name}`]({txt.name})" in md_text


def test_retag_of_same_meeting_overwrites_in_place(tmp_path, project_dir):
    meeting = _meeting("a79e7d25-3333-4000-8000-000000000000")
    first = _write(tmp_path, meeting, "draft transcript")
    second = _write(tmp_path, meeting, "final transcript")

    assert first == second
    assert sorted(p.name for p in (project_dir / "meetings").iterdir()) == [
        "2026-09-29-impromptu-zoom-meeting.md",
        "2026-09-29-impromptu-zoom-meeting.txt",
    ]
    assert second[1].read_text() == "final transcript"


def test_retag_of_suffixed_meeting_stays_on_its_suffix(tmp_path, project_dir):
    _write(tmp_path, _meeting("c727646e-1111-4000-8000-000000000000"), "Herzog")
    second_meeting = _meeting("781333e0-2222-4000-8000-000000000000")
    first_write = _write(tmp_path, second_meeting, "Person v1")
    retag = _write(tmp_path, second_meeting, "Person v2")

    assert retag == first_write
    assert len(list((project_dir / "meetings").iterdir())) == 4
    assert retag[1].read_text() == "Person v2"
    assert (project_dir / "meetings" / "2026-09-29-impromptu-zoom-meeting.txt").read_text() == "Herzog"
