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
    if ($Python) {
        Invoke-Checked $Python @('-m', 'venv', $venv)
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        Invoke-Checked 'py' @('-3.14', '-m', 'venv', $venv)
    } else {
        Invoke-Checked 'python' @('-m', 'venv', $venv)
    }
}

Invoke-Checked $py @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '-r', (Join-Path $repo 'requirements.txt'))

Start-Process -FilePath $pyw -ArgumentList ('"{0}"' -f (Join-Path $repo 'studio_agent.pyw')) -WorkingDirectory $repo
Write-Host 'Мост запущен: значок в трее. Не настроен — окно настройки откроется само.'
Write-Host 'Мост уже работал — он скажет «Мост уже запущен»: выйдите из него в трее и запустите install.ps1 снова.'
