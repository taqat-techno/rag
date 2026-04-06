"""Build script for RAG Tools packaging.

Usage:
  python scripts/build.py              # Full build (PyInstaller + model)
  python scripts/build.py --no-model   # Skip model download (faster for testing)
  python scripts/build.py --installer  # Also build Inno Setup installer

Requirements:
  pip install ".[build]"   # Installs PyInstaller
  Inno Setup 6+ (optional, for --installer flag)
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DIST_DIR = PROJECT_ROOT / "dist" / "rag"
BUILD_DIR = PROJECT_ROOT / "build"
MODEL_CACHE_DIR = BUILD_DIR / "model_cache"
SPEC_FILE = PROJECT_ROOT / "rag.spec"


def download_model():
    """Pre-download the SentenceTransformer model for bundling."""
    print("Downloading embedding model...")
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Use sentence-transformers to download to our cache location
    os.environ["SENTENCE_TRANSFORMERS_HOME"] = str(MODEL_CACHE_DIR)
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2", cache_folder=str(MODEL_CACHE_DIR))
    dim = model.get_sentence_embedding_dimension()
    print(f"Model downloaded: all-MiniLM-L6-v2 (dim={dim})")
    print(f"Cache location: {MODEL_CACHE_DIR}")
    del model  # Free memory


def run_pyinstaller():
    """Run PyInstaller with the spec file."""
    print("Running PyInstaller...")
    cmd = [sys.executable, "-m", "PyInstaller", str(SPEC_FILE), "--noconfirm", "--clean"]
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        print("PyInstaller failed!")
        sys.exit(1)
    print(f"Bundle created: {DIST_DIR}")


def copy_model_to_dist():
    """Copy the model cache into the distribution folder."""
    if not MODEL_CACHE_DIR.exists():
        print("Warning: Model cache not found, skipping model bundling")
        return

    dest = DIST_DIR / "model_cache"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(MODEL_CACHE_DIR, dest)
    print(f"Model copied to: {dest}")


def verify_build():
    """Quick verification that the bundle works."""
    rag_exe = DIST_DIR / "rag.exe"
    if not rag_exe.exists():
        print(f"ERROR: {rag_exe} not found!")
        sys.exit(1)

    print("Verifying build...")
    result = subprocess.run([str(rag_exe), "version"], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"Build verified: {result.stdout.strip()}")
    else:
        print(f"WARNING: 'rag version' failed: {result.stderr}")
        print("The bundle may have issues — test manually.")


def run_inno_setup():
    """Compile the Inno Setup installer."""
    iss_file = PROJECT_ROOT / "installer.iss"

    # Find Inno Setup compiler
    iscc_paths = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        "iscc",  # If in PATH
    ]

    iscc = None
    for p in iscc_paths:
        if Path(p).exists() or shutil.which(p):
            iscc = p
            break

    if iscc is None:
        print("WARNING: Inno Setup not found. Skipping installer creation.")
        print("Install Inno Setup 6 from https://jrsoftware.org/isinfo.php")
        return

    print(f"Running Inno Setup: {iscc}")
    result = subprocess.run([iscc, str(iss_file)], cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        print("Inno Setup compilation failed!")
        sys.exit(1)

    installer = PROJECT_ROOT / "dist" / f"RAGTools-Setup-0.1.0.exe"
    if installer.exists():
        size_mb = installer.stat().st_size / 1024 / 1024
        print(f"Installer created: {installer} ({size_mb:.1f} MB)")


def main():
    parser = argparse.ArgumentParser(description="Build RAG Tools")
    parser.add_argument("--no-model", action="store_true", help="Skip model download")
    parser.add_argument("--installer", action="store_true", help="Also build Inno Setup installer")
    args = parser.parse_args()

    print(f"Project root: {PROJECT_ROOT}")
    print(f"Python: {sys.executable}")
    print()

    # Step 1: Download model
    if not args.no_model:
        download_model()
    else:
        print("Skipping model download (--no-model)")

    # Step 2: PyInstaller
    run_pyinstaller()

    # Step 3: Copy model to dist
    if not args.no_model:
        copy_model_to_dist()

    # Step 4: Verify
    verify_build()

    # Step 5: Inno Setup (optional)
    if args.installer:
        run_inno_setup()

    print("\nBuild complete!")
    print(f"  Bundle: {DIST_DIR}")
    if args.installer:
        print(f"  Installer: dist/RAGTools-Setup-0.1.0.exe")


if __name__ == "__main__":
    main()
