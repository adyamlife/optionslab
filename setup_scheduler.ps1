# setup_scheduler.ps1
# Creates two Windows Task Scheduler tasks for the paper trade engine.
# Run once as Administrator:  powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python      = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { Write-Error "python not found in PATH"; exit 1 }

$MornScript  = Join-Path $ProjectRoot "scripts\morning_scan.py"
$EveScript   = Join-Path $ProjectRoot "scripts\evening_check.py"
$LogDir      = Join-Path $ProjectRoot "data"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

function Register-PaperTradeTask {
    param($Name, $Script, $Hour, $Minute)

    $Action  = New-ScheduledTaskAction `
        -Execute  $Python `
        -Argument "`"$Script`"" `
        -WorkingDirectory $ProjectRoot

    $Trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
        -At ([datetime]"$Hour`:$Minute`:00")

    $Settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
        -StartWhenAvailable `
        -RunOnlyIfNetworkAvailable

    $Principal = New-ScheduledTaskPrincipal `
        -UserId     $env:USERNAME `
        -LogonType  Interactive `
        -RunLevel   Limited

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "Removed existing task: $Name"
    }

    Register-ScheduledTask `
        -TaskName  $Name `
        -Action    $Action `
        -Trigger   $Trigger `
        -Settings  $Settings `
        -Principal $Principal `
        -Force | Out-Null

    Write-Host "Registered: $Name  (runs Mon-Fri at $Hour`:$('{0:D2}' -f $Minute) local time)"
}

Register-PaperTradeTask -Name "OptionLab_MorningScan"  -Script $MornScript -Hour 10 -Minute 0
Register-PaperTradeTask -Name "OptionLab_EveningCheck" -Script $EveScript  -Hour 17 -Minute 0

Write-Host ""
Write-Host "Done. Tasks registered:"
Get-ScheduledTask -TaskName "OptionLab_*" | Select-Object TaskName, State | Format-Table
Write-Host ""
Write-Host "NOTE: Tasks run in your local timezone. If your machine is not in EDT,"
Write-Host "adjust the -At times in this script to match 10:00 AM and 5:00 PM EDT."
