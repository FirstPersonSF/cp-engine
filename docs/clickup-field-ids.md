# ClickUp custom field IDs (FirstPersonSF workspace, team 20537476)

> Reference for the webhook's approve-and-push path. ClickUp custom
> field values are submitted by field UUID — for dropdown fields, the
> value is also a UUID (the option's ID), not the label string.

Captured 2026-06-02 from the `_PROJECT_TEMPLATE` list
(`https://app.clickup.com/20537476/v/li/901327436068`) via
`GET https://api.clickup.com/api/v2/list/901327436068/field`.

When new lists are cloned from `_PROJECT_TEMPLATE`, these UUIDs come
along unchanged — that's why the template-clone pattern is the
load-bearing convention. **Don't recreate custom fields by hand on a
cloned list** — the UUIDs would diverge and the webhook would push to
the wrong field on that list.

## Field IDs (env-var names)

| Field | UUID | Env var |
|---|---|---|
| Type | `0798fb3b-aec0-4d2b-8f04-f12ca1073396` | `CLICKUP_TYPE_FIELD_ID` |
| Confidence | `59c88f47-04cd-44e0-bb08-de7631d12941` | `CLICKUP_CONFIDENCE_FIELD_ID` |
| Linked To | `5dfe6eea-d3d5-4f3e-86c4-b5f6d2de460a` | `CLICKUP_LINKED_TO_FIELD_ID` |

## Dropdown option IDs

ClickUp's API expects the option UUID (not the label) when setting a
dropdown field's value. The webhook should map our internal enum
strings → these UUIDs at push time.

### Type field options

| Internal value | Option UUID |
|---|---|
| `action_item` | `89581e18-9754-40f7-a967-5371bd0f6af2` |
| `client_ask` | `14ccf669-9a27-485d-aa2f-30a9ef7e55f8` |
| `milestone` | `54f02d90-c983-4d44-aaed-c1e0b408f66f` |

### Confidence field options

| Internal value | Option UUID |
|---|---|
| `high` | `74ae1288-ee63-4e1d-b8f5-93fe67b45686` |
| `medium` | `115478bc-85b9-44b4-9a85-de5bd2f92e90` |
| `low` | `60ba60fe-9201-4736-ab32-48583e62fa78` |

## Where to put the env vars

- Railway `cp-engine-webhook` service (for the live approve-and-push path)
- `mc-2/backend/.env` (for local CLI runs that share that env file)
- Anywhere else the webhook code reads — survey `os.environ.get("CLICKUP_*")`
  callsites before deploying

## Pattern for code that consumes these

Don't inline the option-UUID maps in handler code — they're data, not
logic. Put them in a small module-level dict near where the push
payload is built. Reading them from env via separate vars per option
(e.g. `CLICKUP_TYPE_OPTION_MILESTONE`) is overkill — the options
rarely change, the UUIDs are stable, and the workspace-clone pattern
guarantees they propagate unchanged.

If ClickUp ever rebuilds the template (or the workspace is migrated),
re-run the field lookup and update this doc. Tests should mock the
UUIDs, not depend on them.
