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

## Verifying a clean removal

```
rag upgrade scan     # from a copy elsewhere, or after reinstalling
```

It reports any surviving registration, duplicate `PATH` entry or stale artifact.
