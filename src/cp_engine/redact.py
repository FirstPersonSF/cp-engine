"""`cp redact-check` — verify an anonymised draft actually is anonymous.

From the 2026-09-04 RFP session (build spec §4). Anonymising that RFP took
three passes, and each miss was a *second-order* leak that a find-and-
replace on the client-name field would never have caught:

- The client name came out of the brief and stayed in **First Person's own
  client roster** in the boilerplate. A five-name credentials list with
  SAP Concur in it identifies the client in one step.
- The brief named **Ramp, Navan and Workday** as competitors. Those three
  names plus "spend management" identify the client instantly, without
  the client ever being named.
- The draft never said **"we'll name them under NDA once we're in
  conversation."** Without that line an anonymous brief reads as a
  fishing expedition, and the good shops pass.

So this runs as a **verification pass over the finished draft**, never as
substitution during generation. Substitution during generation is what
produces a document that looks redacted and isn't.

**What this module does and does not decide.** It finds what is
mechanical: a literal name, a competitor from a supplied list, our own
roster, a missing NDA line, a descriptor too vague to be useful. It does
NOT decide whether category + audience + competitor set still narrow the
client to a handful of companies — that is a judgment call, it belongs to
the `rfp-authoring` skill's redaction step, and `residual_risk` here
exists to make the model state its conclusion rather than skip it.

Composition is PURE: text in, findings out. No clock, no network, no
writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A descriptor has to be specific enough to be useful to a respondent and
# vague enough to protect the client. "A company" fails the first test;
# the spec's own example of a good one is "a global enterprise software
# company". Fewer than two informative words is not a descriptor.
_VAGUE_DESCRIPTORS = frozenset({
    "a company", "the company", "a client", "the client", "a business",
    "an organization", "an organisation", "a brand", "a customer",
})

# The line that makes an anonymous brief legible as a real engagement
# rather than a fishing expedition. Matched loosely — the wording is the
# author's, the commitment is what matters.
_NDA_LINE_RE = re.compile(
    r"\bunder (?:an? )?NDA\b|\bNDA\b.{0,60}\b(?:name|identify|disclose|share)\b"
    r"|\b(?:name|identify|disclose)\b.{0,60}\bunder (?:an? )?NDA\b",
    re.IGNORECASE,
)

_SEVERITY_ORDER = {"leak": 0, "risk": 1, "missing": 2}


@dataclass(frozen=True)
class Finding:
    """One redaction problem, with the evidence that proves it."""

    severity: str  # leak | risk | missing
    kind: str
    detail: str
    evidence: str = ""

    def render(self) -> str:
        icon = {"leak": "⛔", "risk": "⚠", "missing": "·"}.get(self.severity, "·")
        line = f"{icon} [{self.kind}] {self.detail}"
        if self.evidence:
            line += f"\n    → {self.evidence}"
        return line


@dataclass
class RedactionReport:
    clean: bool = False
    findings: list[Finding] = field(default_factory=list)
    residual_risk: str | None = None

    def to_dict(self) -> dict:
        return {
            "clean": self.clean,
            "findings": [
                {
                    "severity": f.severity,
                    "kind": f.kind,
                    "detail": f.detail,
                    "evidence": f.evidence,
                }
                for f in self.findings
            ],
            "residual_risk": self.residual_risk,
        }


def _contexts(text: str, needle: str, *, width: int = 60) -> list[str]:
    """Every occurrence of `needle`, with surrounding context.

    Word-boundary matched so "SAP" does not fire on "SAPling", and
    case-insensitive so a title-case slip is still caught.
    """
    if not needle.strip():
        return []
    out: list[str] = []
    pattern = re.compile(r"\b" + re.escape(needle.strip()) + r"\b", re.IGNORECASE)
    for m in pattern.finditer(text):
        start = max(0, m.start() - width)
        end = min(len(text), m.end() + width)
        snippet = re.sub(r"\s+", " ", text[start:end]).strip()
        out.append(f"…{snippet}…")
    return out


def check_redaction(
    draft: str,
    *,
    client_name: str,
    client_aliases: tuple[str, ...] = (),
    competitors: tuple[str, ...] = (),
    our_roster: tuple[str, ...] = (),
    descriptor: str | None = None,
) -> RedactionReport:
    """Verify that `draft` does not identify the client.

    `client_aliases` catches the short forms a find-and-replace misses
    ("SAP Concur" replaced, "Concur" left behind). `our_roster` is First
    Person's own credentials list — the second-order leak that cost the
    original session a pass. `competitors` are names that identify the
    client by triangulation even when the client is never named.

    Returns findings ordered by severity; `clean` is True only when
    nothing at `leak` or `missing` severity remains.
    """
    rep = RedactionReport()
    if not draft.strip():
        rep.findings.append(
            Finding("leak", "empty", "Draft is empty — nothing to verify.")
        )
        return rep

    # 1 — the client's own name, and its short forms.
    #
    # Longest first, and each occurrence claimed once: "SAP Concur" and
    # "Concur" are the SAME leak at one position, and reporting it twice
    # trains the reader to skim a list where every line matters.
    # Claim whole SPANS, not start offsets: "Concur" begins inside
    # "SAP Concur" at a different offset, so comparing starts alone lets
    # the alias re-report a position the full name already claimed.
    claimed: list[tuple[int, int]] = []
    for name in sorted({n.strip() for n in (client_name, *client_aliases) if n.strip()},
                       key=len, reverse=True):
        pattern = re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
        for m in pattern.finditer(draft):
            if any(m.start() < c_end and c_start < m.end()
                   for c_start, c_end in claimed):
                continue
            claimed.append((m.start(), m.end()))
            start, end = max(0, m.start() - 60), min(len(draft), m.end() + 60)
            hit = "…" + re.sub(r"\s+", " ", draft[start:end]).strip() + "…"
            rep.findings.append(
                Finding("leak", "client-name",
                        f"The client name “{name}” is still in the draft.",
                        hit)
            )

    # 2 — our own client roster. The leak that is invisible from the
    #     client-name field, because the name is ours, not theirs.
    for name in our_roster:
        if not name.strip():
            continue
        if any(name.strip().lower() == a.strip().lower()
               for a in (client_name, *client_aliases)):
            continue  # already reported above as a direct name leak
        for hit in _contexts(draft, name):
            rep.findings.append(
                Finding("risk", "our-roster",
                        f"“{name.strip()}” appears in our own credentials list. "
                        "If the redacted client is on that list, remove them "
                        "from it for THIS document.",
                        hit)
            )

    # 3 — competitors. Three named competitors plus a category is an
    #     identification, whether or not the client is ever named.
    named_competitors = [
        c.strip() for c in competitors if c.strip() and _contexts(draft, c)
    ]
    for c in named_competitors:
        rep.findings.append(
            Finding("risk", "competitor",
                    f"Competitor “{c}” is named — competitors identify a "
                    "client by triangulation.",
                    (_contexts(draft, c) or [""])[0])
        )
    if len(named_competitors) >= 2:
        rep.findings.append(
            Finding("leak", "competitor-set",
                    f"{len(named_competitors)} competitors named together "
                    f"({', '.join(named_competitors)}). A competitor SET plus a "
                    "category identifies the client in one step — this is the "
                    "5198 case exactly.")
        )

    # 4 — the descriptor has to be usable.
    if descriptor is not None:
        d = descriptor.strip().lower().rstrip(".")
        if d in _VAGUE_DESCRIPTORS or len(d.split()) < 3:
            rep.findings.append(
                Finding("missing", "descriptor",
                        f"Descriptor “{descriptor.strip()}” is too vague to be "
                        "useful. A respondent needs enough to judge fit: "
                        "“a global enterprise software company” works, "
                        "“a company” does not.")
            )

    # 5 — the NDA line.
    if not _NDA_LINE_RE.search(draft):
        rep.findings.append(
            Finding("missing", "nda-line",
                    "No NDA line. Without “we'll name them under NDA once "
                    "we're in conversation”, an anonymous brief reads as a "
                    "fishing expedition and good shops pass.")
        )

    rep.findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.kind))
    rep.clean = not any(f.severity in ("leak", "missing") for f in rep.findings)
    return rep


def render_report(rep: RedactionReport) -> str:
    """Human-readable rendering for the CLI."""
    L: list[str] = ["# Redaction check", ""]
    L.append("**CLEAN**" if rep.clean else "**NOT CLEAN**")
    L.append("")
    if rep.findings:
        for f in rep.findings:
            L.append(f.render())
            L.append("")
    else:
        L.append("_No mechanical findings._")
        L.append("")
    L.append("## Still your call")
    L.append("")
    L.append(
        "This pass finds what is mechanical. It does NOT decide whether "
        "category + audience + competitor set still narrow the client to a "
        "handful of companies. State that conclusion explicitly — the tool "
        "must never imply an anonymity it has not achieved."
    )
    L.append("")
    return "\n".join(L)
