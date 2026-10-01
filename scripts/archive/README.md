# Archived one-shot scripts

Each of these ran once against production and is not meant to run again. They
are kept for the record (what a migration did and how) and so their tests can
still import them. Moved here in architecture plan step 5a (2026-10-01).

| Script | What it did | Applied |
|---|---|---|
| `register-clickup-webhook.py` | Registered the ClickUp → cp-engine-webhook task-closed webhook through ClickUp's API. | 2026-05-27 |
| `backfill_source_coords.py` | Backfilled `source_provider` / `source_file_id` on legacy `rag_assets` rows from their old temp `file_path`. | 2026-06-20 |
| `full_job_name_id_map.py` | Built the read-only old → new id map (`ibx-5192` → full `full_job_name` slug) for the canonical-id migration. | 2026-06-20 |
| `migrate_full_job_name_ids.py` | `git mv`-ed tenant dirs and sprint files from the legacy id to the canonical slug, using the map above. | 2026-06-20 |
| `rekey_commitment_hashes.py` | Step 4a: re-keyed every MC-2 commitment's `cp_hash` to the one `asks.ask_hash` recipe. | 2026-10-01 |
| `reconcile_sprint_asks.py` | Step 4a: matched every sprint-file ask to an MC-2 commitment, or imported it as one. | 2026-10-01 |
| `step4b_migrate_stakeholders.py` | Step 4b: turned every person in the markdown Stakeholders sections into a spine Stakeholders card. | 2026-10-01 |
| `step4c_import_spine_cards.py` | Step 4c: imported hand-written `spine/` cards into MC-2 and moved documents out of `spine/`. | 2026-10-01 |
| `move_meeting_history.py` | Step 4c: moved each `spine/Retrospective/meeting-history.md` to `<workstream>/meeting-history.md`. | 2026-10-01 |

Still in `scripts/` because they are reused: `release.py`, `post_release.py`,
`sweep_dupes.py` (named by `release.py`'s preflight), and
`estimate_scope_reconcile.py` (a repeatable report).
