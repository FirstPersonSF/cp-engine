"""Tests for cp_engine.mc2_bindings — the project_integrations read layer
(Phase B of the flat-column retirement)."""

from unittest.mock import MagicMock

from cp_engine import mc2_bindings as b


def _binding(owner_col, owner_id, service, ref, label=""):
    row = {"project_id": None, "initiative_id": None,
           "service": service, "external_ref": ref, "label": label}
    row[owner_col] = owner_id
    return row


def _client(rows):
    client = MagicMock()
    resp = MagicMock()
    resp.data = rows
    client.table.return_value.select.return_value.in_.return_value.execute.return_value = resp
    return client


# --- fetch --------------------------------------------------------------------


def test_fetch_groups_by_owner():
    rows = [
        _binding("project_id", "p-1", "slack", {"id": "C1"}),
        _binding("project_id", "p-2", "clickup", {"id": "L2"}),
        _binding("project_id", "p-1", "google_drive", {"id": "G1"}),
    ]
    grouped = b.fetch_binding_rows(_client(rows), project_ids=["p-1", "p-2"])
    assert sorted(r["service"] for r in grouped["p-1"]) == ["google_drive", "slack"]
    assert [r["service"] for r in grouped["p-2"]] == ["clickup"]


def test_fetch_empty_ids_makes_no_query():
    client = MagicMock()
    assert b.fetch_binding_rows(client) == {}
    client.table.assert_not_called()


def test_fetch_tolerates_non_list_data():
    """Loose mocks (MagicMock .data) must collapse to no rows, not crash."""
    client = MagicMock()  # .execute().data is a MagicMock, not a list
    assert b.fetch_binding_rows(client, project_ids=["p-1"]) == {}


# --- hydration ------------------------------------------------------------------


def test_hydrate_project_row_full():
    rows = [
        _binding("project_id", "p-1", "slack", {"id": "C1", "url": "slack://channel?id=C1"}),
        _binding("project_id", "p-1", "slack", {"id": "C9"}, label="C9"),
        _binding("project_id", "p-1", "clickup", {
            "id": "L1", "extra": {"folder_id": "F1", "list_id": "L1"},
        }),
        _binding("project_id", "p-1", "google_drive", {"id": "G1"}),
        _binding("project_id", "p-1", "dropbox", {"url": "/1P Dropbox/Jobs/ABC 5001"}),
    ]
    row = b.hydrate_project_row({"id": "p-1", "name": "x"}, rows)
    assert row["slack_channel_id"] == "C1"
    assert row["slack_channel_ids"] == ["C1", "C9"]  # primary first, labeled after
    assert row["clickup_list_id"] == "L1"
    assert row["google_drive_folder_id"] == "G1"
    assert row["mc_dropbox_folder_id"] == "/1P Dropbox/Jobs/ABC 5001"
    assert row["name"] == "x"  # non-binding keys untouched


def test_hydrate_project_row_unbound_sets_empty():
    row = b.hydrate_project_row({"id": "p-1"}, [])
    assert row["slack_channel_id"] is None
    assert row["slack_channel_ids"] == []
    assert row["clickup_list_id"] is None
    assert row["google_drive_folder_id"] is None
    assert row["mc_dropbox_folder_id"] is None


def test_clickup_folder_only_project_has_no_list():
    rows = [_binding("project_id", "p-1", "clickup", {
        "id": "F1", "extra": {"folder_id": "F1"},
    })]
    assert b.hydrate_project_row({"id": "p-1"}, rows)["clickup_list_id"] is None


def test_hydrate_initiative_row():
    """Initiative clickup refs carry the list id as the plain ref id."""
    rows = [
        _binding("initiative_id", "i-1", "clickup", {"id": "L7", "extra": {"list_id": "L7"}}),
        _binding("initiative_id", "i-1", "slack", {"id": "C1"}),
        _binding("initiative_id", "i-1", "slack", {"id": "C2"}, label="C2"),
    ]
    row = b.hydrate_initiative_row({"id": "i-1"}, rows)
    assert row["clickup_list_id"] == "L7"
    assert row["slack_channel_ids"] == ["C1", "C2"]
    # No folder bindings → hydrated keys present but None (mc-2 #192).
    assert row["google_drive_folder_id"] is None
    assert row["mc_dropbox_folder_id"] is None


def test_hydrate_initiative_row_folder_bindings():
    """Drive/Dropbox folder refs hydrate onto initiative rows (mc-2 #192):
    drive id from ref `id`, dropbox folder PATH from ref `url` — the same
    shapes hydrate_project_row consumes."""
    rows = [
        _binding("initiative_id", "i-1", "google_drive", {"id": "drv-9"}),
        _binding("initiative_id", "i-1", "dropbox", {"url": "/Internal/StoryOS"}),
    ]
    row = b.hydrate_initiative_row({"id": "i-1"}, rows)
    assert row["google_drive_folder_id"] == "drv-9"
    assert row["mc_dropbox_folder_id"] == "/Internal/StoryOS"


def test_initiative_clickup_ref_without_extra():
    rows = [_binding("initiative_id", "i-1", "clickup", {"id": "L7"})]
    assert b.hydrate_initiative_row({"id": "i-1"}, rows)["clickup_list_id"] == "L7"
