"""Tests for the legacy source-coord backfill parser.

The recoverable fact lives in `rag_assets.file_path` — the DEAD temp path an
asset was downloaded to before ingest. That temp dir name was built by
`asset_ingest._stable_dir_for` as:

    re.sub(r"[^A-Za-z0-9._-]", "_", f"{file_ref.source}-{file_ref.id}")

so the directory is `<source>-<sanitized-id>`. These tests pin the parser to
the path shapes that code ACTUALLY produces in prod (see the module docstring of
`source_backfill` for the lossy-id analysis).
"""

from cp_engine.source_backfill import parse_source_coords_from_file_path


class TestDropbox:
    def test_tmp_path(self):
        # Real prod example. Dropbox file id is `id:AjJF-o4OzLAAAAAAAFSxBw`; the
        # `:` sanitizes to `_`, so the dir carries `dropbox-id_AjJF-...`.
        path = "/tmp/dropbox-id_AjJF-o4OzLAAAAAAAFSxBw/Infoblox_Platform_v6 - Copy.pptx"
        assert parse_source_coords_from_file_path(path) == (
            "dropbox",
            "id:AjJF-o4OzLAAAAAAAFSxBw",
        )

    def test_var_folders_path(self):
        # macOS-style temp root (`/var/folders/.../T/`).
        path = (
            "/var/folders/h3/48twh2t97rx3vvlxhbcqckl80009rh/T/"
            "dropbox-id_AjJF-o4OzLAAAAAAAFSqwg/Deck.pptx"
        )
        assert parse_source_coords_from_file_path(path) == (
            "dropbox",
            "id:AjJF-o4OzLAAAAAAAFSqwg",
        )

    def test_id_with_no_colon_segment_preserved(self):
        # Defensive: a Dropbox id whose body has hyphens/underscores survives
        # sanitization untouched (only the leading `:` was rewritten).
        path = "/tmp/dropbox-id_Ab-cD_eFgHiJ/x.docx"
        assert parse_source_coords_from_file_path(path) == ("dropbox", "id:Ab-cD_eFgHiJ")


class TestDrive:
    def test_drive_path(self):
        # Drive ids are `[A-Za-z0-9_-]` only → NO sanitization → lossless.
        # Dir is `drive-<id>` (NOT `drive-id_<id>`; Drive ids carry no `id:`).
        path = "/tmp/drive-1AbC-dEfGhIjKlMnOp_qRsTuV/Quarterly Report.docx"
        assert parse_source_coords_from_file_path(path) == (
            "drive",
            "1AbC-dEfGhIjKlMnOp_qRsTuV",
        )

    def test_drive_var_folders_path(self):
        path = (
            "/var/folders/h3/48twh2t97rx3vvlxhbcqckl80009rh/T/"
            "drive-0B1c2D3e4F5g6H7i/Slides.pdf"
        )
        assert parse_source_coords_from_file_path(path) == (
            "drive",
            "0B1c2D3e4F5g6H7i",
        )


class TestUnrecoverable:
    def test_random_tmp_path(self):
        assert parse_source_coords_from_file_path("/tmp/random/x.pptx") is None

    def test_no_source_prefix(self):
        assert parse_source_coords_from_file_path("/tmp/abc123/file.pdf") is None

    def test_empty_string(self):
        assert parse_source_coords_from_file_path("") is None

    def test_none(self):
        assert parse_source_coords_from_file_path(None) is None

    def test_prefix_but_empty_id(self):
        # `dropbox-` / `drive-` with nothing after it isn't a recoverable coord.
        assert parse_source_coords_from_file_path("/tmp/dropbox-/x.pptx") is None
        assert parse_source_coords_from_file_path("/tmp/drive-/x.pptx") is None

    def test_prefix_not_a_full_dir_segment(self):
        # `mydropbox-...` must NOT match — the prefix has to start the dir name.
        path = "/tmp/notdropbox-id_AjJF/x.pptx"
        assert parse_source_coords_from_file_path(path) is None
