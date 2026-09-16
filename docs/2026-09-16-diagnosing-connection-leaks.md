# Diagnosing a connection leak in cp-engine

**2026-09-16 · shipped as v0.117.2**

A `cxp sync` was holding a growing number of open sockets and taking over five
minutes. This records how the cause was found, because the first three
hypotheses were all wrong and the method that eventually worked generalises.

## What it looked like

A climbing count of ESTABLISHED sockets to the Supabase host during a sync,
and a run that exceeded 300s without finishing on a 38-project tenant.

## The three wrong answers

| Hypothesis | Why it was plausible | Why it was wrong |
|---|---|---|
| httpx pool is misconfigured | Nothing in `mc2_db.py` sets `limits=` | Pool defaults are *unreachable* from a sequential caller — one request in flight can't open 100 connections |
| Clients are being constructed per call | `asset_ingest` injects explicit `url=`/`key=`, a different cache key shape | Instrumentation showed `create_client_calls=1` for a whole run |
| The reap-retry transport abandons sockets | `_RetryingTransport` walks away from a connection on every GOAWAY | Wrapping the transport was measured not to affect pool reuse at all |

The common thread: each was a plausible story about *code that exists*, and
none was a measurement.

## What it actually was

`SyncPostgrestClient.schema()` constructs an entire new client on every call —
new `httpx.Client`, new pool, new TLS connection — and never closes it. The
object is discarded at the end of the expression, but httpx only releases a
pool on `close()`, so the socket stays ESTABLISHED until the process exits.

`estimate.fetch_estimate` makes five `.schema()` calls per project. At 38
projects that is ~190 abandoned connections.

**The reason the pool theory was seductive and wrong:** each leaked client
brought its *own* pool. A per-pool `max_connections` cannot bound a
*population* of pools. Tuning the limits would have changed nothing.

## The method that found it

### 1. Instrument construction before theorising about it

`CP_MC2_CLIENT_STATS=1` enables counters in `mc2_db.py`
(`get_client_calls`, `cache_hits`, `create_client_calls`, `retry_after_reap`)
plus `client_stats()` / `report_client_stats()`. The decisive datum was
`create_client_calls=1` against 199 live sockets — two numbers that cannot
both be true unless something *other* than the cached client is opening
connections. That one contradiction eliminated two hypotheses at once.

### 2. Count sockets per state, scoped to one PID

The obvious probe is wrong in two ways:

```sh
# DON'T
lsof -p $(pgrep -f "cxp render") -a -i -Pn | grep -c ESTABLISHED
```

* `pgrep -f` can match several pids (including the grep itself), so `-p`
  silently takes a list and the count mixes processes.
* Counting only ESTABLISHED hides the states that indicate a leak —
  CLOSE_WAIT / FIN_WAIT sockets are still held file descriptors.

```sh
# DO — per-state census for exactly one pid
lsof -nP -p "$PID" -a -i -FT | grep '^TST=' | sed 's/^TST=//' | sort | uniq -c
```

**Validate the probe against a known ramp first.** A control that opens N
sockets, holds them, then closes them proved the corrected probe sees both the
climb *and* 10 sockets sitting in CLOSE_WAIT after close — which the
ESTABLISHED-only count reports as "0, all clean" while 10 fds are still held.

### 3. Trace the constructor when the counters disagree with reality

Patching `httpx.Client.__init__` to record a stack fragment per construction
named the culprit in one run:

```
29  client.py:109:schema <- estimate.py:184:fetch_estimate <- sync.py:623
```

Counts that scale with project count are the tell.

## Results

| | Before | After |
|---|---|---|
| Peak ESTABLISHED | 199, climbing | 3, flat |
| Wall clock (38 projects) | 300s+, unfinished | 95.8s, completed |
| httpx clients | 113 at t=90s | 1 per schema |

## The rule that earned its keep

*A control test that passes proves nothing.* Every claim here was checked by
first making it **fail**:

* the socket probe, against a deliberate 10-socket ramp;
* the leak probe, against unfixed code (81 clients for 80 calls);
* the regression test, by disabling the fix (40 calls → 40 clients).

A test asserting only "sync completes" would have passed on day one.

## Related: three silent failures found alongside

Instrumenting the sync surfaced that mc-2 migration 072 dropped
`spine_elements` while five references survived. Two were swallowed by
best-effort `except` blocks and had been failing **silently**:

* `cp spine` showed **no source documents for any project** (0 → 8 after fix);
* `cxp sweep` reported "Flagged 0 drifted element(s)" while writing nothing;
* `cxp spine-stats` crashed on PGRST205 — retired, since its reports key on
  `type`/`stage`/`target_date`, which `spine_substance` does not carry.

**The lesson worth carrying:** a best-effort `except` around a query converts a
schema break into silence. When a migration drops a table, grep for *every*
reference rather than waiting for a crash — the crash is the loudest failure,
not the worst one.
