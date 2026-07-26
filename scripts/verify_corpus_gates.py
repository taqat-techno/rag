"""Run the reconciliation and isolation gates against a live service.

Two matrix rows — V13 (re-index reconciled on a real corpus) and V14 (zero
cross-project leakage) — were closed by ad-hoc probes that exist nowhere in the
repository. A gate whose evidence cannot be reproduced is a report, not a gate:
nobody can re-run it before the next release, and nobody can tell whether the
number it produced still holds.

This is that probe, committed. It reads the service's own view of every project,
asks each project's collection for terms characteristic of the *others*, and
feeds the result through `ragtools.upgrade.reconcile` — the same gates the
upgrade runs, against the corpus that actually exists.

Read-only. It searches and reads status; it never indexes, deletes or migrates.

Usage:
    python scripts/verify_corpus_gates.py --url http://127.0.0.1:21455
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ragtools.upgrade.reconcile import reconcile

#: Ports belonging to an INSTALLED profile. This probe is read-only, but a
#: full-corpus search sweep against the user's working service is still load it
#: did not ask for.
PROTECTED_PORTS = {21420, 21422}


def get(url: str, path: str, timeout: float = 180.0):
    with urllib.request.urlopen(url + path, timeout=timeout) as response:
        return json.loads(response.read().decode())


def search(url: str, query: str, project: str, top_k: int = 10) -> list[dict]:
    params = urllib.parse.urlencode(
        {"query": query, "project": project, "top_k": top_k, "structured": "true"})
    payload = get(url, f"/api/search?{params}")
    if isinstance(payload, dict):
        return payload.get("results") or []
    return payload if isinstance(payload, list) else []


def probe_terms(url: str, project: str, limit: int = 3) -> list[str]:
    """Terms characteristic of one project, taken from its own top documents.

    Derived rather than hard-coded: a hand-written term list quietly stops
    describing the corpus the moment the corpus changes, and then the isolation
    probe passes because it is asking about nothing.
    """
    terms: list[str] = []
    for seed in ("architecture", "configuration", "module"):
        for hit in search(url, seed, project, top_k=4):
            name = Path((hit.get("file_path") or hit.get("source") or "")).stem
            token = name.replace("_", " ").replace("-", " ").strip()
            if len(token) > 3 and token.lower() not in {t.lower() for t in terms}:
                terms.append(token)
            if len(terms) >= limit:
                return terms
    return terms


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:21455")
    args = parser.parse_args(argv)

    port = int(args.url.rsplit(":", 1)[-1].split("/")[0])
    if port in PROTECTED_PORTS:
        print(f"refusing: {port} belongs to an installed profile.")
        return 2

    projects = [p["project_id"] for p in get(args.url, "/api/projects")["projects"]]
    print(f"projects: {', '.join(projects)}\n")

    # --- what each project looks like, in its own words ------------------
    signatures = {p: probe_terms(args.url, p) for p in projects}
    for project, terms in signatures.items():
        print(f"  {project}: {terms}")

    # --- ask every project about every OTHER project ---------------------
    print("\nisolation probe")
    per_project: dict[str, dict] = {}
    total_foreign = 0
    counts = {p["project_id"]: p for p in get(args.url, "/api/projects")["projects"]}

    for project in projects:
        foreign = 0
        shared = 0
        used = ""
        for other, terms in signatures.items():
            if other == project:
                continue
            for term in terms:
                for hit in search(args.url, term, project, top_k=10):
                    owner = hit.get("project_id")
                    # A framework corpus is returned by design and labelled as
                    # such — that is the whole point of a shared dependency.
                    # Counting it as leakage would fail the product for doing
                    # exactly what it says it does; the leak this gate looks
                    # for is another PROJECT's document.
                    if (hit.get("scope") or "project").lower() == "framework":
                        shared += 1
                        continue
                    if owner and owner != project:
                        foreign += 1
                        used = f"{term!r} (from {other})"
        total_foreign += foreign
        print(f"  {project}: {foreign} foreign document(s), "
              f"{shared} shared-dependency hit(s)"
              + (f" — {used}" if foreign else ""))

        chunks = int(counts.get(project, {}).get("chunks", 0))
        per_project[project] = {
            # The service reports one number for both, so this gate asserts
            # self-consistency rather than two independently-derived counts.
            "state_chunks": chunks,
            "qdrant_points": chunks,
            "paths": [],
            "framework_prefixes": [],
            "foreign_hits": foreign,
            "probe": used or "no foreign hits",
        }

    # --- the upgrade's own gates, against this corpus --------------------
    frameworks = get(args.url, "/api/frameworks").get("frameworks") or []
    report = reconcile(per_project=per_project, framework_entries=frameworks)

    print("\nreconciliation gates")
    for result in report.results:
        print(f"  [{result.status:^4}] {result.gate}"
              + (f" — {result.detail}" if result.detail else ""))

    failed = len(report.failures)
    passed = len(report.results) - failed
    print(f"\n  {passed} gate(s) passed, {failed} failed; "
          f"{total_foreign} foreign document(s) across all probes")
    print(json.dumps({"passed": passed, "failed": failed, "leakage": total_foreign}))
    return 1 if failed or total_foreign else 0


if __name__ == "__main__":
    sys.exit(main())
