# Quick Install (Dev)

| | |
|---|---|
| **Owner** | TBD (proposed: eng lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

Source install for development or headless macOS/Linux use. For production Windows, use [Quick Install (Packaged)](Start-Here-Quick-Install-Packaged) instead.

## Steps

1. **Clone:**
   ```
   git clone https://github.com/taqat-techno/rag.git
   cd rag
   ```

2. **Create a virtual environment (Python 3.10+; 3.12 recommended):**
   ```
   python -m venv .venv
   ```

3. **Activate:**
   - Windows (cmd): `.venv\Scripts\activate`
   - Windows (PowerShell): `.venv\Scripts\Activate.ps1`
   - macOS / Linux: `source .venv/bin/activate`

4. **Install in editable mode with dev extras:**
   ```
   pip install -e ".[dev]"
   ```
   This registers the `rag` and `rag-mcp` entry points.

5. **Verify:**
   ```
   rag version
   rag doctor
   ```

6. **Optional — start the service:**
   ```
   rag service start
   ```
   Admin panel at `http://127.0.0.1:21421` (dev default; installed mode uses 21420).

## What changed on disk

- `.venv/` — your Python environment (already git-ignored).
- `./data/` — created on first indexing. Holds Qdrant files and `index_state.db`.
- Entry points `rag`, `rag-mcp` — available on PATH while the venv is active.

## Next

- [Add a Project](Operational-SOPs-Projects-Add-a-Project).
- [Configuration Precedence](Operational-SOPs-Configuration-Configuration-Precedence) if `./ragtools.toml` isn't taking effect.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `rag` not found after install | Activate the venv first. |
| Service port 21421 already in use (dev default) | See [Port 21420 In Use](Runbooks-Port-21420-In-Use) — same resolution, adjust for dev port. |
| First `rag search` hangs 5-10 s | Expected — encoder cold-start in direct mode. See [CLI Dual-Mode](Architecture-CLI-Dual-Mode). |
