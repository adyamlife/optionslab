# deploy/transfer_data.ps1
# Transfers DuckDB database, trained ML models, and .env to the server.
# Run AFTER setup.sh has created /opt/optionlab/data/ on the server.
# Run again any time you retrain models locally and want to push to server.
#
# Usage:
#   cd "C:\Project Y"
#   .\deploy\transfer_data.ps1

$SERVER  = "admin@192.168.1.199"
$APP_DIR = "/opt/optionlab"
$LOCAL   = "C:\Project Y"

Write-Host "==> Transferring data files to $SERVER ..."

# .env (secrets - never in the bundle)
if (Test-Path "$LOCAL\.env") {
    Write-Host "  Copying .env ..."
    & scp "$LOCAL\.env" "${SERVER}:${APP_DIR}/.env"
} else {
    Write-Host "  WARNING: .env not found - skipping." -ForegroundColor Yellow
    Write-Host "  Create /opt/optionlab/.env on the server manually."
}

# DuckDB database
if (Test-Path "$LOCAL\data\ml_training.duckdb") {
    Write-Host "  Copying ml_training.duckdb ..."
    & scp "$LOCAL\data\ml_training.duckdb" "${SERVER}:${APP_DIR}/data/ml_training.duckdb"
} else {
    Write-Host "  WARNING: ml_training.duckdb not found - skipping." -ForegroundColor Yellow
}

# Trained ML models
$modelsDir = "$LOCAL\data\models"
if (Test-Path $modelsDir) {
    Write-Host "  Copying ML models ..."
    $models = Get-ChildItem "$modelsDir\*.joblib"
    foreach ($f in $models) {
        Write-Host "    $($f.Name)"
        & scp $f.FullName "${SERVER}:${APP_DIR}/data/models/$($f.Name)"
    }
} else {
    Write-Host "  WARNING: data\models\ not found - no models copied." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "==> Transfer complete."
Write-Host ("Verify on server: ls -lh " + $APP_DIR + "/data/")
