# Build FatalGuard 3.1.0-beta.1 and install into a game's UE4SS Mods folder.
param(
    [string]$GameWin64 = "D:\SteamLibrary\steamapps\common\SpongeBob SquarePants Battle for Bikini Bottom - Rehydrated\Pineapple\Binaries\Win64",
    [switch]$NoInstall,
    [switch]$Disable
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$RepoBin = Join-Path $Root "bin"
New-Item -ItemType Directory -Force -Path $RepoBin | Out-Null

# Prefer VS2019 BuildTools (complete CRT headers); then VS2022 Preview MSVC.
$msvcCandidates = @(
    "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\VC\Tools\MSVC\14.29.30133",
    "C:\Program Files\Microsoft Visual Studio\2022\Preview\VC\Tools\MSVC\14.40.33617"
)
$msvc = $msvcCandidates | Where-Object { Test-Path "$_\include\excpt.h" } | Select-Object -First 1
if (-not $msvc) {
    throw "No MSVC with CRT headers found (need excpt.h). Install C++ desktop workload."
}

$wkRoot = "C:\Program Files (x86)\Windows Kits\10"
$sdkVer = (Get-ChildItem "$wkRoot\Include" -Directory | Sort-Object Name -Descending | Select-Object -First 1).Name
$cl = Join-Path $msvc "bin\Hostx64\x64\cl.exe"
$link = Join-Path $msvc "bin\Hostx64\x64\link.exe"
if (-not (Test-Path $cl)) { throw "cl.exe missing under $msvc" }

# Short path avoids space-in-path cl bugs
$work = Join-Path $env:TEMP "FatalGuardBuild"
New-Item -ItemType Directory -Force -Path $work | Out-Null
Copy-Item (Join-Path $Root "FatalGuard.cpp") (Join-Path $work "FatalGuard.cpp") -Force

Push-Location $work
try {
    & $cl /nologo /O2 /EHsc /std:c++17 `
        /I"$msvc\include" `
        /I"$wkRoot\Include\$sdkVer\ucrt" `
        /I"$wkRoot\Include\$sdkVer\um" `
        /I"$wkRoot\Include\$sdkVer\shared" `
        /c FatalGuard.cpp /FoFatalGuard.obj
    if ($LASTEXITCODE -ne 0) { throw "compile failed: $LASTEXITCODE" }

    & $link /nologo /DLL /OUT:main.dll `
        /LIBPATH:"$msvc\lib\x64" `
        /LIBPATH:"$wkRoot\Lib\$sdkVer\ucrt\x64" `
        /LIBPATH:"$wkRoot\Lib\$sdkVer\um\x64" `
        FatalGuard.obj user32.lib kernel32.lib
    if ($LASTEXITCODE -ne 0) { throw "link failed: $LASTEXITCODE" }
}
finally {
    Pop-Location
}

$dll = Join-Path $work "main.dll"
if (-not (Test-Path $dll)) { throw "main.dll missing after build" }
Copy-Item $dll (Join-Path $RepoBin "main.dll") -Force
Write-Host "Built $RepoBin\main.dll ($((Get-Item $dll).Length) bytes) using $msvc"

if ($NoInstall) { return }

$modRoot = Join-Path $GameWin64 "ue4ss\Mods\FatalGuard"
$dllDir = Join-Path $modRoot "dlls"
New-Item -ItemType Directory -Force -Path $dllDir | Out-Null
Remove-Item (Join-Path $dllDir "main.dll.off") -Force -ErrorAction SilentlyContinue
Copy-Item $dll (Join-Path $dllDir "main.dll") -Force

$enable = if ($Disable) { "0" } else { "1" }
$enable | Set-Content (Join-Path $modRoot "enabled.txt") -Encoding ASCII

$modsTxt = Join-Path $GameWin64 "ue4ss\Mods\mods.txt"
$line = "FatalGuard : $enable"
if (Test-Path $modsTxt) {
    $raw = @(Get-Content $modsTxt)
    if (($raw -join "`n") -notmatch "FatalGuard") {
        @($raw) + @($line) | Set-Content $modsTxt -Encoding ASCII
    } else {
        $raw | ForEach-Object {
            if ($_ -match "^\s*FatalGuard\s*:") { $line } else { $_ }
        } | Set-Content $modsTxt -Encoding ASCII
    }
} else {
    @"
UnrealEngineMCP : 1
Keybinds : 1
FatalGuard : $enable
"@ | Set-Content $modsTxt -Encoding ASCII
}

Write-Host "Installed FatalGuard : $enable -> $modRoot"
Write-Host "Log: $GameWin64\FatalGuard.log"
Write-Host "Alive: $GameWin64\UnrealEngineMCP_IPC\process_alive.json"
Write-Host "Env: FATALGUARD=0 | FATALGUARD_DELAY_MS=45000 | FATALGUARD_MSGBOX=1 | FATALGUARD_AUTOKICK=0"
Write-Host "You launch the game. Hooks arm after delay (default 45s)."
Write-Host "3.1.0-beta.1 auto-kick: revive.flag or stale heartbeat → synthetic Ctrl+F9 (mid-game MCP revive)."
