"""RFP respondent pipeline + vendor registry (build spec §3, §5).

Two stores with different lifetimes, deliberately kept apart:

- **`vendors`** — the cross-client registry. The eight shops assembled
  for one RFP are useful on every future one, so they outlive the project
  that occasioned them. They belong to no project, which is why they are
  a table and not spine (`spine_substance.project_id` is NOT NULL and
  `scope` means one company; this closes the spec's open question 2).
- **`rfp_respondents`** — the per-project pipeline. Who was invited to
  THIS RFP and where each one got to.

The status ladder, from §3:

    not_sent → sent → acknowledged → responded → shortlisted
                                                   ↓
                                    selected | declined | passed

`declined` is them saying no; `passed` is us not choosing them. Two
different facts about a relationship you will want again, and collapsing
them loses the one worth keeping.

**The two §5 rules live at the boundary, not in the model's judgment**,
because both were learned expensively:

1. **Never synthesise a contact email from a pattern.** Brokers sell
   `first.last@vendor.com`; a bounced RFP reads as carelessness to
   exactly the shops you most want. An address is storable only with a
   confidence that vouches for it, and the DB CHECK enforces it too.
2. **Watch-outs are as valuable as credentials.** "Feature in production
   may constrain Q4 capacity" changes the send order, so it is a
   first-class field rather than a note in prose.

Composition here is PURE — validation and rendering, no network. The
MC-2 reads/writes live in the MCP verbs that call these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ──────────────────────────────────────────────────────────────────────
#  Vocabulary — kept in lockstep with mig 169's CHECK constraints
# ──────────────────────────────────────────────────────────────────────

STATUSES: tuple[str, ...] = (
    "not_sent",
    "sent",
    "acknowledged",
    "responded",
    "shortlisted",
    "selected",
    "declined",
    "passed",
)

TERMINAL_STATUSES: frozenset[str] = frozenset({"selected", "declined", "passed"})

EMAIL_CONFIDENCE: tuple[str, ...] = (
    "confirmed",     # read off the company's own site
    "likely",        # two independent third-party sources agree
    "unpublished",   # looked for it; they do not publish one
    "unresearched",  # nobody has looked yet
)

# An address may only be stored when someone has vouched for it. This is
# the pattern-synthesis guard in code; mig 169 carries the same rule as a
# CHECK so a direct SQL write cannot bypass it either.
VOUCHED_CONFIDENCE: frozenset[str] = frozenset({"confirmed", "likely"})

# Forward moves in the ladder. Backwards is allowed (a correction), but
# it should be visible, so `advance` reports it rather than refusing.
_ORDER: dict[str, int] = {s: i for i, s in enumerate(STATUSES)}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

# Patterns a data broker sells. Not proof of fabrication on their own —
# plenty of real addresses are first.last@ — but combined with an
# unvouched confidence they are exactly the shape the rule exists for.
_PATTERN_SHAPES = (
    re.compile(r"^[a-z]+\.[a-z]+@", re.IGNORECASE),
    re.compile(r"^[a-z]\.[a-z]+@", re.IGNORECASE),
    re.compile(r"^[a-z]+_[a-z]+@", re.IGNORECASE),
)


class VendorError(ValueError):
    """A vendor or respondent write that must not reach the database."""


def slugify(name: str) -> str:
    """Stable registry key for a vendor name."""
    out = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not out:
        raise VendorError(f"vendor name {name!r} slugifies to nothing")
    return out


def validate_email(
    email: str | None, confidence: str
) -> tuple[str | None, str]:
    """Enforce §5 rule 1. Returns the pair to store, or raises.

    An address with an unvouched confidence is refused rather than
    silently downgraded: storing it and hoping someone reads the flag is
    how a synthesised address reaches a real send.
    """
    conf = (confidence or "unresearched").strip().lower()
    if conf not in EMAIL_CONFIDENCE:
        raise VendorError(
            f"email_confidence {confidence!r} is not one of "
            f"{', '.join(EMAIL_CONFIDENCE)}"
        )
    if email is None or not email.strip():
        return None, conf

    addr = email.strip()
    if not _EMAIL_RE.match(addr):
        raise VendorError(f"{addr!r} is not a well-formed email address")

    if conf not in VOUCHED_CONFIDENCE:
        shape = " It also matches a common broker pattern." if any(
            p.match(addr) for p in _PATTERN_SHAPES
        ) else ""
        raise VendorError(
            f"Refusing to store {addr!r} at confidence '{conf}'. An address is "
            "storable only as 'confirmed' (read off the company's own site) or "
            f"'likely' (two independent sources).{shape} Never synthesise one "
            "from a first.last@ pattern — a bounced RFP reads as carelessness "
            "to exactly the shops you most want."
        )
    return addr, conf


def validate_status(status: str) -> str:
    s = (status or "").strip().lower()
    if s not in _ORDER:
        raise VendorError(
            f"status {status!r} is not one of {', '.join(STATUSES)}"
        )
    return s


def advance(current: str, new: str) -> tuple[str, str | None]:
    """Move a respondent along the ladder.

    Returns `(status, note)`. `note` is set when the move is unusual —
    backwards, or out of a terminal state — so the caller can surface it.
    A correction is legitimate; a silent one is not.
    """
    cur, nxt = validate_status(current), validate_status(new)
    note = None
    if cur in TERMINAL_STATUSES and nxt != cur:
        note = (
            f"'{cur}' is a terminal state — reopening to '{nxt}'. "
            "Confirm this is a correction, not a second decision."
        )
    elif _ORDER[nxt] < _ORDER[cur]:
        note = f"moving backwards: '{cur}' → '{nxt}'."
    return nxt, note


# ──────────────────────────────────────────────────────────────────────
#  Rendering
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Respondent:
    vendor_name: str
    status: str = "not_sent"
    city: str | None = None
    contact_email: str | None = None
    email_confidence: str = "unresearched"
    watch_outs: str | None = None
    response_note: str | None = None
    decline_reason: str | None = None

    @property
    def contact_display(self) -> str:
        """Render an unvouched address differently — §5 rule 1.

        The registry can hold a route without an address; showing "no
        confirmed address" is honest, where showing a plausible-looking
        string invites someone to paste it into a send.
        """
        if not self.contact_email:
            return {
                "unpublished": "— (they publish none)",
                "unresearched": "— (not researched)",
            }.get(self.email_confidence, "—")
        marker = "" if self.email_confidence == "confirmed" else " ⚠ likely"
        return f"{self.contact_email}{marker}"


@dataclass
class Pipeline:
    project_code: str
    respondents: list[Respondent] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.respondents:
            out[r.status] = out.get(r.status, 0) + 1
        return out


def render_pipeline(pipe: Pipeline) -> str:
    """One scannable table plus the things that change the send order."""
    L: list[str] = [f"# RFP respondents — {pipe.project_code}", ""]
    if not pipe.respondents:
        L.append("_No respondents yet._")
        L.append("")
        return "\n".join(L)

    counts = pipe.counts()
    summary = " · ".join(
        f"{s}: {counts[s]}" for s in STATUSES if s in counts
    )
    L.append(f"**{len(pipe.respondents)} invited** — {summary}")
    L.append("")
    L.append("| Vendor | Status | City | Contact |")
    L.append("|---|---|---|---|")
    order = sorted(
        pipe.respondents, key=lambda r: (_ORDER.get(r.status, 99), r.vendor_name)
    )
    for r in order:
        L.append(
            f"| {r.vendor_name} | {r.status} | {r.city or '—'} "
            f"| {r.contact_display} |"
        )
    L.append("")

    watch = [r for r in order if r.watch_outs]
    if watch:
        L.append("## Watch-outs — these change the send order")
        L.append("")
        for r in watch:
            L.append(f"- **{r.vendor_name}** — {r.watch_outs}")
        L.append("")

    unvouched = [
        r for r in order
        if r.status == "not_sent" and r.email_confidence not in VOUCHED_CONFIDENCE
    ]
    if unvouched:
        L.append("## No confirmed address")
        L.append("")
        L.append(
            "Do not synthesise one. Find the route on their own site, or use "
            "the contact form — a bounced RFP reads as carelessness."
        )
        L.append("")
        for r in unvouched:
            L.append(f"- {r.vendor_name} ({r.email_confidence})")
        L.append("")
    return "\n".join(L)
