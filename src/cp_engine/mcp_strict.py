"""Make every tool on an MCP server reject arguments it does not declare (#318).

The MCP SDK validates a call's arguments through a pydantic model whose default
is `extra="ignore"`. So a caller that copied a stale parameter name — the
`type=` that `/cp-tools` once documented for `create_spine_element` — had it
dropped without a word, the default applied, and decisions were filed into
layer Note three times over five days. The advertised schema did not carry
`additionalProperties: false`, so a client had no way to know.

Once every tool is registered, each argument model is swapped for a subclass
with `extra="forbid"` and the advertised schema is regenerated from it. A
misnamed argument now fails the call with pydantic's "Extra inputs are not
permitted" naming the argument, and the schema tells a client before it tries.

Shared by the hosted server and the stdio `cxp mcp` server (vendored verbatim
under `prototypes/hosted-mcp/vendor/`), so both refuse the same way. Call it
AFTER the last `@tool` registration: a tool registered later stays lenient.
"""

from __future__ import annotations

from pydantic import ConfigDict


def forbid_unknown_arguments(server) -> int:
    """Make every registered tool reject undeclared arguments; returns how many
    were changed (a tool already strict is skipped, so a second call is a
    no-op).

    Reaches the SDK's `_tool_manager` — private, but pinned (`mcp>=2.0,<3`)
    and exercised end-to-end by the servers' signature tests, which call a
    tool through `call_tool` with a stray argument.
    """
    count = 0
    for tool in server._tool_manager.list_tools():
        base = tool.fn_metadata.arg_model
        if base.model_config.get("extra") == "forbid":
            continue
        strict = type(
            base.__name__,
            (base,),
            {
                "__module__": base.__module__,
                "model_config": ConfigDict(**{**base.model_config, "extra": "forbid"}),
            },
        )
        tool.fn_metadata.arg_model = strict
        tool.parameters = strict.model_json_schema(by_alias=True)
        count += 1
    return count
