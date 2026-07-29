# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for RAG Tools.

Builds a one-dir bundle with:
- rag.exe entry point
- All Python dependencies (torch, sentence-transformers, qdrant-client, etc.)
- Jinja2 templates and static assets
- Pre-downloaded embedding model

Usage:
  pyinstaller rag.spec

Output:
  dist/rag/rag.exe
"""

import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# Project root
PROJECT_ROOT = os.path.dirname(os.path.abspath(SPEC))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

# Data files to include
datas = [
    # Templates and static assets for admin panel
    (os.path.join(SRC_DIR, "ragtools", "service", "templates"), os.path.join("ragtools", "service", "templates")),
    (os.path.join(SRC_DIR, "ragtools", "service", "static"), os.path.join("ragtools", "service", "static")),
]

# Include pre-downloaded model if available
MODEL_CACHE = os.path.join(PROJECT_ROOT, "build", "model_cache")
if os.path.exists(MODEL_CACHE):
    datas.append((MODEL_CACHE, "model_cache"))

binaries = []

# Collect all for heavy packages with dynamic imports
for pkg in ['sentence_transformers', 'transformers', 'torch']:
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries

# Hidden imports that PyInstaller misses
hiddenimports = (
    collect_submodules('sentence_transformers')
    + collect_submodules('transformers')
    + collect_submodules('uvicorn')
    + collect_submodules('starlette')
    + collect_submodules('fastapi')
    + [
        # Qdrant
        "qdrant_client",
        "qdrant_client.local",
        "qdrant_client.local.qdrant_local",
        "portalocker",
        # Named explicitly, not left to analysis. Five modules import psutil
        # inside `try:` blocks, and two of the four managed-engine ownership
        # proofs — "the listener is our pid", "its image is our binary" —
        # silently do not exist without it. It was missing from the v3.2.0
        # bundle, so whether that boundary held depended on what happened to be
        # in the build venv. A security property must not be decided by luck.
        "psutil",
        # FastAPI + server extras
        "httptools",
        "websockets",
        "email.mime.multipart",
        "email.mime.text",
        "multiprocessing",
        # Other
        "pydantic",
        "pydantic_settings",
        "pydantic.deprecated",
        "pydantic.deprecated.decorator",
        "pathspec",
        "frontmatter",
        "yaml",
        "rich",
        "typer",
        "httpx",
        "jinja2",
        "mcp",
        "watchfiles",
        "watchfiles._rust_notify",
        "tomli_w",
        "sklearn",
        "sklearn.decomposition",
        "sklearn.decomposition._pca",
        # Tray (optional [tray] extra — pystray/Pillow). Listing them here
        # forces PyInstaller to bundle them even though tray.py imports
        # them lazily inside TrayApp.run(). Missing in v2.5.0..v2.5.3 →
        # bundled rag.exe always failed `import pystray` and the icon
        # never appeared. Don't remove without verifying tray.log on a
        # post-install Windows machine.
        "pystray",
        "pystray._win32",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
    ]
)

# Windows-only, and named explicitly for the same reason psutil is. `winotify`
# was declared ONLY in the optional `notifications` extra, which the release
# build never installed — so the packaged app raised `No module named
# 'winotify'` and every toast degraded to log-only. The crash notification for a
# dead service therefore never reached the desktop, on the one platform that has
# one. The extra is now installed by release.yml; this makes the bundling
# explicit rather than a side effect of analysis.
if sys.platform == "win32":
    hiddenimports += ["winotify"]

a = Analysis(
    [os.path.join(SRC_DIR, "ragtools", "cli.py")],
    pathex=[SRC_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude test frameworks and dev tools
        "pytest",
        "pytest_cov",
        "pytest_asyncio",
        "IPython",
        "jupyter",
        "notebook",
        "matplotlib",
        "tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

def _executable(name, *, console):
    """One bootloader over the shared Analysis.

    Both images run the SAME script and share one ``_internal``; the only
    difference is the PE subsystem, which is why this is a parameter and not a
    second Analysis. A second Analysis would double a 500 MB bundle to ship one
    differing byte-range in a 1 MB stub.
    """
    return EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,  # Don't compress — causes AV false positives
        icon=os.path.join(PROJECT_ROOT, "app.ico"),
        console=console,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )


#: The command-line image. Everything a human types resolves to this.
exe = _executable("rag", console=True)

#: The windowless image, and the reason it exists.
#
# Windows gives a console-subsystem process a console whenever *the OS* creates
# it, and Task Scheduler creates the autostart process itself. `CREATE_NO_WINDOW`
# does not help: it is a `subprocess.Popen` creation flag, so it governs
# processes ragtools spawns, and the task XML has no equivalent — there is no
# setting that suppresses a console for an `<Exec>` action. So v3.0.0 opened two
# terminal windows on the desktop at every login, one of them streaming uvicorn
# logs, and closing the window killed the service.
#
# This is the `python.exe` / `pythonw.exe` pattern: same code, same bundle, GUI
# subsystem, no console. `ragtools/_streams.py` handles the consequence — a
# windowed build starts with `sys.stdout is None`.
#
# Windows only, and deliberately so. `console=False` is not a no-op elsewhere:
# on macOS it selects windowed-app semantics, which is a different build shape
# for a binary nothing on that platform would launch. systemd and launchd
# redirect streams themselves and have no console to suppress, so there is
# nothing for a second image to fix. `background_executable()` resolves by
# checking for the file, so its absence here needs no counterpart there.
targets = [exe]
if sys.platform == "win32":
    targets.append(_executable("ragw", console=False))

coll = COLLECT(
    *targets,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="rag",
)
