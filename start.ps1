param(
    [switch]$DevelopmentServer,
    [switch]$Agent,
    [string]$Workspace = (Get-Location).Path,
    [string]$Name = 'folderlens',
    [switch]$AllowWrite,
    [switch]$AllowExec,
    [string[]]$AllowUrlHost = @()
)

$ErrorActionPreference = "Stop"
$FolderLensRoot = $PSScriptRoot

if ($Agent) {
    if ($DevelopmentServer) { throw 'Choose either -Agent or -DevelopmentServer.' }
    $AgentPython = Join-Path $FolderLensRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $AgentPython)) { $AgentPython = 'python' }
    $AgentArgs = @((Join-Path $FolderLensRoot 'folderlens.py'), '--root', $Workspace, '--name', $Name)
    if ($AllowWrite) { $AgentArgs += '--allow-write' }
    if ($AllowExec) { $AgentArgs += '--allow-exec' }
    foreach ($UrlHost in $AllowUrlHost) { $AgentArgs += @('--allow-url-host', $UrlHost) }
    & $AgentPython @AgentArgs
    exit $LASTEXITCODE
}

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
