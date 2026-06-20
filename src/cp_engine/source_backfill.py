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

  Normal Dropbox ingest fetches by `file_ref.path` (the `path_display`, stored
  in `source_path`), and the temp path encodes ONLY the id + leaf filename, never
  the full `path_display` — so this backfill recovers `source_file_id` but not
  `source_path`. That used to mean backfilled Dropbox rows couldn't re-fetch.
  NO LONGER: `asset_ingest.download_file` now has a fetch-by-id FALLBACK — when
  `path_display` is absent it passes the `id:<body>` form to Dropbox's
  `files_download(path=...)`, which accepts it. So a backfilled Dropbox row (id
  but no path) IS re-fetchable via that fallback.

  => For Dropbox, this backfill recovers the provider + (reconstructed) id, which
  is enough to re-fetch via the id fallback. The caveat: the id was recovered by
  reversing the `:`→`_` sanitization BY CONVENTION (see `_restore_dropbox_id`),
  so a RE-INGEST — which re-derives `path_display` from a live Dropbox listing —
  remains the gold-standard. But it is no longer REQUIRED. Drive rows recover the
  true id verbatim, so they need no caveat at all.
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
    `id:<body>` (see module docstring; the Dropbox id re-enables re-fetch via
    `download_file`'s fetch-by-id fallback — re-ingest is gold-standard but not
    required). Returns `None` when the path has no recognizable `<source>-<id>/`
    temp segment.
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
