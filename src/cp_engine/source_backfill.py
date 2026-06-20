"""Recover source re-fetch coords for legacy `rag_assets` rows.

Assets ingested before the Source Live-Link Layer carry NO `source_provider` /
`source_file_id`. But the (now-dead) temp `file_path` they were downloaded to
still encodes the source + id in its directory name, because
`asset_ingest._stable_dir_for` builds that dir deterministically:

    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", f"{file_ref.source}-{file_ref.id}")
    return tmp_root / safe_id

So `file_path` looks like `<tmp_root>/<source>-<sanitized-id>/<leaf-name>`. This
module's pure parser recovers `(provider, file_id)` from that path.

──────────────────────────────────────────────────────────────────────────────
LOSSY-ID ANALYSIS  (the load-bearing caveat — read before trusting backfilled
Dropbox coords)
──────────────────────────────────────────────────────────────────────────────

`_stable_dir_for` SANITIZES the id: any char outside `[A-Za-z0-9._-]` becomes
`_`. Whether that's reversible depends on the source's id alphabet:

* DRIVE — Google Drive file ids are drawn from `[A-Za-z0-9_-]` only. The
  sanitizer is therefore a no-op on them, so the dir name `drive-<id>` carries
  the TRUE id verbatim. Drive re-fetch (`asset_ingest.download_file`) fetches by
  `file_ref.id`. => Backfilling Drive coords FULLY enables re-fetch. Lossless.

* DROPBOX — Dropbox file ids have the shape `id:<body>` where `<body>` is
  `[A-Za-z0-9_-]`. The ONLY char the sanitizer touches is the single leading
  `:` (→ `_`), giving a dir of `dropbox-id_<body>`. Because Dropbox ids only
  ever contain that one out-of-alphabet char (the `:` right after `id`), the
  transform is reversible *by convention*: we rebuild `id:<body>` from
  `id_<body>`. We do that below so `source_file_id` holds the true id.

  HOWEVER — and this is the important part — Dropbox re-fetch does NOT use the
  id at all. `asset_ingest.download_file` fetches Dropbox originals by
  `file_ref.path` (the Dropbox `path_display`, stored in `source_path`), not by
  id. The temp path encodes ONLY the id and the leaf filename, never the full
  `path_display`. So backfilling `source_file_id` for a Dropbox row does NOT by
  itself make `fetch_source` work — `source_path` stays NULL and the download
  step has nothing to fetch by.

  => For Dropbox, this backfill recovers the provider + (reconstructed) id, but
  CANNOT recover the re-fetch path. Task 7 should treat Dropbox rows as only
  partially recovered: the provider tag is correct, but to truly re-enable
  Dropbox re-fetch the asset must be RE-INGESTED (which re-derives path_display
  from a live Dropbox listing). Drive rows are the clean win.

  (If the `:`-reversal convention ever proves wrong for some exotic id, the
  recovered Dropbox id would be slightly off — another reason to prefer
  re-ingest over trusting backfilled Dropbox ids for actual fetches.)
"""

from __future__ import annotations

import re

# A temp dir name is `<source>-<sanitized-id>`. We anchor on the source prefix
# at the START of a path segment (between slashes) so `mydropbox-...` can't
# match. `[A-Za-z0-9._-]+` is exactly the sanitizer's surviving alphabet.
_DIR_RE = re.compile(r"(?:^|/)(dropbox|drive)-(?P<sanitized_id>[A-Za-z0-9._-]+)/")


def _restore_dropbox_id(sanitized_id: str) -> str:
    """Reverse `_stable_dir_for`'s sanitization of a Dropbox id.

    Dropbox ids are `id:<body>` with `<body>` ⊂ `[A-Za-z0-9_-]`; the sanitizer
    rewrote the single leading `:` to `_`, yielding `id_<body>`. Reverse just
    that one substitution. Any id not matching the `id_` convention is returned
    unchanged (we have no basis to rewrite it). See module docstring for why this
    is reversible-by-convention rather than provably lossless.
    """
    if sanitized_id.startswith("id_"):
        return "id:" + sanitized_id[len("id_") :]
    return sanitized_id


def parse_source_coords_from_file_path(file_path: str | None) -> tuple[str, str] | None:
    """Recover `(provider, file_id)` from a legacy temp `file_path`, or `None`.

    `provider` is `"dropbox"` or `"drive"`. `file_id` is the source's file id —
    the TRUE Drive id (lossless), or the convention-reconstructed Dropbox
    `id:<body>` (see module docstring; note Dropbox re-fetch needs `source_path`,
    which this CANNOT recover). Returns `None` when the path has no recognizable
    `<source>-<id>/` temp segment.
    """
    if not file_path:
        return None

    match = _DIR_RE.search(file_path)
    if match is None:
        return None

    provider = match.group(1)
    sanitized_id = match.group("sanitized_id")

    if provider == "dropbox":
        return ("dropbox", _restore_dropbox_id(sanitized_id))
    return ("drive", sanitized_id)
