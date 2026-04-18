# Runbook: MCP Server Fails to Load

| | |
|---|---|
| **Owner** | TBD |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Related failure codes** | TBD — see [Known Failure Codes](Reference-Known-Failure-Codes) |
| **Status** | Draft |

Note: the target wiki IA lists "Plugin Fails to Load" — Reg has no plugin system in v2.4.2 (see [Q-5](Development-SOPs-Documentation-Open-Questions)). The MCP server is the integration surface that actually exists, so this runbook owns that slot.

## Symptom

- Claude CLI does not show Reg MCP tools (`search_knowledge_base`, `list_projects`, `index_status`).
- The MCP server crashes on startup.
- MCP tool calls return `[RAG ERROR]` strings for reasons other than a genuine search miss.
- Startup is slow (>15 s) and then tools still don't work.

## Quick check

```
rag-mcp --help
```
If the binary is not found, the Claude CLI will never launch it either.

Verify Claude Code sees the server: in Claude Code, list available tools and look for `search_knowledge_base`.

Fetch the canonical MCP config from the service (if running):
```
curl http://127.0.0.1:21420/api/mcp-config
```
Compare to your `.mcp.json`.

## Diagnostic commands

| Command | What it tells you |
|---|---|
| `rag version` | The binary runs at all; version reported |
| `rag-mcp` (directly) | Launches stdio transport — should print nothing to stdout, logs to stderr |
| `rag doctor` | Service state, Qdrant collection state, deps |
| `curl http://127.0.0.1:<port>/health` | Whether the probe can succeed (determines proxy vs direct) |
| `rag serve` | Same as `rag-mcp`, but via CLI |

## Root causes

1. **`rag-mcp` not on PATH.** Dev install: venv not active. Installed: PATH edit did not take effect (needs new shell).
2. **Malformed `.mcp.json`.** Typo, missing comma, wrong command, wrong args.
3. **Frozen-exe path wrong in `.mcp.json`.** Installed-mode config points at a non-existent or renamed `rag.exe`.
4. **Service probe failed, direct mode failed.** Classic cascade: service not up, and direct mode can't initialize (no collection, encoder load failure).
5. **Collection never created.** Index has not been run; direct mode returns `[RAG ERROR] Knowledge base not initialized`.
6. **Dev and installed configs collide.** Both `ragtools` and `ragtools-dev` servers defined; Claude CLI uses one and you expect the other.

## Fix procedure

### If cause = `rag-mcp` not on PATH
- Dev: activate venv (`source .venv/bin/activate` or Windows equivalent).
- Installed: open a new terminal. If PATH still does not include `C:\Program Files\RAGTools\`, repair the install.

### If cause = malformed `.mcp.json`
1. Get a known-good config from the service: `curl http://127.0.0.1:21420/api/mcp-config`.
2. Replace the Reg section in your `.mcp.json` with the `config.mcpServers` value.
3. Restart Claude CLI.

### If cause = frozen-exe path wrong
Same as above — always fetch from `/api/mcp-config`. The service auto-detects the right binary location.

### If cause = service probe failed + direct mode failed
1. Start the service: `rag service start`. This resolves the cascade.
2. If the service cannot start, go to [Service Fails to Start](Runbooks-Service-Fails-to-Start).

### If cause = collection never created
```
rag index .
```
(or the project path). After the first successful index, the MCP server can enter direct mode.

### If cause = dev + installed collision
Pick one. For Claude Code contexts, use the installed `ragtools` entry. For local dev, set `RAG_SERVICE_PORT=21421` (the dev default) and use the `ragtools-dev` entry the service emits.

## Verification

- Claude CLI lists `search_knowledge_base`, `list_projects`, `index_status` in its tool catalog.
- `index_status()` from Claude returns a non-error string starting `[RAG STATUS]`.
- MCP startup completes in <1 s (proxy mode) or 5-10 s (direct mode).

## Escalation

Capture:
- The exact `.mcp.json` section used.
- `rag version` and `rag doctor`.
- stderr output from `rag-mcp` launched directly.
- Response from `GET /api/mcp-config`.

File an issue with all four.

## Related
- SOP: [Start and Stop Service](Operational-SOPs-Service-Start-and-Stop-Service).
- Architecture: [MCP Proxy Decision](Architecture-MCP-Proxy-Decision).
- Reference: [MCP Tools](Reference-MCP-Tools), [HTTP API § MCP connection](Reference-HTTP-API#mcp-connection).
- Runbook: [Service Fails to Start](Runbooks-Service-Fails-to-Start).
