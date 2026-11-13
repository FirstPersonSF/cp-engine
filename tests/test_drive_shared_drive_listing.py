"""A project folder in a Shared Drive lists like one in My Drive.

Drive answers `files.list` for a folder that lives in a Shared Drive with an
empty list, and no error, unless the call passes `supportsAllDrives` and
`includeItemsFromAllDrives`. On 2026-11-13 the active project folders moved
into a Shared Drive and every listing went empty. The stand-in here behaves
like Drive: no flags, no files.
"""
from cp_engine import asset_ingest, folder_rules

FOLDER = asset_ingest._DRIVE_FOLDER_MIME


class _SharedDriveConnector:
    """Returns a folder's children only when both shared-drive flags are set."""

    def __init__(self, tree):
        self._tree = tree

    def _list_files_with_pagination(self, query, fields, page_size=100, **kwargs):
        if not (kwargs.get("supportsAllDrives") and kwargs.get("includeItemsFromAllDrives")):
            return []
        parent = query.split("'")[1]
        items = self._tree.get(parent, [])
        if "mimeType='" in query:
            items = [i for i in items if i["mimeType"] == FOLDER]
        return items


TREE = {
    "root": [{"id": "mgmt", "name": "Management", "mimeType": FOLDER}],
    "mgmt": [{"id": "f1", "name": "INV_4414.pdf", "mimeType": "application/pdf",
              "size": "10", "modifiedTime": "2026-10-26T00:00:00Z"}],
}


def test_ingest_listing_reads_a_shared_drive_folder():
    refs = asset_ingest._list_drive(_SharedDriveConnector(TREE), "root")
    assert [r.name for r in refs] == ["INV_4414.pdf"]


def test_subfolder_listing_reads_a_shared_drive_folder():
    subs = folder_rules.list_subfolders("drive", _SharedDriveConnector(TREE), "root")
    assert subs == [("Management", "mgmt")]
