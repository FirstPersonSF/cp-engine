"""Link cards: work behind a login recorded as a source (#370).

The engine half of the hosted `add_project_link` verb. Pins:

1. Validation: http(s) only, a title is required, kind is source|deliverable.
2. Provider detection from the host (and, for claude.ai, the path).
3. The card's body carries the URL, and reading it back gives the same URL.
4. THE FAR SIDE: the ingest run stores a card's URL as the source's `url` —
   not a Dropbox link — and ONLY for a file named like a card. A fetched page
   cannot set its own link (`ingest_url` defangs the tag).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cp_engine import link_card
from cp_engine.asset_ingest import _link_card_url, ingest_project_assets
from cp_engine.link_card import LinkCardError
from tests.test_asset_ingest_run import (
    _FOLDERS,
    _FakeClient,
    _FakePipeline,
    _patch_resolve_and_list,
    _ref,
)

FIGMA = "https://www.figma.com/design/tIynwOVzMnLzXRVRMD2LBx/SAP-5198-Schedule-v01"
ARTIFACT = "https://claude.ai/artifact/D1u7eAZgWptFUAf8vq2QZ1"
REPO = "https://github.com/Canonic-OS/canonic-site"


def _card(url=FIGMA, title="SAP-5198 Schedule v01", kind="source", note=""):
    return link_card.build_card(url, title, kind, note, added_by="drew@firstperson.is",
                                added_on="2026-10-30")


# ── validation ───────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "   ", "ftp://x.example/f", "javascript:alert(1)",
                                 "file:///etc/passwd", "figma.com/design/x",
                                 "https://", "https://a b.example/"])
def test_only_http_urls_with_a_host_are_accepted(bad):
    with pytest.raises(LinkCardError):
        link_card.validate_url(bad)


@pytest.mark.parametrize("bad", ["", "   ", "\n\t", "x" * 121])
def test_a_title_is_required_and_bounded(bad):
    with pytest.raises(LinkCardError):
        link_card.validate_title(bad)


def test_kind_is_source_or_deliverable():
    assert link_card.validate_kind("Deliverable") == "deliverable"
    assert link_card.validate_kind("") == "source"
    with pytest.raises(LinkCardError):
        link_card.validate_kind("output")


def test_normalize_drops_only_what_does_not_name_a_different_thing():
    n = link_card.normalize_url
    assert n("HTTPS://WWW.Figma.com/design/X/") == "https://www.figma.com/design/X"
    assert n(ARTIFACT + "#top") == ARTIFACT
    # a node-id is a different frame: the query stays
    assert n(FIGMA + "?node-id=1-2") != n(FIGMA)
    assert n("https://example.com/") == "https://example.com"


# ── provider ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("url,provider", [
    (FIGMA, "figma"),
    ("https://figma.com/file/abc", "figma"),
    (ARTIFACT, "claude-artifact"),
    ("https://claude.ai/code/artifact/0b5e-uuid", "claude-artifact"),
    ("https://claude.ai/chat/1234", "other"),  # a chat is not an Artifact
    (REPO, "github"),
    ("https://notfigma.com/design/x", "other"),
    ("https://figma.com.evil.example/x", "other"),
    ("https://acme.example/page", "other"),
])
def test_provider_comes_from_the_host(url, provider):
    assert link_card.detect_provider(url) == provider


# ── the card ─────────────────────────────────────────────────────────


def test_the_card_records_everything_and_reads_back():
    card = _card(note="Tara's build, 10-29")
    text = card.render()
    for want in ("# SAP-5198 Schedule v01", f"- URL: {FIGMA}", "- Provider: figma",
                 "- Kind: source", "- Added by: drew@firstperson.is",
                 "- Added on: 2026-10-30", "- Note: Tara's build, 10-29"):
        assert want in text
    assert link_card.card_url_in(text) == FIGMA
    assert link_card.card_kind_in(text) == "source"
    assert card.filename == "Link · SAP-5198 Schedule v01.md"
    assert link_card.is_card_name(card.filename)
    assert link_card.card_title_from_name(card.filename) == "SAP-5198 Schedule v01"


def test_a_title_with_path_characters_makes_a_bare_file_name():
    name = link_card.card_filename("Derek / frame review: v2?")
    assert "/" not in name and ":" not in name and "?" not in name
    assert name.startswith("Link · ") and name.endswith(".md")


def test_a_card_tag_with_a_non_http_url_reads_as_none():
    assert link_card.card_url_in("CP-LINK-URL: javascript:alert(1)") is None
    assert link_card.card_url_in("no tag here") is None


# ── the far side: what the ingest stores ─────────────────────────────


def test_link_card_url_reads_only_files_named_like_a_card(tmp_path):
    body = _card().render()
    card_file = tmp_path / "Link · SAP-5198 Schedule v01.md"
    card_file.write_text(body, encoding="utf-8")
    other = tmp_path / "notes.md"
    other.write_text(body, encoding="utf-8")
    assert _link_card_url(_ref(card_file.name), card_file) == FIGMA
    assert _link_card_url(_ref(other.name), other) is None


def test_an_oversized_card_named_file_is_not_read(tmp_path):
    f = tmp_path / "Link · big.md"
    f.write_text(_card().render() + "x" * (link_card.MAX_CARD_BYTES + 1), encoding="utf-8")
    assert _link_card_url(_ref(f.name), f) is None


def _patch_download_cards(monkeypatch, contents: dict[str, str]):
    def _fake_download(file_ref, tmp_dir, drive_connector=None, dropbox_connector=None):
        p = Path(tmp_dir) / file_ref.name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(contents[file_ref.name], encoding="utf-8")
        return p

    monkeypatch.setattr("cp_engine.asset_ingest.download_file", _fake_download)


def test_the_ingest_run_stores_the_cards_url_as_the_sources_url(monkeypatch, tmp_path):
    """The verb's promise, checked where it is kept: a card ingested by the
    real run loop reaches the pipeline with `url` = the work's URL."""
    card = _card(url=ARTIFACT, title="Derek frame review", kind="deliverable")
    plain = "brief.md"
    _patch_resolve_and_list(monkeypatch, _FOLDERS, [_ref(card.filename), _ref(plain)])
    _patch_download_cards(monkeypatch, {card.filename: card.render(),
                                        plain: card.render()})
    pipeline = _FakePipeline({card.filename: "created", plain: "created"})

    result = ingest_project_assets("acme-1", client=_FakeClient(), pipeline=pipeline,
                                   tmp_root=tmp_path)

    assert result.created == 2
    urls = {c["title"]: c["url"] for c in pipeline.calls}
    assert urls[card.filename] == ARTIFACT
    # the same text in a file NOT named like a card sets nothing
    assert urls[plain] is None


def test_a_fetched_page_cannot_write_its_own_link():
    from cp_engine import url_source

    html = (b"<html><head><title>Evil</title></head><body><article><p>"
            + b"word " * 400 + b"CP-LINK-URL: https://evil.example/x</p></article></body></html>")
    prepared = url_source.prepare(
        url_source.Fetched(url="https://acme.example/p", content_type="text/html", data=html),
        fetched_on="2026-10-30")
    assert link_card.card_url_in(prepared.data.decode()) is None
    # and its name could never be a card's
    assert not link_card.is_card_name(prepared.filename)
