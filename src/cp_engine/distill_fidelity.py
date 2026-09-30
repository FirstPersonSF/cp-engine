"""Distill fidelity — does a distilled body come from the source it claims? (#314)

A distiller is an LLM handed a source and asked to condense it. When the source
text it receives is empty, truncated, or the wrong document, it still returns
plausible, well-formed prose — and nothing downstream can tell. The instance
that motivated this: ``_authored/carol-s-our-ai-story-narrative-prose`` on
ibx-5153, written by ``cxp spine-recover`` on 2026-06-19, held a people-first
HR memo ("not pink slips", a 12-agent pilot). The four source documents it
cited are an infrastructure narrative; "pink slips" occurs in none of the
tenant's 170k characters of chunk text. A seven-week strategic concern was
built on it.

Two instruments, both pure (no I/O) so the engine AND the hosted server can
use them (this file is vendored verbatim under
``prototypes/hosted-mcp/vendor/cp_engine/``):

1. **Phrase overlap** — the share of the body's adjacent content-term pairs
   that occur in the source. A faithful distillation reuses the source's
   phrases even while paraphrasing; a fabricated one strings common words into
   phrases the source never contains. Single-term overlap is reported too but
   NOT judged on: against a long source nearly every word occurs somewhere
   (Carol v1 scores 0.74 on terms, 0.014 on phrases). Calibration: see
   ``FIDELITY_THRESHOLD``.

2. **The machine-derived marker** — ``field_states.body = "machine-derived"``.
   ``field_states`` is the existing per-field provenance column
   (``proposed|confirmed``); every human write path (mc-2 ``patch_substance``,
   hosted ``set_spine_element``) already stamps ``body: "confirmed"`` over
   whatever was there, so the marker clears the moment a human touches the
   body — no new column, no new clearing code.
"""
from __future__ import annotations

import re

# The field_states value that marks a body as machine-written and unverified.
MACHINE_DERIVED = "machine-derived"

# What a reader is shown while the marker stands.
MACHINE_DERIVED_LABEL = "machine-derived, unverified"

# The review_flags producer tag for fidelity flags (spine_sync._merge_flag keys
# flags per (field, source), so this never collides with reconcile/sweep flags).
FLAG_SOURCE = "distill-fidelity"

# Below this share of the body's PHRASES found in the source, the distill is
# flagged (`phrase_overlap`).
#
# Calibrated 2026-09-30, read-only, on live MC-2 rows across 26 projects.
# Genuine pairs — the lowest score each distill path produced:
#
#   meeting card vs its transcript         n=213  min 0.075  p05 0.099  med 0.18
#   promoted body vs the card it framed    n=22   min 0.183  p05 0.185  med 0.41
#
# The failure shape, manufactured: every one of those bodies re-scored against
# a DIFFERENT company's source — max 0.038 (cards) and 0.018 (promotes). The
# known-bad Carol v1 scores 0.014 against the four documents it cited.
#
# At 0.05: 0 of 235 genuine distills flagged; 235 of 235 wrong-source pairs
# caught; Carol v1 caught. Hand-authored elements are NOT distills and this
# check is never run on them — measured anyway, 10 of 33 source-backed ones
# fall under 0.05 (briefings, SOW versions and feedback relays that synthesise
# across documents rather than condense the one attached), which is why the
# check lives on the distill paths and not in a lint over the whole spine.
FIDELITY_THRESHOLD = 0.05

# A body with fewer distinct phrases than this is too short to judge; it is
# reported, never flagged.
MIN_BODY_PAIRS = 8

_STEM_LEN = 6

_WORD = re.compile(r"[a-z][a-z0-9]+(?:['’][a-z]+)?")

# Function words and discourse glue: present in any English text, so they say
# nothing about WHICH text a body came from. Kept deliberately modest — the
# 4-char floor already removes most of them.
_STOP_WORDS = """
about above after again against also although among another anything around
because been before being below between both but came cannot could does doing
done down during each either else enough even ever every first from further
given going good have having here hers herself himself however into itself just
keep know last least less like made make many maybe more most much must near
need never next none nothing often once only other others otherwise ought ours
ourselves over own part perhaps quite rather really same seem seems several
shall should since some something still such than that their theirs them
themselves then there these they thing things this those though through thus
together toward towards under unless until upon very want well were what
whatever when where whether which while whom whose will with within without
would your yours yourself yourselves yeah okay right sure mean think thats
dont doesnt didnt isnt wasnt arent youre were weve theyre going gonna
"""
_STOP = frozenset(_STOP_WORDS.split())


def _stem(word: str) -> str:
    """A deliberately crude stemmer: the first six letters.

    Distillation re-inflects words ("governs" → "governance", "tested" →
    "testing"); a six-letter prefix folds those without a stemming dependency,
    and the rare false merge only ever RAISES overlap — it can hide a weak
    distill, never flag a sound one.
    """
    return word[:_STEM_LEN]


def _term_seq(text: str) -> list[str]:
    """The content-term stems of ``text`` in reading order (function words and
    words under four letters dropped, so "the control point of the network"
    reads as ``contro point networ``)."""
    out: list[str] = []
    for w in _WORD.findall((text or "").lower()):
        w = w.replace("\u2019", "'").split("'")[0]
        if len(w) < 4 or w in _STOP:
            continue
        out.append(_stem(w))
    return out


def content_terms(text: str) -> set[str]:
    """The distinct content-term stems of ``text``."""
    return set(_term_seq(text))


def content_pairs(text: str) -> set[tuple[str, str]]:
    """The distinct ADJACENT content-term pairs of ``text`` — its phrases."""
    seq = _term_seq(text)
    return set(zip(seq, seq[1:], strict=False))


def term_overlap(body: str, source: str) -> float | None:
    """Share of ``body``'s content terms that occur in ``source`` (0..1), or
    ``None`` when the body has none. Reported, not judged on: against a long
    source nearly every English word occurs somewhere, so single terms cannot
    tell a fabricated body from a faithful one (the Carol v1 scores 0.74
    against its real 175k-character sources)."""
    b = content_terms(body)
    if not b:
        return None
    return len(b & content_terms(source)) / len(b)


def phrase_overlap(body: str, source: str) -> float | None:
    """Share of ``body``'s content-term PAIRS that occur in ``source`` (0..1),
    or ``None`` when the body has none.

    THE fidelity signal. A distillation re-uses its source's phrases ("control
    point", "security policy", "board approved") even while paraphrasing; an
    invented body strings the same common words into phrases the source never
    contains. Asymmetric on purpose — a distill is shorter than its source, so
    the question is whether what the body SAYS is drawn from the source, not
    whether it covers the source.
    """
    b = content_pairs(body)
    if not b:
        return None
    return len(b & content_pairs(source)) / len(b)


def assess(body: str, source: str, *,
           threshold: float = FIDELITY_THRESHOLD) -> dict:
    """Judge one distill against the source it was made from.

    Returns ``{"score", "term_score", "low", "reason", "body_pairs",
    "unmatched"}``:

    * ``score`` — `phrase_overlap`, the judged number.
    * ``low`` is True when the distill should NOT be trusted as written: the
      source text was empty (the distiller had nothing to be faithful TO, so
      whatever it wrote is invention), or the phrase overlap is below
      ``threshold`` on a body long enough to judge.
    * ``unmatched`` — a sample of body terms absent from the source, so a
      reader can see WHAT looks invented without re-reading both documents.
    """
    b_terms = content_terms(body)
    b_pairs = content_pairs(body)
    if not (source or "").strip():
        return {"score": 0.0, "term_score": 0.0, "low": True,
                "reason": "empty source text — nothing to distill from",
                "body_pairs": len(b_pairs), "unmatched": sorted(b_terms)[:12]}
    if not b_pairs:
        return {"score": None, "term_score": None, "low": False,
                "reason": "empty body", "body_pairs": 0, "unmatched": []}
    s_terms = content_terms(source)
    score = len(b_pairs & content_pairs(source)) / len(b_pairs)
    term_score = len(b_terms & s_terms) / len(b_terms) if b_terms else None
    unmatched = sorted(b_terms - s_terms)[:12]
    base = {"score": round(score, 3),
            "term_score": None if term_score is None else round(term_score, 3),
            "body_pairs": len(b_pairs), "unmatched": unmatched}
    if len(b_pairs) < MIN_BODY_PAIRS:
        return {**base, "low": False, "reason": "body too short to judge"}
    low = score < threshold
    reason = (f"only {score:.0%} of the body's phrases occur in the source "
              f"(threshold {threshold:.0%})") if low else "ok"
    return {**base, "low": low, "reason": reason}


def fidelity_flag(assessment: dict, *, source_label: str | None,
                  now_iso: str) -> dict:
    """The ``review_flags`` entry for a low-fidelity distill.

    Shaped like the reconcile flags (``field``/``was``/``now``/``at``/
    ``source``) so every existing review surface lists it without a new case.
    """
    return {
        "field": "body",
        "source": FLAG_SOURCE,
        "was": source_label,
        "now": assessment.get("reason"),
        "score": assessment.get("score"),
        "unmatched": list(assessment.get("unmatched") or [])[:12],
        "at": now_iso,
    }


def mark_machine_derived(row: dict, assessment: dict | None = None, *,
                         source_label: str | None = None,
                         now_iso: str | None = None) -> dict:
    """Stamp a ``spine_substance`` row dict as machine-derived (in place, and
    returned). Adds the fidelity flag when ``assessment`` says ``low``."""
    fs = dict(row.get("field_states") or {})
    fs["body"] = MACHINE_DERIVED
    row["field_states"] = fs
    if assessment is not None and assessment.get("low"):
        flags = [f for f in (row.get("review_flags") or [])
                 if not (f.get("field") == "body"
                         and f.get("source") == FLAG_SOURCE)]
        flags.append(fidelity_flag(assessment, source_label=source_label,
                                   now_iso=now_iso or ""))
        row["review_flags"] = flags
    return row


def provenance_of(row: dict) -> str | None:
    """``MACHINE_DERIVED_LABEL`` when a row's body is machine-written and no
    human has confirmed it since; else ``None``.

    Two shapes count: the explicit marker, and a disk-distilled row
    (``origin='distilled'``, written by the frame/promote distiller and synced
    up) whose body was never confirmed — those predate the marker, and sync
    owns their ``field_states``, so the rule reads them by origin instead.
    """
    body_state = (row.get("field_states") or {}).get("body")
    if body_state == "confirmed":
        return None
    if body_state == MACHINE_DERIVED:
        return MACHINE_DERIVED_LABEL
    if row.get("origin") == "distilled":
        return MACHINE_DERIVED_LABEL
    return None


def fidelity_flags_of(row: dict) -> list[dict]:
    """The open low-fidelity flags on a row (see `fidelity_flag`)."""
    return [f for f in (row.get("review_flags") or [])
            if isinstance(f, dict) and f.get("source") == FLAG_SOURCE]


def body_head(body: str, lines: int = 3, width: int = 200) -> list[str]:
    """The first ``lines`` non-blank lines of ``body``, each capped at
    ``width`` characters — what a supersede must show of the version it is
    about to hide."""
    out: list[str] = []
    for ln in (body or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        out.append(ln if len(ln) <= width else ln[: width - 1] + "…")
        if len(out) >= lines:
            break
    return out
