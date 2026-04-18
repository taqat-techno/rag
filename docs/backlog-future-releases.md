# Future Release Backlog

Items explicitly deferred from the current release. Do not forget these.

**Last updated:** 2026-04-18 (post v2.5.1 — Linux packaging shipped)

---

## Semantic Map Performance (Next Version)

**Problem:** The semantic map becomes slow with 50+ indexed files. PCA computation and canvas rendering are the bottlenecks.

**Planned improvements:**
- **Stable cache** — Reduce cache invalidation frequency. Currently invalidates on any index change. Should only invalidate when file-level aggregations actually change.
- **Precomputed map data** — Compute PCA projections during indexing, not on first page load. Store alongside the index.
- **Reduced redraw cost** — Canvas renders all points with glow effects on every mouse event. Use requestAnimationFrame batching and skip glow for non-hovered points.
- **Light rendering mode** — For 200+ files, switch to a simpler dot renderer without glow/shadow effects.
- **Large dataset guardrails** — If 500+ files, show a sampling of representative points instead of all points. Downsample by cluster centroid.
- **ECharts 3D** — ECharts GL already handles large datasets better than Canvas 2D. Consider making 3D the default for large collections.

**Files likely affected:**
- `src/ragtools/service/map_data.py` — PCA computation, caching
- `src/ragtools/service/static/map.js` — Canvas rendering
- `src/ragtools/service/owner.py` — Cache invalidation

---

## Code Signing (Future)

**Problem:** Windows SmartScreen shows "Unknown publisher" warning on the installer. Scares users.

**Solution:** Purchase a code signing certificate and sign both `rag.exe` and the installer.

**Options:**
- Standard EV code signing certificate (~$300/year) — immediate SmartScreen trust
- Standard OV certificate (~$70-200/year) — builds trust over time with download volume
- Free via SignPath.io (for open source) — if the project qualifies

**Process:**
1. Obtain certificate from a trusted CA (DigiCert, Sectigo, SSL.com)
2. Add `signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 /a rag.exe` to build.py
3. Sign the installer after Inno Setup build
4. Verify SmartScreen no longer warns

**Files affected:**
- `scripts/build.py` — Add signing step
- CI/CD pipeline — Store certificate securely

---

## Qdrant Server Mode — Opt-in (Larger Collections)

**Problem:** Qdrant local mode has a ~20,000-point soft ceiling. Beyond that, writes slow down and queries drift. Mahmoud's field report (Apr 2026) hit 27k points on a single knowledge base.

**Current mitigation (v2.5.0):** `compute_scale_warning()` surfaces a warning in `rag doctor` + admin UI + desktop toast when points exceed 18k / 20k.

**Planned for a future release:**
- Opt-in `RAG_QDRANT_MODE=server` that runs `qdrant` as a subprocess and connects to it via localhost
- Still no Docker, still no cloud — the binary ships with the installer
- One-click migration from local mode: drop collection → start server → re-index
- Retain local mode as the default (keeps install simple for users with smaller KBs)

**Why deferred:** Complex, affects install size (+80MB), and only benefits users above the 20k ceiling.

**Files likely affected:**
- `src/ragtools/config.py` — `qdrant_mode: Literal["local","server"]`
- `src/ragtools/service/owner.py` — `get_qdrant_client()` branch on mode
- `scripts/build.py` — Bundle the `qdrant` binary
- Admin UI — Migration wizard

---

## Admin UI: Backup Management

**Problem:** `rag backup {list,create,restore,prune}` are CLI-only. Users who don't live in a terminal can't see or use their backups.

**Planned:**
- New admin-panel page "Backups"
- Table of snapshots: timestamp, trigger, size, note
- Buttons: Create now, Restore (with confirm modal), Delete
- Write endpoints already exist — this is UI-only work

**Files affected:**
- `src/ragtools/service/templates/backups.html` (new)
- `src/ragtools/service/pages.py` — route
- `src/ragtools/service/routes.py` — HTTP wrappers over `ragtools.backup`

**Estimate:** 2–3 hours.

---

## Tray Icon Auto-install on Packaged Launch

**Problem:** v2.5.0 ships the tray as an optional `[tray]` extra. Installed-app users who want it still have to run `rag tray install` manually. The login-task and watchdog both auto-register on packaged launch; the tray should follow the same pattern.

**Planned for v2.5.1 or v2.6.0:**
- In `run.py _post_startup`, if packaged + `[tray]` extra present, register the tray startup script (idempotent, same as login-task + watchdog today)
- Or: include the tray extra automatically in the Inno Setup installer

**Why deferred:** v2.5.0 already ships a LOT. Defer the auto-install so users can opt in first, then promote to default.

---

## Agent-Proposal Queue (for MCP Adds)

**Problem:** `add_project(path)` can't be an MCP tool because arbitrary-path writes from the agent are a foot-cannon. But manually running `rag project add` every time the agent suggests a folder is friction.

**Planned (idea, not committed):**
- New MCP tool `propose_project_add(path, reason)` — writes a suggestion to a queue, does NOT add the project
- Admin panel shows a "Pending proposals from the agent" card
- User reviews, approves or rejects with one click
- Gets the best of both worlds: agent can be helpful without bypassing the user

**Files likely affected:**
- `src/ragtools/integration/mcp_server.py` — new tool
- `src/ragtools/service/proposals.py` (new) — proposal queue storage
- Admin UI — proposals card
- `src/ragtools/service/templates/dashboard.html`

**Why deferred:** Needs user-research on whether this is actually valuable. For now, `rag project add-from-glob` covers the bulk-add use case cleanly.

---

## Phase 4 MCP Tools (Retrieval Quality)

Phase 1–3 delivered agent tooling for inspection, diagnostics, and project-scoped maintenance. Phase 4 is about **retrieval-quality introspection** — tools the agent could use to reason about why a search returned (or didn't return) something.

**Candidates (all read-only, all agent-useful for "why did my search miss this?"):**

| Tool | Purpose |
|---|---|
| `explain_chunks(file_path)` | Return the chunks a specific `.md` file produced. Debug "why isn't X in my search results?" |
| `find_similar_chunks(chunk_id, top_k)` | Raw vector-space neighbors. Debug clustering. |
| `project_clusters(project)` | Heading-based or HDBSCAN clusters. Map content structure. |

**Why deferred:** Phase 4 is speculative. Build after Phase 2/3 tools prove themselves in real workflows. The signal we'd look for: agents consistently struggling with "why didn't search find X?" questions.

---

## Linux Desktop Integration (Deferred from v2.5.1)

**Context:** v2.5.1 shipped Linux packaging (`RAGTools-{version}-linux-x86_64.tar.gz`, XDG data path). The following desktop-integration surfaces are deferred — source users can still run everything via CLI and the admin panel.

**Deferred items:**
- **System tray on Linux** — `pystray` backends on Linux are inconsistent across Wayland / GNOME / KDE / XFCE. Admin panel is the supported control surface on Linux.
- **Login-startup helper** — No `rag service install` equivalent on Linux yet. Options: systemd user unit (`~/.config/systemd/user/ragtools.service` + `systemctl --user enable`), or `.desktop` autostart file in `~/.config/autostart/`.
- **Watchdog task** — Windows Task Scheduler equivalent on Linux: a systemd user timer or cron entry. Same two-tier recovery, different mechanism.
- **Desktop notifications on Linux** — Currently only Windows (`winotify`) and macOS (`osascript`) backends. Linux would use `notify-send` (libnotify) with graceful fallback.
- **Packaging polish** — `.deb` / `.rpm` / AppImage / Flatpak. The tar.gz bundle covers "it works"; OS-native packages would give menu entries, icons, and proper uninstall. Deferred until demand materialises.

**Why deferred:** Ubuntu-usable via tar.gz + admin panel today. Systemd integration is well-scoped but adds test matrix weight; desktop-notify and packaging polish are post-validation concerns.

---

## LESSONS.md Items Worth Upstreaming

Many lessons accumulated in `~/.claude/LESSONS.md` during v2.5.0 development are genuinely useful for:
- Other RAG-project-adjacent work
- Other MCP-server projects
- Other Windows-on-Python packaging projects

Consider:
- Extracting the MCP-architecture lessons (split-server vs access control, pystray pitfalls, etc.) into a `docs/engineering-notes.md` for public readers
- Blog post: "Building a local-first MCP server the right way"
- Keep LESSONS.md internal; make public-facing lessons their own file
