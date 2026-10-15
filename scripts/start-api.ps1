param([int]$Port = 8000, [switch]$Reload)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
# systemd services alone do not keep a WSL distro alive. Own this keeper,
# and terminate only this process when the API exits.
$keeper = Start-Process wsl.exe -ArgumentList '-d','Ubuntu','--','sleep','infinity' -PassThru -WindowStyle Hidden
try {
    & wsl.exe -d Ubuntu -- true
    if ($LASTEXITCODE -ne 0) { throw 'Ubuntu WSL did not start.' }
    & uv run --locked python scripts/setup_db.py --refresh-wsl-host --migrate application
    if ($LASTEXITCODE -ne 0) { throw 'Database setup/check/migration failed.' }
    $apiArgs = @('run','--locked','uvicorn','sourcedesk.main:create_app','--factory','--host','127.0.0.1','--port',"$Port")
    if ($Reload) { $apiArgs += '--reload' }
    & uv @apiArgs
    if ($LASTEXITCODE -ne 0) { throw 'API process exited with an error.' }
} finally {
    if (-not $keeper.HasExited) { Stop-Process -Id $keeper.Id -ErrorAction SilentlyContinue }
}
