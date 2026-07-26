# Shared dependencies as first-class objects

**Problem with the shipped model.** A dependency is declared as a *path typed into
one project*. That makes the same shared thing an invisible, repeated, per-project
string: two projects vendoring one Odoo core must each type a path, dedup only
happens if both spellings resolve to the same build identity, nothing lists what
exists, and "shared" is an emergent property nobody can see or manage.

**Target.** A dependency is a **named object in a catalog**. You add it once. Each
project **selects** which ones it uses (multi-select). Linking is explicit and
reversible. The same actions exist over MCP.

---

## 1. Model

| Layer | Owns | Keyed by |
|---|---|---|
| **Catalog** (`[[dependencies]]` in TOML) | user intent: id, display name, path | **resolved path** (unique) |
| **Links** (`ProjectConfig.dependencies`) | which projects use which catalog entries | (project, dependency id) |
| **Corpus** (`frameworks` table) | the actual Qdrant collection | **build identity** (existing dedup) |

**Catalog entry ≠ collection.** Two catalog entries can resolve to the same build
and therefore share ONE collection — that is the dedup win, not a bug. Deleting
one entry must not drop a collection the other still needs. Conversely one entry
maps to exactly one collection at a time.

**Detection is derived, never stored in config.** A checkout can switch branches;
a stored `version`/`build_id` would silently go stale and mis-route a corpus. The
config holds *intent* (id, path); identity is recomputed at every sync.

```toml
[[dependencies]]
id = "odoo-18-pearl"
name = "Odoo 18 (pearl-pixels)"
path = "C:\\Workspace\\odoo\\pearl-pixels-18\\odoo"

[[projects]]
id = "khayrgate"
dependencies = ["odoo-18-pearl"]     # ids, not paths
```

## 2. Where validation lives

Path rules are **relative to a project**, so they belong to the *link*, not the
catalog entry:

| Rule | Enforced at | Why |
|---|---|---|
| path exists / is a directory | catalog add **and** every sync | a folder can disappear after it was added |
| path is not the project root | **link** | would exclude every file and index the project as a "framework" |
| path does not contain the project | **link** | a dependency cannot be a parent of its dependent |
| nested links collapse to outermost | **sync** | `vendor` + `vendor/odoo` would embed the inner tree twice |
| duplicate id | catalog add | ids are the link key |
| duplicate resolved path | catalog add | two names for one thing defeats "add it once" |

A catalog entry may therefore be perfectly valid and still be un-linkable to a
particular project. The UI must say which, per project — not just "invalid".

## 3. Lifecycle

```
add catalog entry ──► (nothing indexed yet; it is a declaration)
link to project   ──► sync: resolve ▸ detect ▸ register(build id) ▸ index ▸ link ▸ purge from project
unlink            ──► sync: unlink ▸ drop corpus IF no project links it ▸ reindex project (files return)
delete entry      ──► refused while linked, unless cascade=true (unlink everywhere first)
```

Every mutation schedules ONE `sync_frameworks` job; it reconciles both directions
and is idempotent, so a double edit cannot leave a half-applied state.

## 4. Migration from `dependency_paths`

A read-only, idempotent validator on `Settings`: for each legacy path, synthesize
a catalog entry (id from the folder name, deduped) and add its id to that
project's `dependencies`. Two projects with the same legacy path collapse to ONE
entry — the dedup the old model could only achieve by luck.

Read-only on purpose: loading config must never rewrite it. The TOML is
normalised the next time the user saves. `dependency_paths` keeps working
forever; sync takes the union of both.

## 5. Corner cases (each gets a test)

**Catalog** — duplicate id · duplicate resolved path · different spellings of one
path (`..`, symlink, junction, drive-letter case) · path missing at add · path
deleted after add · disabled entry · rename display name (links unaffected).

**Linking** — link to unknown id · link path == project root · link path contains
project · link nested pair · link same entry twice · link entry that is missing
on disk · link while its corpus is mid-index · two projects link one entry (one
collection) · two entries, same build, one collection.

**Unlink / delete** — unlink one of two projects (corpus survives) · unlink last
(corpus dropped) · delete while linked (refused) · delete with cascade · delete
an entry whose collection is shared by another entry (collection survives).

**Restore** — unlinking an *inside-project* dependency must re-index the project
so the files return; an *outside-project* one must not trigger a pointless
re-index.

**MCP** — every write requires the service; destructive ones require a confirm
token; unknown ids return a structured error, never a silent no-op.

## 6. Surfaces

* **`/dependencies` page** — catalog table (name, folder, detected-as, corpus
  state, chunks, used-by), add form with the existing dry-run check, per-row
  delete with cascade confirmation.
* **Project edit** — checkbox multi-select of catalog entries, each row showing
  why it cannot be linked when it cannot.
* **API** — `GET/POST /api/dependencies`, `GET/PATCH/DELETE /api/dependencies/{id}`,
  `PUT /api/projects/{id}/dependencies` (set the full link list).
* **MCP** — `list_dependencies`, `add_dependency`, `link_dependency`,
  `unlink_dependency`, `remove_dependency`.

## 7. Defects this redesign exposed (all fixed)

Making linking easy turned three latent problems into routine ones:

| Defect | Consequence | Fix |
|---|---|---|
| Adoption left `dependency_paths` populated **and** created links | The legacy field became a dead control — clearing the existing textarea silently did nothing, because the adopted link still stood | Adoption **consumes** it, exactly as `index_source_code` is consumed |
| The scanner read `project.dependency_paths` directly | A project that LINKS a dependency had that tree scanned into its own collection *and* the shared corpus — every hit twice | Both scanner and sync call one function, `resolve_project_dependency_paths` |
| Every sync re-imported every declared corpus | Linking a second project to a 32,782-file Odoo core re-embedded all of it to change one row — the opposite of "indexed once" | Reuse when any project already links it; `refresh=True` forces re-import. The **link** is the completeness signal, so an interrupted run is still completed |
| `dependency_delete(cascade=Query(False))` called directly | FastAPI's default is a *truthy sentinel*, so the "refused while in use" safeguard was inert and delete silently cascaded | Coerced with `cascade is True` — anything short of an explicit yes means no |

## 8. What does NOT change

`resolve_dependency_roots`, `describe_dependency`, `framework_collection_name`,
`FrameworkRegistry`, `CollectionRouter`, the sync/release reconciliation, the
scanner exclusion, and search scope tagging. The catalog is a management layer
that feeds the same engine a resolved list of paths — so this is additive, and
`dependency_paths` remains a supported input.
