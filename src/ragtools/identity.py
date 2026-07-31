"""Project & framework identity — the single source of truth (RAG v3, S6/S7).

Today three rules disagree (plan §11.1): the HTTP route validates
``^[a-z0-9][a-z0-9_-]*$``, the CLI *generates* ``[a-z0-9-]`` only, and the CLI
offline branch validates nothing. This module makes ONE validator and ONE
generator the shared contract, with the invariant that binds them:

    the generator's output is always accepted by the validator

(the validator's accepted set is a *superset* of what the generator emits, so
auto-generated ids never round-trip into a rejection).

Identity is also where collection names come from. A project's identity is an
immutable UUID — never its user-facing id (editable) or path (moves) — so the
collection name derives from the UUID and survives rename and relocation. A
framework corpus is stored once per build identity: when a build id (Odoo's
``repos_heads`` SHAs, the dedup key) is known it alone determines the
collection; its *absence* distinguishes a git checkout from a packaged build.

Pure and dependency-free. Enforcement (routes/CLI calling this instead of their
private copies) layers on top.

Plan: docs/planning/RAG_V3_LOCAL_DEV_IMPLEMENTATION_PLAN.md  (S6->G6, S7->G7)
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

MAX_PROJECT_ID_LEN = 64

#: The one accepted shape: lowercase alnum start, then alnum / hyphen / underscore.
#: A superset of the hyphen-only ids the generator produces, so every generated
#: id validates.
_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: Generator internals — everything outside ``[a-z0-9]`` becomes a hyphen, runs
#: collapse, edges strip. Deliberately hyphen-only (a strict subset of the
#: validator) so generated ids are canonical and stable.
_SLUG_INVALID = re.compile(r"[^a-z0-9]+")
_SLUG_COLLAPSE = re.compile(r"-+")


class InvalidProjectId(ValueError):
    """A project id violates the shared identity rules."""


def is_valid_project_id(pid: object) -> bool:
    """True iff ``pid`` satisfies the shared validator (no exception)."""
    try:
        validate_project_id(pid)  # type: ignore[arg-type]
        return True
    except InvalidProjectId:
        return False


def validate_project_id(pid: str, *, field: str = "Project ID") -> str:
    """Validate and return the normalized (trimmed) project id, or raise.

    The single validator every entry point (HTTP, CLI online, CLI offline) must
    call — replacing today's three-way divergence.
    """
    if pid is None or not str(pid).strip():
        raise InvalidProjectId(f"{field} is required")
    norm = str(pid).strip()
    if len(norm) > MAX_PROJECT_ID_LEN:
        raise InvalidProjectId(f"{field} must be {MAX_PROJECT_ID_LEN} characters or fewer")
    if not _PROJECT_ID_RE.match(norm):
        raise InvalidProjectId(
            f"{field} must be lowercase alphanumeric, starting with a letter or "
            "digit, using only hyphens and underscores"
        )
    return norm


def slugify_project_id(text: str) -> str:
    """Turn a folder basename or display name into a canonical project id.

    Lowercase, non-alphanumerics to hyphens, collapse runs, strip edges. Returns
    ``""`` when ``text`` has no alphanumerics — the caller decides what to do
    with an empty id (documented "caller decides" signal). Whatever non-empty id
    this returns is guaranteed to pass :func:`validate_project_id`.
    """
    lowered = str(text).lower()
    hyphenated = _SLUG_INVALID.sub("-", lowered)
    return _SLUG_COLLAPSE.sub("-", hyphenated).strip("-")


def _short_hash(*parts: str, length: int = 16) -> str:
    """Deterministic lowercase-hex digest of ``parts`` (no ambiguous joins)."""
    payload = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:length]


def project_collection_name(project_uuid: str) -> str:
    """Derive the collection name for a project from its immutable UUID.

    Depends on *nothing* but the UUID, so rename (display id changes) and path
    moves leave the collection untouched. Valid ``[a-z0-9_]`` token.
    """
    canon = str(project_uuid).strip().lower().replace("-", "")
    if not canon:
        raise InvalidProjectId("project UUID is required to derive a collection name")
    return f"proj_{canon}"


#: The SHAPE of a project collection name: the prefix, then a UUID with its
#: hyphens removed. A shape is not a claim — another installation's collections
#: match it exactly — so nothing may infer ownership from it. Ask
#: :func:`ragtools.registry.owns_collection`, which reads the rows.
_PROJECT_COLLECTION_RE = re.compile(r"^proj_([0-9a-f]{32})$")


def is_project_collection_name(name: object) -> bool:
    """True iff ``name`` has the project-collection shape. Says nothing about
    who created it."""
    return bool(_PROJECT_COLLECTION_RE.match(str(name).strip().lower()))


def project_uuid_from_collection_name(collection_name: str) -> str:
    """Recover the project UUID a collection name was derived from.

    The exact inverse of :func:`project_collection_name`, and the reason a
    collection orphaned by a lost registry is *recoverable*: its name still
    carries the identity, so the project can be reattached to the vectors it
    already has instead of being handed a freshly minted UUID and an empty
    collection. Round-trips for every name the forward function produces from a
    real UUID::

        project_collection_name(project_uuid_from_collection_name(n)) == n
    """
    match = _PROJECT_COLLECTION_RE.match(str(collection_name).strip().lower())
    if not match:
        raise InvalidProjectId(
            f"{collection_name!r} is not a project collection name "
            f"(expected proj_ followed by 32 hex characters)"
        )
    h = match.group(1)
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def framework_collection_name(
    name: str,
    *,
    version: str,
    edition: str,
    build_id: Optional[str],
) -> str:
    """Derive the collection name for a framework corpus, deduplicated by build.

    When ``build_id`` is known it is *the* dedup key — two source trees sharing a
    build id resolve to one collection regardless of any weaker signal. When it
    is absent (a git checkout with no ``repos_heads``), fall back to the stable
    ``(name, version, edition)`` tuple; the ``build:``/``src:`` tag makes a
    packaged build and a checkout land in different collections (§12.3).
    """
    fw = slugify_project_id(name) or "framework"
    if build_id:
        digest = _short_hash("build", fw, str(build_id))
    else:
        digest = _short_hash("src", fw, str(version), str(edition))
    return f"fw_{fw}_{digest}"
