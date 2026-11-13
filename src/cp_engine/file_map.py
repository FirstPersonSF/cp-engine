"""Where each kind of project file is kept, between Dropbox and Google Drive.

Every project has a Dropbox folder and a Google Drive folder, and the folders
inside each are laid out the same way on every project (Drew confirmed the
template on 2026-11-13). Until this module, cp knew nothing about that layout:
two paths were typed into the hosted server and nothing else was written down.
On 2026-11-13 a session pushed an invoice to Dropbox `03 Assets/06 Spine` when
it was already filed on Drive under `Management/02 Contracts and
Financials/05 Invoices PDFs`, because nothing told it where invoices live.
Drew: "I think we should teach cp where all of the files are kept between
dropbox and drive".

THIS IS THE ONE MAP. Everything that needs a folder reads it from here:

- the hosted `where_is` verb, which resolves a kind for one project and lists
  the folder;
- `push_to_dropbox(kind=...)`, which files an upload by kind;
- the "Where files live" section of the generated tenant `CLAUDE.md`
  (`render_markdown`);
- the two paths the hosted server used to hard-code (`SPINE_OUTPUT_DIR`,
  `URL_SOURCE_DIR`), which are now lookups into `FILE_KINDS`.

Paths are RELATIVE to the project's own folder in that store. The project's
folder itself comes from MC-2 (`project_integrations`), never from here.

To change where a kind of file lives, change its entry below; nothing else
holds a copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DRIVE = "drive"
DROPBOX = "dropbox"

STORE_NAMES = {DRIVE: "Google Drive", DROPBOX: "Dropbox"}

@dataclass(frozen=True)
class FileKind:
    """One kind of project file and the folder it is kept in."""

    key: str                      # the short name to ask for it by
    label: str                    # what it is, in plain words
    store: str                    # DRIVE or DROPBOX
    path: str                     # relative to the project's folder in `store`
    note: str = ""                # anything a reader must know before filing here
    aliases: tuple[str, ...] = ()  # other words people use for it

    @property
    def store_name(self) -> str:
        return STORE_NAMES[self.store]

    @property
    def segments(self) -> tuple[str, ...]:
        """The folder names from the project's folder down to this one."""
        return tuple(self.path.split("/"))


_FINANCIALS = "Management/02 Contracts and Financials"
_EXPENSES = "Management/01 1P Expenses"
_LIVE_ACTION = "Management/03 Live Action and Talent"
_DEFINE = "Define and Approach"

FILE_KINDS: tuple[FileKind, ...] = (
    # ── Google Drive ──
    FileKind("invoices", "invoices to the client", DRIVE,
             f"{_FINANCIALS}/05 Invoices PDFs", aliases=("invoice", "client invoices")),
    FileKind("sows", "statements of work", DRIVE,
             f"{_FINANCIALS}/03 SOWs", aliases=("sow", "statement of work")),
    FileKind("estimates", "estimates", DRIVE,
             f"{_FINANCIALS}/02 Estimate PDFs", aliases=("estimate",)),
    FileKind("purchase-orders", "purchase orders", DRIVE,
             f"{_FINANCIALS}/04 Purchase Orders", aliases=("po", "pos", "purchase order")),
    FileKind("engagement-summaries", "engagement summaries", DRIVE,
             f"{_FINANCIALS}/01 Engagement Summary",
             aliases=("engagement summary", "proposal", "proposals")),
    FileKind("vendor-invoices", "invoices from vendors", DRIVE,
             f"{_EXPENSES}/Vendor Invoices", aliases=("vendor invoice",)),
    FileKind("vendor-contracts", "vendor contracts and estimates", DRIVE,
             f"{_EXPENSES}/Vendor Contracts and Estimates",
             aliases=("vendor contract", "vendor estimates", "vendor estimate")),
    FileKind("receipts", "credit card receipts", DRIVE,
             f"{_EXPENSES}/Credit Card Receipts", aliases=("receipt",)),
    FileKind("live-action", "shoot paperwork", DRIVE,
             f"{_LIVE_ACTION}/Live Action", aliases=("shoot",)),
    FileKind("talent", "talent paperwork", DRIVE,
             f"{_LIVE_ACTION}/Talent"),
    FileKind("wrap", "wrap reports", DRIVE,
             "Management/04 Wrap", aliases=("wrap report", "wrap reports")),
    FileKind("research", "research, interviews and surveys", DRIVE,
             f"{_DEFINE}/01 Research"),
    FileKind("trackers", "trackers and calculators", DRIVE,
             f"{_DEFINE}/02 Trackers and Calcs", aliases=("tracker", "calcs")),
    FileKind("narrative", "narrative and strategy documents", DRIVE,
             f"{_DEFINE}/03 Narrative"),
    FileKind("client-inputs-google",
             "what the client sends us, when it is a Google Doc, Sheet or Slides deck",
             DRIVE, f"{_DEFINE}/04 Client Inputs",
             note=("Only Google-native files (Docs, Sheets, Slides). Every other "
                   "client input goes on Dropbox: see `client-inputs`.")),
    FileKind("client-share", "documents we share with the client", DRIVE,
             f"{_DEFINE}/05 Client Share"),
    # ── Dropbox ──
    FileKind("client-inputs",
             "what the client sends us: documents, brand assets, footage, fonts",
             DROPBOX, "03 Assets/01 Client Assets",
             note=("Everything the client sends except Google-native files (Docs, "
                   "Sheets, Slides), which go on Drive: see `client-inputs-google`."),
             aliases=("client assets",)),
    FileKind("audio", "audio", DROPBOX, "03 Assets/02 Audio"),
    FileKind("renders", "renders", DROPBOX, "03 Assets/03 Renders", aliases=("render",)),
    FileKind("sources-and-stock", "source footage, photos and stock", DROPBOX,
             "03 Assets/04 Sources and Stock", aliases=("stock", "sources")),
    FileKind("working-files",
             "working files: Premiere, After Effects, Illustrator, decks in progress",
             DROPBOX, "02 Working Files"),
    FileKind("deliverables", "finished deliverables", DROPBOX, "04 Deliverables",
             aliases=("deliverable",)),
    FileKind("project-notes", "project notes", DROPBOX, "01 Project Notes",
             aliases=("notes",)),
    FileKind("spine", "documents cp generates", DROPBOX, "03 Assets/06 Spine",
             note=("An initiative's Dropbox folder has no template: there, "
                   "generated documents go in `Spine`.")),
)

_BY_KEY = {k.key: k for k in FILE_KINDS}

# An initiative's Dropbox folder is `<root>/<name>/Spine` and nothing else
# (mc-2, 2026-11-11): it has no template, so generated work lands there.
INITIATIVE_SPINE_DIR = "Spine"

# Drew's rule, 2026-11-13. Shown wherever the map is shown. Built from the
# two entries it is about, so it cannot name a folder the map does not.
CLIENT_INPUTS_RULE = (
    "What the client sends us goes on Dropbox, in "
    f"`{_BY_KEY['client-inputs'].path}`. The one exception is a Google-native "
    "file (a Google Doc, Sheet or Slides deck): that goes on Drive, in "
    f"`{_BY_KEY['client-inputs-google'].path}`."
)


def get(key: str) -> FileKind:
    """The kind with exactly this key. Raises KeyError for anything else:
    for code that names a kind it knows exists. People's words go to `find`."""
    return _BY_KEY[key]


def kinds(store: str | None = None) -> tuple[FileKind, ...]:
    """Every kind, in map order; one store's when `store` is given."""
    return tuple(k for k in FILE_KINDS if store is None or k.store == store)


# ── lookup ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Lookup:
    """What `find` found: one `kind`, or several `candidates`, or neither."""

    kind: FileKind | None = None
    candidates: tuple[FileKind, ...] = ()


def _norm(text: str) -> str:
    """Lower-case words joined by single hyphens: `Purchase_Orders ` → `purchase-orders`."""
    return "-".join(re.findall(r"[a-z0-9]+", (text or "").casefold()))


def _words(kind: FileKind) -> set[str]:
    out: set[str] = set()
    for text in (kind.key, kind.label, *kind.aliases):
        out.update(_norm(text).split("-"))
    out.discard("")
    return out


def find(query: str) -> Lookup:
    """Find a kind from a key, an alias, or a distinctive part of a label.

    Never guesses. In order: an exact key; an exact alias or label; then
    every kind whose key, label and aliases together contain every word asked
    for (a word of three letters or more also matches the start of a longer
    word, so `receip` finds `receipts`). One match is the answer; several
    come back as `candidates` for the caller to choose from; none is an
    empty `Lookup`.
    """
    wanted = _norm(query)
    if not wanted:
        return Lookup()
    if wanted in _BY_KEY:
        return Lookup(kind=_BY_KEY[wanted])
    exact = [k for k in FILE_KINDS
             if wanted in {_norm(a) for a in (*k.aliases, k.label)}]
    if len(exact) == 1:
        return Lookup(kind=exact[0])
    if exact:
        return Lookup(candidates=tuple(exact))

    asked = wanted.split("-")

    def _has(word: str, have: set[str]) -> bool:
        return word in have or (len(word) >= 3 and any(h.startswith(word) for h in have))

    hits = [k for k in FILE_KINDS if all(_has(w, _words(k)) for w in asked)]
    if len(hits) == 1:
        return Lookup(kind=hits[0])
    return Lookup(candidates=tuple(hits))


# ── the tenant CLAUDE.md section ──────────────────────────────────────


def render_markdown() -> str:
    """The map as short markdown, for the generated tenant `CLAUDE.md`.

    One line per kind, grouped by store, then the client-inputs rule. The
    template adds the heading and the instruction to call `where_is`."""
    lines: list[str] = []
    for store in (DRIVE, DROPBOX):
        lines.append(f"**{STORE_NAMES[store]}**")
        lines.append("")
        for kind in kinds(store):
            lines.append(f"- `{kind.key}` ({kind.label}): `{kind.path}`")
        lines.append("")
    lines.append(f"**Client inputs.** {CLIENT_INPUTS_RULE}")
    return "\n".join(lines)
