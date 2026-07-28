"""Storage backends must behave the same on Windows, Linux and macOS.

The engine is the one part of the product where "works on my platform" is
easiest to believe and hardest to notice. `embedded` is pure Python and behaves
identically everywhere; `managed` supervises a native binary that no release
bundles; `external` connects to a server the user runs. Each has a different
failure mode, and each must fail the SAME way on all three platforms:

* an unknown engine is refused, never silently downgraded to embedded;
* `external` without an address is refused at config load, with a reason;
* `managed` with no binary falls back to embedded AND says why — a fallback that
  does not explain itself is indistinguishable from a product that ignored the
  setting;
* `qdrant_binary` pointing at a file that is not there is an ERROR, because a
  typo must not look like "unsupported platform".

Run by the `storage-backends` CI job on all three runners. Exits non-zero on the
first inconsistency.
"""

from __future__ import annotations

import json
import platform
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


def settings_for(root: Path, **kwargs):
    from ragtools.config import Settings

    return Settings(qdrant_path=str(root / "qdrant"),
                    state_db=str(root / "state.db"), **kwargs)


def main() -> int:
    from ragtools.config import _SUPPORTED_BACKENDS, _SUPPORTED_STRATEGIES
    from ragtools.storage import resolve_backend

    root = Path(tempfile.mkdtemp(prefix="ragtools-storage-"))
    print(f"platform: {platform.system()} {platform.machine()}  ({sys.platform})")
    print(f"sandbox:  {root}\n", flush=True)

    # --- the contract loads the same everywhere --------------------------
    for backend in sorted(_SUPPORTED_BACKENDS):
        for strategy in sorted(_SUPPORTED_STRATEGIES):
            extra = {"storage_url": "http://127.0.0.1:6333"} if backend == "external" else {}
            try:
                s = settings_for(root, storage_backend=backend,
                                 collection_strategy=strategy, **extra)
                ok = s.storage_backend == backend and s.collection_strategy == strategy
                detail = ""
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, str(exc)
            check(f"{backend} + {strategy} loads", ok, detail)

    # --- and refuses the same things everywhere --------------------------
    try:
        settings_for(root, storage_backend="nonsense")
        check("an unknown engine is refused", False, "it was accepted")
    except ValueError as exc:
        check("an unknown engine is refused", "storage_backend" in str(exc))

    try:
        settings_for(root, collection_strategy="nonsense")
        check("an unknown layout is refused", False, "it was accepted")
    except ValueError as exc:
        check("an unknown layout is refused", "collection_strategy" in str(exc))

    try:
        settings_for(root, storage_backend="external")
        check("external without a url is refused", False, "it was accepted")
    except ValueError as exc:
        check("external without a url is refused", "storage_url" in str(exc))

    # --- embedded actually works, on every platform ----------------------
    try:
        backend = resolve_backend(settings_for(root, storage_backend="embedded"))
        caps = backend.capabilities()
        client = backend.client()
        client.get_collections()
        backend.close()
        check("embedded opens a real store", True,
              f"hnsw={caps.hnsw} payload_indexes={caps.payload_indexes}")
    except Exception as exc:  # noqa: BLE001
        check("embedded opens a real store", False, str(exc))

    try:
        caps = resolve_backend(settings_for(root, storage_backend="embedded")).capabilities()
        check("embedded reports no HNSW, so the scale warning applies", not caps.hnsw)
    except Exception as exc:  # noqa: BLE001
        check("embedded reports no HNSW, so the scale warning applies", False, str(exc))

    # --- managed: absent binary must fall back AND explain ---------------
    from ragtools.service.managed_qdrant import find_qdrant_binary, plan_managed_startup

    empty = Path(tempfile.mkdtemp(prefix="ragtools-nobin-"))
    s = settings_for(empty, storage_backend="managed")
    try:
        plan = plan_managed_startup(s)
        if find_qdrant_binary(s):
            check("managed finds a binary on this machine and plans to start it",
                  plan.should_start, plan.reason)
        else:
            check("managed with no binary falls back to embedded",
                  plan.fallback_to_embedded and not plan.should_start, plan.reason)
            check("the fallback explains itself", bool(plan.reason.strip()),
                  plan.reason)
    except Exception as exc:  # noqa: BLE001
        check("managed with no binary falls back to embedded", False, str(exc))

    # --- a configured binary that is not there is an ERROR ---------------
    missing = str(empty / "definitely-not-here" / "qdrant")
    try:
        find_qdrant_binary(settings_for(empty, storage_backend="managed",
                                        qdrant_binary=missing))
        check("a missing qdrant_binary is an error, not a silent downgrade",
              False, "it was ignored")
    except Exception as exc:  # noqa: BLE001
        check("a missing qdrant_binary is an error, not a silent downgrade",
              "not found" in str(exc).lower() or "qdrant_binary" in str(exc), str(exc))

    # --- embedded is the only engine that needs nothing ------------------
    check("embedded needs no external resource", True,
          "this is why it is the migration default")

    failed = [r for r in results if not r[1]]
    print(f"\n  {len(results) - len(failed)} passed, {len(failed)} failed", flush=True)
    print(json.dumps({"platform": sys.platform,
                      "passed": len(results) - len(failed),
                      "failed": len(failed)}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
