# Fetch the pinned third-party sources into tools\textures\third_party (git-ignored). Versions: THIRD_PARTY.md.
# The two sources come down as release archives and are unpacked with Windows' own curl.exe and tar.exe, so a build needs no Git install.
# Safe to interrupt and to run twice: a download goes to <name>.part and is moved into place only once its SHA-256 matches the pinned one
# (retried a few times), an archive is unpacked into a staging folder and moved into place only when the files the build needs are there,
# and a folder that exists without those files is thrown away and fetched again.
$ErrorActionPreference = 'Stop'
$tp = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\third_party'))
New-Item -ItemType Directory -Force $tp | Out-Null
$attempts = 4

function Get-Pinned {
    param([string]$Url, [string]$Dest, [string]$Sha256)
    $part = "$Dest.part"
    for ($n = 1; $n -le $attempts; $n++) {
        Remove-Item $part -Force -ErrorAction SilentlyContinue
        & curl.exe -sS -L --fail --retry 2 --retry-delay 3 -o $part $Url
        $err = if ($LASTEXITCODE -ne 0) { "curl exited $LASTEXITCODE" } else { $null }
        if (-not $err) {
            $h = (Get-FileHash $part -Algorithm SHA256).Hash
            if ($h -ieq $Sha256) { Move-Item -Force $part $Dest; return }
            $err = "sha256 of the download is $h, and the pinned one is $Sha256"
        }
        Remove-Item $part -Force -ErrorAction SilentlyContinue
        if ($n -eq $attempts) { throw "could not download $Url after $attempts attempts: $err" }
        Write-Warning "download of $Url failed (attempt $n of $attempts): $err"
        Start-Sleep -Seconds (2 * $n)
    }
}

function Get-Source {
    param([string]$Url, [string]$Sha256, [string]$Name, [string]$Inner, [string]$Folder, [string]$Required)
    $dest = Join-Path $tp $Folder
    if (Test-Path (Join-Path $dest $Required)) { return }
    if (Test-Path $dest) {
        Write-Host "$Folder is incomplete (no $Required); discarding it and fetching again"
        Remove-Item -Recurse -Force $dest
    }
    $archive = Join-Path $tp "$Name.tar.gz"
    Get-Pinned $Url $archive $Sha256
    $stage = Join-Path $tp "$Folder.partial"
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $stage | Out-Null
    & tar.exe -xzf $archive -C $stage
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $stage "$Inner\$Required"))) {
        Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
        Remove-Item -Force $archive -ErrorAction SilentlyContinue
        throw "unpacking $Name did not give $Inner\$Required; run this again"
    }
    Move-Item (Join-Path $stage $Inner) $dest
    Remove-Item -Recurse -Force $stage
    Remove-Item -Force $archive
}

Get-Source -Url 'https://github.com/ARM-software/astc-encoder/archive/refs/tags/5.7.1.tar.gz' `
    -Sha256 '37ff3928c4c156fb3ac0d1adb5df144ff9c5cdbc8a8e1ffeb20229cc7f35c767' `
    -Name 'astc-encoder-5.7.1' -Inner 'astc-encoder-5.7.1' -Folder 'astc-encoder' -Required 'Source\astcenc.h'

Get-Source -Url 'https://github.com/richgel999/bc7enc_rdo/archive/b9438627eef73a1157e84201b6fa6eb2ffd6d9f0.tar.gz' `
    -Sha256 'bbb33d1dbb6178a3a2c044956c3cc0b8ea9fc2f2903824379dd86f87d53b64c6' `
    -Name 'bc7enc_rdo-b9438627' -Inner 'bc7enc_rdo-b9438627eef73a1157e84201b6fa6eb2ffd6d9f0' -Folder 'bc7enc_rdo' -Required 'CMakeLists.txt'

$ispcExe = "$tp\ispc\ispc-v1.32.0-windows\bin\ispc.exe"
if (-not (Test-Path $ispcExe)) {
    Remove-Item -Recurse -Force "$tp\ispc" -ErrorAction SilentlyContinue
    $zip = Join-Path $tp 'ispc-v1.32.0-windows.zip'
    Get-Pinned 'https://github.com/ispc/ispc/releases/download/v1.32.0/ispc-v1.32.0-windows.zip' $zip '148D5CD96ABE7501C730D5FBD00F1F0C893B96BFEADA38D1811C9B6251E86A5C'
    $stage = "$tp\ispc.partial"
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    Expand-Archive $zip $stage
    if (-not (Test-Path "$stage\ispc-v1.32.0-windows\bin\ispc.exe")) {
        Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
        throw "the ispc archive unpacked without ispc-v1.32.0-windows\bin\ispc.exe; delete $zip and run this again"
    }
    Move-Item $stage "$tp\ispc"
    Remove-Item -Force $zip
}
Write-Output "third_party ready in $tp"
