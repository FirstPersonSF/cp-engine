#!/usr/bin/env python3
"""Backfill `source_provider` / `source_file_id` on legacy `rag_assets` rows.

Assets ingested before the Source Live-Link Layer have NULL re-fetch coords. The
dead temp `file_path` they were downloaded to still encodes `<source>-<id>` in
its directory name (built by `asset_ingest._stable_dir_for`). This script
recovers those coords with the pure
`cp_engine.source_backfill.parse_source_coords_from_file_path` and writes them
back, one UPDATE per recoverable row.

IMPORTANT — read `cp_engine.source_backfill`'s module docstring on the LOSSY-ID
question before trusting the results:
  * DRIVE rows recover LOSSLESSLY and re-fetch works (Drive fetches by id).
  * DROPBOX rows recover the provider + a convention-reconstructed id. Dropbox
    re-fetch now uses that id when `source_path` is absent (`download_file`'s
    fetch-by-id fallback passes `id:<body>` to `files_download`), so backfilled
    Dropbox rows ARE re-fetchable. The caveat: the id was recovered by reversing
    the `:`→`_` sanitization by convention, so a re-ingest (which re-derives
    `path_display` from a live Dropbox listing) is the gold-standard — but it is
    no longer required. The summary breaks counts out by provider.

Safety: `--dry-run` is the DEFAULT. It selects + plans + prints but writes
NOTHING. Pass `--apply` to actually run the UPDATEs. Idempotent: it only ever
selects rows where `source_provider IS NULL`, so re-running after an apply is a
no-op for already-backfilled rows. No silent caps or truncation anywhere.

Run from the cp tenant working dir (creds resolve via the same mechanism as
`cp ingest-assets`: env first, then `<mc-2 clone>/backend/.env`).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

# Allow running as a plain script (`python scripts/backfill_source_coords.py`)
# without an editable install: add the package's src/ to the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cp_engine.source_backfill import parse_source_coords_from_file_path  # noqa: E402

# Explicit columns only (never `*`): rag_assets carries large text/meta blobs.
_SELECT_COLUMNS = "id, file_path, title"
# How many unrecoverable sample paths to print (we print a COUNT of all of them;
# this only bounds the illustrative sample — not a silent cap on the work).
_UNRECOVERABLE_SAMPLE = 25


def _resolve_creds() -> tuple[str, str]:
    """Resolve Supabase creds exactly as `asset_ingest` does (env, then mc-2 .env)."""
    from cp_engine import config as cp_config
    from cp_engine.sync_mc2 import _load_supabase_creds

    return _load_supabase_creds(cp_config.load(Path.cwd()))


def _preflight_columns(client) -> None:
    """Probe that migration 077's columns exist before the real select.

    Without this, running the script before `077` adds `source_provider` makes the
    first real SELECT throw a raw PostgREST `42703 column does not exist` stack
    trace. Probe with a tiny select and turn a column-missing error into a clear
    message + clean non-zero exit. Any OTHER error is left to propagate normally.
    """
    try:
        client.table("rag_assets").select("source_provider").limit(1).execute()
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "source_provider" in msg or "42703" in msg or "does not exist" in msg:
            print(
                "rag_assets.source_provider missing — apply migration 077 first.",
                file=sys.stderr,
            )
            raise SystemExit(2) from None
        raise


def _select_null_rows(client) -> list[dict]:
    """Every active-ish rag_assets row missing source_provider, with file_path/title."""
    return (
        client.table("rag_assets")
        .select(_SELECT_COLUMNS)
        .is_("source_provider", "null")
        .execute()
    ).data or []


def _apply_update(client, row_id: str, provider: str, file_id: str) -> None:
    """Stamp recovered coords on one row by id. Only called under --apply."""
    client.table("rag_assets").update(
        {"source_provider": provider, "source_file_id": file_id}
    ).eq("id", row_id).execute()


def run(client, *, apply: bool) -> int:
    """Plan (and, if apply, execute) the backfill. Returns recovered-row count."""
    _preflight_columns(client)
    rows = _select_null_rows(client)
    print(f"Found {len(rows)} rag_assets row(s) with source_provider IS NULL.")

    recovered_by_provider: Counter[str] = Counter()
    unrecoverable: list[dict] = []
    planned = 0

    for row in rows:
        coords = parse_source_coords_from_file_path(row.get("file_path"))
        if coords is None:
            unrecoverable.append(row)
            continue
        provider, file_id = coords
        recovered_by_provider[provider] += 1
        planned += 1
        action = "UPDATE" if apply else "WOULD UPDATE"
        print(
            f"  {action} id={row.get('id')} "
            f"-> provider={provider} file_id={file_id} "
            f"(title={row.get('title')!r}, file_path={row.get('file_path')!r})"
        )
        if apply:
            _apply_update(client, row.get("id"), provider, file_id)

    print()
    print("─" * 60)
    print(f"Summary ({'APPLIED' if apply else 'DRY RUN — nothing written'}):")
    print(f"  NULL rows scanned : {len(rows)}")
    print(f"  recovered         : {planned}")
    for provider in sorted(recovered_by_provider):
        note = (
            " (LOSSLESS — re-fetch enabled)"
            if provider == "drive"
            else " (re-fetchable via id fallback; id recovered by convention, "
            "re-ingest is gold-standard but not required)"
        )
        print(f"      {provider:<8}: {recovered_by_provider[provider]}{note}")
    print(f"  unrecoverable     : {len(unrecoverable)}")

    if unrecoverable:
        shown = min(len(unrecoverable), _UNRECOVERABLE_SAMPLE)
        print(f"  unrecoverable sample (showing {shown} of {len(unrecoverable)}):")
        for row in unrecoverable[:_UNRECOVERABLE_SAMPLE]:
            print(f"      id={row.get('id')} file_path={row.get('file_path')!r}")

    return planned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Plan + print only; write nothing (DEFAULT).",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually run the UPDATEs. Without this, nothing is written.",
    )
    args = parser.parse_args(argv)
    apply = args.apply  # dry-run is the default; --apply is the only way to write.

    supabase_url, supabase_key = _resolve_creds()
    from supabase import create_client

    client = create_client(supabase_url, supabase_key)
    run(client, apply=apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
