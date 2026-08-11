<#
.SYNOPSIS
  Install UE4SS (optional download) + UnrealEngineMCP Lua bridge into a UE game.

.EXAMPLE
  .\Install-ToGame.ps1 -GameDir "D:\SteamLibrary\steamapps\common\SpongeBob SquarePants Battle for Bikini Bottom - Rehydrated"

.EXAMPLE
  # Goat Simulator 3 (safe hooks + confirmed experimental build)
  .\Install-ToGame.ps1 `
    -GameDir "D:\Program Files\EpicGames\GoatSimulator3" `
    -Profile GoatSimulator3 `
    -Ue4ssZip "tools\UE4SS\UE4SS_v3.0.1-942-gc0335505.zip"
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$GameDir,

    [string]$Win64Dir = "",

    [switch]$DownloadUE4SS,

    [switch]$SkipUE4SS,

    # tools/game-profiles/<name> — applies UE4SS-settings.ini, mods.txt, no_hooks.txt
    [string]$Profile = "",

    # Optional path to a UE4SS zip (relative to repo root or absolute)
    [string]$Ue4ssZip = "",

    # Only UnrealEngineMCP + Keybinds (also applied when Profile provides mods.txt)
    [switch]$MinimalMods
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$BridgeSrc = Join-Path $Root "bridge\UnrealEngineMCP"
$ProfilesRoot = Join-Path $Root "tools\game-profiles"

if (-not (Test-Path $GameDir)) {
    throw "GameDir not found: $GameDir"
}
if (-not (Test-Path $BridgeSrc)) {
    throw "Bridge source missing: $BridgeSrc"
}

function Find-Win64([string]$root) {
    $shipping = Get-ChildItem -Path $root -Filter "*-Win64-Shipping.exe" -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($shipping) {
        return $shipping.Directory.FullName
    }
    $win64 = Get-ChildItem -Path $root -Directory -Filter "Win64" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { @(Get-ChildItem $_.FullName -Filter "*.exe" -ErrorAction SilentlyContinue).Count -gt 0 } |
        Select-Object -First 1
    if ($win64) {
        return $win64.FullName
    }
    throw "Could not locate Win64 binaries under $root"
}

function Resolve-Ue4ssRoot([string]$win64) {
    # UE4SS 3.x layout: Win64\dwmapi.dll + Win64\ue4ss\{UE4SS.dll, Mods, ...}
    $nested = Join-Path $win64 "ue4ss"
    if ((Test-Path (Join-Path $nested "UE4SS.dll")) -or (Test-Path (Join-Path $nested "Mods"))) {
        return $nested
    }
    # Older flat layout: everything in Win64
    if ((Test-Path (Join-Path $win64 "UE4SS.dll")) -or (Test-Path (Join-Path $win64 "Mods"))) {
        return $win64
    }
    # Prefer nested path for fresh installs of modern packages
    return $nested
}

function Resolve-ProfileDir([string]$name) {
    if (-not $name) { return $null }
    $dir = Join-Path $ProfilesRoot $name
    if (-not (Test-Path $dir)) {
        throw "Profile not found: $dir"
    }
    return $dir
}

function Auto-DetectProfile([string]$gameDir, [string]$win64) {
    $blob = ($gameDir + " " + $win64).ToLowerInvariant()
    if ($blob -match "goatsimulator3|goat simulator 3|\\goat2\\") {
        return "GoatSimulator3"
    }
    return ""
}

if (-not $Win64Dir) {
    $Win64Dir = Find-Win64 $GameDir
}
Write-Host "Win64: $Win64Dir"

if (-not $Profile) {
    $Profile = Auto-DetectProfile $GameDir $Win64Dir
    if ($Profile) {
        Write-Host "Auto-detected profile: $Profile"
    }
}
$ProfileDir = Resolve-ProfileDir $Profile
if ($ProfileDir) {
    Write-Host "Using profile: $ProfileDir"
    if (-not $MinimalMods -and (Test-Path (Join-Path $ProfileDir "mods.txt"))) {
        $MinimalMods = $true
    }
}

# --- UE4SS ---
if (-not $SkipUE4SS) {
    $ue4ssProbe = Resolve-Ue4ssRoot $Win64Dir
    $hasUe4ss = (Test-Path (Join-Path $Win64Dir "dwmapi.dll")) -and (
        (Test-Path (Join-Path $ue4ssProbe "UE4SS.dll")) -or (Test-Path (Join-Path $Win64Dir "UE4SS.dll"))
    )

    if ($Ue4ssZip) {
        if (-not [System.IO.Path]::IsPathRooted($Ue4ssZip)) {
            $Ue4ssZip = Join-Path $Root $Ue4ssZip
        }
        if (-not (Test-Path $Ue4ssZip)) {
            throw "Ue4ssZip not found: $Ue4ssZip"
        }
        $extract = Join-Path $Root "tools\UE4SS\extracted_install"
        if (Test-Path $extract) {
            Remove-Item -Recurse -Force $extract
        }
        Expand-Archive -Path $Ue4ssZip -DestinationPath $extract -Force
        Write-Host "Copying UE4SS from zip $Ue4ssZip -> $Win64Dir"
        Copy-Item -Path (Join-Path $extract "*") -Destination $Win64Dir -Recurse -Force
    }
    elseif ($DownloadUE4SS -or -not $hasUe4ss) {
        $dl = Join-Path $Root "scripts\Download-UE4SS.ps1"
        $extract = (& $dl | Select-Object -Last 1).ToString().Trim()
        if (-not (Test-Path $extract)) {
            throw "Download-UE4SS did not return a valid extract path: $extract"
        }
        Write-Host "Copying UE4SS from $extract -> $Win64Dir"
        Copy-Item -Path (Join-Path $extract "*") -Destination $Win64Dir -Recurse -Force
    }
    else {
        Write-Host "UE4SS already present in Win64 (skipping download)."
    }
}

$Ue4ssRoot = Resolve-Ue4ssRoot $Win64Dir
Write-Host "UE4SS root: $Ue4ssRoot"
New-Item -ItemType Directory -Force -Path $Ue4ssRoot | Out-Null

# --- Profile: settings + optional extras into ue4ss root ---
if ($ProfileDir) {
    $settingsSrc = Join-Path $ProfileDir "UE4SS-settings.ini"
    if (Test-Path $settingsSrc) {
        $destSettings = Join-Path $Ue4ssRoot "UE4SS-settings.ini"
        Copy-Item $settingsSrc $destSettings -Force
        Write-Host "Applied profile UE4SS-settings.ini"
    }
    # Copy any extra folders/files except known control files
    Get-ChildItem $ProfileDir -Force | Where-Object {
        $_.Name -notin @("UE4SS-settings.ini", "mods.txt", "mods.json", "no_hooks.txt", "README.md")
    } | ForEach-Object {
        $dest = Join-Path $Ue4ssRoot $_.Name
        Copy-Item $_.FullName $dest -Recurse -Force
        Write-Host "Profile copy: $($_.Name)"
    }
}

# --- Mod ---
$modsDir = Join-Path $Ue4ssRoot "Mods"
New-Item -ItemType Directory -Force -Path $modsDir | Out-Null
$destMod = Join-Path $modsDir "UnrealEngineMCP"
if (Test-Path $destMod) {
    Remove-Item -Recurse -Force $destMod
}
Copy-Item -Path $BridgeSrc -Destination $destMod -Recurse -Force
if (-not (Test-Path (Join-Path $destMod "scripts\main.lua"))) {
    throw "main.lua missing after copy to $destMod"
}
"1" | Set-Content -Path (Join-Path $destMod "enabled.txt") -Encoding ASCII

if ($ProfileDir -and (Test-Path (Join-Path $ProfileDir "no_hooks.txt"))) {
    Copy-Item (Join-Path $ProfileDir "no_hooks.txt") (Join-Path $destMod "no_hooks.txt") -Force
    Write-Host "Installed no_hooks.txt (delay-only pump; no Lua RegisterHook)"
}

# mods.txt
$modsTxt = Join-Path $modsDir "mods.txt"
$profileMods = if ($ProfileDir) { Join-Path $ProfileDir "mods.txt" } else { $null }

if ($profileMods -and (Test-Path $profileMods)) {
    Copy-Item $profileMods $modsTxt -Force
    Write-Host "Applied profile mods.txt"
}
$profileModsJson = if ($ProfileDir) { Join-Path $ProfileDir "mods.json" } else { $null }
if ($profileModsJson -and (Test-Path $profileModsJson)) {
    Copy-Item $profileModsJson (Join-Path $modsDir "mods.json") -Force
    Write-Host "Applied profile mods.json"
}
elseif ($MinimalMods) {
    @(
        "CheatManagerEnablerMod : 0",
        "ConsoleCommandsMod : 0",
        "ConsoleEnablerMod : 0",
        "SplitScreenMod : 0",
        "LineTraceMod : 0",
        "BPML_GenericFunctions : 0",
        "BPModLoaderMod : 0",
        "UnrealEngineMCP : 1",
        "Keybinds : 1"
    ) | Set-Content $modsTxt -Encoding UTF8
    Write-Host "Wrote minimal mods.txt"
}
else {
    $line = "UnrealEngineMCP : 1"
    if (Test-Path $modsTxt) {
        $rawLines = Get-Content $modsTxt
        $found = $false
        $out = foreach ($l in $rawLines) {
            if ($l -match "^\s*UnrealEngineMCP\s*:") {
                $found = $true
                $line
            }
            else {
                $l
            }
        }
        if (-not $found) {
            $out = @($out) + $line
        }
        $out | Set-Content $modsTxt -Encoding UTF8
    }
    else {
        @(
            "CheatManagerEnablerMod : 1",
            "ConsoleCommandsMod : 1",
            "ConsoleEnablerMod : 1",
            "SplitScreenMod : 0",
            "LineTraceMod : 1",
            "BPML_GenericFunctions : 1",
            "BPModLoaderMod : 1",
            "UnrealEngineMCP : 1",
            "Keybinds : 1"
        ) | Set-Content $modsTxt -Encoding UTF8
    }
}

# Ensure stock mods that are disabled do not re-enable via enabled.txt alone
# (UE4SS also reads mods.txt load order.)

# Also enable via mods.json if present (UE4SS 3.x — array or object map)
$modsJson = Join-Path $modsDir "mods.json"
if (Test-Path $modsJson) {
    try {
        $raw = Get-Content $modsJson -Raw | ConvertFrom-Json
        $minimal = $MinimalMods -or ($profileMods -and (Test-Path $profileMods))
        $disable = @(
            "CheatManagerEnablerMod", "ConsoleCommandsMod", "ConsoleEnablerMod",
            "SplitScreenMod", "LineTraceMod", "BPML_GenericFunctions", "BPModLoaderMod"
        )
        if ($raw -is [System.Array]) {
            $foundMcp = $false
            foreach ($entry in $raw) {
                $n = $entry.mod_name
                if ($n -eq "UnrealEngineMCP") {
                    $entry.mod_enabled = $true
                    $foundMcp = $true
                }
                elseif ($n -eq "Keybinds") {
                    $entry.mod_enabled = $true
                }
                elseif ($minimal -and ($disable -contains $n)) {
                    $entry.mod_enabled = $false
                }
            }
            if (-not $foundMcp) {
                $raw = @($raw) + [pscustomobject]@{ mod_name = "UnrealEngineMCP"; mod_enabled = $true }
            }
            ($raw | ConvertTo-Json -Depth 8) | Set-Content $modsJson -Encoding UTF8
        }
        elseif ($raw -is [System.Management.Automation.PSCustomObject]) {
            $raw | Add-Member -NotePropertyName "UnrealEngineMCP" -NotePropertyValue @{ mod_enabled = $true } -Force
            if ($minimal) {
                foreach ($name in $disable) {
                    if ($raw.PSObject.Properties.Name -contains $name) {
                        $raw.$name = @{ mod_enabled = $false }
                    }
                }
            }
            ($raw | ConvertTo-Json -Depth 8) | Set-Content $modsJson -Encoding UTF8
        }
    }
    catch {
        Write-Host "Warning: could not update mods.json: $_"
    }
}

# IPC dir always next to shipping exe (stable path for Python)
$ipc = Join-Path $Win64Dir "UnrealEngineMCP_IPC"
New-Item -ItemType Directory -Force -Path $ipc | Out-Null
"" | Set-Content (Join-Path $ipc ".keep") -Encoding ASCII

# Remove mistaken top-level Mods-only install if ue4ss nested is the real root
$legacyMods = Join-Path $Win64Dir "Mods\UnrealEngineMCP"
if ((Test-Path $legacyMods) -and ($Ue4ssRoot -ne $Win64Dir)) {
    Write-Host "Removing legacy mod path: $legacyMods"
    Remove-Item -Recurse -Force $legacyMods -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Installed UnrealEngineMCP to:"
Write-Host "  $destMod"
Write-Host "IPC directory:"
Write-Host "  $ipc"
if ($Profile) {
    Write-Host "Profile: $Profile"
}
Write-Host ""
Write-Host "Next:"
Write-Host "  1. Launch the game (you launch - agents do not)."
Write-Host "  2. Wait ~45s / ready:true; check ue4ss\UE4SS.log if crash."
Write-Host "  3. python translator\unreal_mcp_server.py --ipc-dir `"$ipc`""
Write-Host "  4. python tests\smoke_live.py --ipc-dir `"$ipc`""
