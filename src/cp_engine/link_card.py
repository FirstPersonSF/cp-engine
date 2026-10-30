"""A work product that lives behind a login, recorded as a source (#370).

Work made in a session but stored outside Drive and Dropbox — a Figma file, a
claude.ai Artifact, a GitHub repo — left no record in the source store or the
spine. Only chat and Exec Summary prose pointed at it, so `semantic_search`,
`list_project_sources` and `seal_sweep` could not find it.

This module is the engine half of the hosted `add_project_link` verb. It
writes nothing and fetches nothing: those URLs need a login, so the link is
recorded rather than read. The record is a small markdown **link card** —
title, URL, provider, kind, note, who added it and when — which the hosted
verb uploads to the project's Dropbox and ingests like any other file. The
card then becomes a normal source.

THE URL RIDES IN THE BODY. The card carries one `CP-LINK-URL: <url>` line, and
the ingest (`asset_ingest._link_card_url`) stores that URL as the source's
`url` instead of a Dropbox share link, so a citation opens the work itself.
It is honoured only on a file named like a card (`Link · <title>.md`); a page
`ingest_url` fetched is slug-named and its text is defanged, so third-party
text cannot set its own link.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

# Every card's file name, and so its title in `list_project_sources`, starts
# with this. The ingest keys on it too.
LINK_CARD_PREFIX = "Link · "
LINK_CARD_EXT = ".md"
LINK_URL_TAG = "CP-LINK-URL:"
LINK_KIND_TAG = "CP-LINK-KIND:"

KINDS = ("source", "deliverable")
MAX_TITLE_CHARS = 120
# A card is a few hundred bytes. Anything far bigger named like one is not
# one, and is not read for a URL.
MAX_CARD_BYTES = 64 * 1024

_URL_RE = re.compile(re.escape(LINK_URL_TAG) + r"[ \t]*(\S+)")
_KIND_RE = re.compile(re.escape(LINK_KIND_TAG) + r"[ \t]*(\S+)")
# Characters a Dropbox (or any) file name should not carry.
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

_PROVIDER_LABELS = {
    "figma": "Figma",
    "claude-artifact": "Claude Artifact",
    "github": "GitHub",
    "other": "Web link",
}


class LinkCardError(ValueError):
    """A url, title or kind the card cannot be written for."""


@dataclass(frozen=True)
class LinkCard:
    url: str
    title: str
    kind: str
    provider: str
    note: str
    added_by: str
    added_on: str

    @property
    def filename(self) -> str:
        return card_filename(self.title)

    def render(self) -> str:
        return render_card(self)


def normalize_url(url: str) -> str:
    """The URL as compared for idempotency: trimmed, scheme and host
    lower-cased, fragment dropped, one trailing slash on the path dropped.
    The query stays (a Figma `node-id` names a different frame)."""
    raw = (url or "").strip()
    p = urlparse(raw)
    path = p.path[:-1] if p.path.endswith("/") and p.path != "/" else p.path
    if path == "/":
        path = ""
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, p.params, p.query, ""))


def validate_url(url: str) -> str:
    """The normalized URL, or `LinkCardError` naming what is wrong."""
    raw = (url or "").strip()
    if not raw:
        raise LinkCardError("url is required")
    if any(c.isspace() for c in raw):
        raise LinkCardError("url must not contain whitespace")
    p = urlparse(raw)
    if p.scheme.lower() not in ("http", "https"):
        raise LinkCardError(f"url must be http(s), not {p.scheme or 'schemeless'!r}: {raw}")
    if not p.hostname:
        raise LinkCardError(f"url has no host: {raw}")
    return normalize_url(raw)


def validate_title(title: str) -> str:
    clean = " ".join((title or "").split())
    if not clean:
        raise LinkCardError("title is required — the name the link is listed under")
    if len(clean) > MAX_TITLE_CHARS:
        raise LinkCardError(f"title is {len(clean)} characters; keep it under {MAX_TITLE_CHARS}")
    return clean


def validate_kind(kind: str) -> str:
    k = (kind or "source").strip().lower()
    if k not in KINDS:
        raise LinkCardError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    return k


def detect_provider(url: str) -> str:
    """figma | claude-artifact | github | other, from the host (and, for
    claude.ai, the path — a chat link is not an Artifact)."""
    p = urlparse((url or "").strip())
    host = (p.hostname or "").lower()

    def on(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    if on("figma.com"):
        return "figma"
    if on("claude.ai") and "/artifact/" in f"{p.path}/":
        return "claude-artifact"
    if on("github.com"):
        return "github"
    return "other"


def card_filename(title: str) -> str:
    """`Link · <title>.md` — the card's Dropbox name and listed title."""
    safe = _UNSAFE_NAME_RE.sub("-", validate_title(title)).strip(" .-") or "untitled"
    return f"{LINK_CARD_PREFIX}{safe}{LINK_CARD_EXT}"


def is_card_name(name: str | None) -> bool:
    n = name or ""
    return n.startswith(LINK_CARD_PREFIX) and n.lower().endswith(LINK_CARD_EXT)


def build_card(url: str, title: str, kind: str = "source", note: str = "",
               *, added_by: str, added_on: str) -> LinkCard:
    """Validate everything and return the card. Raises `LinkCardError`."""
    clean_url = validate_url(url)
    return LinkCard(
        url=clean_url,
        title=validate_title(title),
        kind=validate_kind(kind),
        provider=detect_provider(clean_url),
        note=" ".join((note or "").split()),
        added_by=(added_by or "unknown").strip() or "unknown",
        added_on=added_on,
    )


def render_card(card: LinkCard) -> str:
    """The card's markdown. The tags sit in the BODY (a markdown parser may
    strip frontmatter), one per line, so the ingest can read them off."""
    label = _PROVIDER_LABELS.get(card.provider, "Web link")
    lines = [
        f"# {card.title}",
        "",
        f"A {label} link recorded as a project {card.kind} with add_project_link. "
        "The work itself is at the URL below and was not fetched: it needs a login.",
        "",
        f"- Title: {card.title}",
        f"- URL: {card.url}",
        f"- Provider: {card.provider}",
        f"- Kind: {card.kind}",
        f"- Added by: {card.added_by}",
        f"- Added on: {card.added_on}",
    ]
    if card.note:
        lines.append(f"- Note: {card.note}")
    lines += ["", f"{LINK_URL_TAG} {card.url}", f"{LINK_KIND_TAG} {card.kind}", ""]
    return "\n".join(lines)


def card_url_in(text: str | None) -> str | None:
    """The http(s) URL a card's body records, or None."""
    m = _URL_RE.search(text or "")
    if not m:
        return None
    try:
        return validate_url(m.group(1))
    except LinkCardError:
        return None


def card_kind_in(text: str | None) -> str | None:
    m = _KIND_RE.search(text or "")
    return m.group(1).strip().lower() if m else None


def card_title_from_name(name: str) -> str:
    """`Link · Foo.md` → `Foo`."""
    stem = name[len(LINK_CARD_PREFIX):] if name.startswith(LINK_CARD_PREFIX) else name
    return stem[: -len(LINK_CARD_EXT)] if stem.lower().endswith(LINK_CARD_EXT) else stem
