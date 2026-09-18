"""VENDORED from `cp_engine.substance` — the version-ordering primitive only.

`project_sources._version_rank` imports `version_number` at call time; the real
`substance` pulls frontmatter + yaml at module level. The regex travels with the
function because it reads it (#287: the first shim inlined the function and
not the regex, so it raised NameError on first call).
"""

from __future__ import annotations

import re


# Leading integer after an optional case-insensitive "v": "v7", "V2", "2",
# "v2.1", "v2 (note)" all yield their leading int. Canonical labels are
# `v<int>` (_HEADER_RE enforces that on disk), but DB labels have historically
# drifted by hand-edit, so ranking must tolerate the drifted shapes.
_VERSION_NUM_RE = re.compile(r"\s*v?\s*(\d+)", re.IGNORECASE)


def version_number(label) -> int:
    """The leading version integer of a version_label, or -1 when unparseable.

    Shared version-ordering primitive (#113): both the spine read path (dedupe
    duplicate live rows to the latest version) and the substance mirror (shield
    a newer authored live version from a stale disk 'live') need to compare
    version labels numerically — "v10" must beat "v9", which string comparison
    gets wrong. Parses "v7", "V2", bare "2", "v2.1" → 2, "v2 (note)" → 2.
    Tolerant of None/odd labels so a dirty row can't raise — but note the -1
    fallback means a truly unparseable label LOSES to any parseable one
    regardless of recency (callers tie-break on version_date next)."""
    m = _VERSION_NUM_RE.match(str(label or ""))
    return int(m.group(1)) if m else -1
