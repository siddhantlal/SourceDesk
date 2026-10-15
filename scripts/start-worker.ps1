param([switch]$Once)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$keeper = Start-Process wsl.exe -ArgumentList '-d','Ubuntu','--','sleep','infinity' -PassThru -WindowStyle Hidden
try {
    & wsl.exe -d Ubuntu -- true
    if ($LASTEXITCODE -ne 0) { throw 'Ubuntu WSL did not start.' }
    # API startup applies migrations. Worker only checks, never migrates while
    # another process might be starting or holding a migration lock.
    & uv run --locked python scripts/setup_db.py --check
    if ($LASTEXITCODE -ne 0) { throw 'Database check failed. Start API first to refresh WSL address/migrate.' }
    $workerArgs = @('run','--locked','python','-m','sourcedesk.worker')
    if ($Once) { $workerArgs += '--once' }
    & uv @workerArgs
    if ($LASTEXITCODE -ne 0) { throw 'Worker process exited with an error.' }
} finally {
    if (-not $keeper.HasExited) { Stop-Process -Id $keeper.Id -ErrorAction SilentlyContinue }
}
