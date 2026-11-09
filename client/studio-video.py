#!/usr/bin/env python3
"""Пакетное оживление картинок папки через API видео шлюза srvllm (Wan2.2 Remix v3, I2V).

Аналог studio-video.ps1 для Linux и macOS: те же параметры (--folder вместо -Folder и т. д.),
те же имена роликов и video-manifest.tsv. Нужен только Python 3.8+.

Скрипт ходит прямо в шлюз (POST <url>/videos и т. д., форма OpenAI Videos) ключом владельца
для картинок; студия в этом не участвует. Работает из домашней сети или через туннель.

Берутся картинки .jpg .jpeg .png .webp прямо в --folder (без подпапок и скрытых файлов), по
имени; файлы больше 20 МБ пропускаются с предупреждением. Ролики ложатся рядом с картинкой под
именами <имя>_<N>s_s<seed>_<отпечаток>.mp4 (занятое имя не перезаписывается — к нему
добавляется -2, -3, …), рядом — журнал video-manifest.tsv (картинка, ролик, seed,
длительность, качество, дорисовка, вариант, откуда промпт, промпт, модель, отпечаток).

Промпт картинки: <имя>.txt рядом (UTF-8, целиком) → иначе --prompt → иначе <имя>.auto.txt
(промпт, придуманный службой в прошлый раз) → иначе пусто: служба опишет движение по фото
сама, а скрипт сохранит её промпт в <имя>.auto.txt — повторный запуск отдаст его как свой.

Задания уходят очередью: скрипт шлёт все, пока служба принимает; «очередь полна» (429) —
ждёт, пока выйдут отправленные, и досылает; «карта или служба недоступна» (503) — задание
уходит в конец очереди и повторяется через 5 с, всего до 3 попыток. Сетевой сбой при отправке
не повторяется: запрос мог дойти и начать рендер, повтор сделал бы ролик дважды, — несделанное
доделает повторный запуск по журналу. Повторный запуск пропускает ролик, если в журнале
есть ролик с тем же отпечатком и его файл на месте. Отпечаток — sha256 содержимого картинки,
модели, seed (если задан --seed), длительности, качества, дорисовки, номера варианта и своего
промпта (<имя>.txt или --prompt; промпт службы и <имя>.auto.txt в него не входят): смена
любого из них — новый ролик. Строки журнала прежней версии (без отпечатка) сверяются только
по имени картинки, длительности, качеству, дорисовке и варианту — смены промпта, seed, модели
и содержимого картинки по ним не видно, скрипт об этом предупреждает. --redo — сделать заново.

Код выхода: 0 — все ролики готовы или уже были; 3 — хотя бы один не вышел (не отправлен,
не сделан службой, не скачался, картинка больше 20 МБ или промпт длиннее предела); 1 — скрипт
остановился с ошибкой (нет ключа, папки, картинок); 2 — неверные параметры; 130 — Ctrl+C.

Ключ. В репозитории скрипт без ключа. Один раз соберите локальную копию с ключом из vault:
    python3 studio-video.py --make-local [--vault-path /путь/vault.env]
Рядом появится studio-video.local.py (в .gitignore, права 0700) — дальше запускайте её; её
можно переносить на другие компьютеры как есть. Ключ на один запуск — --token (он виден в
списке процессов).

Карта одна на всех: пока её держит другой потребитель, ролик ждёт («жду карту»). Первый ролик
после подъёма слота дольше — загрузка Wan. По окончании карта освобождается и возвращается
gemma4; --keep-card — оставить карту. Ctrl+C отменяет все свои незавершённые задания и тоже
освобождает карту.

Примеры:
    python3 studio-video.py --make-local
    python3 studio-video.local.py --folder ~/photos
    python3 studio-video.local.py --folder ~/photos --seconds 8 --quality high --count 2 --seed 100
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

# Строка-заглушка ключа: в репозитории пусто, --make-local вписывает ключ в локальную копию.
EMBEDDED_KEY = ""
LOCAL_NAME = "studio-video.local.py"
VAULT_KEY = "GATEWAY_KEY_OWNER_IMAGES"
VAULT_PATH = r"C:\work\projects\local-config\secrets\vault.env"
URL = "http://10.77.1.15:4000/slot"
MODEL = "wan2.2-remix-i2v-v3"
TIMEOUT = 300                          # один запрос к шлюзу: загрузка картинки или выдача mp4 (маршрут шлюза — 180 с)
POLL = 5                               # опрос заданий и пауза перед повтором отправки после 503, с
SEND_TRIES = 3                         # попыток отправки задания, на которые служба отвечает 503
MAX_PROMPT = 4000                      # предел API видео
MAX_IMAGE = 20 * 1024 * 1024
MAX_SEED = 4294967295
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
MANIFEST = "video-manifest.tsv"
HEADER = ["image", "video", "seed", "seconds", "quality", "smooth", "variant", "prompt_source", "prompt", "model",
          "fingerprint"]
OLD_HEADER = HEADER[:9]                # журнал прежней версии: без модели и отпечатка
LINES = re.compile(r"\r\n|\r|\n")
# Ячейка журнала — без табуляции и любых разделителей строк Unicode (тот же класс у PowerShell-версии)
SEPARATORS = re.compile(r"[\t\n\v\f\r\x1c-\x1e\x85\u2028\u2029]+")


class Fail(Exception):
    """Отказ с понятной причиной: печатается без трассировки."""


def say(text):
    print(text, flush=True)


def warn(text):
    print("ВНИМАНИЕ: " + text, file=sys.stderr, flush=True)


def bounded(low, high):
    def check(text):
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError("не целое число: " + text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"от {low} до {high}")
        return value
    return check


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", help="папка с картинками; ролики и журнал ложатся туда же")
    p.add_argument("--prompt", default="", help="общий промпт для картинок без своего <имя>.txt")
    p.add_argument("--seconds", type=bounded(1, 30), default=5,
                   help="длительность ролика, целые секунды от 1 до 30 (по умолчанию 5); длиннее 5 с — "
                        "склейка кусков по 5 с, она «уплывает»")
    p.add_argument("--quality", choices=["fast", "standard", "high"], default="standard",
                   help="fast (короткая сторона 480), standard (576, по умолчанию) или high (720)")
    p.add_argument("--no-smooth", action="store_true", help="без дорисовки кадров: 16 кадров/с вместо 32")
    p.add_argument("--count", type=bounded(1, 20), default=1, help="сколько вариантов на каждую картинку (1–20)")
    p.add_argument("--seed", type=int, default=None,
                   help="начальный seed; без него — случайный у каждого ролика, с ним — seed, seed+1, … "
                        "для --count вариантов одной картинки")
    p.add_argument("--redo", action="store_true", help="делать заново и то, что уже есть в журнале")
    p.add_argument("--keep-card", action="store_true", help="не освобождать карту в конце")
    p.add_argument("--model", default=MODEL, help="модель видео у шлюза")
    p.add_argument("--url", default=URL, help="база API шлюза")
    p.add_argument("--token", default="", help="ключ шлюза; по умолчанию — вписанный командой --make-local")
    p.add_argument("--make-local", action="store_true",
                   help=f"собрать рядом {LOCAL_NAME} с ключом {VAULT_KEY} из vault и выйти")
    p.add_argument("--vault-path", default=VAULT_PATH, help="файл vault для --make-local")
    args = p.parse_args(argv)
    if not args.make_local and not args.folder:
        p.error("нужен --folder (или --make-local)")
    return args


def make_local(vault_path):
    vault = Path(vault_path)
    if not vault.is_file():
        raise Fail(f"Нет файла vault: {vault} (путь — параметром --vault-path)")
    key = ""
    for line in vault.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if line.startswith(VAULT_KEY + "="):
            key = line[len(VAULT_KEY) + 1:].strip().strip("\"'")
            break
    if not key:
        raise Fail(f"В {vault} нет строки {VAULT_KEY}=<ключ>: ключ владельца для картинок не выпущен "
                   "или не внесён в vault")
    script = Path(__file__).resolve()
    source = script.read_text(encoding="utf-8")
    slots = list(re.finditer(r"^EMBEDDED_KEY = .*$", source, re.M))
    if len(slots) != 1:
        raise Fail(f'В {script} не найдена строка-заглушка ключа (EMBEDDED_KEY = "")')
    text = source[:slots[0].start()] + "EMBEDDED_KEY = " + repr(key) + source[slots[0].end():]
    target = script.with_name(LOCAL_NAME)
    # Ключ — только владельцу файла; LF — иначе shebang на Linux не найдёт python3
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700), "w",
                   encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(target, 0o700)
    say(f"Готово: {target} — ключ вписан (файл в .gitignore). Дальше запускайте его.")


def multipart(fields, file_field, image):
    boundary = uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n'
                      f"Content-Type: text/plain; charset=utf-8\r\n\r\n".encode() + str(value).encode() + b"\r\n")
    ext = image.suffix.lower()
    # Имя файла — своё, не владельца: в журналы службы оно попадать не должно
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="input{ext}"\r\n'
                  f"Content-Type: {MIME.get(ext, 'application/octet-stream')}\r\n\r\n".encode()
                  + image.read_bytes() + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def read_json(data):
    try:
        return json.loads(data)
    except ValueError:
        return None


def reason(text):
    """Причина отказа: error.message (формат OpenAI), error строкой или detail; иначе — начало тела."""
    j = read_json(text)
    if isinstance(j, dict):
        error = j.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if error:
            return str(error)
        if j.get("detail"):
            return str(j["detail"])
    return text[:300] + "…" if len(text) > 300 else text


def fail_text(status, data):
    why = reason(data.decode("utf-8", "replace"))
    return {
        400: f"запрос отклонён (400): {why}",
        401: "шлюз не принял ключ (401): ключ неверный или отозван — пересоберите копию через --make-local",
        403: f"ключу не разрешён этот маршрут или модель (403): {why}",
        404: f"нет такого задания (404): {why}",
        409: f"не готово (409): {why}",
        429: f"очередь службы полна (429): {why}",
        503: f"карта или служба недоступна (503): {why}",
    }.get(status, f"шлюз ответил {status}: {why}")


def span(seconds):
    if seconds < 60:
        return f"{seconds:.0f} с"
    return f"{int(seconds // 60)} мин {int(seconds % 60)} с"


class Videos:
    """Один прогон по папке: очередь своих заданий у службы, журнал и счётчики."""

    def __init__(self, args, token, folder):
        self.args = args
        self.url = args.url.rstrip("/")
        self.token = token
        self.folder = folder
        self.manifest = folder / MANIFEST
        self.smooth = "false" if args.no_smooth else "true"
        self.done = self.failed = self.skipped = 0
        self.total = 0
        self.auto_saved = set()
        self.list_error = ""

    def call(self, method, path, json_body=None, form=None, image=None):
        """Запрос к шлюзу: (код ответа, тело байтами). Сбой сети — Fail с понятной причиной."""
        headers = {"Authorization": "Bearer " + self.token}
        data = None
        if image is not None:
            data, headers["Content-Type"] = multipart(form, "input_reference", image)
        elif json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as e:
            data = e.read()
            e.close()
            return e.code, data
        except socket.timeout:
            raise Fail(f"шлюз не ответил за {TIMEOUT} с")
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                raise Fail(f"шлюз не ответил за {TIMEOUT} с")
            raise Fail(f"шлюз недоступен ({self.url}): {e.reason}")
        except (OSError, http.client.HTTPException) as e:
            raise Fail(f"шлюз недоступен ({self.url}): {e}")

    def add_row(self, cells):
        with open(self.manifest, "a", encoding="utf-8", newline="\n") as f:
            f.write("\t".join(SEPARATORS.sub(" ", c) for c in cells) + "\n")

    def label(self, job):
        return f"ролик {job.n}/{self.total} {job.image.name}"

    def show(self, job, text):
        if job.detail != text:
            job.detail = text
            say(f"{self.label(job)}: {text}")

    def remove(self, job):
        try:
            status, data = self.call("DELETE", f"/videos/{job.id}")
            if status not in (200, 404):
                warn(f"{self.label(job)}: задание не удалено — {fail_text(status, data)}")
        except Fail as e:
            warn(f"{self.label(job)}: задание не удалено — {e}")

    def send(self, job):
        a = self.args
        form = {"model": a.model, "seconds": a.seconds, "quality": a.quality, "smooth": self.smooth,
                "seed": job.seed}
        if job.prompt:
            form["prompt"] = job.prompt
        return self.call("POST", "/videos", form=form, image=job.image)

    def listing(self):
        """Список заданий службы одним запросом (id → объект): опрос по заданию упёрся бы в предел
        запросов ключа (rpm) уже на паре десятков роликов. None — список не получен (list_error)."""
        self.list_error = ""
        try:
            status, data = self.call("GET", "/videos")
        except Fail as e:
            self.list_error = f"нет ответа: {e}"
            return None
        obj = read_json(data) if status == 200 else None
        if not isinstance(obj, dict):
            self.list_error = fail_text(status, data)
            return None
        return {str(v["id"]): v for v in obj.get("data") or [] if isinstance(v, dict) and v.get("id")}

    def update(self, job, listing):
        """Ход одного задания по списку; True — задание закончено (готово, не вышло или пропало у службы)."""
        if listing is None:
            self.show(job, self.list_error)
            return False
        if job.id not in listing:
            warn(f"{self.label(job)}: служба не знает задание (перезапуск службы?)")
            self.failed += 1
            return True
        obj = listing[job.id]
        if obj.get("detail"):
            self.show(job, str(obj["detail"]))
        if obj.get("status") == "in_progress" and not job.running:
            job.running = time.monotonic()
        # Придуманный службой промпт — в <имя>.auto.txt, у вариантов он общий: один раз
        if (not job.prompt and str(obj.get("prompt_source")) == "auto" and obj.get("prompt")
                and job.base not in self.auto_saved):
            (self.folder / f"{job.base}.auto.txt").write_bytes(str(obj["prompt"]).strip().encode())
            self.auto_saved.add(job.base)
            say(f"{self.label(job)}: промпт службы сохранён в {job.base}.auto.txt")
        if obj.get("status") == "failed":
            error = obj.get("error")
            why = "служба не назвала причину"
            if isinstance(error, dict) and error.get("message"):
                why = str(error["message"])
            warn(f"{self.label(job)}: не вышел — {why}")
            self.remove(job)
            self.failed += 1
            return True
        if obj.get("status") != "completed":
            return False

        why = ""
        try:
            status, video = self.call("GET", f"/videos/{job.id}/content")
            if status != 200:
                why = fail_text(status, video)
        except Fail as e:
            why = str(e)
        if why:
            # Готовый ролик служба держит 30 минут: сетевой сбой не повод его терять
            job.tries += 1
            if job.tries < 3:
                self.show(job, f"не скачался ({why}), повторю")
                return False
            warn(f"{self.label(job)}: не скачался — {why}")
            self.remove(job)
            self.failed += 1
            return True
        seed = int(obj["seed"]) if obj.get("seed") is not None else job.seed
        # Отпечаток в имени разводит картинки с одним именем и разные запросы; занятое имя не
        # перезаписывается (--redo с тем же seed), а недописанный файл не ляжет под именем ролика
        stem = f"{job.base}_{self.args.seconds}s_s{seed}_{job.key[:8]}"
        file, n = stem + ".mp4", 1
        while (self.folder / file).exists():
            n += 1
            file = f"{stem}-{n}.mp4"
        part = self.folder / (file + ".part")
        try:
            part.write_bytes(video)
            os.replace(part, self.folder / file)
        except OSError as e:
            # Диск полон, нет прав, имя занято: один ролик — не повод ронять прогон и снимать чужие
            warn(f"{self.label(job)}: не записался — {e}")
            try:
                if part.is_file():
                    part.unlink()
            except OSError:
                pass
            self.remove(job)
            self.failed += 1
            return True
        self.remove(job)
        prompt = job.prompt or str(obj.get("prompt") or "")
        source = job.source or str(obj.get("prompt_source") or "") or "auto"
        self.add_row([job.image.name, file, str(seed), str(self.args.seconds), self.args.quality, self.smooth,
                      str(job.variant), source, prompt, self.args.model, job.key])
        self.done += 1
        say(f"{self.label(job)}: готово за {span(time.monotonic() - (job.running or job.sent))} → {file}")
        return True

    def plan(self):
        """Задания по картинкам папки без уже сделанных по журналу."""
        a = self.args
        recorded, legacy = set(), set()
        rows = []
        if self.manifest.exists():
            raw = self.manifest.read_bytes()
            bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
            # Строки — только по CR/LF, как .NET ReadAllLines у PowerShell-версии: splitlines резал бы и по
            # U+2028/VT/FF/NEL внутри промпта, и половина строки теряла бы отпечаток
            rows = LINES.split(raw[len(bom):].decode("utf-8", "replace"))
            if rows and not rows[-1]:
                rows.pop()
            if rows and rows[0].split("\t") == OLD_HEADER:
                # Журнал прежней версии: шапку — с новыми столбцами, строки как есть
                part = self.folder / (MANIFEST + ".part")
                part.write_bytes(bom + raw[len(bom):].replace(rows[0].encode(), "\t".join(HEADER).encode(), 1))
                os.replace(part, self.manifest)
            for row in rows:
                c = row.split("\t")
                if len(c) < 9 or c[0] == "image" or not (self.folder / c[1]).is_file():
                    continue
                if len(c) >= 11 and c[10]:
                    recorded.add(c[10])
                else:
                    legacy.add((c[0], c[3], c[4], c[5], c[6]))
        if not any(row.strip() for row in rows):
            # Журнала нет или в нём ни одной строки (пустой файл, один BOM) — начать с шапки
            self.manifest.write_bytes(b"")
            self.add_row(HEADER)
        legacy_skipped = 0

        images = sorted((p for p in self.folder.iterdir()
                         if p.suffix.lower() in MIME and not p.name.startswith(".") and p.is_file()),
                        key=lambda p: p.name.lower())
        if not images:
            raise Fail(f"В папке {self.folder} нет картинок (.jpg .jpeg .png .webp)")

        common = a.prompt.strip()
        smooth_label = "без дорисовки" if a.no_smooth else "с дорисовкой"
        say(f"Папка {self.folder}: картинок {len(images)}; {a.seconds} с, {a.quality}, {smooth_label}, "
            f"вариантов {a.count}")
        todo = []
        for img in images:
            if img.stat().st_size > MAX_IMAGE:
                warn(f"{img.name}: больше 20 МБ — пропущена")
                self.failed += a.count
                continue
            base = img.stem
            own = self.folder / f"{base}.txt"
            saved = self.folder / f"{base}.auto.txt"
            text = source = ""
            if own.is_file():
                text = own.read_text(encoding="utf-8-sig", errors="replace").strip()
            if text:
                source, label = "file", f"свой ({base}.txt)"
            elif common:
                text, source, label = common, "common", "общий"
            else:
                if saved.is_file():
                    text = saved.read_text(encoding="utf-8-sig", errors="replace").strip()
                if text:
                    source, label = "auto", "авто (сохранён)"
                else:
                    label = "придумает служба"
            if len(text) > MAX_PROMPT:
                warn(f"{img.name}: промпт длиннее {MAX_PROMPT} символов — пропущена")
                self.failed += a.count
                continue
            # Отпечаток запроса (тот же у PowerShell-версии): промпт — только свой; придуманный
            # службой и <имя>.auto.txt не входят, иначе повтор переделывал бы готовое
            digest = hashlib.sha256(img.read_bytes()).hexdigest()
            own_prompt = re.sub(r"\r\n?", "\n", text) if source in ("file", "common") else ""
            added = 0
            for k in range(1, a.count + 1):
                seed = a.seed + k - 1 if a.seed is not None else secrets.randbits(32)
                key = hashlib.sha256("\n".join([
                    digest, a.model, str(seed) if a.seed is not None else "", str(a.seconds), a.quality,
                    self.smooth, str(k), own_prompt]).encode()).hexdigest()
                if not a.redo and key in recorded:
                    self.skipped += 1
                    continue
                if not a.redo and (img.name, str(a.seconds), a.quality, self.smooth, str(k)) in legacy:
                    self.skipped += 1
                    legacy_skipped += 1
                    continue
                todo.append(SimpleNamespace(n=len(todo) + 1, image=img, base=base, variant=k, seed=seed,
                                            prompt=text, source=source, key=key, id="", detail="", sent=0.0,
                                            running=0.0, tries=0, unavailable=0))
                added += 1
            if added:
                say(f"  {img.name}: роликов {added}, промпт — {label}")
        self.total = len(todo)
        if self.skipped:
            say(f"Уже в журнале — пропущено: {self.skipped} (--redo — сделать заново)")
        if legacy_skipped:
            warn(f"из них {legacy_skipped} — по строкам журнала прежней версии без отпечатка: смена промпта, "
                 "seed, модели или содержимого картинки по ним не видна; --redo — сделать заново")
        return todo

    def run(self):
        todo = self.plan()
        active = []
        next_job = sent = 0
        full = False
        started = time.monotonic()
        try:
            while next_job < self.total or active:
                moved = wait = False
                if not active:
                    full = False     # своих в очереди нет — пробуем снова после паузы
                before = sent
                while next_job < self.total and not full:
                    job = todo[next_job]
                    status, data, why = None, b"", ""
                    # Сетевой сбой не повторяется: запрос мог дойти, повтор сделал бы ролик дважды
                    try:
                        status, data = self.send(job)
                    except Fail as e:
                        why = str(e)
                    if status == 429:
                        full = True
                        say(f"Очередь службы полна: жду, пока выйдут отправленные (отправлено {sent} из {self.total})")
                        break
                    # 503 — служба задание не приняла, повтор безопасен: задание — в конец очереди, и
                    # остальные ждут паузу вместе с ним (карта недоступна — не примут и их)
                    if status == 503 and job.unavailable + 1 < SEND_TRIES:
                        job.unavailable += 1
                        say(f"{self.label(job)}: {fail_text(status, data)} — повторю через {POLL} с")
                        todo.append(todo.pop(next_job))
                        wait = True
                        break
                    next_job += 1
                    moved = True
                    obj = read_json(data) if status is not None and 200 <= status < 300 else None
                    if isinstance(obj, dict) and obj.get("id"):
                        job.id = str(obj["id"])
                        job.sent = time.monotonic()
                        active.append(job)
                        sent += 1
                    else:
                        if not why:
                            why = fail_text(status, data) if status >= 300 else "в ответе службы нет id задания"
                        warn(f"{self.label(job)}: не отправлен — {why}")
                        self.failed += 1
                if sent != before:
                    say(f"Отправлено {sent} из {self.total}")
                listing = self.listing() if active else None
                for job in list(active):
                    if self.update(job, listing):
                        active.remove(job)
                        full = False
                        moved = True
                if wait or (not moved and (active or full)):
                    time.sleep(POLL)
        finally:
            # Ctrl+C или ошибка: свои незавершённые задания — снять, чтобы служба не рендерила впустую
            if active:
                warn(f"Отменяю свои незавершённые задания: {len(active)}")
                for job in list(active):
                    self.remove(job)
            # Освобождение — в своём try: сетевой сбой не должен затереть исходную ошибку и итог
            if self.args.keep_card:
                pass
            elif sent == 0:
                say("Ничего не отправлено — карту не трогаю")
            else:
                try:
                    status, data = self.call("POST", "/images/release", json_body={"model": self.args.model})
                    if status == 200:
                        say("Карта освобождена — вернётся gemma4")
                    elif status == 409:
                        say(f"Карта занята: {reason(data.decode('utf-8', 'replace'))}")
                    else:
                        warn(f"Карту освободить не удалось: {fail_text(status, data)}")
                except Fail as e:
                    warn(f"Карту освободить не удалось: {e}")
            say(f"Итого: готово {self.done}, не вышло {self.failed}, пропущено {self.skipped}, "
                f"{(time.monotonic() - started) / 60:.0f} мин. Папка: {self.folder}")
        return self.failed


def run(args):
    """Прогон по папке; возвращает число не вышедших роликов."""
    token = args.token or EMBEDDED_KEY
    if not token:
        raise Fail("Нет ключа шлюза. Соберите локальную копию с ключом из vault: python3 studio-video.py "
                   f"--make-local, затем запускайте {LOCAL_NAME} (или передайте ключ параметром --token)")
    folder = Path(args.folder)
    if not folder.is_dir():
        raise Fail(f"Нет папки: {args.folder}")
    if len(args.prompt.strip()) > MAX_PROMPT:
        raise Fail(f"Общий промпт длиннее {MAX_PROMPT} символов")
    if args.seed is not None and (args.seed < 0 or args.seed + args.count - 1 > MAX_SEED):
        raise Fail(f"Seed — от 0 до {MAX_SEED}")
    return Videos(args, token, folder.resolve()).run()


def main(argv=None):
    # в трубу и файл Python пишет кодировкой локали (Windows — cp1251): «→» справки и сообщений
    # не должна ронять скрипт посреди работы
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = parse_args(argv)
    try:
        if args.make_local:
            make_local(args.vault_path)
        elif run(args):
            return 3                    # хотя бы один ролик не вышел: внешний запуск должен это видеть
    except (Fail, OSError) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("прервано", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
