<#
.SYNOPSIS
    Ingests Algo CLI rebrand memory facts into your local Algo CLI memory.

.DESCRIPTION
    This script reads memory facts and appends them to ~/.algo_cli/memory.json.
    It uses an exclusive lock, reloads memory while holding that lock, performs
    duplicate detection against the latest file contents, creates a backup, and
    writes via a same-directory temp file plus atomic replace.

.PARAMETER FactsFile
    Path to the memory facts markdown file. Defaults to the one in the source repo.

.PARAMETER DryRun
    If specified, shows what would be added without actually modifying your memory file.

.EXAMPLE
    .\scripts\ingest-algo-cli-memory.ps1

.EXAMPLE
    .\scripts\ingest-algo-cli-memory.ps1 -DryRun

.NOTES
    After running, you can also use the /memories command inside Algo CLI to verify.
#>

param(
    [string]$FactsFile = "$PSScriptRoot\..\docs\memory-facts-algo-cli.md",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$MemoryDir = Join-Path $env:USERPROFILE ".algo_cli"
$MemoryFile = Join-Path $MemoryDir "memory.json"
$LockFile = Join-Path $MemoryDir "memory.json.lock"
$BackupFile = Join-Path $MemoryDir "memory.json.backup-before-rebrand-ingest"

function Read-MemoryList {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return @() }
    try {
        $loaded = Get-Content $Path -Raw | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $loaded) { return @() }
        if ($loaded -is [array]) { return @($loaded | ForEach-Object { [string]$_ }) }
        return @([string]$loaded)
    } catch {
        throw "Could not parse existing memory.json. Refusing to overwrite corrupt memory: $Path"
    }
}

function Write-MemoryListAtomic {
    param(
        [string]$Path,
        [string[]]$Memories
    )
    $dir = Split-Path -Parent $Path
    $tmp = Join-Path $dir ("." + [IO.Path]::GetFileName($Path) + "." + [guid]::NewGuid().ToString("N") + ".tmp")
    $json = ConvertTo-Json -InputObject @($Memories) -Depth 10
    try {
        [System.IO.File]::WriteAllText($tmp, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
        if (Test-Path $Path) {
            $replaceBackup = $Path + ".replace-backup"
            [System.IO.File]::Replace($tmp, $Path, $replaceBackup, $true)
            Remove-Item $replaceBackup -Force -ErrorAction SilentlyContinue
        } else {
            [System.IO.File]::Move($tmp, $Path)
        }
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "=== Algo CLI Rebrand Memory Ingest ===" -ForegroundColor Cyan
Write-Host ""

# Resolve full path to facts file before taking the lock.
$FactsFile = (Resolve-Path $FactsFile).Path
Write-Host "Facts file: $FactsFile"

if (-not (Test-Path $FactsFile)) {
    Write-Error "Facts file not found: $FactsFile"
    exit 1
}

$facts = @(Get-Content $FactsFile | Where-Object { $_ -match '^\s*-\s+' } | ForEach-Object {
    ($_ -replace '^\s*-\s+', '').Trim()
} | Where-Object { $_ -ne '' })

Write-Host "Found $($facts.Count) facts to consider."

if (-not (Test-Path $MemoryDir)) {
    New-Item -ItemType Directory -Path $MemoryDir -Force | Out-Null
    Write-Host "Created $MemoryDir"
}

$lock = $null
try {
    $lock = [System.IO.File]::Open($LockFile, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)

    # Load inside the lock so duplicate checks and append decisions use the latest memory file.
    $existingMemories = @(Read-MemoryList -Path $MemoryFile)
    $existingNormalized = @($existingMemories | ForEach-Object { $_.ToLowerInvariant().Trim() })

    $newFacts = @($facts | Where-Object {
        $normalized = $_.ToLowerInvariant().Trim()
        $normalized -notin $existingNormalized
    })

    Write-Host "New facts to add: $($newFacts.Count)" -ForegroundColor Green

    if ($newFacts.Count -eq 0) {
        Write-Host "Nothing new to ingest. Your memory is already up to date." -ForegroundColor Yellow
        exit 0
    }

    if ($DryRun) {
        Write-Host "`n[DRY RUN] The following facts would be added:" -ForegroundColor Yellow
        $newFacts | ForEach-Object { Write-Host ("  - " + $_) }
        Write-Host "`nRun without -DryRun to actually apply these changes."
        exit 0
    }

    if (Test-Path $MemoryFile) {
        Copy-Item $MemoryFile $BackupFile -Force
        Write-Host "Backup created: $BackupFile" -ForegroundColor Green
    }

    $updatedMemories = @($existingMemories + $newFacts)
    Write-MemoryListAtomic -Path $MemoryFile -Memories $updatedMemories

    Write-Host "`nSuccessfully added $($newFacts.Count) new facts to your Algo CLI memory." -ForegroundColor Green
    Write-Host "Memory file updated: $MemoryFile"
    Write-Host "`nYou can verify with:  algo-cli /memories" -ForegroundColor Cyan

    Write-Host "`nNew facts added:" -ForegroundColor Gray
    $newFacts | ForEach-Object { Write-Host ("  - " + $_) -ForegroundColor DarkGray }
} finally {
    if ($null -ne $lock) {
        $lock.Dispose()
    }
}
