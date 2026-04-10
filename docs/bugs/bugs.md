###RAGTools Uninstall Residue Report

  Date: 2026-04-10
  App: RAGTools v2.0.0 (Local Markdown RAG system for Claude CLI)
  Uninstall method: Windows Control Panel > Uninstall

  ---
  What the uninstaller left behind

  ┌───────────────┬──────────────────────────────────┬──────────────────────────────────────────────────────────────┐
  │    Residue    │             Location             │                           Contents                           │
  ├───────────────┼──────────────────────────────────┼──────────────────────────────────────────────────────────────┤
  │ Install       │ AppData\Local\Programs\RAGTools\ │ _internal/, _internal.bak/, model_cache/                     │
  │ directory     │                                  │ (~sentence-transformers weights), rag.exe.bak (69MB)         │
  ├───────────────┼──────────────────────────────────┼──────────────────────────────────────────────────────────────┤
  │ Data          │ AppData\Local\RAGTools\          │ Qdrant vector database (data/qdrant/), service logs          │
  │ directory     │                                  │                                                              │
  ├───────────────┼──────────────────────────────────┼──────────────────────────────────────────────────────────────┤
  │ Pip package   │ Python 3.12 site-packages        │ ragtools 2.0.0 editable install + all metadata               │
  └───────────────┴──────────────────────────────────┴──────────────────────────────────────────────────────────────┘

  What was properly removed

  - The main rag.exe executable
  - Windows registry entries (Control Panel no longer lists it)
  - rag-mcp was no longer on PATH

  Root cause analysis

  The uninstaller has three gaps:

  1. Backup files ignored - .bak files (rag.exe.bak, _internal.bak/) were not included in the uninstall manifest. The
  uninstaller likely only tracks the original filenames, not backups created during updates.
  2. User data not cleaned - The separate AppData\Local\RAGTools\ data directory (Qdrant DB, logs, model cache) was
  never registered with the uninstaller. This is a common pattern where apps split "program files" from "user data," but
   the uninstaller only handles the former.
  3. Pip package not uninstalled - RAGTools was also installed as a Python pip package (editable install from
  C:\MY-WorkSpace\rag). The Windows uninstaller has no awareness of pip and made no attempt to run pip uninstall
  ragtools.

  Impact

  - ~70MB+ of dead files on disk (model weights, backup exe, vector DB)
  - Python environment polluted with orphaned package and its 16 dependencies
  - Potential port conflict if anything tried to bind 127.0.0.1:21420 (the old service address)

  Expected behavior

  A well-behaved uninstaller should:
  - Remove all files in its install directory, including .bak variants
  - Prompt the user to optionally delete user data (AppData\Local\RAGTools\)
  - Unregister any pip packages it installed, or at minimum warn the user
  - Clean up model cache files

  Resolution

  All residue was manually removed on 2026-04-10 via:
  - pip uninstall ragtools -y
  - Deleted both AppData directories



###RAG MCP Server — Issue & Fix Report

  Issue

  The RAGTools MCP server was not working / not configured in Claude Code.

  Root Cause

  The original configuration used the wrong command:
  {
    "mcpServers": {
      "ragtools": {
        "command": "python",
        "args": ["-m", "ragtools.integration.mcp_server"]
      }
    }
  }
  This failed because:
  1. The ragtools Python package is not installed via pip — it doesn't exist on PyPI
  2. RAGTools was installed as a standalone desktop application (RAGTools-Setup-2.1.0.exe), not a Python module
  3. The actual executable is at C:\Users\DELL\AppData\Local\Programs\RAGTools\rag.exe

  Fix Applied

  Added the correct MCP server config using the installed .exe:
  claude mcp add ragtools -- "C:/Users/DELL/AppData/Local/Programs/RAGTools/rag.exe" serve

  Verification

  ┌─────────────────┬────────────────────────────────────────────────────┐
  │      Check      │                       Result                       │
  ├─────────────────┼────────────────────────────────────────────────────┤
  │ MCP Connection  │ Connected (proxy mode)                             │
  ├─────────────────┼────────────────────────────────────────────────────┤
  │ Collection      │ markdown_kb                                        │
  ├─────────────────┼────────────────────────────────────────────────────┤
  │ Indexed Files   │ 1 file, 1 chunk                                    │
  ├─────────────────┼────────────────────────────────────────────────────┤
  │ Projects        │ 1                                                  │
  ├─────────────────┼────────────────────────────────────────────────────┤
  │ Tools Available │ index_status, list_projects, search_knowledge_base │
  └─────────────────┴────────────────────────────────────────────────────┘


### RAG installation trust issue
Check the image in this path C:\MY-WorkSpace\rag\docs\bugs\image.png


### Mantec map slowing
The Mantec map is too taking too long time if I have a lot of data


### auto reg start not working
Service is not running without the command line Reg service start if I have restarted the device oi did restart the device Try to open the admin it's not opening the service is not running file watch is not working and I need to open the Terminal to call the start command rag service start


### The rag start icon
The rag start icon is too too Bush as a user experience Whether enhance it as a desktop application with just a button to start the service if the service is not running or stop the service if I don't keep it running with a symbol UI
C:\MY-WorkSpace\rag\docs\bugs\image copy.png
