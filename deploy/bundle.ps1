# deploy/bundle.ps1
# Run from Windows to create a code bundle and copy it to the Ubuntu server.
# Does NOT include .env, data/, or venv/ — transfer those separately.
#
# Usage:
#   cd "C:\Project Y"
#   .\deploy\bundle.ps1
#
# Prerequisites: OpenSSH must be installed on Windows (it is by default on Win10/11)

$SERVER   = "admin@192.168.1.199"
$BUNDLE   = "optionlab_bundle.tar.gz"
$DEST     = "/home/admin/$BUNDLE"

$EXCLUDE = @(
    "--exclude=.env",
    "--exclude=venv",
    "--exclude=.venv",
    "--exclude=__pycache__",
    "--exclude=*.pyc",
    "--exclude=*.pyo",
    "--exclude=data",
    "--exclude=backup",
    "--exclude=backup_extracted",
    "--exclude=deploy_bundle.zip",
    "--exclude=optionlab_bundle.tar.gz",
    "--exclude=tmpclaude*",
    "--exclude=*.jsonl",
    "--exclude=*.csv",
    "--exclude=*.json",
    "--exclude=.git",
    "--exclude=node_modules"
)

Write-Host "==> Bundling project (excluding data/, venv/, .env) ..."
$tarArgs = @("-czf", $BUNDLE) + $EXCLUDE + @("-C", "C:\Project Y", ".")
& tar $tarArgs

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: tar failed." -ForegroundColor Red
    exit 1
}

$sizeMB = [math]::Round((Get-Item $BUNDLE).Length / 1MB, 1)
Write-Host "==> Bundle created: $BUNDLE ($sizeMB MB)"

Write-Host "==> Copying bundle to $SERVER`:$DEST ..."
& scp $BUNDLE "${SERVER}:${DEST}"

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: scp failed. Make sure SSH access to $SERVER works." -ForegroundColor Red
    exit 1
}

Remove-Item $BUNDLE
Write-Host "==> Done. Bundle is at $DEST on the server."
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. SSH into server:  ssh $SERVER"
Write-Host "  2. First deploy:     bash /home/admin/deploy/setup.sh"
Write-Host "  3. Deploy code:      bash /home/admin/deploy/deploy.sh"
