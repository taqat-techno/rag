# Uninstalling ragtools

```
rag upgrade uninstall          # stops everything, removes registrations
```

Then use the platform uninstaller:

| Platform | Command |
|---|---|
| Windows | Settings → Apps → RAG Tools, or `%LOCALAPPDATA%\Programs\RAGTools\unins000.exe` |
| Linux | `apt remove ragtools` / `dnf remove ragtools`, or delete the extracted directory |
| macOS | Drag `RAG Tools.app` to the Trash |

## What is removed

* the service and tray autostart registrations (all mechanisms, including any
  superseded ones)
* the product's `PATH` entry
* program files

## What is left behind, deliberately

**Your data directory is not deleted.** It holds your index and configuration,
and an uninstaller that silently destroys a rebuilt 147,000-point index is doing
something the user did not ask for. Remove it yourself when you are sure:

| Platform | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\RAGTools` |
| Linux | `~/.local/share/RAGTools` (or `$XDG_DATA_HOME/RAGTools`) |
| macOS | `~/Library/Application Support/RAGTools` |

**Your projects are never touched.** ragtools indexes files in place; it has
never held the only copy of anything you wrote.

### If you do choose to delete it (Windows)

The Windows uninstaller offers to remove the data directory. It defaults to
**No**, and No is the right answer unless you are certain.

Saying Yes deletes two very different things at once:

| | Cost of losing it |
|---|---|
| Vector collections, state DB, model cache | hours of re-indexing |
| `config.toml` — projects, ignore rules, per-project modes | **cannot be rebuilt** |

From v3.0.1 the uninstaller copies `config.toml` to
`%LOCALAPPDATA%\RAGTools-config-backup-<timestamp>\` before deleting anything,
tells you where it put it, and **cancels the deletion entirely if that copy
fails**. Reinstalling and dropping the file back restores your whole project
list; the index rebuilds itself from there.

On **v3.0.0 and earlier there was no backup** — the wipe took the configuration
with it. If you are uninstalling one of those versions, copy `config.toml`
somewhere safe first.

## Verifying a clean removal

```
rag upgrade scan     # from a copy elsewhere, or after reinstalling
```

It reports any surviving registration, duplicate `PATH` entry or stale artifact.
