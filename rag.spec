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
    ]
)

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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rag",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # Don't compress — causes AV false positives
    icon=os.path.join(PROJECT_ROOT, "app.ico"),
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="rag",
)
