param(
    [switch]$Local,
    [Alias("completion-no-database-access")][switch]$CompletionNoDatabaseAccess,
    [Alias("host")][string]$BindHost,
    [switch]$Reload,
    [switch]$NoReload,
    [Alias("frontend-port")]
    [ValidateRange(1, 65535)][int]$UiPort = 5130,
    [Alias("backend-port")]
    [ValidateRange(1, 65535)][int]$BackendPort = 8130,
    [Alias("kokoro-port")]
    [ValidateRange(1, 65535)][int]$KokoroPort = 8131,
    [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
if ($Local -and $BindHost) {
    throw "Use either -Local or -BindHost, not both."
}
$backendPython = Join-Path $root ".venv\Scripts\python.exe"
if ($PythonExecutable) {
    $backendPython = $PythonExecutable
}
elseif ($env:VIRTUAL_ENV) {
    $backendPython = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
}
elseif ($env:CONDA_PREFIX) {
    $backendPython = Join-Path $env:CONDA_PREFIX "python.exe"
}
if (-not (Test-Path -LiteralPath $backendPython)) {
    throw "Backend Python environment is missing. Run setup.bat first."
}
if ($Reload -and $NoReload) {
    throw "Use either -Reload or -NoReload, not both."
}

# Share host resolution, busy-port fallback, and interface discovery with run.sh.
$planningArguments = @("--print-plan-json")
if ($Local) { $planningArguments += "--local" }
elseif ($BindHost) { $planningArguments += @("--host", $BindHost) }
if ($PSBoundParameters.ContainsKey("UiPort")) { $planningArguments += @("--frontend-port", [string]$UiPort) }
if ($PSBoundParameters.ContainsKey("BackendPort")) { $planningArguments += @("--backend-port", [string]$BackendPort) }
if ($PSBoundParameters.ContainsKey("KokoroPort")) { $planningArguments += @("--kokoro-port", [string]$KokoroPort) }
$planText = (& $backendPython (Join-Path $root "run.py") @planningArguments) -join [Environment]::NewLine
if ($LASTEXITCODE -ne 0) { throw "Could not select the service bind address and ports." }
$plan = $planText | ConvertFrom-Json
$publicHost = [string]$plan.host
$kokoroHost = $publicHost
$UiPort = [int]$plan.ports.frontend
$BackendPort = [int]$plan.ports.backend
$KokoroPort = [int]$plan.ports.kokoro
foreach ($name in @("backend", "frontend", "kokoro")) {
    if ($plan.ports.$name -ne $plan.requested_ports.$name) {
        Write-Host "[RUN] $name port $($plan.requested_ports.$name) is busy or reserved; using $($plan.ports.$name)."
    }
}

Write-Host "========================================================"
Write-Host "Starting The Connector Full-Stack Services"
Write-Host "Frontend: $($plan.urls.frontend)"
Write-Host "Backend:  $($plan.urls.backend) (API docs: $($plan.urls.backend)/docs)"
Write-Host "Kokoro:   $($plan.urls.kokoro) (speech backend)"
foreach ($url in $plan.lan_urls.frontend) { Write-Host "LAN:      $url" }
foreach ($url in $plan.lan_urls.backend) { Write-Host "LAN API:  $url/docs" }
foreach ($url in $plan.lan_urls.kokoro) { Write-Host "LAN TTS:  $url/health" }
Write-Host "========================================================"
if ($plan.lan_urls.frontend.Count -gt 0) {
    Write-Host "Open a LAN URL on another device connected to the same network."
}

# Put this supervisor in a Windows job whose members are killed when the
# supervisor's last handle closes. The three servers are launched directly into
# this job, so closing the console cannot leave them running in the background.
Add-Type -TypeDefinition @"
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;

public static class ConnectorProcessJob
{
    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
    private const int JobObjectExtendedLimitInformation = 9;
    private static IntPtr jobHandle = IntPtr.Zero;

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr securityAttributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr job,
        int informationClass,
        ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION information,
        uint informationLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    public static bool Enable(out int error)
    {
        error = 0;
        jobHandle = CreateJobObject(IntPtr.Zero, null);
        if (jobHandle == IntPtr.Zero)
        {
            error = Marshal.GetLastWin32Error();
            return false;
        }

        var information = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if (!SetInformationJobObject(
                jobHandle,
                JobObjectExtendedLimitInformation,
                ref information,
                (uint)Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION))))
        {
            error = Marshal.GetLastWin32Error();
            CloseHandle(jobHandle);
            jobHandle = IntPtr.Zero;
            return false;
        }

        if (!AssignProcessToJobObject(jobHandle, Process.GetCurrentProcess().Handle))
        {
            error = Marshal.GetLastWin32Error();
            CloseHandle(jobHandle);
            jobHandle = IntPtr.Zero;
            return false;
        }

        return true;
    }
}
"@

$jobError = 0
if (-not [ConnectorProcessJob]::Enable([ref]$jobError)) {
    Write-Warning "Could not enable automatic process-tree cleanup (Windows error $jobError). Normal shutdown cleanup will still be used."
}

$services = @()
$isStopping = $false

function Start-ConnectorProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $process = Start-Process -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory `
        -NoNewWindow `
        -PassThru
    Write-Host "[RUN.BAT] Started $Name (PID $($process.Id))."
    return [PSCustomObject]@{
        Name = $Name
        Process = $process
    }
}

function Stop-ConnectorProcesses {
    if ($script:isStopping) {
        return
    }
    $script:isStopping = $true

    Write-Host "`n[RUN.BAT] Stopping all services..."
    for ($index = $script:services.Count - 1; $index -ge 0; $index--) {
        $service = $script:services[$index]
        $process = $service.Process
        $process.Refresh()
        if ($process.HasExited) {
            continue
        }

        # /T includes any service descendants. Ignore a
        # taskkill failure here (for example, if a process exited between the
        # refresh and this call); the job object remains the final backstop.
        $previousErrorPreference = $ErrorActionPreference
        $ErrorActionPreference = "SilentlyContinue"
        try {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F 2>$null | Out-Null
            if (-not $process.HasExited) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        }
        finally {
            $ErrorActionPreference = $previousErrorPreference
        }
    }
    Write-Host "[RUN.BAT] All services stopped."
}

$exitCode = 0
try {
    $kokoroPython = Join-Path $root "python-kokoro\.venv\Scripts\python.exe"
    $kokoroScript = Join-Path $root "python-kokoro\server.py"
    $node = (Get-Command "node.exe" -ErrorAction Stop).Source
    $viteScript = Join-Path $root "frontend\node_modules\vite\bin\vite.js"

    if (-not (Test-Path -LiteralPath $viteScript)) {
        throw "Frontend dependencies are missing. Run setup.bat first."
    }

    $env:CONNECTOR_BACKEND_PORT = [string]$BackendPort
    $env:CONNECTOR_BACKEND_URL = [string]$plan.urls.backend
    $env:VITE_BACKEND_URL = [string]$plan.urls.backend
    $env:KOKORO_BASE_URL = [string]$plan.urls.kokoro
    if ($plan.PSObject.Properties.Name -contains "cors_origins") {
        $env:CORS_ORIGINS = [string]$plan.cors_origins
    }

    $backendArguments = @("run_backend.py", "--host", $publicHost, "--port", [string]$BackendPort, "--strict-port")
    if (-not $NoReload) {
        $backendArguments += "--reload"
    }
    if ($CompletionNoDatabaseAccess) {
        $backendArguments += "--completion-no-database-access"
    }

    $services += Start-ConnectorProcess `
        -Name "Connector API" `
        -FilePath $backendPython `
        -ArgumentList $backendArguments `
        -WorkingDirectory $root

    $services += Start-ConnectorProcess `
        -Name "Kokoro speech" `
        -FilePath $kokoroPython `
        -ArgumentList @("-u", ('"{0}"' -f $kokoroScript), "--host", $kokoroHost, "--port", [string]$KokoroPort, "--device", "auto") `
        -WorkingDirectory (Join-Path $root "python-kokoro")

    $services += Start-ConnectorProcess `
        -Name "frontend" `
        -FilePath $node `
        -ArgumentList @(('"{0}"' -f $viteScript), "--host", $publicHost, "--port", [string]$UiPort, "--strictPort") `
        -WorkingDirectory (Join-Path $root "frontend")

    Write-Host "[RUN.BAT] All output is sharing this window. Press Ctrl+C or close it to stop everything."

    while ($true) {
        Start-Sleep -Milliseconds 500
        foreach ($service in $services) {
            $service.Process.Refresh()
            if ($service.Process.HasExited) {
                $service.Process.WaitForExit()
                $code = $service.Process.ExitCode
                if ($null -eq $code -or "$code" -eq "") {
                    throw "$($service.Name) stopped; shutting down the stack."
                }
                throw "$($service.Name) stopped with exit code $code; shutting down the stack."
            }
        }
    }
}
catch {
    $exitCode = 1
    Write-Host "`n[RUN.BAT] $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    Stop-ConnectorProcesses
}

exit $exitCode
