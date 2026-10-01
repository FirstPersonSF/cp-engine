#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""Register the ClickUp → cp-engine webhook ONCE.

Calls ClickUp's REST API to create a webhook pointing at the
cp-engine-webhook service's /clickup-task-closed endpoint. ClickUp
returns a `secret` in the response — this is the HMAC secret you set
on the Railway service as CLICKUP_WEBHOOK_SECRET.

Usage
-----
Set environment variables OR pass as args:

    CLICKUP_API_TOKEN=pk_xxx CLICKUP_TEAM_ID=12345 ./register-clickup-webhook.py

Or:

    ./register-clickup-webhook.py --token pk_xxx --team-id 12345

The team_id (also called workspace ID) is the number after /v/o/ in
your ClickUp URL. E.g., https://app.clickup.com/12345/v/o/12345 →
team_id = 12345.

The script prints the returned secret. Copy it into Railway:

    cp-engine-webhook service → Variables → New Variable:
        CLICKUP_WEBHOOK_SECRET = <pasted secret>

Then redeploy the service so it picks up the env var.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


CP_ENGINE_WEBHOOK_URL = (
    "https://cp-engine-production.up.railway.app/clickup-task-closed"
)
# taskStatusUpdated fires on any status transition; the handler filters
# for "after.type == closed" so non-close transitions return 204.
EVENTS = ["taskStatusUpdated"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--token",
        default=os.environ.get("CLICKUP_API_TOKEN"),
        help="ClickUp Personal API Token (starts with `pk_`). "
        "Get from https://app.clickup.com/settings/apps. "
        "Defaults to $CLICKUP_API_TOKEN.",
    )
    parser.add_argument(
        "--team-id",
        default=os.environ.get("CLICKUP_TEAM_ID"),
        help="ClickUp Workspace (team) ID. Defaults to $CLICKUP_TEAM_ID.",
    )
    parser.add_argument(
        "--endpoint",
        default=CP_ENGINE_WEBHOOK_URL,
        help=f"Webhook endpoint URL (default: {CP_ENGINE_WEBHOOK_URL}).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List existing webhooks instead of creating one.",
    )
    parser.add_argument(
        "--delete",
        metavar="WEBHOOK_ID",
        help="Delete a webhook by ID instead of creating one.",
    )
    args = parser.parse_args()

    if not args.token:
        print("Error: --token or $CLICKUP_API_TOKEN required", file=sys.stderr)
        return 2
    if not args.team_id and not args.delete:
        print("Error: --team-id or $CLICKUP_TEAM_ID required", file=sys.stderr)
        return 2

    headers = {"Authorization": args.token, "Content-Type": "application/json"}

    if args.list:
        url = f"https://api.clickup.com/api/v2/team/{args.team_id}/webhook"
        resp = httpx.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        webhooks = data.get("webhooks") or []
        if not webhooks:
            print("(no webhooks registered for this team)")
            return 0
        for w in webhooks:
            print(f"id:       {w.get('id')}")
            print(f"endpoint: {w.get('endpoint')}")
            print(f"events:   {w.get('events')}")
            print(f"health:   {w.get('health', {}).get('status')}")
            print()
        return 0

    if args.delete:
        url = f"https://api.clickup.com/api/v2/webhook/{args.delete}"
        resp = httpx.delete(url, headers=headers, timeout=30)
        if resp.status_code in (200, 204):
            print(f"Deleted webhook {args.delete}.")
            return 0
        print(
            f"Delete failed ({resp.status_code}): {resp.text}",
            file=sys.stderr,
        )
        return 1

    # Create
    url = f"https://api.clickup.com/api/v2/team/{args.team_id}/webhook"
    body = {"endpoint": args.endpoint, "events": EVENTS}
    print(f"Creating webhook:")
    print(f"  team_id:  {args.team_id}")
    print(f"  endpoint: {args.endpoint}")
    print(f"  events:   {EVENTS}")
    print()

    resp = httpx.post(url, headers=headers, json=body, timeout=30)
    if resp.status_code not in (200, 201):
        print(
            f"Create failed ({resp.status_code}): {resp.text}",
            file=sys.stderr,
        )
        return 1

    data = resp.json()
    webhook = data.get("webhook") or data
    secret = webhook.get("secret")
    webhook_id = webhook.get("id")

    print("✓ Webhook created.")
    print()
    print(f"  webhook_id: {webhook_id}")
    print(f"  secret:     {secret}")
    print()
    print("NEXT STEPS:")
    print()
    print("  1. Open Railway → cp-engine-webhook service → Variables")
    print("  2. Add (or update) variable:")
    print(f"        CLICKUP_WEBHOOK_SECRET = {secret}")
    print("  3. Redeploy the service so it picks up the env var.")
    print()
    print("  4. Smoke test: find a ClickUp task created from a cp ask")
    print("     (i.e. one with [cp:hash=...] in its description) and")
    print("     mark it Closed. Within ~30s, the cp tenant should get")
    print("     a `[clickup-close] <code>: hash <hash>` commit.")
    print()
    print("  Full webhook record (for reference):")
    print(json.dumps(webhook, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
