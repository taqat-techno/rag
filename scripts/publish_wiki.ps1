# publish_wiki.ps1 — sync docs/wiki-src/ to the GitHub Wiki.
# Phase 1 deliverable: manual, idempotent. Run from the repo root.
#
# Usage:
#   scripts/publish_wiki.ps1 -DryRun
#   scripts/publish_wiki.ps1
#   scripts/publish_wiki.ps1 -WikiRemote https://github.com/<owner>/<repo>.wiki.git

[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$WikiRemote = $env:RAG_WIKI_REMOTE
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$src = Join-Path $repoRoot 'docs/wiki-src'
if (-not (Test-Path $src)) {
    throw "Source directory not found: $src"
}

if (-not $WikiRemote -and -not $DryRun) {
    $origin = $null
    try {
        $origin = (git -C $repoRoot remote get-url origin 2>$null)
    } catch {}
    if ($origin) {
        $origin = $origin.Trim()
        if ($origin -match '\.git$') {
            $WikiRemote = ($origin -replace '\.git$','') + '.wiki.git'
        } else {
            $WikiRemote = "$origin.wiki.git"
        }
    } else {
        throw "Could not determine wiki remote. Pass -WikiRemote or set RAG_WIKI_REMOTE."
    }
}

Write-Host "Source:      $src"
if ($WikiRemote) { Write-Host "Wiki remote: $WikiRemote" } else { Write-Host "Wiki remote: (not resolved - dry run only)" }
Write-Host "Dry run:     $DryRun"
Write-Host ""

# Enumerate source files and compute slug targets.
$pages = @()
Get-ChildItem -Path $src -Recurse -File | Where-Object {
    $_.Name -ne '.gitkeep' -and $_.Extension -eq '.md'
} | ForEach-Object {
    $rel = $_.FullName.Substring($src.Length + 1) -replace '\\','/'

    if ($rel -in @('_Sidebar.md','_Footer.md','Home.md')) {
        # Wiki special files publish at the root, unchanged.
        $slug = $rel
    } else {
        # Slug transform: Folder/Sub/Name.md -> Folder-Sub-Name.md ; dots in segments -> dashes.
        $noExt = $rel -replace '\.md$',''
        $slug  = ($noExt -replace '/','-') -replace '\.','-'
        $slug  = "$slug.md"
    }

    $pages += [pscustomobject]@{ Source = $_.FullName; Slug = $slug }
}

Write-Host "Pages to publish: $($pages.Count)"
$pages | Sort-Object Slug | ForEach-Object { Write-Host "  $($_.Slug)" }

if ($DryRun) {
    Write-Host ""
    Write-Host "Dry run complete. No changes pushed."
    exit 0
}

# Clone wiki into a temp dir.
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("rag-wiki-" + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Path $tmp | Out-Null
Write-Host ""
Write-Host "Cloning wiki into $tmp..."
git clone $WikiRemote $tmp

# Clear old content (except .git) and copy new.
Get-ChildItem -Path $tmp -Force | Where-Object { $_.Name -ne '.git' } | Remove-Item -Recurse -Force

foreach ($p in $pages) {
    $target = Join-Path $tmp $p.Slug
    Copy-Item -Path $p.Source -Destination $target -Force
}

# Commit and push.
Push-Location $tmp
try {
    git add -A
    $pending = git status --porcelain
    if (-not $pending) {
        Write-Host "No changes to publish."
        return
    }
    $sha = (git -C $repoRoot rev-parse --short HEAD).Trim()
    git commit -m "docs(wiki): sync from $sha"
    git push
    Write-Host "Published."
} finally {
    Pop-Location
}
