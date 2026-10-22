"""Document order for `asset_chunks` rows, and an honest flag when it is unknown (#349).

`asset_chunks.id` is a random uuid and the table carries no insert
timestamp, so the order PostgREST returns rows in means nothing. A chunk's
position is recorded in two places:

  - `meta.chunk_index` — stamped by the ingest pipeline since 1p-lib
    6a0f2db (`storage_api.store_document`);
  - the typed `chunk_index` column — mc-2 migration 200, filled from
    `meta.chunk_index` by a trigger on every write.

Readers sort by the position when EVERY chunk of the document has one, and
otherwise keep their old fallback and say so: `order` is
`"chunk_index"` (guaranteed), `"start_seconds"` (time-coded media, every
chunk timed) or `"unknown"` (a legacy, pre-stamp ingest). A partly stamped
document is `"unknown"` — a sort that is right for most chunks is the
convincing kind of wrong.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

ORDER_INDEXED = "chunk_index"
ORDER_TIMED = "start_seconds"
ORDER_UNKNOWN = "unknown"

UNKNOWN_ORDER_NOTE = (
    "chunk order is not guaranteed: this source was ingested before chunks "
    "recorded their position, so its text may be out of document order. "
    "Re-ingest it, or read the original via `fetch_project_source`."
)

# PostgREST projection of the stamped position, aliased so it cannot collide
# with the `chunk_index` column (an unaliased `meta->>chunk_index` arrives
# under the key `chunk_index`).
META_CHUNK_INDEX_SELECT = "meta_chunk_index:meta->>chunk_index"


def _as_position(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def chunk_position(row: dict) -> int | None:
    """A chunk's document position: the column, else the `meta` stamp."""
    for key in ("chunk_index", "meta_chunk_index"):
        pos = _as_position(row.get(key))
        if pos is not None:
            return pos
    meta = row.get("meta")
    if isinstance(meta, dict):
        return _as_position(meta.get("chunk_index"))
    return None


def order_chunks(
    rows: Iterable[dict],
    fallback_key: Callable[[dict], Any] | None = None,
) -> tuple[list[dict], str | None]:
    """Return `(rows in document order, order label)`.

    Every row positioned, positions distinct → sorted by position,
    `"chunk_index"`. Otherwise the caller's legacy behaviour: a stable sort by
    `fallback_key` when given, else the rows as returned — labelled
    `"start_seconds"` when every row is time-coded, else `"unknown"`. No rows
    → `None` (there is no order to claim).
    """
    rows = list(rows)
    if not rows:
        return rows, None
    positions = [chunk_position(r) for r in rows]
    if None not in positions and len(set(positions)) == len(positions):
        ordered = sorted(zip(positions, rows, strict=True), key=lambda p: p[0])
        return [r for _, r in ordered], ORDER_INDEXED
    if fallback_key is not None:
        rows = sorted(rows, key=fallback_key)
    if all(r.get("start_seconds") is not None for r in rows):
        return rows, ORDER_TIMED
    return rows, ORDER_UNKNOWN


def order_fields(label: str | None) -> dict:
    """The `order` / `order_note` keys a pull result carries for `label`."""
    if label is None:
        return {}
    out: dict = {"order": label}
    if label == ORDER_UNKNOWN:
        out["order_note"] = UNKNOWN_ORDER_NOTE
    return out


# PostgREST's `max-rows` on MC-2: no single response carries more rows than
# this, whatever `.limit()` or `.range()` asked for. Reads page at it.
POSTGREST_MAX_ROWS = 1000

# The most chunks one document read will fetch (#349 item 6). A bound, so a
# runaway asset cannot pull an unbounded transfer; a read that reaches it with
# more rows behind it reports `capped` instead of passing as whole. The
# largest live document is ~2,000 chunks.
MAX_DOC_CHUNKS = 20_000

CAPPED_NOTE = (
    "chunk read stopped at {n} chunks (the per-document bound); the document "
    "has more — its tail is missing. Read the original via `fetch_project_source`."
)


def read_asset_chunks_paged(
    client,
    asset_id: str,
    columns: str,
    *,
    table: str = "asset_chunks",
    page: int | None = None,
    max_chunks: int | None = None,
) -> tuple[list[dict], bool]:
    """Every chunk row of one asset, paged past PostgREST's row cap (#349).

    One unpaged request is silently cut at `max-rows` (1000): a 2,500-chunk
    deck came back as its first 1,000 chunks with nothing saying so. This
    reads `.range()` windows ordered by `id` (a stable key, so windows
    neither overlap nor skip — document order is `order_chunks`' job, after)
    until a short window says the asset is exhausted, or `max_chunks` is
    reached.

    Returns `(rows, capped)`: `capped` is True only when the bound stopped
    the read with a full last window, i.e. more rows may exist. `page` must
    not exceed the server's `max-rows`, or a full window reads as short.
    Read errors propagate (callers decide whether to retry on 42703).
    """
    page = page or POSTGREST_MAX_ROWS
    bound = MAX_DOC_CHUNKS if max_chunks is None else max_chunks
    rows: list[dict] = []
    start = 0
    while start < bound:
        want = min(page, bound - start)
        batch = (
            client.table(table)
            .select(columns)
            .eq("asset_id", asset_id)
            .order("id")
            .range(start, start + want - 1)
            .execute()
            .data
        ) or []
        rows.extend(batch)
        if len(batch) < want:
            return rows, False
        start += want
    # The bound was reached on a full window. One probe row decides whether
    # anything lies beyond it, so a document exactly at the bound is whole.
    more = (
        client.table(table)
        .select("id")
        .eq("asset_id", asset_id)
        .order("id")
        .range(bound, bound)
        .execute()
        .data
    ) or []
    return rows, bool(more)


def is_missing_column(exc: BaseException, column: str = "chunk_index") -> bool:
    """True for PostgREST's "column does not exist" (42703) naming `column`.

    Lets a reader select the migration-200 column and still work against a
    database where the migration is not yet applied.
    """
    msg = str(exc)
    return column in msg and ("42703" in msg or "does not exist" in msg)
