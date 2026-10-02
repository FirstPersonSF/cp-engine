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


# ──────────────────────────────────────────────────────────────────────
#  Managed regions are projections, not content (2026-10-01: 35 false
#  "MISSING" after a merge, every one inside a generated region)
# ──────────────────────────────────────────────────────────────────────

REGION_BODY = (
    "## Carry-forward\n"
    "<!-- cp-engine:start carry-forward -->\n"
    "- [risk · watching] projected from last week <!-- cp:hash=aaaa1111 -->\n"
    "<!-- cp-engine:end carry-forward -->\n"
    "## Open asks\n"
    "<!-- cp-engine:start open-asks -->\n"
    "- [ask] rendered from MC-2 <!-- cp:hash=bbbb2222 -->\n"
    "<!-- cp-engine:end open-asks -->\n"
    "- [ask] hand-typed below the markers <!-- cp:hash=cccc3333 -->\n"
)


def test_a_hash_leaving_a_managed_region_is_not_reported(repo):
    """Sync regenerated the regions without the projected bullets; the
    hand-written one survived — nothing was lost."""
    p = _seed(repo, REGION_BODY)
    p.write_text(
        "## Carry-forward\n"
        "<!-- cp-engine:start carry-forward -->\n"
        "<!-- cp-engine:end carry-forward -->\n"
        "## Open asks\n"
        "<!-- cp-engine:start open-asks -->\n"
        "<!-- cp-engine:end open-asks -->\n"
        "- [ask] hand-typed below the markers <!-- cp:hash=cccc3333 -->\n"
    )

    lost, _ = check_merge(repo, ref="remote-side")

    assert lost == []


def test_a_hash_removed_outside_the_markers_is_still_reported(repo):
    """The check exists for hand-written content: a blanket --ours on
    2026-08-25 deleted 20 such bullets. Regions beside it change nothing."""
    p = _seed(repo, REGION_BODY)
    p.write_text(REGION_BODY.replace(
        "- [ask] hand-typed below the markers <!-- cp:hash=cccc3333 -->\n", ""))

    lost, _ = check_merge(repo, ref="remote-side")

    assert [item.hash for item in lost] == ["cccc3333"]


def test_a_hash_both_inside_and_outside_a_region_is_still_required(repo):
    """A carry-forward projection of a bullet does not excuse losing the
    hand-written original in the same file."""
    body = REGION_BODY.replace("aaaa1111", "cccc3333")
    p = _seed(repo, body)
    p.write_text(body.replace(
        "- [ask] hand-typed below the markers <!-- cp:hash=cccc3333 -->\n", "")
        .replace("- [risk · watching] projected from last week "
                 "<!-- cp:hash=cccc3333 -->\n", ""))

    lost, _ = check_merge(repo, ref="remote-side")

    assert [item.hash for item in lost] == ["cccc3333"]


# ──────────────────────────────────────────────────────────────────────
#  #310 — sync on a clone behind its upstream
# ──────────────────────────────────────────────────────────────────────


def _behind_clone(tmp: Path) -> Path:
    """A clone one commit behind origin/main after a fetch — the 2026-09-22
    shape: the webhook pushed, the session fetched (or not) but never merged."""
    origin = tmp / "origin.git"
    _git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
    a, b = tmp / "a", tmp / "b"
    for d in (a, b):
        _git(tmp, "clone", "-q", str(origin), str(d))
        _git(d, "config", "user.email", "t@example.com")
        _git(d, "config", "user.name", "T")
    (a / "x.md").write_text("one\n")
    _git(a, "add", "-A"); _git(a, "commit", "-qm", "one"); _git(a, "push", "-q", "origin", "HEAD:main")
    _git(b, "pull", "-q", "origin", "main")
    _git(b, "branch", "-q", "--set-upstream-to=origin/main")
    (a / "x.md").write_text("two <!-- cp:hash=deadbeef -->\n")
    _git(a, "commit", "-qam", "[auto-ingest] webhook"); _git(a, "push", "-q", "origin", "HEAD:main")
    _git(b, "fetch", "-q")
    return b


def test_commits_behind_upstream_counts_the_webhook_commit(tmp_path):
    """`cxp sync` must be able to see that the clone is behind (#310); the
    count comes from git itself against the remote-tracking ref."""
    from cp_engine.merge_check import commits_behind_upstream

    b = _behind_clone(tmp_path)
    assert commits_behind_upstream(b) == ("origin/main", 1)
    _git(b, "merge", "-q", "--ff-only", "origin/main")
    assert commits_behind_upstream(b) == ("origin/main", 0)


def test_commits_behind_upstream_is_none_without_an_upstream(repo):
    """No upstream is not a warning — there is nothing to be behind."""
    from cp_engine.merge_check import commits_behind_upstream

    _seed(repo, "x\n")
    assert commits_behind_upstream(repo) is None


def test_sync_warns_loudly_when_the_clone_is_behind(tmp_path):
    """A sync one fetch behind rendered sprint files without the webhook's
    newest bullets, and the rebase after it dropped 18 of them (2026-09-22,
    #310). The CLI must say so before it renders — warn, not refuse."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from cp_engine.cli_cmds.core import sync
    from cp_engine.sync import SyncResult

    b = _behind_clone(tmp_path)

    class _Cfg:
        root = b

    result = SyncResult(
        projects_seen=1, files_written=(), files_deactivated=(), no_op=True
    )
    with (
        patch("cp_engine.cli_cmds.core.load", return_value=_Cfg()),
        patch("cp_engine.cli_cmds.core.sync_tenant", return_value=result) as st,
    ):
        res = CliRunner().invoke(sync, [])
    assert res.exit_code == 0, res.output
    assert st.called  # warned, did not refuse
    assert "1 commit behind origin/main" in res.output
    assert "cxp merge-check" in res.output
