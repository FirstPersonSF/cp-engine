"""The one parser for workstream codes (#301, phase 3.2).

Every workstream in MC-2 is a `projects` row and every row has a company
code and a job number; the canonical cp code is `slug(full_job_name)`:
`ggl-5136-go-safety-website`, `1pi-9005-mission-control`. Callers meet the
same identity in four spellings —

- the canonical slug            ``ggl-5136-go-safety-website``
- the short form                ``ggl-5136``
- MC-2's display name           ``GGL 5136 Go Safety Website``
- a hand-typed upper-case code  ``GGL-5136``

— and before this module five regexes disagreed about which of them counted
(`plan_from_transcript._ENGAGEMENT_CODE_RE` rejected every real slug code;
`sync._code_takes_slug` needed four digits; `tag_resolve` needed exactly
three letters, which `1pi` is not). One grammar, here:

    <company> <sep> <number> [<sep> <slug…>]

`company` is 2–4 alphanumerics and MAY start with a digit (``1pi``);
`number` is the FIRST all-digit segment after it and is a JOB number — at
least three digits (MC-2 seeds 4-digit numbers; three keeps the old tag
parser's floor), so a repo slug ending in a short number (``mc-2``) is not
a code; `slug` is everything after that, kebab-cased. Anything else — a
bare word (``storyos``), an empty string — is not a code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Segments are split on whitespace, `-`, `_` and `/`; the company must be
# 2–4 alphanumerics with at least one letter (``2026-05`` must not parse
# as company ``2026``), the number at least three digits.
_SEP = re.compile(r"[\s\-_/]+")
_COMPANY = re.compile(r"^(?=.*[a-z])[a-z0-9]{2,4}$")
_NUMBER = re.compile(r"^\d{3,}$")
# The glued display form fathom tags sometimes carry: ``IBX5167 DDI …``.
# Only a letters-then-digits head is split; ``1pi9005`` stays ambiguous.
_GLUED = re.compile(r"^([a-z]{2,4})(\d{3,})$")
_NON_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ParsedCode:
    """A workstream code taken apart: company code, job number, name slug."""

    company: str  # lower-case: ggl, ibx, 1pi, cnc
    number: int  # the MC-2 `projects.number`
    slug: str | None  # kebab name tail, None for the short form

    @property
    def short(self) -> str:
        """The `<company>-<number>` form (``ggl-5136``)."""
        return f"{self.company}-{self.number}"


def parse_code(text: str | None) -> ParsedCode | None:
    """Parse any spelling of a workstream code, or None when it is not one."""
    if not isinstance(text, str):
        return None
    lowered = text.strip().lower()
    if not lowered:
        return None
    segments = [s for s in _SEP.split(lowered) if s]
    glued = _GLUED.match(segments[0]) if segments else None
    if glued:
        segments = [glued.group(1), glued.group(2), *segments[1:]]
    if len(segments) < 2:
        return None
    company, number = segments[0], segments[1]
    if not _COMPANY.match(company) or not _NUMBER.match(number):
        return None
    tail = "-".join(_NON_SLUG.sub("-", s).strip("-") for s in segments[2:])
    tail = re.sub(r"-{2,}", "-", tail).strip("-")
    return ParsedCode(company=company, number=int(number), slug=tail or None)


def canonical_code(parsed: ParsedCode) -> str:
    """The cp code for a parsed identity: ``ggl-5136-go-safety-website``,
    or ``ggl-5136`` when there is no slug to carry."""
    if parsed.slug:
        return f"{parsed.short}-{parsed.slug}"
    return parsed.short


def code_number(text: str | None) -> int | None:
    """The job number in any spelling of a code, or None (a convenience
    wrapper most call sites want: *is this a workstream, and which one?*)."""
    parsed = parse_code(text)
    return parsed.number if parsed else None
