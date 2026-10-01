#!/usr/bin/env python3
"""Provision one Railway cron service for a cp-engine cron job (docs/railway-cron.md).

    python webhook/cron/provision.py sync                 # print the plan, change nothing
    python webhook/cron/provision.py sync --apply         # do it
    python webhook/cron/provision.py sync --apply --verify   # first run is a dry run

WHY A SCRIPT. Creating cp-cron-health by hand (2026-10-01) found three traps:

- `railway add -r FirstPersonSF/cp-engine` 401s (the Railway GitHub app has
  no grant on the repo), so the service is created EMPTY and deployed by
  `railway up <dir> --path-as-root` of a minimal upload: the Dockerfile at
  the upload's root plus webhook/cron/trigger.py at its repo path, so the
  Dockerfile's `COPY webhook/cron/trigger.py` works unchanged.
- A `railway.json` `cronSchedule` in that upload is IGNORED. The schedule and
  restart policy NEVER are set with GraphQL `serviceInstanceUpdate`.
- Nothing confirms the schedule took unless you read it back: this reads
  `cronSchedule`, `restartPolicyType` and `nextCronRunAt` and exits 1 when
  they are not what was asked for.

STEPS (idempotent — an existing service of the same name is reused):
    1. find or `serviceCreate` the service (empty: no repo, no image);
    2. `variableCollectionUpsert` (skipDeploys) the trigger's variables —
       WEBHOOK_HMAC_SECRET as the reference `${{cp-engine.WEBHOOK_HMAC_SECRET}}`
       (never a value), CP_WEBHOOK_URL, CRON_JOB, CRON_SCHEDULE; with
       --verify also CRON_DRY_RUN=1 + CRON_SLOT=<first slot>;
    3. `railway up <upload> --path-as-root --service <id> --ci`;
    4. `serviceInstanceUpdate` {cronSchedule, restartPolicyType: NEVER};
    5. read back and check.

THE FIRST DEPLOY RUNS THE TRIGGER ONCE (Railway starts the container). Away
from a slot it exits 2 ("not guessing") — a red run, harmless. Inside a slot's
45-minute window it performs that slot's real run, which the route's per-slot
idempotency makes the one run for that slot. --verify avoids both: the first
run is a dry run of the first slot (the route renders / drafts, commits
nothing). Then remove the two variables:
    railway variable delete CRON_DRY_RUN --service <name> --skip-deploys
    railway variable delete CRON_SLOT --service <name> --skip-deploys

AUTH. The Railway CLI's user token (~/.railway/config.json user.accessToken)
or RAILWAY_API_TOKEN. Used only in the Authorization header; never printed.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

GRAPHQL = "https://backboard.railway.com/graphql/v2"
PROJECT_ID = "401083bc-1c2d-49e1-8ec6-1a9b9e230e3b"   # "Fathom Meeting sync"
ENVIRONMENT = "production"
WEBHOOK_SERVICE = "cp-engine"                          # holds WEBHOOK_HMAC_SECRET
CP_WEBHOOK_URL = "https://${{cp-engine.RAILWAY_PUBLIC_DOMAIN}}"

#: job → (service name, Railway cron schedule, UTC). Must match the route's
#: JOBS (webhook/routers/cron.py) and docs/railway-cron.md.
JOBS: dict[str, tuple[str, str]] = {
    "health": ("cp-cron-health", "23 12,13 * * *"),
    "sync": ("cp-cron-sync", "0 14,22 * * *"),
    "draft-summaries": ("cp-cron-drafts", "17 12,13 * * 1"),
}

REPO = Path(__file__).resolve().parents[2]


class ProvisionError(Exception):
    pass


def _token() -> str:
    tok = os.environ.get("RAILWAY_API_TOKEN")
    if tok:
        return tok
    try:
        cfg = json.loads((Path.home() / ".railway" / "config.json").read_text())
        return cfg["user"]["accessToken"]
    except (OSError, KeyError, ValueError) as exc:
        raise ProvisionError(
            f"no Railway token (RAILWAY_API_TOKEN, or `railway login`): {type(exc).__name__}") from None


def gql(query: str, variables: dict | None = None) -> dict:
    req = urllib.request.Request(
        GRAPHQL, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json",
                 "User-Agent": "cp-engine-cron-provision"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — fixed host
        out = json.loads(resp.read().decode())
    if out.get("errors"):
        raise ProvisionError("GraphQL: " + "; ".join(e.get("message", "?") for e in out["errors"]))
    return out["data"]


def first_slot(schedule: str) -> str:
    """``"0 14,22 * * *"`` → ``"0 14 * * *"``; ``"17 12,13 * * 1"`` → ``"17 12 * * 1"``."""
    m, h, dom, mon, dow = schedule.split()
    return f"{m.split(',')[0]} {h.split(',')[0]} {dom} {mon} {dow.split(',')[0]}"


def trigger_variables(job: str, schedule: str, *, verify: bool) -> dict[str, str]:
    variables = {
        "WEBHOOK_HMAC_SECRET": f"${{{{{WEBHOOK_SERVICE}.WEBHOOK_HMAC_SECRET}}}}",
        "CP_WEBHOOK_URL": CP_WEBHOOK_URL,
        "CRON_JOB": job,
        "CRON_SCHEDULE": schedule,
    }
    if verify:
        variables["CRON_DRY_RUN"] = "1"
        variables["CRON_SLOT"] = first_slot(schedule)
    return variables


def build_upload(dest: Path) -> Path:
    """The minimal upload: Dockerfile at the root + the trigger at its repo
    path (the Dockerfile COPYs `webhook/cron/trigger.py`)."""
    (dest / "webhook" / "cron").mkdir(parents=True)
    shutil.copy2(REPO / "webhook" / "cron" / "Dockerfile", dest / "Dockerfile")
    shutil.copy2(REPO / "webhook" / "cron" / "trigger.py", dest / "webhook" / "cron" / "trigger.py")
    return dest


def _lookup() -> tuple[str, dict[str, str]]:
    data = gql("""query($id: String!) { project(id: $id) {
        environments { edges { node { id name } } }
        services { edges { node { id name } } } } }""", {"id": PROJECT_ID})
    proj = data["project"]
    envs = {e["node"]["name"]: e["node"]["id"] for e in proj["environments"]["edges"]}
    if ENVIRONMENT not in envs:
        raise ProvisionError(f"no {ENVIRONMENT!r} environment in project {PROJECT_ID}")
    services = {s["node"]["name"]: s["node"]["id"] for s in proj["services"]["edges"]}
    if WEBHOOK_SERVICE not in services:
        raise ProvisionError(f"no {WEBHOOK_SERVICE!r} service to reference")
    return envs[ENVIRONMENT], services


def read_back(service_id: str, env_id: str) -> dict:
    return gql("""query($s: String!, $e: String!) { serviceInstance(serviceId: $s, environmentId: $e) {
        cronSchedule restartPolicyType nextCronRunAt latestDeployment { status createdAt } } }""",
               {"s": service_id, "e": env_id})["serviceInstance"]


def provision(job: str, *, apply: bool, verify: bool, out=sys.stdout) -> int:
    name, schedule = JOBS[job]
    variables = trigger_variables(job, schedule, verify=verify)
    env_id, services = _lookup()
    service_id = services.get(name)
    say = lambda msg: print(msg, file=out)  # noqa: E731
    say(f"job {job!r} → service {name!r} ({'exists ' + service_id if service_id else 'to create'})")
    say(f"  variables: {', '.join(variables)}  (WEBHOOK_HMAC_SECRET is a reference, not a value)")
    say(f"  upload:    Dockerfile + webhook/cron/trigger.py, `railway up --path-as-root`")
    say(f"  schedule:  {schedule!r} UTC, restart policy NEVER (GraphQL serviceInstanceUpdate)")
    if not apply:
        say("plan only — pass --apply to provision.")
        return 0

    if service_id is None:
        service_id = gql("""mutation($in: ServiceCreateInput!) { serviceCreate(input: $in) { id } }""",
                         {"in": {"projectId": PROJECT_ID, "name": name}})["serviceCreate"]["id"]
        say(f"created {name} ({service_id})")

    gql("""mutation($in: VariableCollectionUpsertInput!) { variableCollectionUpsert(input: $in) }""",
        {"in": {"projectId": PROJECT_ID, "environmentId": env_id, "serviceId": service_id,
                "variables": variables, "skipDeploys": True}})
    say(f"variables set: {', '.join(variables)}")

    with tempfile.TemporaryDirectory(prefix="cp-cron-upload-") as tmp:
        upload = build_upload(Path(tmp) / "upload")
        rc = subprocess.run(["railway", "up", str(upload), "--path-as-root",
                             "--project", PROJECT_ID, "--environment", ENVIRONMENT,
                             "--service", service_id, "--ci",
                             "--message", f"cp-engine cron trigger ({job})"]).returncode
    if rc != 0:
        raise ProvisionError(f"railway up exited {rc}")

    gql("""mutation($s: String!, $e: String!, $in: ServiceInstanceUpdateInput!) {
        serviceInstanceUpdate(serviceId: $s, environmentId: $e, input: $in) }""",
        {"s": service_id, "e": env_id,
         "in": {"cronSchedule": schedule, "restartPolicyType": "NEVER"}})

    got = read_back(service_id, env_id)
    say(f"read back: cronSchedule={got.get('cronSchedule')!r} "
        f"restartPolicyType={got.get('restartPolicyType')!r} "
        f"nextCronRunAt={got.get('nextCronRunAt')!r} "
        f"latestDeployment={(got.get('latestDeployment') or {}).get('status')!r}")
    if got.get("cronSchedule") != schedule or got.get("restartPolicyType") != "NEVER" \
            or not got.get("nextCronRunAt"):
        say("MISMATCH — the service is not scheduled as asked. Fix it before retiring "
            "the GitHub schedule.")
        return 1
    if verify:
        say(f"verify: read `railway logs --service {name}` for `HTTP 202 · accepted` "
            "(or `200 · skipped`), then delete CRON_DRY_RUN and CRON_SLOT (see the header).")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("job", choices=sorted(JOBS))
    ap.add_argument("--apply", action="store_true", help="provision (default: print the plan)")
    ap.add_argument("--verify", action="store_true",
                    help="first run is a dry run of the first slot (CRON_DRY_RUN + CRON_SLOT)")
    args = ap.parse_args(argv)
    try:
        return provision(args.job, apply=args.apply, verify=args.verify)
    except (ProvisionError, OSError) as exc:
        print(f"provision: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
