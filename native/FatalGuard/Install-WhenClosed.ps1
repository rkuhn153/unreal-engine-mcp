# Swap staged FatalGuard main.dll when the game is not locking it.
param(
    [string]$GameWin64 = "D:\SteamLibrary\steamapps\common\SpongeBob SquarePants Battle for Bikini Bottom - Rehydrated\Pineapple\Binaries\Win64"
)

$ErrorActionPreference = "Stop"
$dllDir = Join-Path $GameWin64 "ue4ss\Mods\FatalGuard\dlls"
$new = Join-Path $dllDir "main.dll.new"
$dst = Join-Path $dllDir "main.dll"
$repo = Join-Path $PSScriptRoot "bin\main.dll"

if (-not (Test-Path $new)) {
    if (Test-Path $repo) {
        Copy-Item $repo $new -Force
    } else {
        throw "No main.dll.new and no repo bin\main.dll - build first."
    }
}

$procs = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessName -match 'Pineapple|SpongeBob|Shipping'
}
if ($procs) {
    Write-Host "Game still running (PIDs: $($procs.Id -join ', ')). Close it, then re-run."
    exit 2
}

Copy-Item $new $dst -Force
$len = (Get-Item $dst).Length
$when = (Get-Item $dst).LastWriteTime
Write-Host "Installed FatalGuard -> $dst ($len bytes, $when)"
Write-Host "Relaunch the game. process_alive.json should show version:310, version_semver:3.1.0-beta.1, autokick:true."
