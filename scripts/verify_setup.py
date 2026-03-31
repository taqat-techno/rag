"""Verify Stage 0 setup is complete.

Run: python scripts/verify_setup.py
"""

import sys


def main() -> int:
    errors = []

    # 1. Check ragtools import
    print("1. Checking ragtools import...", end=" ")
    try:
        from ragtools import __version__

        print(f"OK (v{__version__})")
    except ImportError as e:
        print(f"FAIL: {e}")
        errors.append("ragtools not importable")

    # 2. Check config
    print("2. Checking config...", end=" ")
    try:
        from ragtools.config import Settings

        settings = Settings()
        print(f"OK (qdrant_path={settings.qdrant_path})")
    except Exception as e:
        print(f"FAIL: {e}")
        errors.append("config broken")

    # 3. Check Qdrant local mode
    print("3. Checking Qdrant local mode...", end=" ")
    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(":memory:")
        collections = client.get_collections()
        print(f"OK ({len(collections.collections)} collections)")
    except Exception as e:
        print(f"FAIL: {e}")
        errors.append("qdrant-client broken")

    # 4. Check sentence-transformers
    print("4. Checking sentence-transformers...", end=" ")
    try:
        import sentence_transformers

        print(f"OK (v{sentence_transformers.__version__})")
    except ImportError as e:
        print(f"FAIL: {e}")
        errors.append("sentence-transformers not installed")

    # 5. Check models
    print("5. Checking models...", end=" ")
    try:
        from ragtools.models import Chunk, FileRecord, SearchResult

        print("OK")
    except ImportError as e:
        print(f"FAIL: {e}")
        errors.append("models broken")

    # 6. Check CLI entry point
    print("6. Checking CLI entry point...", end=" ")
    try:
        from ragtools.cli import app

        print("OK")
    except ImportError as e:
        print(f"FAIL: {e}")
        errors.append("CLI broken")

    # Summary
    print()
    if errors:
        print(f"SETUP INCOMPLETE — {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err}")
        return 1
    else:
        print("ALL CHECKS PASSED — Stage 0 complete!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
