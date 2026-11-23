# Сборка запускателя «Studio Agent.exe» из ZIP моста: значок и версия — из того же пакета. Только штатный .NET Framework.
param(
    [Parameter(Mandatory = $true)][string]$Archive,
    [Parameter(Mandatory = $true)][string]$Output
)

$ErrorActionPreference = 'Stop'
$archivePath = (Resolve-Path -LiteralPath $Archive).Path
$outputPath = [IO.Path]::GetFullPath($Output)
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Для сборки нужен .NET Framework 4.5+ на Windows.' }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$work = Join-Path ([IO.Path]::GetTempPath()) ('studio-agent-launcher-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($work) | Out-Null
try {
    $zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $init = $zip.GetEntry('studio-agent/studio_agent/__init__.py')
        $icon = $zip.GetEntry('studio-agent/studio_agent/studio-agent.ico')
        if ($null -eq $init -or $null -eq $icon) { throw 'В пакете нет версии или значка моста' }
        $reader = New-Object IO.StreamReader($init.Open())
        try { $text = $reader.ReadToEnd() } finally { $reader.Dispose() }
        [IO.Compression.ZipFileExtensions]::ExtractToFile($icon, (Join-Path $work 'studio-agent.ico'))
    } finally { $zip.Dispose() }
    $match = [regex]::Match($text, '(?m)^VERSION = "([0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5})"\r?$')
    if (-not $match.Success) { throw 'Версия моста не прочитана' }
    $version = $match.Groups[1].Value
    $attributes = "[assembly: System.Reflection.AssemblyVersion(`"$version.0`")]`n" +
        "[assembly: System.Reflection.AssemblyFileVersion(`"$version.0`")]`n" +
        "[assembly: System.Reflection.AssemblyInformationalVersion(`"$version`")]`n"
    [IO.File]::WriteAllText((Join-Path $work 'version.cs'), $attributes, (New-Object Text.UTF8Encoding($false)))
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($outputPath)) | Out-Null
    & $compiler /nologo /target:winexe /platform:anycpu /optimize+ /utf8output `
        "/win32icon:$(Join-Path $work 'studio-agent.ico')" "/out:$outputPath" `
        (Join-Path $PSScriptRoot 'agent-launcher.cs') (Join-Path $work 'version.cs')
    if ($LASTEXITCODE -ne 0) { throw "Сборка запускателя завершилась с кодом $LASTEXITCODE" }
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "Запускатель собран: $outputPath ($version)"
