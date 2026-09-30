"""VENDORED from `cp_engine.state` — the derived workstream label only.

`spine_lint.run_all_lints` resolves the project's label at call time so the
standing Brief/SOW check can skip initiatives, which have no agreement to
author (#319). The real `state` module is pure stdlib but large; the rule is
two symbols, and `test_vendor_drift.py` compares each to its source.
"""

from __future__ import annotations

from typing import Literal

WorkstreamLabel = Literal["account", "program", "job", "initiative"]


def derive_label(
    *, company_kind: str, parent_code: str | None, has_agreement: bool, has_children: bool
) -> WorkstreamLabel:
    """The display label for a workstream, from its shape alone.

    Rendering and reference-style only — the engine branches on
    `has_agreement`, `parent_code` and `company_kind` directly.

    An account never carries an agreement (mig 191 creates it with
    `deal_stage NULL`), so the account rule requires `not has_agreement`:
    a parentless client row WITH an agreement is a job whose parent is not
    in hand (an archived-only company had no account node until mig 195),
    never an account. Five SentinelOne jobs rendered "Account" before this.
    """
    if parent_code is None and company_kind == "client" and not has_agreement:
        return "account"
    if has_children:
        return "program"
    if has_agreement:
        return "job"
    return "initiative"
