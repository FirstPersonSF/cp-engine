"""`cp preflight` — is this project ready for an artifact to be drafted?

Built from the 2026-09-04 RFP session, where a complete, plausible,
well-formatted production RFP was written against **sap-5200**, a
competitive-messaging project with no video in it at all. Nothing in the
system objected. The `cp.md` was an empty scaffold created the day
before — every exec-summary field still placeholder text — and the
generated document was indistinguishable in polish from a real one.

That is the failure this module exists to make impossible: **a confident
artifact drafted from an empty or wrong-shaped project.** The check runs
before any drafting and answers three questions in order of severity:

1. **Is this the right SHAPE of project?** (`shape_warning`) A production
   RFP does not fit a strategy engagement. One string here would have
   ended the 5200 session in forty seconds instead of forty minutes.
2. **What does CP actually KNOW?** (`found`) Read broadly — the real 5198
   scope lived in the sprint file's Inbound bullets, while the structured
   deliverables card said `(no deliverables in the estimate yet)`. The
   structured field was empty while the knowledge was rich, so a reader
   that only trusts structure sees an empty project.
3. **What is missing or contradictory?** (`missing`, `conflicts`) Missing
   drives elicitation — only ask the human what CP genuinely doesn't
   know. Conflicts are surfaced, never silently resolved: the 5198
   delivery date was Dec 15 in the client deck and "post into 2027" in
   the exec summary, and that disagreement was real and load-bearing.

Composition is PURE: text in, a report dataclass out. No clock reads, no
network, no writes. Every source is best-effort — a project with no
sprint file still gets a report, with that absence recorded rather than
raised. The point is to survive a thin project and *say* it is thin.

Deliberately generalized past RFPs (`artifact_kind`): the same three
questions apply to SOWs, briefs and estimates, so the kind-specific part
is a rules table (`_ARTIFACT_RULES`) rather than a separate verb.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from cp_engine.exec_summary_lint import _PLACEHOLDER_RE, _split_fields
from cp_engine.render import slice_exec_summary_region

# ──────────────────────────────────────────────────────────────────────
#  Artifact rules
# ──────────────────────────────────────────────────────────────────────

# What each artifact kind needs before it can honestly be drafted, and
# what shape of project it fits. `requires` drives `missing`; `shape`
# drives `shape_warning`.
#
# `production_shaped` means the project actually makes something filmed,
# designed or built. The 5200 lesson: a project whose deliverables are
# messaging frameworks and architecture diagrams is not production-
# shaped, and a production RFP against it is a category error, not a
# thin draft.


@dataclass(frozen=True)
class ArtifactRule:
    """One artifact kind's readiness contract."""

    label: str
    requires: tuple[str, ...]
    # Signals that the project is the right shape. Matched case-insensitively
    # against deliverables + objective + status text.
    shape_signals: tuple[str, ...]
    # Signals that it is the WRONG shape — strategy/messaging work that
    # produces documents rather than production assets.
    antishape_signals: tuple[str, ...]
    shape_note: str


_PRODUCTION_SIGNALS = (
    "video", "film", "shoot", "footage", "edit", "post", "production",
    "spot", ":30", ":15", ":06", "animation", "motion", "photography",
    "campaign", "creative", "broadcast", "cut", "talent", "director",
)
_STRATEGY_SIGNALS = (
    "messaging framework", "competitive", "positioning", "narrative",
    "architecture", "strategy", "playbook", "audit", "research",
    "workshop", "readout", "assessment",
)

_ARTIFACT_RULES: dict[str, ArtifactRule] = {
    "rfp": ArtifactRule(
        label="RFP (production partner)",
        # partner_budget is NOT in `requires`: it is an argument to the
        # draft verb, not a fact CP holds. Conflating it with the
        # engagement fee is the error this whole feature guards against,
        # so preflight refuses to infer it from anything it reads.
        requires=("deliverables", "audience", "schedule"),
        shape_signals=_PRODUCTION_SIGNALS,
        antishape_signals=_STRATEGY_SIGNALS,
        shape_note=(
            "This project's deliverables read as strategy artifacts "
            "(messaging, positioning, architecture). A production RFP is "
            "not applicable — an RFP asks a partner to MAKE something."
        ),
    ),
    "sow": ArtifactRule(
        label="SOW",
        requires=("deliverables", "schedule", "engagement_fee"),
        shape_signals=(),  # a SOW fits any engagement shape
        antishape_signals=(),
        shape_note="",
    ),
    "brief": ArtifactRule(
        label="Creative brief",
        requires=("deliverables", "audience"),
        shape_signals=(),
        antishape_signals=(),
        shape_note="",
    ),
    "estimate": ArtifactRule(
        label="Estimate",
        requires=("deliverables",),
        shape_signals=(),
        antishape_signals=(),
        shape_note="",
    ),
}

ARTIFACT_KINDS: tuple[str, ...] = tuple(_ARTIFACT_RULES)


# ──────────────────────────────────────────────────────────────────────
#  Report
# ──────────────────────────────────────────────────────────────────────


@dataclass
class PreflightReport:
    """Structured readiness answer. Never prose — the caller renders it.

    `ready` is deliberately conservative: it is False whenever anything
    required is missing, the scaffold is unauthored, or the shape is
    wrong. A caller that wants to proceed anyway must do so explicitly.
    """

    project_code: str
    artifact_kind: str
    ready: bool = False
    confidence: str = "none"  # none | partial | good
    found: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    shape_warning: str | None = None
    sources_read: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "project_code": self.project_code,
            "artifact_kind": self.artifact_kind,
            "ready": self.ready,
            "confidence": self.confidence,
            "found": self.found,
            "missing": self.missing,
            "conflicts": self.conflicts,
            "stale": self.stale,
            "shape_warning": self.shape_warning,
            "sources_read": self.sources_read,
            "notes": self.notes,
        }


# ──────────────────────────────────────────────────────────────────────
#  Extraction
# ──────────────────────────────────────────────────────────────────────

_DELIVERABLE_HINTS = re.compile(
    r"(:\d{2}\b|\b\d+x\d+\b|\bvideo\b|\bfilm\b|\bspot\b|\bcut\b|\bdeck\b|"
    r"\bplaybook\b|\breport\b|\bframework\b|\bsite\b|\bwebsite\b|"
    r"\bcampaign\b|\bstills?\b|\bsource files?\b)",
    re.IGNORECASE,
)
_AUDIENCE_HINTS = re.compile(
    r"\baudience\b|\bdecision[- ]makers?\b|\bbuyers?\b|\bICP\b|"
    r"\bCISO\b|\bCIO\b|\bpractitioners?\b|\bSMB\b|\bmid[- ]market\b|"
    r"\benterprise\b",
    re.IGNORECASE,
)
_FEE_HINTS = re.compile(r"\$[\d,]+(?:k|K)?\b|\bfixed fee\b|\bnot[- ]to[- ]exceed\b")
_SCHEDULE_HINTS = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\b|"
    r"\b\d{4}-\d{2}-\d{2}\b|\bQ[1-4]\b|\bdeliver(?:y|ed)?\b|\bshoot\b|"
    r"\blaunch\b|\bin[- ]market\b",
    re.IGNORECASE,
)
_USAGE_HINTS = re.compile(r"\busage\b|\brights?\b|\bterm\b|\bperpetu", re.IGNORECASE)

# Date-ish tokens, for conflict detection across sources.
_DATE_TOKEN = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s*\d{4}?\b"
    r"|\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)


# Bookkeeping noise that must never reach a readiness report: hash
# markers, bracketed status metadata, and markdown link targets.
_HASH_MARKER_RE = re.compile(r"<!--\s*cp:[^>]*-->")
_BRACKET_META_RE = re.compile(r"^\[[^\]]{0,120}\]\s*")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _clean(text: str) -> str:
    """Strip markdown emphasis, tracking metadata and collapse whitespace.

    The metadata strip matters for the report's legibility: a bullet
    carrying `<!-- cp:hash=… -->` and a `[ask · date · owner]` prefix is
    a tracking row, and echoing it verbatim turns a readiness answer back
    into something the reader has to parse."""
    out = _HASH_MARKER_RE.sub("", text)
    out = _MD_LINK_RE.sub(r"\1", out)
    out = out.replace("**", "").replace("`", "").replace("_", " ")
    out = out.strip().lstrip("-").strip()
    out = _BRACKET_META_RE.sub("", out)
    return re.sub(r"\s+", " ", out).strip()


def is_unauthored_scaffold(exec_region: str | None) -> bool:
    """True when the exec summary is still template placeholders.

    The 5200 tell. A scaffold created by sync has every field seeded with
    `_<...>_`; a project nobody has written up yet looks structurally
    identical to one that is simply quiet, and only the placeholders
    distinguish them.
    """
    if not exec_region or not exec_region.strip():
        return True
    fields = _split_fields(exec_region)
    if not fields:
        return True
    substantive = 0
    for label, body in fields.items():
        if label == "Updates":
            continue  # migration stubs land here and prove nothing
        stripped = _PLACEHOLDER_RE.sub("", body).strip(" _-\n\t")
        if len(stripped) > 12:
            substantive += 1
    return substantive == 0


def _harvest(texts: list[str], pattern: re.Pattern) -> list[str]:
    """Return cleaned lines from `texts` that match `pattern`."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in texts:
        line = _clean(raw)
        if not line or len(line) < 8:
            continue
        if not pattern.search(line):
            continue
        # A 300-word exec-summary paragraph technically "matches", but a
        # readiness report that quotes it has answered nothing. Prefer
        # fact-shaped lines and clip the rest hard.
        if len(line) > 400:
            continue
        key = line.lower()[:90]
        if key in seen:
            continue
        seen.add(key)
        out.append(line if len(line) <= 180 else line[:177] + "…")
    return out


def _shape_check(rule: ArtifactRule, corpus: list[str]) -> str | None:
    """Is the project the right shape for this artifact kind?

    Returns a warning string, or None when the shape fits or when the
    kind has no shape opinion.

    Only fires when there is positive evidence of the WRONG shape and
    none of the right one. A thin project produces neither signal and
    must not be shape-warned — that is a `missing` problem, and calling
    it a shape error would send the reader to fix the wrong thing.
    """
    if not rule.shape_signals and not rule.antishape_signals:
        return None
    blob = " ".join(_clean(t).lower() for t in corpus)
    if not blob.strip():
        return None
    hits = [s for s in rule.shape_signals if s in blob]
    anti = [s for s in rule.antishape_signals if s in blob]
    if hits:
        return None
    if not anti:
        return None
    return (
        f"{rule.shape_note} Matched: {', '.join(sorted(anti)[:4])}. "
        f"No production signals found."
    )


def _date_conflicts(labelled: list[tuple[str, str]]) -> list[str]:
    """Surface disagreeing dates across sources.

    Deliberately reports rather than resolves. The 5198 delivery date was
    Dec 15 per the client deck and "post into 2027" per the exec summary;
    picking one silently would have baked an unowned decision into a
    client-facing document.
    """
    buckets: dict[str, set[str]] = {}
    for source, text in labelled:
        for tok in _DATE_TOKEN.findall(text):
            buckets.setdefault(_clean(tok).lower(), set()).add(source)
    years: dict[str, set[str]] = {}
    for tok, sources in buckets.items():
        m = re.search(r"(20\d{2})", tok)
        if m:
            years.setdefault(m.group(1), set()).update(sources)
    if len(years) > 1:
        parts = [f"{y} (from {', '.join(sorted(s))})" for y, s in sorted(years.items())]
        return [
            "Delivery/schedule years disagree across sources: "
            + "; ".join(parts)
            + " — resolve before drafting."
        ]
    return []


# ──────────────────────────────────────────────────────────────────────
#  The check
# ──────────────────────────────────────────────────────────────────────


def run_preflight(
    project_code: str,
    artifact_kind: str,
    *,
    cp_md_text: str | None = None,
    sprint_texts: list[tuple[str, str]] | None = None,
    spine_titles: list[str] | None = None,
    source_titles: list[str] | None = None,
) -> PreflightReport:
    """Assess whether `project_code` can support drafting `artifact_kind`.

    Pure. `sprint_texts` is `[(label, raw_markdown)]` newest first;
    passing raw text rather than parsed objects keeps this testable and
    lets the caller decide how many weeks to read.

    Raises ValueError on an unknown `artifact_kind` — a typo must not
    silently degrade to "everything is fine".
    """
    kind = artifact_kind.strip().lower()
    if kind not in _ARTIFACT_RULES:
        raise ValueError(
            f"unknown artifact_kind {artifact_kind!r}; "
            f"expected one of {', '.join(ARTIFACT_KINDS)}"
        )
    rule = _ARTIFACT_RULES[kind]
    rep = PreflightReport(project_code=project_code, artifact_kind=kind)

    sprint_texts = sprint_texts or []
    spine_titles = spine_titles or []
    source_titles = source_titles or []

    # ---- Gate 1: is there anything authored at all? -------------------
    exec_region = slice_exec_summary_region(cp_md_text) if cp_md_text else None
    if cp_md_text is None:
        rep.notes.append("No cp.md found for this project.")
    else:
        rep.sources_read.append("cp.md exec summary")

    scaffold = is_unauthored_scaffold(exec_region)
    if scaffold:
        rep.notes.append(
            "Exec summary is an unauthored scaffold — every field is still "
            "placeholder text. This is the 5200 signature: a project that "
            "exists in MC-2 but has never been written up. Nothing should be "
            "drafted from it."
        )

    fields = _split_fields(exec_region) if exec_region else {}
    objective = fields.get("Objective", "")
    status = fields.get("Status", "")
    where = fields.get("Where it stands", "")
    blockers = fields.get("Blockers", "")

    # ---- Gather the corpus -------------------------------------------
    # Read broadly on purpose. The 5198 scope lived in sprint Inbound
    # bullets while the deliverables card was empty; a reader that only
    # trusts the structured field sees an empty project.
    corpus_lines: list[str] = []
    labelled: list[tuple[str, str]] = []
    for label, body in ((("cp.md Objective"), objective), ("cp.md Status", status),
                        ("cp.md Where it stands", where)):
        if body and not _PLACEHOLDER_RE.search(body):
            corpus_lines.extend(body.splitlines())
            labelled.append((label, body))

    for label, raw in sprint_texts:
        if not raw:
            continue
        rep.sources_read.append(label)
        for line in raw.splitlines():
            s = line.strip()
            if not s.startswith("- ") or _PLACEHOLDER_RE.search(s):
                continue
            corpus_lines.append(s[2:])
            labelled.append((label, s))

    if spine_titles:
        rep.sources_read.append(f"spine ({len(spine_titles)} elements)")
        corpus_lines.extend(spine_titles)
    if source_titles:
        rep.sources_read.append(f"sources ({len(source_titles)} docs)")
        corpus_lines.extend(source_titles)

    # ---- Gate 2: shape ------------------------------------------------
    rep.shape_warning = _shape_check(rule, corpus_lines)

    # ---- Gather found facts -------------------------------------------
    deliverables = _harvest(corpus_lines, _DELIVERABLE_HINTS)
    audience = _harvest(corpus_lines, _AUDIENCE_HINTS)
    schedule = _harvest(corpus_lines, _SCHEDULE_HINTS)
    fee = _harvest(corpus_lines, _FEE_HINTS)
    usage = _harvest(corpus_lines, _USAGE_HINTS)

    if deliverables:
        rep.found["deliverables"] = deliverables[:8]
    if audience:
        rep.found["audience"] = audience[:3]
    if schedule:
        rep.found["schedule"] = schedule[:5]
    if fee:
        # Named `engagement_fee`, never `budget`. The $425k engagement fee
        # is NOT the partner budget, and a field called `budget` invites
        # exactly the conflation that would have been a serious error.
        rep.found["engagement_fee"] = fee[:3]
    if usage:
        rep.found["usage"] = usage[:3]

    # ---- Missing ------------------------------------------------------
    for req in rule.requires:
        if req not in rep.found:
            rep.missing.append(req)

    # partner_budget is never derivable from anything CP holds.
    if kind == "rfp":
        rep.missing.append("partner_budget")
        rep.notes.append(
            "partner_budget must be supplied explicitly — it is NOT the "
            "engagement fee, and preflight will not infer one from the other."
        )

    # ---- Conflicts + stale --------------------------------------------
    rep.conflicts.extend(_date_conflicts(labelled))
    # Blockers, minus the scaffold seed. An unauthored `_<what's stuck…>_`
    # rendered as a real blocker is precisely the confident-noise this
    # tool exists to suppress, so filter placeholders before reporting.
    if blockers and not _PLACEHOLDER_RE.search(blockers):
        for raw in blockers.splitlines():
            line = _clean(raw.lstrip("- ").strip())
            if line and line.lower() not in ("none", "none.") and len(line) > 3:
                rep.stale.append(line)

    # ---- Verdict -------------------------------------------------------
    hard_block = scaffold or rep.shape_warning is not None
    # partner_budget is elicited, not blocking on its own.
    substantive_missing = [m for m in rep.missing if m != "partner_budget"]
    rep.ready = not hard_block and not substantive_missing
    if hard_block:
        rep.confidence = "none"
    elif substantive_missing:
        rep.confidence = "partial"
    elif rep.conflicts:
        # Everything required is present, but two sources disagree about
        # it. Ready to draft, not ready to trust unread — the conflict is
        # a decision someone owes, and the draft will have to take a
        # position on it either way.
        rep.confidence = "partial"
    elif rep.found:
        rep.confidence = "good"
    else:
        rep.confidence = "none"
    return rep


# ──────────────────────────────────────────────────────────────────────
#  Rendering
# ──────────────────────────────────────────────────────────────────────


def render_report(rep: PreflightReport) -> str:
    """Human-readable rendering. The verb returns the dict; this is CLI."""
    L: list[str] = []
    verdict = "READY" if rep.ready else "NOT READY"
    L.append(f"# Preflight — {rep.project_code} · {rep.artifact_kind}")
    L.append("")
    L.append(f"**{verdict}** · confidence: {rep.confidence}")
    L.append("")

    if rep.shape_warning:
        L.append("## ⛔ Wrong shape")
        L.append("")
        L.append(rep.shape_warning)
        L.append("")

    if rep.found:
        L.append("## Found")
        L.append("")
        for key, val in rep.found.items():
            L.append(f"**{key}**")
            for v in (val if isinstance(val, list) else [val]):
                L.append(f"- {v}")
            L.append("")
    else:
        L.append("## Found")
        L.append("")
        L.append("_Nothing. CP holds no usable facts for this project._")
        L.append("")

    if rep.missing:
        L.append("## Missing")
        L.append("")
        for m in rep.missing:
            L.append(f"- {m}")
        L.append("")

    if rep.conflicts:
        L.append("## ⚠ Conflicts — resolve before drafting")
        L.append("")
        for c in rep.conflicts:
            L.append(f"- {c}")
        L.append("")

    if rep.stale:
        L.append("## Blockers / stale")
        L.append("")
        for s in rep.stale[:6]:
            L.append(f"- {s}")
        L.append("")

    if rep.notes:
        L.append("## Notes")
        L.append("")
        for n in rep.notes:
            L.append(f"- {n}")
        L.append("")

    L.append("## Sources read")
    L.append("")
    L.append(", ".join(rep.sources_read) if rep.sources_read else "_none_")
    L.append("")
    return "\n".join(L)


def collect_sprint_texts(
    tenant_root: Path, project_code: str, *, weeks: int = 3
) -> list[tuple[str, str]]:
    """Read the newest `weeks` sprint files for a project, newest first.

    Best-effort by design: an unreadable or absent week is skipped, not
    raised. Returns `[(label, raw_markdown)]`.
    """
    sprints_dir = tenant_root / "sprints"
    if not sprints_dir.is_dir():
        return []
    out: list[tuple[str, str]] = []
    try:
        week_dirs = sorted(
            (d for d in sprints_dir.iterdir() if d.is_dir()),
            key=lambda d: d.name,
            reverse=True,
        )
    except OSError:
        return []
    for wd in week_dirs:
        if len(out) >= weeks:
            break
        f = wd / f"{project_code}.md"
        if not f.is_file():
            continue
        try:
            out.append((f"sprint {wd.name}", f.read_text(encoding="utf-8")))
        except OSError:
            continue
    return out
