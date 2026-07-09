# bundle.ps1 — Pack project files into deploy_bundle.zip (run from "C:\Project Y")

$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$OutFile = Join-Path $Root "deploy_bundle.zip"

# Remove previous bundle
if (Test-Path $OutFile) { Remove-Item $OutFile -Force }

# Dirs / files to include
$Include = @(
    "web",
    "scripts",
    "config",
    "rulebook"
)

# Patterns to exclude (relative path fragments)
$Exclude = @(
    "config\secrets.toml",
    "__pycache__",
    ".pyc",
    ".pyo",
    "venv",
    "node_modules",
    ".git",
    "deploy_bundle.zip",
    "data"
)

function ShouldExclude($path) {
    foreach ($pat in $Exclude) {
        if ($path -like "*$pat*") { return $true }
    }
    return $false
}

Write-Host "Collecting files..." -ForegroundColor Cyan

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($OutFile, 'Create')

foreach ($dir in $Include) {
    $full = Join-Path $Root $dir
    if (-not (Test-Path $full)) { Write-Host "  Skipping missing: $dir"; continue }

    Get-ChildItem -Path $full -Recurse -File | ForEach-Object {
        $abs = $_.FullName
        $rel = $abs.Substring($Root.Length).TrimStart('\').Replace('\', '/')

        if (ShouldExclude $abs) { return }

        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $abs, $rel) | Out-Null
        Write-Host "  + $rel"
    }
}

# Also bundle requirements.txt
$reqfile = Join-Path $Root "requirements.txt"
if (Test-Path $reqfile) {
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $reqfile, "requirements.txt") | Out-Null
    Write-Host "  + requirements.txt"
}

# Also bundle deploy_optionlab.sh
$deploysh = Join-Path $Root "deploy_optionlab.sh"
if (Test-Path $deploysh) {
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $deploysh, "deploy_optionlab.sh") | Out-Null
    Write-Host "  + deploy_optionlab.sh"
}

$zip.Dispose()

$size = [math]::Round((Get-Item $OutFile).Length / 1KB, 1)
Write-Host ""
Write-Host "Bundle created: deploy_bundle.zip ($size KB)" -ForegroundColor Green
Write-Host "Upload to server:  scp deploy_bundle.zip abhijeet@<server-ip>:~/"
Write-Host "Then on server:    bash deploy_optionlab.sh"
