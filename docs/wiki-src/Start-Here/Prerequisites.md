# Prerequisites

| | |
|---|---|
| **Owner** | TBD (proposed: ops lead) |
| **Last validated against version** | 2.4.2 |
| **Last reviewed** | 2026-04-18 |
| **Status** | Draft |

## Supported platforms

| Platform | Status | Notes |
|---|---|---|
| Windows 11 | Supported | Primary target. Installer available. |
| macOS (arm64) | Supported | CI builds run on macOS arm64. |
| Linux | Not tested | Source install likely works; not part of the release pipeline. |

## Runtime requirements

| Requirement | Minimum | Notes |
|---|---|---|
| Python | 3.10+ (3.12 recommended) | Dev install only. `pyproject.toml` requires `>=3.10`. Packaged installer bundles its own interpreter. |
| Disk | ~1 GB free | Installer is ~500 MB; encoder + indexed data grow over time. |
| RAM | ~100 MB headroom | Service holds the SentenceTransformer encoder in memory while running. |
| Network | First run only | Initial encoder download from Hugging Face (~80 MB) unless pre-bundled by the installer. |

## What is not required

- Docker, containers, or virtualization. Reg is local-first by design — see [Architecture Decisions](Standards-and-Governance-Architecture-Decisions).
- Cloud accounts or API keys for indexing/search.
- A running database service — Qdrant runs in embedded mode inside the Reg process.
- A build toolchain on the target machine when using the packaged installer.

## What you will interact with

| Path | Purpose | Dev | Installed |
|---|---|---|---|
| `./data/qdrant/` | Qdrant collection files | Yes | - |
| `%LOCALAPPDATA%\RAGTools\` | Data dir and config | - | Yes |
| `127.0.0.1:21420` (installed) / `21421` (dev) | Service HTTP + admin panel | Both | Both |

See [File Layout](Reference-File-Layout) for the full inventory.

## Next steps

- Source install: [Quick Install (Dev)](Start-Here-Quick-Install-Dev).
- Windows installer: [Quick Install (Packaged)](Start-Here-Quick-Install-Packaged).
