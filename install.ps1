# Установка моста studio-agent: среда .venv, пакеты, запуск без консоли.
# Из архива студии (рядом нет .git) — в %LOCALAPPDATA%\Programs\studio-agent: работающий оттуда мост
# останавливается, файлы архива копируются поверх (среда .venv остаётся), «Mark of the Web» снимается.
# Из git-копии — на месте (разработка). Повторный запуск безопасен: среда остаётся, пакеты догоняют
# requirements.txt. Автозапуск ставит окно моста и указывает на ту копию, из которой мост запущен; установка в
# Programs уже включённый автозапуск (значение StudioAgent в Run) переписывает на себя, не включённый — не заводит.
#   install.cmd  (двойной щелчок)  или  powershell -ExecutionPolicy Bypass -File install.ps1 [-Python <python.exe>]
#   -NoVenv — без среды и пакетов, -NoStart — без запуска, -RunKey — ключ автозапуска (тесты установки).
param(
    [string]$Python = '',
    [string]$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
    [switch]$NoVenv,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe завершился с кодом $LASTEXITCODE" }
}

function Stop-Bridge([string]$Dir) {
    # Мост этой копии: pythonw.exe среды и процесс базового Python, который он запускает, — оба с
    # studio_agent.pyw этой папки в командной строке.
    $script = Join-Path $Dir 'studio_agent.pyw'
    $found = @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($script, [StringComparison]::OrdinalIgnoreCase) -ge 0 })
    foreach ($p in $found) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    foreach ($p in $found) {
        Wait-Process -Id $p.ProcessId -Timeout 15 -ErrorAction SilentlyContinue
    }
    if ($found.Count) { Write-Host 'Работавший мост остановлен.' }
}

if (Test-Path -LiteralPath (Join-Path $source '.git')) {
    $repo = $source
} else {
    $repo = Join-Path $env:LOCALAPPDATA 'Programs\studio-agent'
    $from = [IO.Path]::GetFullPath($source).TrimEnd('\')
    $to = [IO.Path]::GetFullPath($repo).TrimEnd('\')
    if ($from -ne $to) {
        Stop-Bridge $to
        New-Item -ItemType Directory -Force -Path $to | Out-Null
        Get-ChildItem -LiteralPath $from -Force | Where-Object { $_.Name -ne '.venv' } |
            Copy-Item -Destination $to -Recurse -Force
        Write-Host "Мост скопирован в $to"
    }
    Get-ChildItem -LiteralPath $to -Recurse -File -Force |
        Where-Object { $_.FullName -notlike (Join-Path $to '.venv\*') } | Unblock-File
    # Автозапуск включён (git-копией или прежней установкой) — теперь он запускает эту копию, как autostart.command().
    if ($null -ne (Get-ItemProperty -LiteralPath $RunKey -Name 'StudioAgent' -ErrorAction SilentlyContinue)) {
        $command = '"{0}" "{1}"' -f (Join-Path $to '.venv\Scripts\pythonw.exe'), (Join-Path $to 'studio_agent.pyw')
        Set-ItemProperty -LiteralPath $RunKey -Name 'StudioAgent' -Value $command
        Write-Host 'Автозапуск теперь запускает установленную копию.'
    }
}

$venv = Join-Path $repo '.venv'
$py = Join-Path $venv 'Scripts\python.exe'
$pyw = Join-Path $venv 'Scripts\pythonw.exe'

if (-not $NoVenv) {
    if (-not (Test-Path -LiteralPath $py)) {
        # Python для среды: -Python, иначе новейший 3.x лаунчера py, нет лаунчера — python из PATH.
        $base = @()
        if ($Python) {
            $exe = $Python
        } elseif (Get-Command py -ErrorAction SilentlyContinue) {
            $exe = 'py'
            $base = @('-3')
        } else {
            $exe = 'python'
        }
        $ok = $false
        try {
            & $exe ($base + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'))
            $ok = ($LASTEXITCODE -eq 0)
        } catch {
            $ok = $false
        }
        if (-not $ok) {
            throw "Нужен Python 3.11 или новее ($exe $base не подходит): поставьте его или укажите -Python <путь к python.exe>"
        }
        Invoke-Checked $exe ($base + @('-m', 'venv', $venv))
    }
    Invoke-Checked $py @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '-r', (Join-Path $repo 'requirements.txt'))
}

if (-not $NoStart) {
    Start-Process -FilePath $pyw -ArgumentList ('"{0}"' -f (Join-Path $repo 'studio_agent.pyw')) -WorkingDirectory $repo
    Write-Host 'Мост запущен: значок в трее. Не подключён — окно моста откроется само.'
    if ($repo -eq $source) {
        Write-Host 'Мост уже работал — он скажет «Мост уже запущен»: выйдите из него в трее и запустите install.ps1 снова.'
    }
}
