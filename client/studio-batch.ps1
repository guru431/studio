<#
.SYNOPSIS
  Пакетная генерация картинок через API картинок шлюза srvllm (SNOFS, FLUX, FineView или Jib).

.DESCRIPTION
  Скрипт ходит прямо в шлюз (POST <Url>/images/generations) ключом владельца для картинок;
  студия в этом не участвует. Работает из домашней сети или через туннель.

  Промпты берутся из текстового файла в UTF-8: одна строка — один промпт. Пустые строки и
  строки, начинающиеся с #, пропускаются.

  Картинки ложатся в -OutDir под именами вида 003-02_<начало промпта>_s<seed>.png. К занятому
  имени добавляется -2, -3, … Рядом — manifest.tsv (номер, файл, seed, размер, модель, промпт),
  чтобы повторить удачную картинку тем же seed.

  Прерванный пакет (Ctrl+C, отказ шлюза) доделывает -Resume с той же -OutDir: пропускаются пары
  «промпт × вариант», уже записанные в manifest.tsv с той же моделью и размером, если файл на месте.
  «Лимит запросов» (429) и «карта или модель недоступна» (503) — повтор через 5 с, всего до 3 попыток.

  Ключ. В репозитории скрипт без ключа. Один раз соберите локальную копию с ключом из vault:
    powershell -ExecutionPolicy Bypass -File studio-batch.ps1 -MakeLocal [-GatewayUrl http://<шлюз>:4000/slot]
  Рядом появится studio-batch.local.ps1 (в .gitignore) — дальше запускайте её; её можно
  переносить на другие компьютеры как есть. Ключ на один запуск — параметром -Token.

  Карта одна на всех: пока её держит другой потребитель, шлюз ждёт. Первая картинка занимает
  больше времени из-за загрузки модели. Шлюз ждёт ответ до 21 мин (FineView и Jib — скрипт до 31 мин).
  По окончании карта освобождается и возвращается gemma4; -KeepCard — оставить карту.
  Ctrl+C обрывает текущий запрос и тоже освобождает карту.

  Код выхода: 0 — все картинки готовы; 3 — хотя бы одна не вышла (отказ шлюза, промпт длиннее
  предела); 1 — скрипт остановился с ошибкой (нет ключа, файла промптов, фото) или неверные
  параметры.

  Против дубля человека и лишних рук: одному человеку — начинать промпт с «solo, 1girl» (или
  «1boy»), руки описывать явно («left hand on hip, right hand holding a glass»), и брать -Count 3–4,
  чтобы выбрать удачную. Негатива у Turbo нет: без CFG он не работает (проверено 2026-10-17).

.PARAMETER PromptsFile
  Файл с промптами, одна строка — один промпт.
.PARAMETER Model
  snofs (по умолчанию, SNOFS Krea 2), flux (FLUX 3 Image Klein), fineview (FineView QW21) или
  jib-moss (Jib Mix Moss V2).
.PARAMETER ImagePath
  Исходное фото PNG/JPEG/WebP до 10 МиБ. Одно фото для всех промптов; запрос /images/edits.
.PARAMETER Strength
  Сила изменения SNOFS и Jib от 0.05 до 1 (по умолчанию 0.65). Меньше — ближе к исходнику.
.PARAMETER OutDir
  Куда класть картинки. По умолчанию — новая папка studio-<дата-время> в текущем каталоге.
.PARAMETER Resume
  Пропустить пары «промпт × вариант», уже записанные в manifest.tsv папки -OutDir с той же моделью
  и размером, если файл на месте. Только с -OutDir.
.PARAMETER Size
  portrait (832x1216), square (1024x1024) или landscape (1216x832).
.PARAMETER Count
  Сколько картинок на каждый промпт.
.PARAMETER Seed
  Начальный seed. Пусто — случайный у каждой картинки; задан — seed, seed+1, … для -Count
  картинок одного промпта (у всех промптов одинаковые — удобно сравнивать промпты).
.PARAMETER KeepCard
  Не освобождать карту в конце.
.PARAMETER Url
  База API картинок шлюза.
.PARAMETER Token
  Ключ шлюза. По умолчанию — вписанный командой -MakeLocal.
.PARAMETER MakeLocal
  Собрать рядом studio-batch.local.ps1 с ключом GATEWAY_KEY_OWNER_IMAGES из vault и выйти.
.PARAMETER VaultPath
  Файл vault для -MakeLocal.
.PARAMETER GatewayUrl
  Адрес шлюза (база API, как -Url): -MakeLocal впишет его в копию умолчанием -Url. Без него — нынешнее
  умолчание -Url этого скрипта.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File studio-batch.ps1 -MakeLocal
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File studio-batch.local.ps1 -PromptsFile prompts.txt
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File studio-batch.local.ps1 -PromptsFile prompts.txt -Model flux -Size square -Count 3 -OutDir D:\studio
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File studio-batch.local.ps1 -PromptsFile prompts.txt -Model flux -Size square -Count 3 -OutDir D:\studio -Resume
#>
[CmdletBinding(DefaultParameterSetName = 'Run')]
param(
    [Parameter(ParameterSetName = 'Run', Mandatory = $true)][string]$PromptsFile,
    [Parameter(ParameterSetName = 'Run')][ValidateSet('snofs', 'flux', 'fineview', 'jib-moss')][string]$Model = 'snofs',
    [Parameter(ParameterSetName = 'Run')][string]$ImagePath = '',
    [Parameter(ParameterSetName = 'Run')][ValidateRange(0.05, 1.0)][double]$Strength = 0.65,
    [Parameter(ParameterSetName = 'Run')]
    [string]$OutDir = (Join-Path (Get-Location) ('studio-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))),
    [Parameter(ParameterSetName = 'Run')][ValidateSet('portrait', 'square', 'landscape')][string]$Size = 'portrait',
    [Parameter(ParameterSetName = 'Run')][switch]$Resume,
    [Parameter(ParameterSetName = 'Run')][ValidateRange(1, 100)][int]$Count = 1,
    [Parameter(ParameterSetName = 'Run')][Nullable[long]]$Seed = $null,
    [Parameter(ParameterSetName = 'Run')][switch]$KeepCard,
    [Parameter(ParameterSetName = 'Run')][string]$Url = 'http://10.77.1.15:4000/slot',
    [Parameter(ParameterSetName = 'Run')][string]$Token = '',
    [Parameter(ParameterSetName = 'MakeLocal', Mandatory = $true)][switch]$MakeLocal,
    [Parameter(ParameterSetName = 'MakeLocal')][string]$VaultPath = 'C:\work\projects\local-config\secrets\vault.env',
    [Parameter(ParameterSetName = 'MakeLocal')][string]$GatewayUrl = ''
)
# Строка-заглушка ключа: в репозитории пусто, -MakeLocal вписывает ключ в локальную копию.
$EmbeddedKey = ''
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$VaultKey = 'GATEWAY_KEY_OWNER_IMAGES'
$Models = @{ snofs = 'snofs-krea2-v1.6'; flux = 'flux-3-image-klein'
             fineview = 'fineview-qw21'; 'jib-moss' = 'jib-moss-v2' }
$StrengthModels = @('snofs', 'jib-moss')    # FLUX и FineView силу изменения не получают
$Sizes = @{ portrait = '832x1216'; square = '1024x1024'; landscape = '1216x832' }
$Mime = @{ '.jpg' = 'image/jpeg'; '.jpeg' = 'image/jpeg'; '.png' = 'image/png'; '.webp' = 'image/webp' }
$TimeoutSec = 1300                     # маршрут генерации у шлюза — до 1260 с
$MediaTimeoutSec = 1900                # FineView и Jib: холодная загрузка новых весов (как у студии)
$SendTries = 3                         # попыток запроса, на которые шлюз отвечает 429 или 503
$PauseSec = 5                          # пауза перед повтором после 429 и 503, с
$MaxPrompt = 24000                     # предел API картинок
$MaxSeed = 4294967295

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
    $what = 'ключ вписан'
    if ($GatewayUrl) {
        if ($GatewayUrl -notmatch '^https?://[^\s''"`]+$') {
            throw "Адрес шлюза — http(s)://хост[:порт]/путь без пробелов и кавычек: $GatewayUrl"
        }
        $GatewayUrl = $GatewayUrl.TrimEnd('/')
        $urlSlot = [Regex]::Matches($text, "(?m)^(\s*\[Parameter\(ParameterSetName = 'Run'\)\]\[string\]\`$Url = )'(?:[^']|'')*'")
        if ($urlSlot.Count -ne 1) { throw "В $PSCommandPath не найдена строка умолчания -Url" }
        $text = $text.Substring(0, $urlSlot[0].Index) + $urlSlot[0].Groups[1].Value + "'" + $GatewayUrl + "'" +
            $text.Substring($urlSlot[0].Index + $urlSlot[0].Length)
        $what = "ключ и адрес шлюза $GatewayUrl вписаны"
    }
    $target = Join-Path $PSScriptRoot 'studio-batch.local.ps1'
    [IO.File]::WriteAllText($target, $text, (New-Object Text.UTF8Encoding $true))
    Write-Host "Готово: $target — $what (файл в .gitignore). Дальше запускайте его."
    return
}

if ($Resume -and -not $PSBoundParameters.ContainsKey('OutDir')) { throw '-Resume — только с -OutDir прежнего запуска' }
if (-not $Token) { $Token = $EmbeddedKey }
if (-not $Token) {
    throw ("Нет ключа шлюза. Соберите локальную копию с ключом из vault: " +
        "powershell -ExecutionPolicy Bypass -File studio-batch.ps1 -MakeLocal, " +
        "затем запускайте studio-batch.local.ps1 (или передайте ключ параметром -Token)")
}
$Url = $Url.TrimEnd('/')
Add-Type -AssemblyName System.Net.Http
$modelId = $Models[$Model]
# FineView и Jib занимают слот ComfyUI модели SNOFS: release — по слоту, как у студии
$releaseId = if ($Model -eq 'flux') { $Models.flux } else { $Models.snofs }
if ($Model -eq 'fineview' -or $Model -eq 'jib-moss') { $TimeoutSec = $MediaTimeoutSec }
if ($ImagePath) {
    $imageInfo = Get-Item -LiteralPath $ImagePath
    if ($imageInfo.PSIsContainer -or $imageInfo.Length -eq 0 -or $imageInfo.Length -gt 10MB) {
        throw 'Фото должно быть непустым файлом до 10 МиБ'
    }
    $ImagePath = $imageInfo.FullName
}

$prompts = @(Get-Content -LiteralPath $PromptsFile -Encoding UTF8 |
    ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })
if ($prompts.Count -eq 0) { throw "В файле $PromptsFile нет промптов" }
if ($null -ne $Seed -and ($Seed -lt 0 -or $Seed + $Count - 1 -gt $MaxSeed)) { throw "Seed — от 0 до $MaxSeed" }

# Запрос к шлюзу: код ответа и тело текстом. HttpClient ведёт себя одинаково в Windows
# PowerShell 5.1 и pwsh 7 (Invoke-WebRequest на кодах HTTP бросает там разные исключения);
# тело — UTF-8 сами: ответ без charset Windows PowerShell прочёл бы как Latin-1.
# Ждём ответ кусочками по 250 мс: так Ctrl+C прерывает и долгий запрос.
function Invoke-Gateway([string]$Path, $Body, [int]$Timeout, [string]$ImageFile = '') {
    # Фото читается до запроса: сбой чтения — не «шлюз недоступен». Не через if-выражение:
    # оно развернуло бы массив байтов в object[]
    $imageBytes = $null
    if ($ImageFile) { $imageBytes = [IO.File]::ReadAllBytes($ImageFile) }
    $client = New-Object Net.Http.HttpClient
    $content = $null
    $response = $null
    try {
        $client.Timeout = [TimeSpan]::FromSeconds($Timeout)
        $client.DefaultRequestHeaders.Authorization = New-Object Net.Http.Headers.AuthenticationHeaderValue('Bearer', $Token)
        if ($ImageFile) {
            $content = New-Object Net.Http.MultipartFormDataContent
            foreach ($entry in $Body.GetEnumerator()) {
                $value = [Convert]::ToString($entry.Value, [Globalization.CultureInfo]::InvariantCulture)
                $content.Add((New-Object Net.Http.StringContent($value, [Text.Encoding]::UTF8)), $entry.Key)
            }
            $ext = [IO.Path]::GetExtension($ImageFile).ToLowerInvariant()
            $type = if ($Mime.ContainsKey($ext)) { $Mime[$ext] } else { 'application/octet-stream' }
            $part = New-Object Net.Http.ByteArrayContent -ArgumentList (,$imageBytes)
            $part.Headers.ContentType = New-Object Net.Http.Headers.MediaTypeHeaderValue($type)
            # Имя файла — своё, не владельца: в журналы службы оно попадать не должно
            $content.Add($part, 'image', "input$ext")
        } else {
            $content = New-Object Net.Http.StringContent(($Body | ConvertTo-Json -Compress),
                [Text.Encoding]::UTF8, 'application/json')
        }
        $task = $client.PostAsync("$Url$Path", $content)
        while (-not $task.Wait(250)) { }
        $response = $task.Result
        $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        return [pscustomobject]@{ Status = [int]$response.StatusCode; Text = [Text.Encoding]::UTF8.GetString($bytes) }
    } catch {
        $e = $_.Exception
        while ($e.InnerException -and ($e -is [Management.Automation.MethodInvocationException] -or
                $e -is [AggregateException] -or $e -is [Net.Http.HttpRequestException])) { $e = $e.InnerException }
        if ($e -is [Threading.Tasks.TaskCanceledException]) { throw "шлюз не ответил за $Timeout с" }
        throw "шлюз недоступен ($Url): $($e.Message)"
    } finally {
        if ($response) { $response.Dispose() }
        if ($content) { $content.Dispose() }
        $client.Dispose()
    }
}

# Причина отказа: error.message (формат OpenAI), error строкой или detail; иначе — начало тела.
function Get-Reason([string]$Text) {
    try {
        $j = $Text | ConvertFrom-Json
        if ($j.error.message) { return "$($j.error.message)" }
        if ($j.error) { return "$($j.error)" }
        if ($j.detail) { return "$($j.detail)" }
    } catch { }
    if ($Text.Length -gt 300) { return $Text.Substring(0, 300) + '…' }
    return $Text
}

function Get-FailText($Response) {
    $reason = Get-Reason $Response.Text
    switch ($Response.Status) {
        400 { return "запрос отклонён (400): $reason" }
        401 { return "шлюз не принял ключ (401): ключ неверный или отозван — пересоберите копию через -MakeLocal" }
        403 { return "ключу не разрешена эта модель или маршрут (403): $reason" }
        429 { return "превышен лимит запросов ключа (429): $reason" }
        503 { return "карта или модель недоступна (503): $reason" }
        default { return "шлюз ответил $($Response.Status): $reason" }
    }
}

$rng = [Security.Cryptography.RandomNumberGenerator]::Create()    # один на скрипт: Fill в .NET Framework нет
function New-Seed {
    $bytes = New-Object byte[] 4
    $rng.GetBytes($bytes)
    return [long][BitConverter]::ToUInt32($bytes, 0)
}

function Get-Slug([string]$Text) {
    $bad = [Regex]::Escape(-join [IO.Path]::GetInvalidFileNameChars())
    $slug = ($Text -replace "[$bad]", '' -replace '\s+', '_').Trim('_', '.')
    if ($slug.Length -gt 40) { $slug = $slug.Substring(0, 40).TrimEnd('_', '.') }
    if (-not $slug) { $slug = 'prompt' }
    return $slug
}

function Write-Picture([string]$Stem, [byte[]]$Bytes) {
    $part = Join-Path $OutDir ('.studio-batch-{0:N}.part' -f [Guid]::NewGuid())
    $stream = [IO.File]::Open($part, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    try {
        try { $stream.Write($Bytes, 0, $Bytes.Length) } finally { $stream.Dispose() }
        $file = "$Stem.png"
        for ($n = 2; ; $n++) {
            $path = Join-Path $OutDir $file
            try { [IO.File]::Move($part, $path); return $file }
            catch {
                if (-not (Test-Path -LiteralPath $path)) { throw }
                $file = "$Stem-$n.png"
            }
        }
    } finally {
        [IO.File]::Delete($part)
    }
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$manifest = Join-Path $OutDir 'manifest.tsv'
$manifestEncoding = New-Object System.Text.UTF8Encoding($false)
$newManifest = $null
try { $newManifest = [IO.File]::Open($manifest, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write) }
catch { if (-not [IO.File]::Exists($manifest)) { throw } }
if ($newManifest) {
    try {
        $header = $manifestEncoding.GetBytes("n`tfile`tseed`tsize`tmodel`tprompt`n")
        $newManifest.Write($header, 0, $header.Length)
    } finally { $newManifest.Dispose() }
}

# -Resume: готовые пары «промпт × вариант» той же модели и размера; счётчик — на повторы промпта.
# Словарь .NET, не @{}: ключи хеш-таблицы PowerShell не различают регистр, а промпты — различают
$recorded = New-Object 'Collections.Generic.Dictionary[string,int]'
if ($Resume) {
    foreach ($row in @([IO.File]::ReadAllLines($manifest, $manifestEncoding) | Select-Object -Skip 1)) {
        $c = $row.Split("`t")
        if ($c.Count -lt 6 -or -not $c[0].Contains('-')) { continue }
        try { $saved = New-Object IO.FileInfo ([IO.Path]::Combine($OutDir, $c[1])) } catch { continue }
        if (-not $saved.Exists -or $saved.Length -eq 0) { continue }
        $key = $c[0].Substring($c[0].LastIndexOf('-') + 1) + "`t" + $c[3] + "`t" + $c[4] + "`t" + $c[5]
        $have = 0
        [void]$recorded.TryGetValue($key, [ref]$have)
        $recorded[$key] = $have + 1
    }
}
$todo = @()                            # номер промпта (с 0) и варианты к работе (с 0)
$skipped = 0
for ($p = 0; $p -lt $prompts.Count; $p++) {
    $clean = $prompts[$p] -replace '[\t\n\v\f\r\x1c-\x1e\x85  ]+', ' '
    $variants = @()
    for ($k = 0; $k -lt $Count; $k++) {
        $key = "$($k + 1)`t$Size`t$Model`t$clean"
        $have = 0
        if ($recorded.TryGetValue($key, [ref]$have) -and $have -gt 0) { $recorded[$key] = $have - 1; $skipped++ }
        else { $variants += $k }
    }
    if ($variants.Count) { $todo += [pscustomobject]@{ P = $p; Variants = $variants } }
}

$total = 0
foreach ($item in $todo) { $total += $item.Variants.Count }
$done = 0
$failed = 0
$started = Get-Date
if ($skipped) { Write-Host "Уже в manifest.tsv — пропущено: $skipped" }
Write-Host "Шлюз: $total картинок ($($prompts.Count) промптов × $Count), $Model, размер $Size → $OutDir"
Write-Host 'Если карту держит другой потребитель, первая картинка ждёт её освобождения'
try {
    foreach ($item in $todo) {
        $p = $item.P
        $prompt = $prompts[$p]
        $preview = if ($prompt.Length -gt 60) { $prompt.Substring(0, 60) + '…' } else { $prompt }
        if ($prompt.Length -gt $MaxPrompt) {
            Write-Warning "[$($p + 1)] промпт длиннее $MaxPrompt символов — пропущен: $preview"
            $failed += $item.Variants.Count
            continue
        }
        foreach ($k in $item.Variants) {
            $n = $done + $failed + 1
            Write-Host "[$n/$total] $preview"
            $seedValue = if ($null -ne $Seed) { $Seed + $k } else { New-Seed }
            $t0 = Get-Date
            try {
                $body = @{ model = $modelId; prompt = $prompt; size = $Sizes[$Size]; n = 1
                           seed = $seedValue; response_format = 'b64_json' }
                if ($ImagePath -and $StrengthModels -contains $Model) { $body.strength = $Strength }
                # 429 и 503 — шлюз запрос не выполнил: повтор безопасен, до $SendTries попыток
                for ($try = 1; ; $try++) {
                    if ($ImagePath) {
                        $r = Invoke-Gateway '/images/edits' $body $TimeoutSec $ImagePath
                    } else {
                        $r = Invoke-Gateway '/images/generations' $body $TimeoutSec
                    }
                    if (($r.Status -ne 429 -and $r.Status -ne 503) -or $try -ge $SendTries) { break }
                    Write-Host "  $(Get-FailText $r) — повторю через $PauseSec с"
                    Start-Sleep -Seconds $PauseSec
                }
                if ($r.Status -ne 200) { throw (Get-FailText $r) }
                $item = @(($r.Text | ConvertFrom-Json).data)[0]
                if (-not $item.b64_json) { throw 'в ответе шлюза нет картинки (data[0].b64_json)' }
                # FLUX может не вернуть seed — тогда в manifest запрошенный
                $seedOut = if ($null -ne $item.seed) { [long]$item.seed } else { $seedValue }
                $stem = '{0:D3}-{1:D2}_{2}_s{3}' -f ($p + 1), ($k + 1), (Get-Slug $prompt), $seedOut
                $file = Write-Picture $stem ([Convert]::FromBase64String($item.b64_json))
                $manifestLine = "{0}-{1}`t{2}`t{3}`t{4}`t{5}`t{6}`n" -f ($p + 1), ($k + 1), $file, $seedOut, $Size, $Model,
                    ($prompt -replace '[\t\n\v\f\r\x1c-\x1e\x85\u2028\u2029]+', ' ')
                [IO.File]::AppendAllText($manifest, $manifestLine, $manifestEncoding)
                $done++
                Write-Host ("  готово за {0:N0} с → {1}" -f ((Get-Date) - $t0).TotalSeconds, $file)
            } catch {
                $failed++
                Write-Warning "  не вышло: $($_.Exception.Message)"
            }
        }
    }
} finally {
    # Освобождение — в своём try: сетевой сбой не должен затереть исходную ошибку и итог
    if (-not $KeepCard) {
        try {
            $r = Invoke-Gateway '/images/release' @{ model = $releaseId } 60
            if ($r.Status -eq 200) { Write-Host 'Карта освобождена — вернётся gemma4' }
            elseif ($r.Status -eq 409) { Write-Warning 'Карта занята генерацией, освободится сама через 10 минут' }
            else { Write-Warning "Карту освободить не удалось: $(Get-FailText $r)" }
        } catch {
            Write-Warning "Карту освободить не удалось: $($_.Exception.Message)"
        }
    }
    Write-Host ("Итого: {0} готово, {1} не вышло, {2:N0} мин. Папка: {3}" -f $done, $failed,
        ((Get-Date) - $started).TotalMinutes, $OutDir)
}
if ($failed) { exit 3 }    # хотя бы одна картинка не вышла: внешний запуск должен это видеть
