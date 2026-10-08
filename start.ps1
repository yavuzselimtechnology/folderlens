param(
    [switch]$DevelopmentServer
)

$ErrorActionPreference = "Stop"
$FolderLensRoot = $PSScriptRoot

if (-not $DevelopmentServer) {
    python (Join-Path $FolderLensRoot "run.py")
    exit $LASTEXITCODE
}

$VirtualEnv = Join-Path $FolderLensRoot ".venv"
$Python = Join-Path $VirtualEnv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv $VirtualEnv
}
& $Python -m pip install --disable-pip-version-check -r (Join-Path $FolderLensRoot "requirements.txt")
& $Python (Join-Path $FolderLensRoot "run.py") --server
