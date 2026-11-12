"""Recover text the old chunker dropped (library #26, cp-engine v0.136.2).

Before 2026-11-11, TextChunker dropped a document's final piece when it was
under 300 characters. This re-reads each active source from its ORIGINAL
(Dropbox / Drive download, or the Fathom meeting's summary / transcript),
re-chunks it with the fixed chunker, and — only when the new text is the old
text plus a recovered tail — replaces that asset's chunks IN PLACE: the
asset id, and everything that points at it (spine sources, links, lessons),
is unchanged. New chunks are written before the old ones are deleted, so a
document is never empty mid-run.

Anything that reads materially differently (a re-OCR'd deck, an edited
file) is left alone and listed: this recovers lost tails, it does not
re-ingest changed files.

Resumable: processed asset ids are appended to --state; a re-run skips them.
Prints titles' ids and counts only, never env values.

  railway run -s cp-engine -- python scripts/reprocess_tails.py [--apply] [--limit N]
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from supabase import create_client

from cp_engine.asset_ingest import FileRef, download_file
from cp_engine.meetings import flatten_transcript
from cp_engine.spine_promote import _default_pipeline_factory


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def classify(old: list[str], new: list[str]) -> str:
    """same | tail | differs, comparing chunks as a SET: stored chunk order
    can differ from a fresh run (ties in old order stamps), which is not a
    change. `tail`: every old chunk is still there except one, which is the
    start of one new chunk — the fixed chunker appended what it used to drop
    — or the document gained chunks it never had (it was all dropped)."""
    from collections import Counter

    o, n = Counter(norm(x) for x in old), Counter(norm(x) for x in new)
    if o == n:
        return "same"
    gone, added = o - n, n - o
    if not old and new:
        return "tail"
    if len(gone) == 1 and len(added) == 1:
        (g,), (a,) = gone.elements(), added.elements()
        if a.startswith(g) and len(a) > len(g):
            return "tail"
    if not gone and added:  # only new text, nothing lost or changed
        return "tail"
    return "differs"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--state", default=str(Path(tempfile.gettempdir()) / "reprocess_tails.done"))
    args = ap.parse_args()

    url, key = os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]
    c = create_client(url, key)
    done = set(Path(args.state).read_text().split()) if Path(args.state).exists() else set()

    assets, start = [], 0
    while True:
        page = (c.table("rag_assets")
                .select("id, project_id, title, url, source_type, source_provider, source_file_id, source_path, kind:meta->>kind")
                .eq("status", "active").order("created_at").range(start, start + 999).execute().data or [])
        assets += page
        if len(page) < 1000:
            break
        start += 1000
    todo = [a for a in assets if a["id"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"active assets {len(assets)}; already done {len(done)}; this run {len(todo)}; "
          f"mode {'APPLY' if args.apply else 'DRY RUN'}", flush=True)

    pipelines: dict[str, object] = {}
    counts = {"same": 0, "tail": 0, "differs": 0, "skipped": 0, "failed": 0, "replaced": 0}
    differs: list[str] = []
    state = open(args.state, "a")
    t0 = time.time()
    for i, a in enumerate(todo, 1):
        aid, pid = a["id"], a["project_id"]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                local = None
                provider = a.get("source_provider")
                if provider in ("dropbox", "drive") and a.get("source_file_id"):
                    local = download_file(FileRef(source=provider, id=a["source_file_id"],
                                                  name=a.get("title") or "source", mime_type=None,
                                                  size=None, modified=None, path=a.get("source_path")),
                                          Path(tmp))
                elif provider == "fathom" and a.get("kind") in ("meeting_summary", "meeting_transcript"):
                    m = (c.table("fathom_meetings").select("summary, transcript")
                         .eq("recording_id", a.get("source_file_id")).limit(1).execute().data or [])
                    if m:
                        text = (m[0].get("summary") if a["kind"] == "meeting_summary"
                                else flatten_transcript(m[0].get("transcript")))
                        if text:
                            name = "summary.md" if a["kind"] == "meeting_summary" else "transcript.md"
                            local = Path(tmp) / name
                            local.write_text(text, encoding="utf-8")
                if local is None:
                    counts["skipped"] += 1
                    state.write(aid + "\n")
                    continue
                pipe = pipelines.get(pid) or pipelines.setdefault(pid, _default_pipeline_factory(pid, url, key))
                parser = pipe._select_parser(str(local))
                if parser is None:
                    counts["skipped"] += 1
                    state.write(aid + "\n")
                    continue
                doc = parser.parse(str(local), a.get("title") or local.name, a.get("url"))
                chunks = pipe._chunk_document(doc)
            old = (c.table("asset_chunks").select("id, text, chunk_index").eq("asset_id", aid)
                   .order("chunk_index").execute().data or [])
            verdict = classify([o["text"] or "" for o in old], [ch.text for ch in chunks])
            counts[verdict] += 1
            if verdict == "differs":
                differs.append(f"{aid} {a.get('title')!r}")
            if verdict == "tail" and args.apply and chunks:
                embeddings = pipe.embedder.embed_batch([ch.text for ch in chunks])
                pipe.storage.create_chunks_with_embeddings(
                    asset_id=aid, asset_url=a.get("url"), source_type=a.get("source_type") or doc.source_type,
                    chunks=chunks, embeddings=embeddings, embed_model=pipe.embed_model)
                # Embeddings go with their chunks (FK cascade).
                c.table("asset_chunks").delete().in_("id", [o["id"] for o in old]).execute()
                counts["replaced"] += 1
            state.write(aid + "\n")
        except Exception as exc:  # noqa: BLE001 — one document never stops the run
            counts["failed"] += 1
            print(f"  failed {aid}: {type(exc).__name__}: {str(exc)[:140]}", flush=True)
        state.flush()
        if i % 25 == 0:
            print(f"  {i}/{len(todo)} {counts} ({int(time.time() - t0)}s)", flush=True)
    print("done:", counts, flush=True)
    for d in differs[:60]:
        print("  differs (left alone):", d, flush=True)


if __name__ == "__main__":
    sys.exit(main())
