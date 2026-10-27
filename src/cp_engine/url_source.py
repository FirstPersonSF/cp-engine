"""A web URL as a source document — fetch, extract, and say when it is gated (#350).

08-20: a client sent 11 web URLs. There was no URL path into the source store,
so the workaround was fetch → markdown → `push_to_dropbox` → a separate
ingest — and 4 of the 7 guide URLs were form-gated. What landed was the
landing-page abstract, stored and later read as if it were the document.

This module is the engine half of the hosted `ingest_url` verb. It does three
things and writes nothing:

1. `fetch_url` — GET a public http(s) URL, re-checking EVERY redirect hop
   against private / loopback / link-local / reserved addresses (the hosted
   server runs inside a private network, so an unchecked fetch is a way into
   it) and capping the bytes. Raises `UrlFetchError` with a named reason.
2. `detect_gating` — read the page for the shape of a gate: an email-capture
   or marketing-automation form, gate phrasing ("download the guide", "fill
   out the form"), a thin body, a PDF link that was not fetched. A form alone
   is NOT a gate (every blog has a newsletter box); it takes a form plus thin
   or gate-worded copy, or gate wording on a thin page.
3. `prepare` — the file to upload: an HTML page becomes markdown with a source
   header; a PDF passes through as the document itself. A gated page's
   markdown carries one `CP-INGEST-NOTE:` line IN THE BODY (a markdown parser
   may strip frontmatter), which the ingest reads off the chunks it just wrote
   and records as `status_note` (`asset_ingest._flag_confidentiality`) — so the
   caveat reaches the point of use, not just this call's result.

KNOWN LIMITS. Detection is heuristic and reads only the HTML that was served:
a gate rendered entirely by JavaScript with no form markup and no gate words
in the served HTML is not seen. A DNS answer can change between the check and
the connect (rebinding); the check narrows the door, it does not weld it shut.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlparse

from cp_engine.source_markers import INGEST_NOTE_TAG

MAX_FETCH_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
FETCH_TIMEOUT_SECONDS = 30.0
_USER_AGENT = "Mozilla/5.0 (compatible; cp-engine ingest_url)"

# A body under this many words, on a page that also carries a capture form or
# gate wording, reads as an abstract rather than a document. Tuned loose: a
# real guide or report runs to thousands of words; a landing page to a few
# hundred.
THIN_BODY_WORDS = 400

GATED_NOTE_HEAD = "form-gated — landing-page abstract, not the document"

# Phrases a landing page uses to send you to the form. Matched on the page's
# visible text, case-insensitive.
_GATE_PHRASES = re.compile(
    r"\b(?:download\s+(?:the|this|our|your)\s+(?:free\s+)?(?:full\s+)?"
    r"(?:guide|report|e-?book|white\s?paper|playbook|checklist|study|research)"
    r"|(?:fill\s+out|complete|submit)\s+(?:the|this)\s+form"
    r"|get\s+(?:the|your)\s+(?:free\s+)?(?:full\s+)?"
    r"(?:guide|report|e-?book|white\s?paper|playbook|copy)"
    r"|access\s+the\s+(?:full\s+)?(?:guide|report|e-?book|white\s?paper|research)"
    r"|register\s+to\s+(?:download|read|access|view)"
    r"|(?:instant|immediate)\s+access"
    r"|gated\s+content)\b",
    re.IGNORECASE,
)

# Marketing-automation form markers (Marketo, HubSpot, Pardot, Eloqua) — a
# vendor form on a short page is the strongest single sign of a gate.
_MAP_FORM = re.compile(r"mktoForm|hbspt|hs-form|pardot|eloqua|elqForm", re.IGNORECASE)

_STRIP_TAGS = ("script", "style", "noscript", "iframe", "svg", "template",
               "nav", "header", "footer", "aside", "form", "button")
_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre")


class UrlFetchError(RuntimeError):
    """A URL that could not be fetched, with the reason a caller can act on."""


@dataclass(frozen=True)
class Fetched:
    """The response that answered: the FINAL url (after redirects), its type, bytes."""

    url: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class Gating:
    gated: bool
    signals: list[str] = field(default_factory=list)
    pdf_links: list[str] = field(default_factory=list)

    @property
    def note(self) -> str | None:
        """The `status_note` a gated page earns — what it is, and how we know."""
        if not self.gated:
            return None
        return f"{GATED_NOTE_HEAD} (detected at ingest: {'; '.join(self.signals)})"


@dataclass(frozen=True)
class Page:
    title: str
    markdown: str
    words: int


@dataclass(frozen=True)
class Prepared:
    """The file `ingest_url` uploads, and what was learnt making it."""

    filename: str
    data: bytes
    kind: str  # html | pdf | text
    title: str
    gating: Gating
    words: int | None


# ── fetch ─────────────────────────────────────────────────────────────


def _host_refusal(url: str, resolver) -> str | None:
    """Why `url` must not be fetched from inside the hosted network, or None."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"only http(s) URLs can be ingested, not {parsed.scheme or 'a bare path'!r}"
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        return f"no host in {url!r}"
    if host == "localhost" or host.endswith((".localhost", ".internal", ".local")):
        return f"{host} is a private host"
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global or literal.is_multicast:
            return f"{host} is a private address"
        return None
    try:
        infos = resolver(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        return f"{host} did not resolve: {exc}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not ip.is_global or ip.is_multicast:
            return f"{host} resolves to a private address ({ip})"
    return None


def fetch_url(
    url: str,
    *,
    transport=None,
    resolver=socket.getaddrinfo,
    max_bytes: int = MAX_FETCH_BYTES,
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> Fetched:
    """GET `url`, following up to MAX_REDIRECTS hops, each one host-checked.

    `transport` / `resolver` are the test seams (an `httpx.MockTransport`, a
    fake `getaddrinfo`). Raises `UrlFetchError` — never returns a partial body.
    """
    import httpx

    current = (url or "").strip()
    with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False,
                      headers={"User-Agent": _USER_AGENT}) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            refusal = _host_refusal(current, resolver)
            if refusal:
                raise UrlFetchError(f"refused {current}: {refusal}")
            try:
                with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise UrlFetchError(f"{current} redirected with no location")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        raise UrlFetchError(f"{current} answered HTTP {resp.status_code}")
                    body = bytearray()
                    for part in resp.iter_bytes():
                        body.extend(part)
                        if len(body) > max_bytes:
                            raise UrlFetchError(
                                f"{current} is over the cap of {max_bytes} bytes")
                    return Fetched(url=current,
                                   content_type=resp.headers.get("content-type", ""),
                                   data=bytes(body))
            except httpx.HTTPError as exc:
                raise UrlFetchError(f"could not fetch {current}: "
                                    f"{type(exc).__name__}: {exc}") from exc
    raise UrlFetchError(f"{url} redirected more than {MAX_REDIRECTS} times")


# ── read the page ─────────────────────────────────────────────────────


def _soup(html: str):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def html_to_markdown(html: str, base_url: str) -> Page:
    """The page's readable content as markdown — headings, paragraphs, lists.

    Chrome (nav, header, footer, aside, forms, scripts) is dropped; the
    content root is `<article>`, else `<main>`, else `<body>`.
    """
    soup = _soup(html)
    title = ""
    if soup.title and soup.title.get_text(strip=True):
        title = _squash(soup.title.get_text())
    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    h1 = root.find("h1")
    if h1 and h1.get_text(strip=True):
        title = _squash(h1.get_text())
    lines: list[str] = []
    for el in root.find_all(list(_BLOCK_TAGS)):
        # A <p> inside an <li> (or similar nesting) is emitted by the outer
        # block; skip the inner one so its text does not appear twice.
        if el.find_parent(list(_BLOCK_TAGS)) is not None:
            continue
        text = _squash(el.get_text(" "))
        if not text:
            continue
        if el.name.startswith("h"):
            lines.append(f"{'#' * int(el.name[1])} {text}")
        elif el.name == "li":
            lines.append(f"- {text}")
        elif el.name == "blockquote":
            lines.append(f"> {text}")
        else:
            lines.append(text)
    markdown = "\n\n".join(lines)
    return Page(title=title or urlparse(base_url).netloc, markdown=markdown,
                words=len(markdown.split()))


def detect_gating(html: str, base_url: str) -> Gating:
    """Is this page a gate in front of a document, rather than the document?"""
    soup = _soup(html)
    signals: list[str] = []

    email_form = any(
        f.find("input", attrs={"type": "email"})
        or f.find("input", attrs={"name": re.compile("e-?mail", re.I)})
        for f in soup.find_all("form")
    )
    map_form = bool(_MAP_FORM.search(html))
    form = email_form or map_form
    if map_form:
        signals.append("marketing-automation form")
    elif email_form:
        signals.append("email-capture form")

    visible = _squash(_soup(html).get_text(" "))
    phrase = _GATE_PHRASES.search(visible)
    if phrase:
        signals.append(f"“{_squash(phrase.group(0))}”")

    words = html_to_markdown(html, base_url).words
    thin = words < THIN_BODY_WORDS
    if thin:
        signals.append(f"thin body ({words} words)")

    pdf_links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if urlparse(href).path.lower().endswith(".pdf") and href not in pdf_links:
            pdf_links.append(href)
    if pdf_links:
        signals.append(f"links {len(pdf_links)} PDF(s) not fetched")

    gated = (form and (thin or bool(phrase))) or (bool(phrase) and thin)
    return Gating(gated=gated, signals=signals if gated else [], pdf_links=pdf_links)


# ── the file to upload ────────────────────────────────────────────────


def _slug(text: str, limit: int = 80) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:limit].rstrip("-")


def source_filename(url: str, title: str, ext: str) -> str:
    """A bare, stable file name: `<host>-<title or last path segment>.<ext>`."""
    parsed = urlparse(url)
    host = (parsed.hostname or "web").removeprefix("www.").split(".")[0]
    last = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    last = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", last)
    stem = _slug(title) or _slug(last) or "page"
    if not stem.startswith(_slug(host)):
        stem = f"{_slug(host)}-{stem}"
    return f"{stem}.{ext}"


def _kind(content_type: str, data: bytes, url: str) -> str | None:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype == "application/pdf" or data[:5] == b"%PDF-":
        return "pdf"
    if ctype in ("text/html", "application/xhtml+xml") or (
        not ctype and data.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html"))
    ):
        return "html"
    if ctype in ("text/plain", "text/markdown"):
        return "text"
    if not ctype and urlparse(url).path.lower().endswith(".pdf"):
        return "pdf"
    return None


def prepare(fetched: Fetched, *, fetched_on: str) -> Prepared:
    """Turn a fetched response into the file to upload. Raises `UrlFetchError`
    for a content type this path does not carry (fetch the document itself)."""
    kind = _kind(fetched.content_type, fetched.data, fetched.url)
    if kind is None:
        raise UrlFetchError(
            f"{fetched.url} is {fetched.content_type or 'an unknown type'}; ingest_url "
            "carries HTML pages, PDFs and plain text — download other files and "
            "push them with push_to_dropbox(ingest=True)"
        )
    if kind == "pdf":
        name = source_filename(fetched.url, "", "pdf")
        return Prepared(filename=name, data=fetched.data, kind="pdf", title=name,
                        gating=Gating(gated=False), words=None)

    text = fetched.data.decode("utf-8", errors="replace")
    if kind == "html":
        page = html_to_markdown(text, fetched.url)
        gating = detect_gating(text, fetched.url)
    else:
        page = Page(title="", markdown=text.strip(), words=len(text.split()))
        gating = Gating(gated=False)
    # The body is third-party text: it must not be able to write our note.
    body = page.markdown.replace(INGEST_NOTE_TAG, INGEST_NOTE_TAG.replace("-", " "))
    head = [f"# {page.title}" if page.title else "",
            f"Source: {fetched.url}", f"Fetched: {fetched_on} via ingest_url"]
    if gating.gated:
        head.append(f"> {INGEST_NOTE_TAG} {gating.note}")
        if gating.pdf_links:
            head.append("Linked PDFs (not fetched): " + ", ".join(gating.pdf_links))
    content = "\n\n".join(h for h in head if h) + "\n\n" + body + "\n"
    name = source_filename(fetched.url, page.title, "md")
    return Prepared(filename=name, data=content.encode("utf-8"), kind=kind,
                    title=name, gating=gating, words=page.words)
