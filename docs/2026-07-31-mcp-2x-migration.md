---
Project: cp-engine
Provenance: Version 01 | 2026-07-31
Filename: 2026-07-31-mcp-2x-migration.md
Author: Claude
---

# Migrating `cp mcp` to the MCP Python SDK 2.x

**Outcome: a two-line change.** `mcp_server.py` is 1,821 lines with 37
`@mcp.tool()` definitions; none of them needed edits. The decorator API and
`run(transport="stdio")` are unchanged between 1.x and 2.x.

## Why this came up

The [MCP 2026-07-28 spec release](https://claude.com/blog/bringing-mcp-2026-07-28-to-claude)
shipped alongside Python SDK **2.0.0**, which **removes `mcp.server.fastmcp`** —
`FastMCP` is replaced by `mcp.server.MCPServer`.

cp-engine pinned `mcp>=1.2` with no upper bound. `uv.lock` held 1.28.0, so
local runs were fine, but the webhook image rebuilds cp-engine from source on
deploy — it resolved 2.0.0 and crashed at import:

```
ModuleNotFoundError: No module named 'mcp.server.fastmcp'
  meetings.py:251      _default_resolver
  mcp_server.py:29     from mcp.server.fastmcp import FastMCP
```

That failure is fixed separately (PR #130) by moving the pure MC-2 resolvers
out of `mcp_server` so webhook code never imports an MCP server at all. **This
doc covers the other half: porting the actual MCP server to 2.x.**

## The change

```diff
-from mcp.server.fastmcp import FastMCP
-mcp = FastMCP("cp-sources")
+from mcp.server import MCPServer
+mcp = MCPServer("cp-sources")
```

```diff
-"mcp>=1.2,<2",
+"mcp>=2.0,<3",
```

Note the floor moves to `>=2.0`: after this change `mcp_server.py` does **not**
run on 1.x, so a floor of 1.2 would be a lie. The upper bound stays — an
unbounded floor is exactly what let 2.0.0 land silently mid-deploy.

## What was verified (not assumed)

Signature comparison against a real 2.0.0 install:

| Surface | 1.x | 2.0.0 |
|---|---|---|
| `.tool()` decorator | `@mcp.tool()` | identical signature |
| `.run()` | `run(transport="stdio")` | identical signature |

End-to-end, against real `mcp==2.0.0`:

- `import cp_engine.mcp_server` → OK, `type(mcp).__name__ == "MCPServer"`
- `await mcp.list_tools()` → **37 tools**, all registered
- **Live stdio JSON-RPC session** — spawned `run_stdio()` as a subprocess and
  drove it over stdin/stdout: `initialize` returned `serverInfo.name =
  "cp-sources"`, then `tools/list` returned all 37 tools with `inputSchema`
  present.
- Full pytest suite (`tests/`, minus a stray duplicate file) — **all pass** on
  2.0.0.

### One rename that looks alarming and isn't

`Tool.inputSchema` (1.x) is `Tool.input_schema` (2.x) on the **Python object**.
The **wire format is unchanged** — `model_dump(by_alias=True)` still emits
`inputSchema`, confirmed in the live session above. So Claude Code sees exactly
what it saw before. Only in-process code touching `tool.inputSchema` would need
updating; cp-engine has none.

### Breaking changes from the 2.0.0 release notes — exposure check

| Change | cp-engine exposure |
|---|---|
| `FastMCP` → `MCPServer` | **2 lines** (fixed here) |
| `Client(cache=False)` → `cache=None` + `CacheConfig()` | none (`use_cache` hits are cp-engine's own asset-ingest flag) |
| `FileResource(is_binary=)` → `encoding` | none |
| `MCP_*` env vars removed | none (`_MCP_SERVER_NAME` is a local constant) |
| Streamable HTTP rejects bodies >4 MiB | none — `cp mcp` is stdio |

## Worth knowing

- **Protocol version negotiated in the smoke test was `2025-11-25`**, not
  `2026-07-28`. The SDK negotiates down to what the client asks for, so this is
  expected; it does not mean 2.x is running an old protocol.
- `uv lock` also dropped `pydantic-settings` and `python-dotenv` and added
  `truststore` — transitive churn from the 2.x dependency tree, not chosen here.
- The **architecture shift** the spec headlines (bidirectional-stateful →
  request/response) is what makes serverless/edge MCP deployment possible. It
  does not change the stdio path `cp mcp` uses, so it is upside available later
  rather than work required now.

## Gotcha hit during this work (unrelated to MCP)

`uv` refused to run in the cp-engine venv:

```
error: Failed to read metadata from: `.../StrEnum-0.4.15 2.dist-info`
  Caused by: after parsing `0.4.15 `, found `2`, which is not part of a valid version
```

The venv's `site-packages` had accumulated **141** Finder/iCloud-style `" 2"`
duplicate directories (`aiohappyeyeballs-2.6.2 2.dist-info`, `_pytest 2`, …).
`uv` parses every `*.dist-info` name as a version and hard-errors on these.
Quarantined to `/tmp/venv-dupe-quarantine` (each verified to have a canonical
twin first; the one stale record without a twin —
`cp_engine-0.80.0 2.dist-info`, superseded by 0.80.1 — moved too). Reversible.

There is a matching stray `tests/test_mcp_server 2.py` in the repo. Same cause,
still untracked, left alone.
