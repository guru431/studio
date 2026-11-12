# Сборка одного EXE из ZIP того же коммита. Только штатный .NET Framework Windows, без загрузок.
param(
    [Parameter(Mandatory = $true)][string]$Archive,
    [Parameter(Mandatory = $true)][string]$Output
)

$ErrorActionPreference = 'Stop'
$archivePath = (Resolve-Path -LiteralPath $Archive).Path
$outputPath = [IO.Path]::GetFullPath($Output)
if ($archivePath -eq $outputPath) { throw 'EXE и ZIP должны быть разными файлами.' }
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Для сборки нужен .NET Framework 4.5+ на Windows.' }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
try {
    foreach ($name in @('install.ps1', 'studio_agent.pyw', 'requirements.txt', 'constraints.txt', 'studio_agent/__init__.py')) {
        if ($null -eq $zip.GetEntry("studio-agent/$name")) { throw "В пакете нет $name" }
    }
} finally { $zip.Dispose() }

[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($outputPath)) | Out-Null
& $compiler /nologo /target:exe /platform:anycpu /optimize+ /utf8output `
    /reference:System.IO.Compression.dll /reference:System.IO.Compression.FileSystem.dll `
    "/resource:$archivePath,studio-agent.zip" "/out:$outputPath" (Join-Path $PSScriptRoot 'agent-installer.cs')
if ($LASTEXITCODE -ne 0) { throw "Сборка установщика завершилась с кодом $LASTEXITCODE" }
Write-Host "Установщик собран: $outputPath"
