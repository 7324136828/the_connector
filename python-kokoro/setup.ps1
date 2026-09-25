param(
    [ValidateSet("cuda", "cpu")]
    [string]$Device = "cuda",
    [switch]$ReuseSystemPackages
)

$ErrorActionPreference = "Stop"
$serviceDirectory = $PSScriptRoot
$environmentDirectory = Join-Path $serviceDirectory ".venv"
$servicePython = Join-Path $environmentDirectory "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $servicePython)) {
    $environmentArguments = @("-3.12", "-m", "venv", $environmentDirectory)
    if ($ReuseSystemPackages) {
        $environmentArguments += "--system-site-packages"
    }
    & py @environmentArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Creating the Kokoro environment failed. Install Python 3.12 and try again."
    }
}

& $servicePython -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "python-kokoro/.venv must use Python 3.12."
}

# Install into the dedicated Python 3.12 service environment, never the app's Python.
if ($Device -eq "cuda") {
    & $servicePython -c "import importlib.metadata, sys; sys.exit(0 if importlib.metadata.version('torch') == '2.11.0+cu128' else 1)"
    if ($LASTEXITCODE -ne 0) {
        & $servicePython -m pip install --upgrade -r (Join-Path $serviceDirectory "requirements-cuda.txt")
        if ($LASTEXITCODE -ne 0) {
            throw "Installing CUDA PyTorch failed."
        }
    }
} else {
    & $servicePython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('torch') else 1)"
    if ($LASTEXITCODE -ne 0) {
        & $servicePython -m pip install torch --index-url https://download.pytorch.org/whl/cpu
        if ($LASTEXITCODE -ne 0) {
            throw "Installing CPU PyTorch failed."
        }
    }
}

& $servicePython -m pip install -r (Join-Path $serviceDirectory "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Installing Kokoro requirements failed."
}
& $servicePython -c "import sys, kokoro, torch; print('Python:', sys.executable); print('Version:', sys.version.split()[0]); print('PyTorch:', torch.__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "The Kokoro environment could not load its dependencies."
}
if ($Device -eq "cuda") {
    & $servicePython -c "import torch; assert torch.version.cuda, 'Install a CUDA-enabled PyTorch build'; assert torch.cuda.is_available(), 'CUDA is unavailable; check the NVIDIA driver'; torch.ones(1, device='cuda').add_(1).item(); print('CUDA:', torch.version.cuda); print('GPU:', torch.cuda.get_device_name(0))"
    if ($LASTEXITCODE -ne 0) {
        throw "CUDA verification failed. The service will not silently switch to CPU; fix the driver or explicitly use -Device cpu."
    }
}

Write-Host "Run .\run_kokoro.bat -Device $Device to start the standalone Python 3.12 speech service on port 8302."
Write-Host "The first speech request may download the Kokoro model and English language data."
