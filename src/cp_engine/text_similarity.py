"""Cheap, deterministic topic similarity for ingest bullets (#321, #322).

Two bullets are about the same thing when they share most of their content
words. This is a bag-of-words measure on purpose: no model, no embedding,
nothing that can drift between runs. The callers use it for two narrow
checks — an in-meeting reversal (two decisions on one topic in one plan)
and a cross-routed near-duplicate (one risk arriving twice on a target) —
and both were tuned against the tenant's real sprint files, not fixtures.
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"[a-z0-9$][a-z0-9$.\-']*[a-z0-9k%]|[a-z0-9]")
_MARKER_RE = re.compile(r"<!--.*?-->|\[(?:cross-project\?|cross-routed from|attribution unverified)[^\]]*\]")
_AMOUNT_RE = re.compile(r"\$\s?\d[\d,.]*\s?[kKmM]?\b|\b\d[\d,.]*\s?[kK]\b")

# Function words plus the vocabulary every bullet in this tenant shares
# (names of the firm and its partners, generic work nouns). Leaving these
# in makes every two decisions look alike.
_STOP = frozenset(
    """
    a an and are as at be been being but by can could did do does done for
    from had has have he her him his i if in into is it its just me more
    most my no not now of off on once only or other our out over own same
    she should so some such than that the their them then there these they
    this those through to too under until up very was we were what when
    where which while who whom why will with would you your yes also any
    all each both few via per about after again against before below
    between during further here how nor let get got going go
    fp drew marcello tony brandon maria first person team client
    """.split()
)


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def content_tokens(text: str) -> frozenset[str]:
    text = _MARKER_RE.sub(" ", text or "").lower()
    toks = set()
    for w in _WORD_RE.findall(text):
        w = w.strip(".'-")
        if not w or w in _STOP or re.fullmatch(r"\d{4}-\d{2}-\d{2}", w):
            continue
        if len(w) < 3 and not any(ch.isdigit() for ch in w):
            continue
        toks.add(_stem(w))
    return frozenset(toks)


def amounts(text: str) -> frozenset[str]:
    """Money-shaped figures (`$22K`, `~$22k`, `$425,000`, `35k`),
    normalised to lowercase without spaces or commas."""
    out = set()
    for m in _AMOUNT_RE.findall(text or ""):
        out.add(re.sub(r"[\s,$]", "", m).lower())
    return frozenset(out)


def jaccard(a: str, b: str) -> float:
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def normalized_key(text: str) -> str:
    """Order-insensitive fingerprint of a bullet's content words — equal for
    the same item written with different punctuation, markers or case."""
    return " ".join(sorted(content_tokens(text)))
