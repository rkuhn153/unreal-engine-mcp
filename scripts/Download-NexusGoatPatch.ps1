<#
.SYNOPSIS
  Download UE4SS GOAT Patch (Nexus mod 42) using a free-account browser key.

Free Nexus accounts cannot use download_link.json without key+expires.
Flow:
  1. Open the mod files page in your browser (logged into Nexus).
  2. Click Manual download once — the URL contains key= and expires=.
  3. Pass that URL, OR pass -Key and -Expires from the URL query string.
  4. This script downloads into tools/game-profiles/GoatSimulator3/nexus_goat_patch
     and can install into a Win64 tree.

.EXAMPLE
  .\Download-NexusGoatPatch.ps1 -DownloadUrl "https://..." -InstallToWin64 "D:\...\Win64"
#>
param(
    [string]$ApiKey = "",
    [string]$DownloadUrl = "",
    [string]$Key = "",
    [string]$Expires = "",
    [int]$FileId = 59,
    [string]$InstallToWin64 = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$OutDir = Join-Path $Root "tools\game-profiles\GoatSimulator3\nexus_goat_patch"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$headers = @{
    "Application-Name"    = "UnrealEngineMCP"
    "Application-Version" = "1.0"
    "Accept"              = "application/json"
    "User-Agent"          = "UnrealEngineMCP/1.0"
}
if ($ApiKey) {
    $headers["apikey"] = $ApiKey
}

$zip = Join-Path $OutDir "goat_patch.zip"

if ($DownloadUrl) {
    Write-Host "Downloading from provided URL..."
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $zip -UseBasicParsing -Headers @{ "User-Agent" = "UnrealEngineMCP" }
}
elseif ($Key -and $Expires) {
    if (-not $ApiKey) {
        throw "ApiKey required with -Key/-Expires (free-account API download)."
    }
    $uri = "https://api.nexusmods.com/v1/games/goatsimulator3/mods/42/files/$FileId/download_link.json?key=$Key&expires=$Expires"
    $links = Invoke-RestMethod -Uri $uri -Headers $headers
    $url = if ($links -is [System.Array]) { $links[0].URI } else { $links.URI }
    Write-Host "Got CDN link, downloading..."
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
}
else {
    Write-Host @"
Cannot download: free Nexus accounts need a one-time browser download URL.

1. Open: https://www.nexusmods.com/goatsimulator3/mods/42?tab=files
2. Manual Download the MAIN file.
3. From the download manager / browser history, copy the CDN URL (contains key= and expires=)
   OR drop the zip as:
     $zip
4. Re-run:
     .\Download-NexusGoatPatch.ps1 -DownloadUrl '<url>' -InstallToWin64 '<Win64>'
   OR if you already saved the zip to that path:
     .\Download-NexusGoatPatch.ps1 -InstallToWin64 '<Win64>'
"@
    if (-not (Test-Path $zip)) {
        return
    }
    Write-Host "Found existing zip at $zip — continuing extract/install."
}

if (-not (Test-Path $zip)) {
    throw "Zip missing: $zip"
}

$ex = Join-Path $OutDir "extracted"
if (Test-Path $ex) { Remove-Item $ex -Recurse -Force }
Expand-Archive $zip $ex -Force
Write-Host "Extracted:"
Get-ChildItem $ex -Recurse | ForEach-Object { Write-Host "  $($_.FullName)" }

if ($InstallToWin64) {
    if (-not (Test-Path $InstallToWin64)) {
        throw "InstallToWin64 not found: $InstallToWin64"
    }
    # Flat stable layout: files go next to UE4SS.dll; nested: under ue4ss\
    $dest = $InstallToWin64
    if (Test-Path (Join-Path $InstallToWin64 "ue4ss\UE4SS.dll")) {
        $dest = Join-Path $InstallToWin64 "ue4ss"
    }
    Copy-Item (Join-Path $ex "*") $dest -Recurse -Force
    Write-Host "Installed GOAT patch files into $dest"
    Write-Host "Files: MemberVariableLayout.ini, VTableLayout.ini, UE4SS-settings.ini (overwritten from patch)"
}

Write-Host "Done."
