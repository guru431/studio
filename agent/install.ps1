# Установка моста studio-agent: среда .venv, пакеты, запуск без консоли.
# Из архива студии (рядом нет .git) — в %LOCALAPPDATA%\Programs\studio-agent: работающий оттуда мост
# должен быть закрыт владельцем после завершения заданий; затем файлы копируются поверх (среда остаётся).
# Из git-копии — на месте (разработка). Повторный запуск безопасен: среда остаётся, пакеты догоняют
# requirements.txt. Автозапуск ставит окно моста и указывает на ту копию, из которой мост запущен; установка в
# Programs уже включённый автозапуск (значение StudioAgent в Run) переписывает на себя, не включённый — не заводит,
# и ставит ярлык «Studio Agent» в меню «Пуск» пользователя (%APPDATA%) с той же командой. Команда — запускатель
# «Studio Agent.exe» пакета студии, если он прошёл пробу (--probe → 0), иначе pythonw.exe среды (как autostart.command()).
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
$installed = $false

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe завершился с кодом $LASTEXITCODE" }
}

function Assert-BridgeStopped([string]$Dir) {
    # Both the venv launcher and base Python include this script path. Never terminate either.
    # Самообновлённый мост работает из releases\release-* этой же папки (updater.py) — он тоже «запущен».
    $root = [regex]::Escape([IO.Path]::GetFullPath($Dir).TrimEnd('\'))
    $script = $root + '\\(?:releases\\[^\\"]+\\)?studio_agent\.pyw'
    $found = @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $script })
    if ($found.Count) {
        throw 'Мост запущен. Обновление не выполнено: выберите в меню значка в трее «Выйти после завершения» (заданий нет — «Выход сейчас») и, когда мост закроется, запустите установку снова.'
    }
}

Assert-BridgeStopped $source
if ((Test-Path -LiteralPath (Join-Path $source '.git')) -or
    ((Split-Path -Leaf $source) -eq 'agent' -and
     (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $source) '.git')))) {
    $repo = $source
} else {
    $repo = Join-Path $env:LOCALAPPDATA 'Programs\studio-agent'
    $installed = $true
    $from = [IO.Path]::GetFullPath($source).TrimEnd('\')
    $to = [IO.Path]::GetFullPath($repo).TrimEnd('\')
    if ($from -ne $to) {
        Assert-BridgeStopped $to
        New-Item -ItemType Directory -Force -Path $to | Out-Null
        Get-ChildItem -LiteralPath $from -Force | Where-Object { $_.Name -ne '.venv' } |
            Copy-Item -Destination $to -Recurse -Force
        Write-Host "Мост скопирован в $to"
    }
    Assert-BridgeStopped $to
    Get-ChildItem -LiteralPath $to -Recurse -File -Force |
        Where-Object { $_.FullName -notlike (Join-Path $to '.venv\*') } | Unblock-File
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
    Invoke-Checked $py @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '-r', (Join-Path $repo 'requirements.txt'), '-c', (Join-Path $repo 'constraints.txt'))
}

# Запускатель «Studio Agent.exe» есть только в пакете студии. Автозапуск, ярлык и первый запуск переходят на него лишь
# после пробы: антивирус мог его убрать или запретить — тогда pythonw.exe, как раньше (update_helper.launcher).
$script = Join-Path $repo 'studio_agent.pyw'
$launcher = Join-Path $repo 'Studio Agent.exe'
$ready = $false
if (Test-Path -LiteralPath $launcher) {
    try {
        $ready = (Start-Process -FilePath $launcher -ArgumentList '--probe' -Wait -PassThru -WindowStyle Hidden).ExitCode -eq 0
    } catch {
        $ready = $false
    }
}
if ($ready) { $command = '"{0}"' -f $launcher } else { $command = '"{0}" "{1}"' -f $pyw, $script }

if ($installed) {
    # Автозапуск включён (git-копией или прежней установкой) — теперь он запускает эту копию, как autostart.command().
    if ($null -ne (Get-ItemProperty -LiteralPath $RunKey -Name 'StudioAgent' -ErrorAction SilentlyContinue)) {
        Set-ItemProperty -LiteralPath $RunKey -Name 'StudioAgent' -Value $command
        Write-Host 'Автозапуск теперь запускает установленную копию.'
    }
    # Ярлык «Studio Agent» в меню «Пуск» пользователя — та же команда и папка, что автозапуск, и значок студии
    # (shortcut.target). AppUserModelID ярлыку добавляет сам мост при запуске (autostart.repair): WScript.Shell его не
    # пишет. Самообновление переводит ярлык на новый выпуск; копия из git ярлык не ставит.
    $menu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    New-Item -ItemType Directory -Force -Path $menu | Out-Null
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $menu 'Studio Agent.lnk'))
    if ($ready) {
        $link.TargetPath = $launcher
        $link.Arguments = ''
    } else {
        $link.TargetPath = $pyw
        $link.Arguments = '"{0}"' -f $script
    }
    $link.WorkingDirectory = $repo
    $link.Description = 'Studio Agent'
    $link.IconLocation = '{0},0' -f (Join-Path $repo 'studio_agent\studio-agent.ico')
    $link.Save()
    Write-Host 'Ярлык «Studio Agent» — в меню «Пуск».'
}

if (-not $NoStart) {
    if ($ready) {
        Start-Process -FilePath $launcher -WorkingDirectory $repo
    } else {
        Start-Process -FilePath $pyw -ArgumentList ('"{0}"' -f $script) -WorkingDirectory $repo -WindowStyle Hidden
    }
    Write-Host 'Мост запущен: значок в трее. Не подключён — окно моста откроется само.'
}
