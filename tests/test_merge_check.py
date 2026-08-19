"""`cp merge-check` — catching content a merge resolution silently dropped.

Grounded in the 2026-08-19 incident: merging 19 remote commits produced
add/add conflicts on 39 W35 scaffold files, and the tenant convention
("resolve generated files --ours") would have discarded seven auto-ingest
bullets — including an escalated resourcing risk on storyos — with no error
and no visible sign. The hashes below are the real ones from that merge.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

from cp_engine.merge_check import check_merge


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo():
    d = Path(tempfile.mkdtemp())
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "T")
    return d


def _seed(repo: Path, body: str, rel: str = "sprints/2026-W35/storyos.md") -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "remote side")
    _git(repo, "branch", "remote-side")
    return p


REMOTE_BODY = (
    "## Dependencies & risks\n"
    "- [risk · escalated · resourcing · 2026-08-18] Deal structure only funds "
    "2 badged contractors, not 4 <!-- cp:hash=06bb3b34 -->\n"
    "- [risk · watching · schedule · 2026-08-18] Sean has no confirmed HR "
    "timeline <!-- cp:hash=4374eb27 -->\n"
)


def test_blanket_ours_resolution_is_caught(repo):
    """The incident shape: --ours replaced ingest content with a bare scaffold."""
    p = _seed(repo, REMOTE_BODY)
    p.write_text("## Dependencies & risks\n\n<!-- <risk — prefix> -->\n")

    lost, checked = check_merge(repo, ref="remote-side")

    assert checked == 1
    assert {item.hash for item in lost} == {"06bb3b34", "4374eb27"}
    # The snippet must be recoverable-by-eye, not just a bare hash.
    escalated = next(i for i in lost if i.hash == "06bb3b34")
    assert "badged contractors" in escalated.snippet


def test_correct_resolution_reports_clean(repo):
    p = _seed(repo, REMOTE_BODY)
    p.write_text("## Dependencies & risks\n\n<!-- scaffold -->\n")
    _git(repo, "checkout", "remote-side", "--", "sprints/2026-W35/storyos.md")

    lost, checked = check_merge(repo, ref="remote-side")

    assert lost == []
    assert checked == 1


def test_local_additions_alongside_remote_content_are_fine(repo):
    """Keeping BOTH sides is the good outcome — no false positive."""
    p = _seed(repo, REMOTE_BODY)
    p.write_text(
        REMOTE_BODY
        + "- [risk · watching · delivery · 2026-08-19] A locally added risk\n"
    )

    lost, _ = check_merge(repo, ref="remote-side")

    assert lost == []


def test_deleted_file_reports_its_hashes_lost(repo):
    """Deleting a file full of ingest bullets is exactly what this catches."""
    p = _seed(repo, REMOTE_BODY)
    p.unlink()

    lost, _ = check_merge(repo, ref="remote-side")

    assert len(lost) == 2


def test_files_without_hashes_are_not_counted(repo):
    """Hand-written docs carry no cp:hash; they aren't part of the check."""
    _seed(repo, "# Just prose\n\nNo ingest markers here.\n", rel="notes.md")

    lost, checked = check_merge(repo, ref="remote-side")

    assert lost == []
    assert checked == 0


def test_reordered_content_is_not_a_loss(repo):
    """Position doesn't matter — only that the content survived somewhere."""
    p = _seed(repo, REMOTE_BODY)
    lines = REMOTE_BODY.strip().split("\n")
    p.write_text("\n".join([lines[0], lines[2], lines[1]]) + "\n")

    lost, _ = check_merge(repo, ref="remote-side")

    assert lost == []
