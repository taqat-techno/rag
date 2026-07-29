"""Release gate: the shipped client and the shipped server must be compatible.

v3.2.0 shipped ``qdrant-client 1.18.0`` against ``qdrant 1.15.5`` — three minor
versions apart, outside the client's own stated support window, warning on every
startup. Nobody did anything wrong at the keyboard. The two halves of the pair
were pinned by *different mechanisms*:

* the server by a source constant, ``PINNED_QDRANT_VERSION``;
* the client by an unbounded dependency range, resolved fresh at build time.

So a warm developer machine and a cold CI runner resolved different clients, and
nothing anywhere compared the result against the engine actually being shipped.
That is not a mistake to be more careful about next time; it is a missing gate.

This is the gate. It runs on Windows, Linux and macOS, before packaging, and it
fails the build rather than emitting a warning nobody reads.

    python scripts/check_qdrant_compat.py

The rule is the client's own, stated in the warning it emits: major versions must
match, and the minor difference must not exceed one.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: How far apart the minors may be. The client's rule, not ours.
MAX_MINOR_DELTA = 1


def _fail(message: str) -> int:
    print(f"FAIL  {message}")
    return 1


def _parse(version: str) -> tuple[int, int, int]:
    parts = re.findall(r"\d+", version)
    if len(parts) < 2:
        raise ValueError(f"not a version: {version!r}")
    major, minor = int(parts[0]), int(parts[1])
    patch = int(parts[2]) if len(parts) > 2 else 0
    return major, minor, patch


def pinned_server_version() -> str:
    """Read the engine pin from source, the way the product does."""
    text = (ROOT / "src" / "ragtools" / "storage_managed.py").read_text(encoding="utf-8")
    match = re.search(r'PINNED_QDRANT_VERSION\s*=\s*"([^"]+)"', text)
    if not match:
        raise RuntimeError("PINNED_QDRANT_VERSION not found in storage_managed.py")
    return match.group(1)


def installed_client_version() -> str:
    """The client that is ACTUALLY installed — not what the range permits.

    Reading the range would test our intentions. Reading the resolved package
    tests what is about to be packaged, which is the thing that shipped wrong.
    """
    from importlib.metadata import version

    return version("qdrant-client")


def declared_client_range() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'"(qdrant-client[^"]*)"', text)
    if not match:
        raise RuntimeError("qdrant-client is not declared in pyproject.toml")
    return match.group(1)


def main() -> int:
    server = pinned_server_version()
    client = installed_client_version()
    declared = declared_client_range()

    print(f"server (PINNED_QDRANT_VERSION) : {server}")
    print(f"client (installed)             : {client}")
    print(f"client (declared)              : {declared}")

    failures = 0

    # 1. An unbounded range is the defect itself, independent of what it happens
    #    to resolve to today. A build that is green by luck is not green.
    if "<" not in declared:
        failures += _fail(
            f"the qdrant-client requirement {declared!r} has no upper bound. "
            f"That is how v3.2.0 shipped a three-minor mismatch: a fresh resolve "
            f"picked a client nobody chose, and no gate compared it to the "
            f"engine.")

    # 2. The actual pair.
    s_major, s_minor, _ = _parse(server)
    c_major, c_minor, _ = _parse(client)

    if s_major != c_major:
        failures += _fail(
            f"major mismatch: client {client} vs server {server}. The client "
            f"requires matching majors.")
    elif abs(s_minor - c_minor) > MAX_MINOR_DELTA:
        failures += _fail(
            f"minor gap of {abs(s_minor - c_minor)} between client {client} and "
            f"server {server}; the client supports a gap of at most "
            f"{MAX_MINOR_DELTA}. This exact pair ({client} / {server}) is what "
            f"v3.2.0 shipped.")

    if failures:
        print("\nThe pair this build would ship is not supported by the client "
              "itself. Adjust the qdrant-client bound in pyproject.toml or the "
              "PINNED_QDRANT_VERSION constant so both halves move together.")
        return 1

    print(f"\nOK  client {client} and server {server} are within the supported "
          f"window (majors match, minor gap <= {MAX_MINOR_DELTA}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
