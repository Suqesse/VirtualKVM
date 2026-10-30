param([switch]$MediaOnly)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$buildPackages = @('ffmpeg-full')
if (!$MediaOnly) { $buildPackages += 'innosetup' }
$installed = $false
foreach ($attempt in 1..3) {
    Write-Host "Installing native build tools (attempt $attempt of 3)"
    & choco install @buildPackages -y --no-progress
    $buildExit = $LASTEXITCODE
    if ($buildExit -eq 0 -or $buildExit -eq 3010) {
        $installed = $true
        break
    }
    if ($attempt -lt 3) {
        Write-Host "Chocolatey exited with $buildExit; retrying verified package installation"
        Start-Sleep -Seconds (2 * $attempt)
    }
}
if (!$installed) { throw "Chocolatey build-tool installation failed with exit code $buildExit after three attempts" }
$media = Get-ChildItem "$env:ChocolateyInstall\lib\ffmpeg-full" -Filter ffmpeg.exe -Recurse | Select-Object -First 1
if (!$media) { throw 'Real FFmpeg executable missing; launcher shims cannot be bundled' }
$probe = Join-Path $media.DirectoryName 'ffprobe.exe'
if (!(Test-Path $probe)) { throw 'ffprobe missing' }
if (!$MediaOnly) {
    $compiler = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'
    if (!(Test-Path $compiler)) { throw 'Inno Setup compiler missing after package installation' }
}
"VIRTUALKVM_FFMPEG=$($media.FullName)" >> $env:GITHUB_ENV
"VIRTUALKVM_FFPROBE=$probe" >> $env:GITHUB_ENV
$media.DirectoryName >> $env:GITHUB_PATH
Write-Host 'Verified native build-tool paths; application checks will validate media capabilities'
