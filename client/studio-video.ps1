<#
.SYNOPSIS
  Пакетное оживление картинок папки через API видео шлюза srvllm (Wan2.2 Remix v3, I2V).

.DESCRIPTION
  Скрипт ходит прямо в шлюз (POST <Url>/videos и т. д., форма OpenAI Videos) ключом владельца
  для картинок; студия в этом не участвует. Работает из домашней сети или через туннель.

  Берутся картинки .jpg .jpeg .png .webp прямо в -Folder (без подпапок), по имени; файлы
  больше 20 МБ пропускаются с предупреждением. Ролики ложатся рядом с картинкой под именами
  <имя>_<N>s_s<seed>.mp4, рядом — журнал video-manifest.tsv (картинка, ролик, seed,
  длительность, качество, дорисовка, вариант, откуда промпт, промпт).

  Промпт картинки: <имя>.txt рядом (UTF-8, целиком) → иначе -Prompt → иначе <имя>.auto.txt
  (промпт, придуманный службой в прошлый раз) → иначе пусто: служба опишет движение по фото
  сама, а скрипт сохранит её промпт в <имя>.auto.txt — повторный запуск отдаст его как свой.

  Задания уходят очередью: скрипт шлёт все, пока служба принимает; «очередь полна» (429) —
  ждёт, пока выйдут отправленные, и досылает. Повторный запуск пропускает ролики, уже
  записанные в журнал с той же картинкой, длительностью, качеством, дорисовкой и номером
  варианта (если файл ролика на месте); -Redo — сделать заново.

  Ключ. В репозитории скрипт без ключа. Один раз соберите локальную копию с ключом из vault:
    powershell -ExecutionPolicy Bypass -File studio-video.ps1 -MakeLocal
  Рядом появится studio-video.local.ps1 (в .gitignore) — дальше запускайте её; её можно
  переносить на другие компьютеры как есть. Ключ на один запуск — параметром -Token.

  Карта одна на всех: пока её держит другой потребитель, ролик ждёт («жду карту»). Первый ролик
  после подъёма слота дольше — загрузка Wan. По окончании карта освобождается и возвращается
  gemma4; -KeepCard — оставить карту. Ctrl+C отменяет все свои незавершённые задания и тоже
  освобождает карту.

.PARAMETER Folder
  Папка с картинками; ролики и журнал ложатся туда же.
.PARAMETER Prompt
  Общий промпт для картинок без своего <имя>.txt.
.PARAMETER Seconds
  Длительность ролика, целые секунды от 1 до 30 (по умолчанию 5). Длиннее 5 с — склейка
  кусков по 5 с, она «уплывает».
.PARAMETER Quality
  fast (короткая сторона 480), standard (576, по умолчанию) или high (720).
.PARAMETER NoSmooth
  Без дорисовки кадров: 16 кадров/с вместо 32.
.PARAMETER Count
  Сколько вариантов на каждую картинку (1–20).
.PARAMETER Seed
  Начальный seed. Пусто — случайный у каждого ролика; задан — seed, seed+1, … для -Count
  вариантов одной картинки.
.PARAMETER Redo
  Делать заново и то, что уже есть в журнале.
.PARAMETER KeepCard
  Не освобождать карту в конце.
.PARAMETER Model
  Модель видео у шлюза.
.PARAMETER Url
  База API шлюза.
.PARAMETER Token
  Ключ шлюза. По умолчанию — вписанный командой -MakeLocal.
.PARAMETER MakeLocal
  Собрать рядом studio-video.local.ps1 с ключом GATEWAY_KEY_OWNER_IMAGES из vault и выйти.
.PARAMETER VaultPath
  Файл vault для -MakeLocal.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File studio-video.ps1 -MakeLocal
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File studio-video.local.ps1 -Folder D:\photos
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File studio-video.local.ps1 -Folder D:\photos -Seconds 8 -Quality high -Count 2 -Seed 100
#>
[CmdletBinding(DefaultParameterSetName = 'Run')]
param(
    [Parameter(ParameterSetName = 'Run', Mandatory = $true)][string]$Folder,
    [Parameter(ParameterSetName = 'Run')][string]$Prompt = '',
    [Parameter(ParameterSetName = 'Run')][ValidateRange(1, 30)][int]$Seconds = 5,
    [Parameter(ParameterSetName = 'Run')][ValidateSet('fast', 'standard', 'high')][string]$Quality = 'standard',
    [Parameter(ParameterSetName = 'Run')][switch]$NoSmooth,
    [Parameter(ParameterSetName = 'Run')][ValidateRange(1, 20)][int]$Count = 1,
    [Parameter(ParameterSetName = 'Run')][Nullable[long]]$Seed = $null,
    [Parameter(ParameterSetName = 'Run')][switch]$Redo,
    [Parameter(ParameterSetName = 'Run')][switch]$KeepCard,
    [Parameter(ParameterSetName = 'Run')][string]$Model = 'wan2.2-remix-i2v-v3',
    [Parameter(ParameterSetName = 'Run')][string]$Url = 'http://10.77.1.15:4000/slot',
    [Parameter(ParameterSetName = 'Run')][string]$Token = '',
    [Parameter(ParameterSetName = 'MakeLocal', Mandatory = $true)][switch]$MakeLocal,
    [Parameter(ParameterSetName = 'MakeLocal')][string]$VaultPath = 'C:\work\projects\local-config\secrets\vault.env'
)
# Строка-заглушка ключа: в репозитории пусто, -MakeLocal вписывает ключ в локальную копию.
$EmbeddedKey = ''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$VaultKey = 'GATEWAY_KEY_OWNER_IMAGES'
$TimeoutSec = 300                      # один запрос к шлюзу: загрузка картинки или выдача mp4 (маршрут шлюза — 180 с)
$PollSec = 5                           # опрос заданий
$MaxPrompt = 4000                      # предел API видео
$MaxImage = 20MB
$MaxSeed = 4294967295
$Mime = @{ '.jpg' = 'image/jpeg'; '.jpeg' = 'image/jpeg'; '.png' = 'image/png'; '.webp' = 'image/webp' }
$Utf8Bom = New-Object Text.UTF8Encoding $true
$Utf8NoBom = New-Object Text.UTF8Encoding $false

if ($MakeLocal) {
    if (-not (Test-Path -LiteralPath $VaultPath)) { throw "Нет файла vault: $VaultPath (путь — параметром -VaultPath)" }
    $line = Select-String -LiteralPath $VaultPath -Pattern "^$VaultKey=" -CaseSensitive -Encoding UTF8 | Select-Object -First 1
    $key = if ($line) { $line.Line.Substring($VaultKey.Length + 1).Trim().Trim('"', "'") } else { '' }
    if (-not $key) { throw "В $VaultPath нет строки $VaultKey=<ключ>: ключ владельца для картинок не выпущен или не внесён в vault" }
    $source = [IO.File]::ReadAllText($PSCommandPath)
    $slot = [Regex]::Matches($source, "(?m)^\`$EmbeddedKey = '(?:[^']|'')*'")
    if ($slot.Count -ne 1) { throw "В $PSCommandPath не найдена строка-заглушка ключа (`$EmbeddedKey = '')" }
    $text = $source.Substring(0, $slot[0].Index) + "`$EmbeddedKey = '" + $key.Replace("'", "''") + "'" +
        $source.Substring($slot[0].Index + $slot[0].Length)
    $target = Join-Path $PSScriptRoot 'studio-video.local.ps1'
    [IO.File]::WriteAllText($target, $text, $Utf8Bom)
    Write-Host "Готово: $target — ключ вписан (файл в .gitignore). Дальше запускайте его."
    return
}

if (-not $Token) { $Token = $EmbeddedKey }
if (-not $Token) {
    throw ("Нет ключа шлюза. Соберите локальную копию с ключом из vault: " +
        "powershell -ExecutionPolicy Bypass -File studio-video.ps1 -MakeLocal, " +
        "затем запускайте studio-video.local.ps1 (или передайте ключ параметром -Token)")
}
$Url = $Url.TrimEnd('/')
if (-not (Test-Path -LiteralPath $Folder -PathType Container)) { throw "Нет папки: $Folder" }
$Folder = (Resolve-Path -LiteralPath $Folder).ProviderPath
$common = $Prompt.Trim()
if ($common.Length -gt $MaxPrompt) { throw "Общий промпт длиннее $MaxPrompt символов" }
if ($null -ne $Seed -and ($Seed -lt 0 -or $Seed + $Count - 1 -gt $MaxSeed)) { throw "Seed — от 0 до $MaxSeed" }
$smoothText = if ($NoSmooth) { 'false' } else { 'true' }

Add-Type -AssemblyName System.Net.Http
$Http = New-Object Net.Http.HttpClient
$Http.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
$Http.DefaultRequestHeaders.Authorization = New-Object Net.Http.Headers.AuthenticationHeaderValue('Bearer', $Token)

# Запрос к шлюзу: код ответа, тело байтами и текстом (UTF-8 — сами, без угадывания кодировки).
# Ждём ответ кусочками по 250 мс: так Ctrl+C прерывает и долгий запрос.
function Invoke-Gateway([string]$Method, [string]$Path, $Json = $null, $Form = $null, [string]$ImageFile = '',
                        [switch]$Binary) {
    # Картинка читается до запроса: сбой чтения — не «шлюз недоступен». Не через if-выражение:
    # оно развернуло бы массив байтов в object[]
    $imageBytes = $null
    if ($ImageFile) { $imageBytes = [IO.File]::ReadAllBytes($ImageFile) }
    $request = New-Object Net.Http.HttpRequestMessage -ArgumentList (New-Object Net.Http.HttpMethod $Method), "$Url$Path"
    $response = $null
    try {
        if ($ImageFile) {
            $content = New-Object Net.Http.MultipartFormDataContent
            foreach ($entry in $Form.GetEnumerator()) {
                $value = [Convert]::ToString($entry.Value, [Globalization.CultureInfo]::InvariantCulture)
                $content.Add((New-Object Net.Http.StringContent($value, [Text.Encoding]::UTF8)), $entry.Key)
            }
            $ext = [IO.Path]::GetExtension($ImageFile).ToLowerInvariant()
            $file = New-Object Net.Http.ByteArrayContent -ArgumentList (,$imageBytes)
            $file.Headers.ContentType = New-Object Net.Http.Headers.MediaTypeHeaderValue($Mime[$ext])
            # Имя файла — своё, не владельца: в журналы службы оно попадать не должно
            $content.Add($file, 'input_reference', "input$ext")
            $request.Content = $content
        } elseif ($null -ne $Json) {
            $request.Content = New-Object Net.Http.StringContent(($Json | ConvertTo-Json -Compress),
                [Text.Encoding]::UTF8, 'application/json')
        }
        $task = $Http.SendAsync($request)
        while (-not $task.Wait(250)) { }
        $response = $task.Result
        $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        $status = [int]$response.StatusCode
        $text = if ($Binary -and $status -eq 200) { '' } else { [Text.Encoding]::UTF8.GetString($bytes) }
        return [pscustomobject]@{ Status = $status; Bytes = $bytes; Text = $text }
    } catch {
        $e = $_.Exception
        while ($e.InnerException -and ($e -is [Management.Automation.MethodInvocationException] -or
                $e -is [AggregateException] -or $e -is [Net.Http.HttpRequestException])) { $e = $e.InnerException }
        if ($e -is [Threading.Tasks.TaskCanceledException]) { throw "шлюз не ответил за $TimeoutSec с" }
        throw "шлюз недоступен ($Url): $($e.Message)"
    } finally {
        if ($response) { $response.Dispose() }
        $request.Dispose()
    }
}

function Read-Json([string]$Text) {
    try { return $Text | ConvertFrom-Json } catch { return $null }
}

# Причина отказа: error.message (формат OpenAI), error строкой или detail; иначе — начало тела.
function Get-Reason([string]$Text) {
    $j = Read-Json $Text
    if ($j) {
        if ($j.error.message) { return "$($j.error.message)" }
        if ($j.error) { return "$($j.error)" }
        if ($j.detail) { return "$($j.detail)" }
    }
    if ($Text.Length -gt 300) { return $Text.Substring(0, 300) + '…' }
    return $Text
}

function Get-FailText($Response) {
    $reason = Get-Reason $Response.Text
    switch ($Response.Status) {
        400 { return "запрос отклонён (400): $reason" }
        401 { return "шлюз не принял ключ (401): ключ неверный или отозван — пересоберите копию через -MakeLocal" }
        403 { return "ключу не разрешён этот маршрут или модель (403): $reason" }
        404 { return "нет такого задания (404): $reason" }
        409 { return "не готово (409): $reason" }
        429 { return "очередь службы полна (429): $reason" }
        503 { return "карта или служба недоступна (503): $reason" }
        default { return "шлюз ответил $($Response.Status): $reason" }
    }
}

function New-Seed {
    $bytes = New-Object byte[] 4
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return [long][BitConverter]::ToUInt32($bytes, 0)
}

function Format-Span([TimeSpan]$Span) {
    if ($Span.TotalSeconds -lt 60) { return ('{0:N0} с' -f $Span.TotalSeconds) }
    return ('{0} мин {1} с' -f [int][Math]::Floor($Span.TotalMinutes), $Span.Seconds)
}

function Add-ManifestLine([string[]]$Cells) {
    $line = ($Cells | ForEach-Object { $_ -replace '[\t\r\n]+', ' ' }) -join "`t"
    [IO.File]::AppendAllText($manifest, $line + "`r`n", $Utf8Bom)    # BOM — только в новом файле
}

function Get-Label($Job) { return "ролик $($Job.N)/$total $($Job.Image.Name)" }

function Show-Detail($Job, [string]$Text) {
    if ($Job.Detail -ne $Text) {
        $Job.Detail = $Text
        Write-Host "$(Get-Label $Job): $Text"
    }
}

function Remove-Video($Job) {
    try {
        $r = Invoke-Gateway 'DELETE' "/videos/$($Job.Id)"
        if ($r.Status -ne 200 -and $r.Status -ne 404) { Write-Warning "$(Get-Label $Job): задание не удалено — $(Get-FailText $r)" }
    } catch {
        Write-Warning "$(Get-Label $Job): задание не удалено — $($_.Exception.Message)"
    }
}

function Send-Video($Job) {
    $form = [ordered]@{ model = $Model; seconds = $Seconds; quality = $Quality; smooth = $smoothText; seed = $Job.Seed }
    if ($Job.Prompt) { $form.prompt = $Job.Prompt }
    return Invoke-Gateway 'POST' '/videos' -Form $form -ImageFile $Job.Image.FullName
}

# Список заданий службы одним запросом (id → объект): опрос по заданию упёрся бы в предел
# запросов ключа (rpm) уже на паре десятков роликов. $null — список не получен ($script:listError).
function Get-Listing {
    $script:listError = ''
    try { $r = Invoke-Gateway 'GET' '/videos' }
    catch { $script:listError = "нет ответа: $($_.Exception.Message)"; return $null }
    $obj = if ($r.Status -eq 200) { Read-Json $r.Text } else { $null }
    if (-not $obj) { $script:listError = Get-FailText $r; return $null }
    $map = @{}
    foreach ($v in @($obj.data)) { if ($v.id) { $map["$($v.id)"] = $v } }
    return $map
}

# Ход одного задания по списку; $true — задание закончено (готово, не вышло или пропало у службы).
function Update-Video($Job, $Listing) {
    if ($null -eq $Listing) { Show-Detail $Job $script:listError; return $false }
    if (-not $Listing.ContainsKey($Job.Id)) {
        Write-Warning "$(Get-Label $Job): служба не знает задание (перезапуск службы?)"
        $script:failed++
        return $true
    }
    $obj = $Listing[$Job.Id]
    if ($obj.detail) { Show-Detail $Job "$($obj.detail)" }
    if ($obj.status -eq 'in_progress' -and -not $Job.Running) { $Job.Running = Get-Date }
    # Придуманный службой промпт — в <имя>.auto.txt, у вариантов он общий: один раз
    if (-not $Job.Prompt -and "$($obj.prompt_source)" -eq 'auto' -and $obj.prompt -and
            -not $autoSaved.ContainsKey($Job.Base)) {
        [IO.File]::WriteAllText((Join-Path $Folder "$($Job.Base).auto.txt"), "$($obj.prompt)".Trim(), $Utf8NoBom)
        $autoSaved[$Job.Base] = $true
        Write-Host "$(Get-Label $Job): промпт службы сохранён в $($Job.Base).auto.txt"
    }
    if ($obj.status -eq 'failed') {
        $why = if ($obj.error.message) { "$($obj.error.message)" } else { 'служба не назвала причину' }
        Write-Warning "$(Get-Label $Job): не вышел — $why"
        Remove-Video $Job
        $script:failed++
        return $true
    }
    if ($obj.status -ne 'completed') { return $false }

    $why = ''
    try {
        $c = Invoke-Gateway 'GET' "/videos/$($Job.Id)/content" -Binary
        if ($c.Status -ne 200) { $why = Get-FailText $c }
    } catch { $why = $_.Exception.Message }
    if ($why) {
        # Готовый ролик служба держит 30 минут: сетевой сбой не повод его терять
        $Job.Tries++
        if ($Job.Tries -lt 3) { Show-Detail $Job "не скачался ($why), повторю"; return $false }
        Write-Warning "$(Get-Label $Job): не скачался — $why"
        Remove-Video $Job
        $script:failed++
        return $true
    }
    $seedOut = if ($null -ne $obj.seed) { [long]$obj.seed } else { $Job.Seed }
    $file = '{0}_{1}s_s{2}.mp4' -f $Job.Base, $Seconds, $seedOut
    [IO.File]::WriteAllBytes((Join-Path $Folder $file), $c.Bytes)
    Remove-Video $Job
    $promptOut = if ($Job.Prompt) { $Job.Prompt } else { "$($obj.prompt)" }
    $sourceOut = if ($Job.Source) { $Job.Source } elseif ($obj.prompt_source) { "$($obj.prompt_source)" } else { 'auto' }
    Add-ManifestLine @($Job.Image.Name, $file, "$seedOut", "$Seconds", $Quality, $smoothText, "$($Job.Variant)",
                       $sourceOut, $promptOut)
    $script:done++
    $from = if ($Job.Running) { $Job.Running } else { $Job.Sent }
    Write-Host "$(Get-Label $Job): готово за $(Format-Span ((Get-Date) - $from)) → $file"
    return $true
}

$manifest = Join-Path $Folder 'video-manifest.tsv'
$recorded = @{}
if (Test-Path -LiteralPath $manifest) {
    foreach ($row in [IO.File]::ReadAllLines($manifest, [Text.Encoding]::UTF8)) {
        $c = $row.Split("`t")
        if ($c.Count -lt 9 -or $c[0] -eq 'image') { continue }
        if (Test-Path -LiteralPath (Join-Path $Folder $c[1]) -PathType Leaf) {
            $recorded[(@($c[0], $c[3], $c[4], $c[5], $c[6]) -join "`t")] = $true
        }
    }
} else {
    Add-ManifestLine @('image', 'video', 'seed', 'seconds', 'quality', 'smooth', 'variant', 'prompt_source', 'prompt')
}

$images = @(Get-ChildItem -LiteralPath $Folder -File |
    Where-Object { $Mime.ContainsKey($_.Extension.ToLowerInvariant()) } |
    Sort-Object { $_.Name.ToLowerInvariant() })
if ($images.Count -eq 0) { throw "В папке $Folder нет картинок (.jpg .jpeg .png .webp)" }

$done = 0
$failed = 0
$skipped = 0
$todo = New-Object Collections.ArrayList
$smoothLabel = if ($NoSmooth) { 'без дорисовки' } else { 'с дорисовкой' }
Write-Host "Папка $Folder`: картинок $($images.Count); $Seconds с, $Quality, $smoothLabel, вариантов $Count"
foreach ($img in $images) {
    if ($img.Length -gt $MaxImage) {
        Write-Warning "$($img.Name): больше 20 МБ — пропущена"
        $failed += $Count
        continue
    }
    $base = [IO.Path]::GetFileNameWithoutExtension($img.Name)
    $own = Join-Path $Folder "$base.txt"
    $saved = Join-Path $Folder "$base.auto.txt"
    $text = ''
    $source = ''
    if (Test-Path -LiteralPath $own -PathType Leaf) { $text = [IO.File]::ReadAllText($own, [Text.Encoding]::UTF8).Trim() }
    if ($text) { $source = 'file'; $label = "свой ($base.txt)" }
    elseif ($common) { $text = $common; $source = 'common'; $label = 'общий' }
    else {
        if (Test-Path -LiteralPath $saved -PathType Leaf) { $text = [IO.File]::ReadAllText($saved, [Text.Encoding]::UTF8).Trim() }
        if ($text) { $source = 'auto'; $label = 'авто (сохранён)' } else { $label = 'придумает служба' }
    }
    if ($text.Length -gt $MaxPrompt) {
        Write-Warning "$($img.Name): промпт длиннее $MaxPrompt символов — пропущена"
        $failed += $Count
        continue
    }
    $added = 0
    for ($k = 1; $k -le $Count; $k++) {
        if (-not $Redo -and $recorded.ContainsKey((@($img.Name, "$Seconds", $Quality, $smoothText, "$k") -join "`t"))) {
            $skipped++
            continue
        }
        $seedValue = if ($null -ne $Seed) { [long]$Seed + $k - 1 } else { New-Seed }
        [void]$todo.Add([pscustomobject]@{ N = $todo.Count + 1; Image = $img; Base = $base; Variant = $k
                                           Seed = $seedValue; Prompt = $text; Source = $source; Id = ''
                                           Detail = ''; Sent = $null; Running = $null; Tries = 0 })
        $added++
    }
    if ($added) { Write-Host "  $($img.Name): роликов $added, промпт — $label" }
}
$total = $todo.Count
if ($skipped) { Write-Host "Уже в журнале — пропущено: $skipped (-Redo — сделать заново)" }

$active = New-Object Collections.ArrayList
$autoSaved = @{}
$next = 0
$sent = 0
$full = $false
$started = Get-Date
try {
    while ($next -lt $total -or $active.Count -gt 0) {
        $moved = $false
        if ($active.Count -eq 0) { $full = $false }    # своих в очереди нет — пробуем снова после паузы
        $before = $sent
        while ($next -lt $total -and -not $full) {
            $job = $todo[$next]
            $r = $null
            $why = ''
            try { $r = Send-Video $job } catch { $why = $_.Exception.Message }
            if ($r -and $r.Status -eq 429) {
                $full = $true
                Write-Host "Очередь службы полна: жду, пока выйдут отправленные (отправлено $sent из $total)"
                break
            }
            $next++
            $moved = $true
            $obj = if ($r -and $r.Status -ge 200 -and $r.Status -lt 300) { Read-Json $r.Text } else { $null }
            if ($obj -and $obj.id) {
                $job.Id = "$($obj.id)"
                $job.Sent = Get-Date
                [void]$active.Add($job)
                $sent++
            } else {
                if (-not $why) { $why = if ($r.Status -ge 300) { Get-FailText $r } else { 'в ответе службы нет id задания' } }
                Write-Warning "$(Get-Label $job): не отправлен — $why"
                $failed++
            }
        }
        if ($sent -ne $before) { Write-Host "Отправлено $sent из $total" }
        $listing = if ($active.Count -gt 0) { Get-Listing } else { $null }
        foreach ($job in @($active)) {
            if (Update-Video $job $listing) {
                [void]$active.Remove($job)
                $full = $false
                $moved = $true
            }
        }
        if (-not $moved -and ($active.Count -gt 0 -or $full)) { Start-Sleep -Seconds $PollSec }
    }
} finally {
    # Ctrl+C или ошибка: свои незавершённые задания — снять, чтобы служба не рендерила впустую
    if ($active.Count -gt 0) {
        Write-Warning "Отменяю свои незавершённые задания: $($active.Count)"
        foreach ($job in @($active)) { Remove-Video $job }
    }
    # Освобождение — в своём try: сетевой сбой не должен затереть исходную ошибку и итог
    if ($KeepCard) { }
    elseif ($sent -eq 0) { Write-Host 'Ничего не отправлено — карту не трогаю' }
    else {
        try {
            $r = Invoke-Gateway 'POST' '/images/release' -Json @{ model = $Model }
            if ($r.Status -eq 200) { Write-Host 'Карта освобождена — вернётся gemma4' }
            elseif ($r.Status -eq 409) { Write-Host "Карта занята: $(Get-Reason $r.Text)" }
            else { Write-Warning "Карту освободить не удалось: $(Get-FailText $r)" }
        } catch {
            Write-Warning "Карту освободить не удалось: $($_.Exception.Message)"
        }
    }
    $Http.Dispose()
    Write-Host ("Итого: готово {0}, не вышло {1}, пропущено {2}, {3:N0} мин. Папка: {4}" -f $done, $failed,
        $skipped, ((Get-Date) - $started).TotalMinutes, $Folder)
}
