# Download RE-UE4SS experimental (or a pinned asset) into tools/UE4SS
param(
    [string]$OutDir = "",
    [string]$Tag = "experimental-latest"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $OutDir) {
    $OutDir = Join-Path $Root "tools\UE4SS"
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$api = "https://api.github.com/repos/UE4SS-RE/RE-UE4SS/releases/tags/$Tag"
Write-Host "Fetching release metadata: $Tag"
try {
    $release = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "UnrealEngineMCP" }
} catch {
    # Fall back to latest release if experimental tag API shape differs
    Write-Host "Tag fetch failed, trying /releases/latest"
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/UE4SS-RE/RE-UE4SS/releases/latest" -Headers @{ "User-Agent" = "UnrealEngineMCP" }
}

$asset = $release.assets | Where-Object {
    $_.name -match '^UE4SS_.*\.zip$' -and $_.name -notmatch 'zDEV' -and $_.name -notmatch 'zCustom'
} | Select-Object -First 1

if (-not $asset) {
    $asset = $release.assets | Where-Object { $_.name -match '\.zip$' } | Select-Object -First 1
}
if (-not $asset) {
    throw "No UE4SS zip asset found on release."
}

$zipPath = Join-Path $OutDir $asset.name
Write-Host "Downloading $($asset.name) ..."
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -UseBasicParsing

$extract = Join-Path $OutDir "extracted"
if (Test-Path $extract) {
    Remove-Item -Recurse -Force $extract
}
Expand-Archive -Path $zipPath -DestinationPath $extract -Force
Write-Host "UE4SS downloaded to $extract"
Write-Output $extract
