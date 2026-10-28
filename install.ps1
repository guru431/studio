# Установка моста studio-agent: среда .venv рядом, пакеты, запуск без консоли.
# Повторный запуск безопасен: среда остаётся, пакеты догоняют requirements.txt.
# Автозапуск ставит окно настройки (галочка «Запускать при входе в Windows»).
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Python <путь к python.exe>]
param(
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$venv = Join-Path $repo '.venv'
$py = Join-Path $venv 'Scripts\python.exe'
$pyw = Join-Path $venv 'Scripts\pythonw.exe'

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe завершился с кодом $LASTEXITCODE" }
}

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

Start-Process -FilePath $pyw -ArgumentList ('"{0}"' -f (Join-Path $repo 'studio_agent.pyw')) -WorkingDirectory $repo
Write-Host 'Мост запущен: значок в трее. Не настроен — окно настройки откроется само.'
Write-Host 'Мост уже работал — он скажет «Мост уже запущен»: выйдите из него в трее и запустите install.ps1 снова.'
