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

# Hidden imports that PyInstaller misses
hiddenimports = [
    # Sentence Transformers + PyTorch
    "sentence_transformers",
    "sentence_transformers.models",
    "torch",
    "torch.nn",
    "torch.nn.functional",
    "transformers",
    "tokenizers",
    "huggingface_hub",
    # Qdrant
    "qdrant_client",
    "qdrant_client.local",
    "portalocker",
    # FastAPI + Uvicorn
    "fastapi",
    "uvicorn",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "starlette",
    "httptools",
    "websockets",
    # Other
    "pydantic",
    "pydantic_settings",
    "pathspec",
    "frontmatter",
    "yaml",
    "rich",
    "typer",
    "httpx",
    "jinja2",
    "mcp",
    "watchfiles",
    "tomli_w",
]

a = Analysis(
    [os.path.join(SRC_DIR, "ragtools", "cli.py")],
    pathex=[SRC_DIR],
    binaries=[],
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
