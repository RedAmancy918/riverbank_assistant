$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
npm ci
npm run dist:win
Write-Host "Build complete. Installers are in: $PSScriptRoot\dist" -ForegroundColor Green
