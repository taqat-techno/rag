# SOP: Add a New Plugin

| | |
|---|---|
| **Owner** | TBD (proposed: DX lead) |
| **Last validated against version** | 2.5.1 |
| **Last reviewed** | 2026-04-19 |
| **Status** | Active |

## Purpose
Explain how to author a new Claude Code plugin for the ragtools operational workflow — or for any adjacent domain — and publish it to the [taqat-techno plugin marketplace](https://github.com/taqat-techno/plugins) so Claude Code users can install it via `/plugins → Add Marketplace`.

## Scope

**In scope:**
- Authoring a **Claude Code plugin** (the `.claude-plugin/` convention) that wraps, orchestrates, or augments ragtools — or any other tool.
- Publishing the plugin to the taqat-techno marketplace so it appears under `/plugins`.
- The `rag-plugin` ship pattern (an operator-facing plugin that talks to the running ragtools service, never re-implements its search).

**Out of scope:**
- **In-process ragtools extension surfaces.** If you want to extend the `ragtools` Python package itself (new CLI subcommand, new HTTP route, new MCP tool, new slash command shipped inside the ragtools repo), see:
  - [Add a New Command](Development-SOPs-Commands-Add-a-New-Command)
  - [Add a New HTTP Endpoint](Development-SOPs-HTTP-API-Add-a-New-Endpoint)
  - [Add a New Slash Command](Development-SOPs-Skills-Add-a-New-Slash-Command)

  Those surfaces remain the canonical extension points for *code inside* ragtools. This SOP is specifically about *external* plugins that Claude Code users install.

## The plugin model

Claude Code plugins are self-contained directories that Claude Code loads at startup. A plugin can ship any combination of:

| Component | Directory | Purpose |
|---|---|---|
| **Manifest** | `.claude-plugin/plugin.json` | Required. Plugin name, version, description, author, homepage, keywords. |
| **MCP auto-wire** | `.mcp.json` (at plugin root) | Optional. Flat-shape MCP server declaration. Installing the plugin auto-registers the server — no manual user step. |
| **Skills** | `skills/<name>/SKILL.md` | Router-style skill definitions for on-demand loading. |
| **Commands** | `commands/<name>.md` | Slash-command entry points users invoke. |
| **Agents** | `agents/<name>.md` | Scoped subagents with their own tool allowlists. |
| **Hooks** | `hooks/<type>.py` (or `.sh`) | `PreToolUse` / `PostToolUse` / `UserPromptSubmit` hooks. Harness-enforced. |
| **Rules** | `rules/<name>.md` | Context rules the plugin can install into `~/.claude/CLAUDE.md`. |
| **Scripts** | `scripts/` | Helpers invoked by commands/hooks. |
| **Docs** | `ARCHITECTURE.md`, `CHANGELOG.md`, `README.md` | Plugin-level documentation. |

See the marketplace's [CLAUDE_CODE_PLUGIN_DEVELOPMENT_GUIDE.md](https://github.com/taqat-techno/plugins/blob/main/CLAUDE_CODE_PLUGIN_DEVELOPMENT_GUIDE.md) for the authoritative schema.

## Reference plugin: `rag-plugin`

The taqat-techno marketplace currently ships **7 plugins**. The one directly relevant to ragtools is `rag-plugin` — use it as the canonical example when authoring a new ragtools-adjacent plugin.

| Attribute | Value |
|---|---|
| Plugin path | [`rag-plugin/`](https://github.com/taqat-techno/plugins/tree/main/rag-plugin) |
| Manifest version | `v0.6.0` |
| Role | Operational console for ragtools — install, configure, diagnose, repair, upgrade. Never re-implements search. |
| Commands | `/rag-doctor`, `/rag-setup`, `/rag-projects`, `/rag-reset`, `/rag-config`, `/rag-sync-docs` (maintainer) |
| Skills | `ragtools-ops` (operator), `ragtools-release` (maintainer) |
| Agents | `rag-log-scanner` (Haiku-tier JSON-returning log pattern matcher) |
| Hooks | `lock_conflict_check.py` (PreToolUse Bash guardrail — warns before commands that fight the Qdrant single-process lock); `UserPromptSubmit` retrieval-reminder hook |
| Rules installed | `claude-md-retrieval-rule.md` → inserted into `~/.claude/CLAUDE.md` during `/rag-setup` |
| MCP | Flat-shape `.mcp.json` at plugin root spawns `rag serve`, auto-registers the full ragtools 22-tool surface |

**What it teaches** (re-use these conventions):
- **Never wrap `search_knowledge_base`.** The ragtools MCP server exposes search directly; a plugin that wraps search adds latency and loses fidelity.
- **Never open the Qdrant file lock directly.** The service is the sole owner. Plugins interact with ragtools via the HTTP API, the MCP surface, or `rag <subcommand>` shell-outs.
- **Never CWD-relative config writes.** All project writes go through `POST /api/projects`; config edits go through the server so `get_config_write_path()` is the only write target. (See Decision 2 and failure F-001 in the `rag-plugin` rules.)
- **State-aware commands.** Every command probes install state (not-installed / packaged-Win / packaged-mac / dev / DOWN / UP-but-old / UP-and-current) and branches intelligently. The shared contract is in `rag-plugin/rules/state-detection.md`.

## Preconditions

- [ ] You have **git** and can clone public GitHub repos.
- [ ] You have **Claude Code** installed locally and can run `/plugins`.
- [ ] You have read the [plugin development guide](https://github.com/taqat-techno/plugins/blob/main/CLAUDE_CODE_PLUGIN_DEVELOPMENT_GUIDE.md).
- [ ] If your plugin will auto-wire an MCP server, the target binary is on `PATH` on all supported platforms (or the plugin writes an absolute-path `~/.claude/.mcp.json` during setup, as `rag-plugin` does for edge cases).
- [ ] Your plugin's scope is clearly defined — write it in one sentence before touching code.

## Inputs

- Plugin name (short, lowercase, hyphens only: e.g. `my-plugin`).
- Semantic version (start at `0.1.0`).
- Target marketplace: `https://github.com/taqat-techno/plugins`.
- Your author metadata (name, email, homepage).

## Steps

### 1. Fork or clone the marketplace

```bash
git clone https://github.com/taqat-techno/plugins.git
cd plugins
git checkout -b add-<plugin-name>-plugin
```

### 2. Scaffold the plugin directory

```bash
mkdir -p <plugin-name>-plugin/{.claude-plugin,skills,commands,agents,hooks,rules,scripts,docs}
```

### 3. Author the manifest

Create `<plugin-name>-plugin/.claude-plugin/plugin.json`:

```json
{
  "name": "<plugin-name>",
  "version": "0.1.0",
  "description": "<one concise sentence>",
  "author": {
    "name": "<your name or org>",
    "email": "<email>",
    "url": "<homepage or github>"
  },
  "homepage": "https://github.com/taqat-techno/plugins/tree/main/<plugin-name>-plugin",
  "keywords": ["<keyword>", "..."]
}
```

### 4. Add capability files

Pick only what you need. Follow the schemas documented in the plugin development guide. Common patterns:

- **A single slash command** → `commands/<name>.md` + optional `scripts/<helper>.py`.
- **An MCP server auto-wire** → `.mcp.json` at plugin root with the `{ "mcpServers": { ... } }` flat shape.
- **A skill-driven workflow** → `skills/<router-name>/SKILL.md` plus reference content; the skill body stays short and loads references on demand.
- **A context rule** → `rules/<name>.md` — opt-in installer via one of your commands.
- **A guardrail hook** → `hooks/<trigger>.py` — harness-enforced; must be fast and side-effect-free on the happy path.

### 5. Register in the marketplace manifest

Edit `.claude-plugin/marketplace.json` at the repo root. Append your plugin to the `plugins` array:

```json
{
  "name": "<plugin-name>",
  "description": "<one-line>",
  "author": { "name": "<name>", "email": "<email>" },
  "source": "./<plugin-name>-plugin",
  "category": "<development|productivity|design>",
  "homepage": "https://github.com/taqat-techno/plugins/tree/main/<plugin-name>-plugin"
}
```

### 6. Write plugin docs

- `<plugin-name>-plugin/README.md` — user-facing: install, quick start, command catalog, what the plugin is / is NOT.
- `<plugin-name>-plugin/ARCHITECTURE.md` — non-obvious design decisions. Future-you will thank present-you.
- `<plugin-name>-plugin/CHANGELOG.md` — start with `0.1.0` and what shipped.

### 7. Validate

```bash
# From the marketplace repo root
python validate_plugin.py <plugin-name>-plugin
# or for quick sanity check:
python validate_plugin_simple.py <plugin-name>-plugin
```

Fix any schema violations before you open a PR.

### 8. Local smoke test

```bash
# Copy the marketplace dir into your local Claude Code plugins cache
# (use the marketplace's documented local install path)
# Then in Claude Code:
/plugins
# Verify your plugin appears in the list
# Run each command you added, check the hooks fire, check the MCP server registers
```

### 9. Submit a PR

Follow [CONTRIBUTING.md](https://github.com/taqat-techno/plugins/blob/main/CONTRIBUTING.md). Keep the PR scoped to a single plugin; if you're adding multiple, open separate PRs.

## Validation / expected result

The plugin is considered successfully added when:

1. `python validate_plugin.py <plugin-name>-plugin` exits 0.
2. In Claude Code, `/plugins` lists the plugin under **taqat-techno-plugins**.
3. Every command you declared in `commands/` appears in `/help` or the slash-command menu.
4. If the plugin ships `.mcp.json`, the declared MCP server shows as **connected** in `/plugins` detail.
5. Any hooks fire on the expected triggers without blocking unrelated actions.
6. Your PR is merged and the marketplace's `validate_plugin` CI check passes.

## Failure modes

| Symptom | Likely cause | Resolution |
|---|---|---|
| `/plugins` doesn't list the plugin after marketplace install | `marketplace.json` entry missing or malformed JSON | Re-run `validate_plugin_simple.py`; check JSON syntax. |
| MCP server shows as **disconnected** | Binary not on `PATH` on the user's platform | Follow the `rag-plugin` v0.6.0 pattern — on `/<plugin>-setup` branch, write a user-level `~/.claude/.mcp.json` with an absolute binary path. |
| Hook fires on every prompt but doesn't inject anything | The hook's shape-heuristic filter is inverted, OR the injection uses the wrong JSON key | Test with a deliberate matching prompt; inspect stdout/stderr; the injection key is `hookSpecificOutput.additionalContext`. |
| `validate_plugin.py` rejects the manifest | `plugin.json` missing required fields (name, version, description, author) | Fill the missing fields; `name` must match the directory name without the `-plugin` suffix. |
| Claude Code user reports the plugin adds latency | Command probes ragtools state on every invocation | Cache state for the session; see `rag-plugin/rules/state-detection.md`. |

## Code paths

- Marketplace manifest schema → `.claude-plugin/marketplace.json` in the marketplace repo
- Plugin manifest schema → `<plugin>/.claude-plugin/plugin.json`
- Validators → [`validate_plugin.py`](https://github.com/taqat-techno/plugins/blob/main/validate_plugin.py), [`validate_plugin_simple.py`](https://github.com/taqat-techno/plugins/blob/main/validate_plugin_simple.py)
- Reference implementation → [`rag-plugin/`](https://github.com/taqat-techno/plugins/tree/main/rag-plugin)
- Dev guide → [`CLAUDE_CODE_PLUGIN_DEVELOPMENT_GUIDE.md`](https://github.com/taqat-techno/plugins/blob/main/CLAUDE_CODE_PLUGIN_DEVELOPMENT_GUIDE.md)
- Contributing → [`CONTRIBUTING.md`](https://github.com/taqat-techno/plugins/blob/main/CONTRIBUTING.md)

## Related

- [Add a New Command](Development-SOPs-Commands-Add-a-New-Command) — for extending ragtools' CLI from inside the ragtools repo.
- [Add a New HTTP Endpoint](Development-SOPs-HTTP-API-Add-a-New-Endpoint) — for adding a new route to the ragtools service.
- [Add a New Slash Command](Development-SOPs-Skills-Add-a-New-Slash-Command) — for adding a `/` skill shipped inside the ragtools repo.
- [Reference: MCP Tools](Reference-MCP-Tools) — what ragtools exposes to MCP clients today.
- [Q-5 resolution](Development-SOPs-Documentation-Open-Questions) — records why the plugin model landed on external Claude Code plugins rather than an in-process loader.

## Change log

| Date | Version | Change |
|---|---|---|
| 2026-04-19 | 2.5.1 | Rewritten from a stub. Plugin system is now active — documents the Claude Code plugin + marketplace model, with `rag-plugin` v0.6.0 as the reference implementation. Resolves Q-5. |
| 2026-04-18 | 2.4.2 | Initial stub; plugin system "not implemented" (blocked on Q-5). |
