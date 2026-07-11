"""Inbound-frameworks integration (slice 1) — pure helpers under the MCP tools.

Wraps the `inbound-frameworks` package (1p-component-library
services/inbound-frameworks) for use on client engagements: the curated-menu
listing, corpus assembly from a project's spine/sources/working-dir files,
and the LLM adapter factory. The MCP tool layer in `mcp_server.py` stays
thin wiring over these, per that module's convention.

Design doc: cp tenant `docs/plans/2026-07-11-inbound-frameworks-integration-design.md`.
Validated by the 2026-07-11 sap-5174 pilot (readiness map in that project's
`frameworks/` dir).

Non-negotiables carried from the package's IMPLEMENTATION.md:
- Anti-graveyard: a `discarded` outcome is a no-op — never a substitute prompt.
- Framework names/ids/fields never reach a CLIENT surface. MCP returns are
  internal (session-side), so they may carry framework identity; the render
  boundary is exports/deliverables, enforced at authoring time.
- Engines persist nothing; results carry `usage` for the caller's telemetry.

Imports of the package itself stay INSIDE functions (matching mcp_server.py's
config/supabase discipline) so `import cp_engine.frameworks` is cheap and the
package is only required when a framework tool is actually called.
"""

from __future__ import annotations

import os
from pathlib import Path

# Model knobs. Decompose is extraction-shaped (a fast tier works); the pilot
# validated sonnet-tier quality, so both default there and the env knobs let a
# tenant trade cost for quality without a release.
DECOMPOSE_MODEL_ENV = "CP_FRAMEWORKS_DECOMPOSE_MODEL"
COMPOSE_MODEL_ENV = "CP_FRAMEWORKS_COMPOSE_MODEL"
_DEFAULT_MODEL = "claude-sonnet-5"

# Corpus cap: decompose feeds the WHOLE assembled corpus to one completion.
# The pilot ran ~106k chars (~42k tokens) comfortably; cap well above that but
# below context limits so a runaway source list fails loudly, not weirdly.
MAX_CORPUS_CHARS = 600_000


def get_catalog():
    """Process-lifetime FrameworkCatalog singleton (per IMPLEMENTATION.md)."""
    global _CATALOG
    try:
        return _CATALOG
    except NameError:
        from inbound_frameworks import FrameworkCatalog

        _CATALOG = FrameworkCatalog()
        return _CATALOG


def make_llm(kind: str):
    """Build the LLM adapter for one engine direction ('decompose'|'compose').

    Uses the package's AnthropicLLM; the caller is responsible for having run
    `_load_ingest_creds` first so ANTHROPIC_API_KEY is in the environment (the
    same contract the visual-PDF ingest path uses).
    """
    from inbound_frameworks import AnthropicLLM

    env = DECOMPOSE_MODEL_ENV if kind == "decompose" else COMPOSE_MODEL_ENV
    return AnthropicLLM(model=os.environ.get(env, _DEFAULT_MODEL))


def readiness_menu(layer: str | None = None) -> dict:
    """The curated framework menu + snapshot identity. No LLM call.

    Lists ONLY frameworks with a curated template in at least one direction
    (`decomposable()` ∪ `composable()`) — never all 130 (IMPLEMENTATION.md).
    `layer` filters case-insensitively on `unf_layer`.
    """
    catalog = get_catalog()
    seen: dict[str, dict] = {}
    for fw in list(catalog.decomposable()) + list(catalog.composable()):
        row = seen.setdefault(
            fw.id,
            {
                "id": fw.id,
                "name": fw.name,
                "layer": fw.unf_layer,
                "decomposable": False,
                "composable": False,
            },
        )
        row["decomposable"] = row["decomposable"] or fw.has_decompose_template
        row["composable"] = row["composable"] or fw.has_compose_template
    rows = sorted(seen.values(), key=lambda r: r["id"])
    if layer is not None:
        rows = [r for r in rows if (r["layer"] or "").lower() == layer.lower()]
    return {"snapshot": catalog.snapshot_meta(), "frameworks": rows}


def content_to_body(content: dict) -> str:
    """Adapt compose() output (`{sections:[{id,text,order,title}], fields?}`)
    to a spine-element markdown body — the explicit adapter the package's
    IMPLEMENTATION.md requires (never reshape the package's output; convert
    at the consumer boundary). Sections render in `order`; a section title
    becomes an `##` heading; typed `fields` render as a trailing bold-label
    list. Framework identity is deliberately NOT rendered — it belongs in the
    version note, never the body (the IP invariant at the authoring layer).
    """
    parts: list[str] = []
    sections = sorted(content.get("sections") or [],
                      key=lambda s: s.get("order") or 0)
    for s in sections:
        title = (s.get("title") or "").strip()
        text = (s.get("text") or "").strip()
        if title:
            parts.append(f"## {title}")
        if text:
            parts.append(text)
    fields = content.get("fields") or {}
    if fields:
        parts.append("\n".join(f"- **{k}**: {v}" for k, v in fields.items()))
    return "\n\n".join(parts)


def diff_decompositions(baseline: dict, current: dict) -> dict:
    """The pre/post decision diff: what changed between two decompose results
    over the same framework (e.g. corpus-only vs corpus+workshop-transcript).

    `baseline`/`current` are `{field_values, field_confidence}` shapes (what
    framework_decompose returns). Returns per-field rows plus a summary:

        {fields: {name: {status: unchanged|changed|new|dropped,
                         baseline?, current?,
                         confidence: {baseline?, current?, moved: bool}}},
         summary: {changed: [...], new: [...], dropped: [...],
                   hardened: [...], softened: [...]}}

    `hardened`/`softened` track confidence moves (uncertain→likely→certain
    and the reverse) — a field whose VALUE held but whose confidence hardened
    after the workshop is a decision RATIFIED, which is exactly what the
    P&P Report wants to cite.
    """
    rank = {"uncertain": 0, "likely": 1, "certain": 2}
    b_vals = baseline.get("field_values") or {}
    c_vals = current.get("field_values") or {}
    b_conf = baseline.get("field_confidence") or {}
    c_conf = current.get("field_confidence") or {}

    fields: dict[str, dict] = {}
    summary: dict[str, list] = {"changed": [], "new": [], "dropped": [],
                                "hardened": [], "softened": []}
    for name in sorted(set(b_vals) | set(c_vals)):
        row: dict = {}
        in_b, in_c = name in b_vals, name in c_vals
        if in_b and not in_c:
            row["status"] = "dropped"
            row["baseline"] = b_vals[name]
            summary["dropped"].append(name)
        elif in_c and not in_b:
            row["status"] = "new"
            row["current"] = c_vals[name]
            summary["new"].append(name)
        else:
            same = (b_vals[name] or "").strip() == (c_vals[name] or "").strip()
            row["status"] = "unchanged" if same else "changed"
            row["baseline"], row["current"] = b_vals[name], c_vals[name]
            if not same:
                summary["changed"].append(name)
        bc, cc = b_conf.get(name), c_conf.get(name)
        moved = bc is not None and cc is not None and bc != cc
        row["confidence"] = {"baseline": bc, "current": cc, "moved": moved}
        if moved and bc in rank and cc in rank:
            summary["hardened" if rank[cc] > rank[bc] else "softened"].append(name)
        fields[name] = row
    return {"fields": fields, "summary": summary}


def assemble_corpus(
    client, project_id: str, company_id: str | None, tenant_root: Path,
    source_keys: list[str],
) -> tuple[str, list[dict]]:
    """Resolve `source_keys` to text and join them into one corpus.

    Scoping is first-class (the pilot's Audience-framework lesson: decompose
    follows the corpus's dominant subject, so callers pick sources per
    framework). Each key resolves through three doors, first hit wins:

      1. Repo-relative file path under the tenant root (the working-dir
         synthesis files — the richest corpus in practice). Path-escape
         guarded: the resolved path must stay inside the tenant root.
      2. Spine element key (est_item_id or framing substring — `pull_spine`'s
         discipline).
      3. Source-doc title (`pull_source`, full-doc order).

    Returns `(corpus_text, manifest)` where manifest has one row per key:
    `{key, resolved: "file"|"spine"|"source"|None, chars, note?}`. Unresolved
    keys are recorded, never silently dropped (no-silent-caps).
    """
    from cp_engine.project_sources import pull_source, pull_spine

    parts: list[str] = []
    manifest: list[dict] = []
    root = tenant_root.resolve()
    for key in source_keys:
        text, resolved, note = None, None, None

        candidate = (root / key).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            text, resolved = candidate.read_text(), "file"
        if text is None:
            el = pull_spine(client, project_id, key)
            if not el.get("error") and el.get("body"):
                text, resolved = el["body"], "spine"
        if text is None and company_id is not None:
            doc = pull_source(client, project_id, company_id, key)
            chunks = doc.get("chunks") or []
            if chunks:
                text, resolved = "\n".join(chunks), "source"
            elif doc.get("note"):
                note = doc["note"]
        if text is None:
            manifest.append(
                {"key": key, "resolved": None, "chars": 0,
                 "note": note or "no file, spine element, or source matched"}
            )
            continue
        parts.append(f"=== {key} ===\n{text}")
        manifest.append({"key": key, "resolved": resolved, "chars": len(text)})

    corpus = "\n\n\n".join(parts)
    if len(corpus) > MAX_CORPUS_CHARS:
        raise ValueError(
            f"assembled corpus is {len(corpus)} chars (cap {MAX_CORPUS_CHARS}); "
            "narrow source_keys — decompose sends the whole corpus to one call"
        )
    return corpus, manifest
