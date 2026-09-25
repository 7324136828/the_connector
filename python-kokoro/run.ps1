param(
    [ValidateSet("auto", "cpu", "cuda")]
    [string]$Device = "auto",
    [ValidateRange(1, 65535)]
    [int]$Port = 8302,
    [switch]$Foreground,
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
$servicePython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$serviceScript = Join-Path $PSScriptRoot "server.py"
if (-not (Test-Path -LiteralPath $servicePython)) {
    throw "Run .\python-kokoro\setup.ps1 first to prepare the Python 3.12 environment."
}
& $servicePython -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Kokoro requires its separate Python 3.12 environment. Run python-kokoro/setup.ps1."
}

$healthUrl = "http://127.0.0.1:$Port/health"
$health = $null
try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
} catch {
    # The service is normally absent before it is started.
}
if ($health.service -eq "python-kokoro" -and $health.status -eq "ok") {
    if (-not $Restart) {
        if ($Device -ne "auto" -and $health.device -ne $Device) {
            throw "Kokoro is running on $($health.device), but $Device was requested. Run with -Restart to switch this repository's service."
        }
        if ($health.python -notlike "3.12.*") {
            throw "The existing Kokoro service is not using Python 3.12. Run with -Restart to replace this repository's service."
        }
        Write-Host "Kokoro is already running at http://127.0.0.1:$Port ($($health.device), Python $($health.python))."
        exit 0
    }
    # Stop only the listener whose command identifies this repository's service.
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($listeners.Count -ne 1) {
        throw "Cannot identify a unique Kokoro listener on port $Port."
    }
    $listenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($listeners[0])"
    $scriptPattern = '(?:^|[\s\x22])' + [regex]::Escape($serviceScript) + '(?=[\s\x22]|$)'
    if ($listenerProcess.Name -notin @("python.exe", "pythonw.exe") -or $listenerProcess.CommandLine -notmatch $scriptPattern) {
        throw "The listener was not started from $serviceScript; it will not be stopped."
    }
    Stop-Process -Id $listenerProcess.ProcessId -ErrorAction Stop
    Wait-Process -Id $listenerProcess.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
}

if ($Foreground) {
    & $servicePython -u $serviceScript --host 127.0.0.1 --port $Port --device $Device
    exit $LASTEXITCODE
}

$workspaceDirectory = Split-Path -Parent $PSScriptRoot
$logDirectory = Join-Path $workspaceDirectory "data\workspace\kokoro-service"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$outputLog = Join-Path $logDirectory "service-$Port.stdout.log"
$errorLog = Join-Path $logDirectory "service-$Port.stderr.log"
$arguments = @("-u", ('"{0}"' -f $serviceScript), "--host", "127.0.0.1", "--port", $Port, "--device", $Device)
$serviceProcess = Start-Process -FilePath $servicePython -ArgumentList $arguments `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $outputLog -RedirectStandardError $errorLog
$serviceProcess.Id | Set-Content -LiteralPath (Join-Path $logDirectory "service-$Port.pid")

for ($attempt = 0; $attempt -lt 45; $attempt++) {
    $serviceProcess.Refresh()
    if ($serviceProcess.HasExited) {
        throw "Kokoro stopped during startup. Read $errorLog"
    }
    $health = $null
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 1
    } catch {
        # PyTorch can take several seconds to load before the HTTP port binds.
    }
    if ($health.service -eq "python-kokoro" -and $health.status -eq "ok") {
        if ($Device -ne "auto" -and $health.device -ne $Device) {
            throw "The started service is on $($health.device), not the requested $Device."
        }
        if ($health.python -notlike "3.12.*") {
            throw "The started Kokoro service is not using Python 3.12."
        }
        Write-Host "Kokoro is running at http://127.0.0.1:$Port ($($health.device), Python $($health.python)); PID $($serviceProcess.Id)."
        Write-Host "Logs: $logDirectory"
        exit 0
    }
    Start-Sleep -Milliseconds 500
}
Write-Host "Kokoro is starting in the background (PID $($serviceProcess.Id))."
Write-Host "Check $healthUrl or the logs in $logDirectory."
