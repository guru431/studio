#!/usr/bin/env python3
"""Пакетная генерация картинок через API картинок шлюза srvllm (SNOFS, FLUX, FineView или Jib).

Аналог studio-batch.ps1 для Linux и macOS: те же параметры (--prompts-file вместо
-PromptsFile и т. д.), те же имена файлов и manifest.tsv. Нужен только Python 3.8+.

Скрипт ходит прямо в шлюз (POST <url>/images/generations) ключом владельца для картинок;
студия в этом не участвует. Работает из домашней сети или через туннель.

Промпты берутся из текстового файла в UTF-8: одна строка — один промпт. Пустые строки и
строки, начинающиеся с #, пропускаются.

Картинки ложатся в --out-dir под именами вида 003-02_<начало промпта>_s<seed>.png. К занятому
имени добавляется -2, -3, … Рядом — manifest.tsv (номер, файл, seed, размер, модель, промпт),
чтобы повторить удачную картинку тем же seed.

Прерванный пакет (Ctrl+C, отказ шлюза) доделывает --resume с той же --out-dir: пропускаются пары
«промпт × вариант», уже записанные в manifest.tsv с той же моделью и размером, если файл на месте.
«Лимит запросов» (429) и «карта или модель недоступна» (503) — повтор через 5 с, всего до 3 попыток.

Ключ. В репозитории скрипт без ключа. Один раз соберите локальную копию с ключом из vault:
    python3 studio-batch.py --make-local [--vault-path /путь/vault.env] [--gateway-url http://<шлюз>:4000/slot]
Рядом появится studio-batch.local.py (в .gitignore, права 0700) — дальше запускайте её; её
можно переносить на другие компьютеры как есть. Ключ на один запуск — --token (он виден в
списке процессов).

Карта одна на всех: пока её держит другой потребитель, шлюз ждёт. Первая картинка занимает
больше времени из-за загрузки модели. Шлюз ждёт ответ до 21 мин (SNOFS, FineView и Jib — скрипт до 31 мин).
По окончании карта освобождается и возвращается gemma4; --keep-card — оставить карту.
Ctrl+C обрывает текущий запрос и тоже освобождает карту.

Код выхода: 0 — все картинки готовы; 3 — хотя бы одна не вышла (отказ шлюза, промпт длиннее
предела); 1 — скрипт остановился с ошибкой (нет ключа, файла промптов, фото); 2 — неверные
параметры; 130 — Ctrl+C.

Против дубля человека и лишних рук: одному человеку — начинать промпт с «solo, 1girl» (или
«1boy»), руки описывать явно («left hand on hip, right hand holding a glass»), и брать
--count 3–4, чтобы выбрать удачную. Негатива у Turbo нет: без CFG он не работает.

Примеры:
    python3 studio-batch.py --make-local
    python3 studio-batch.local.py --prompts-file prompts.txt
    python3 studio-batch.local.py --prompts-file prompts.txt --model flux --size square --count 3 --out-dir ~/studio
    python3 studio-batch.local.py --prompts-file prompts.txt --model flux --size square --count 3 --out-dir ~/studio --resume
"""
import argparse
import base64
import errno
import http.client
import json
import os
import re
import secrets
import socket
import stat
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

# Строка-заглушка ключа: в репозитории пусто, --make-local вписывает ключ в локальную копию.
EMBEDDED_KEY = ""
LOCAL_NAME = "studio-batch.local.py"
VAULT_KEY = "GATEWAY_KEY_OWNER_IMAGES"
VAULT_PATH = r"C:\work\projects\local-config\secrets\vault.env"
URL = "http://10.77.1.15:4000/slot"
MODELS = {"snofs": "snofs-krea2-v1.6", "flux": "flux-3-image-klein",
          "fineview": "fineview-qw21", "jib-moss": "jib-moss-v2"}
STRENGTH_MODELS = {"snofs", "jib-moss"}  # FLUX и FineView силу изменения не получают
SIZES = {"portrait": "832x1216", "square": "1024x1024", "landscape": "1216x832"}
TIMEOUT = 1300                         # маршрут генерации у шлюза — до 1260 с
MEDIA_TIMEOUT = 1900                   # модели ComfyUI (всё, кроме FLUX): холодная загрузка весов (как у студии)
SEND_TRIES = 3                         # попыток запроса, на которые шлюз отвечает 429 или 503
PAUSE = 5                              # пауза перед повтором после 429 и 503, с
MAX_PROMPT = 24000                     # предел API картинок
MAX_IMAGE = 10 * 1024 * 1024
MAX_SEED = 4294967295
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
# Недопустимое в имени файла Windows: папку с картинками могут унести и туда
BAD_NAME = re.compile(r'[\x00-\x1f"<>|:*?\\/]')
# Промпт в журнале — без табуляции и разделителей строк Unicode (тот же класс у PowerShell-версии)
SEPARATORS = re.compile(r"[\t\n\v\f\r\x1c-\x1e\x85\u2028\u2029]+")


class Fail(Exception):
    """Отказ с понятной причиной: печатается без трассировки."""


def say(text):
    print(text, flush=True)


def warn(text):
    print("ВНИМАНИЕ: " + text, file=sys.stderr, flush=True)


def bounded(kind, low, high):
    def check(text):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError("не число: " + text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"от {low} до {high}")
        return value
    return check


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prompts-file", help="файл с промптами, одна строка — один промпт")
    p.add_argument("--model", choices=sorted(MODELS), default="snofs",
                   help="snofs (SNOFS Krea 2, по умолчанию), flux (FLUX 3 Image Klein), fineview (FineView QW21) "
                        "или jib-moss (Jib Mix Moss V2)")
    p.add_argument("--image-path", default="",
                   help="исходное фото PNG/JPEG/WebP до 10 МиБ, одно на все промпты (запрос /images/edits)")
    p.add_argument("--strength", type=bounded(float, 0.05, 1.0), default=0.65,
                   help="сила изменения SNOFS и Jib от 0.05 до 1 (по умолчанию 0.65); меньше — ближе к исходнику")
    p.add_argument("--out-dir", default="",
                   help="куда класть картинки; по умолчанию — новая папка studio-<дата-время> в текущем каталоге")
    p.add_argument("--resume", action="store_true",
                   help="пропустить пары «промпт × вариант», уже записанные в manifest.tsv папки --out-dir "
                        "с той же моделью и размером, если файл на месте")
    p.add_argument("--size", choices=["portrait", "square", "landscape"], default="portrait",
                   help="portrait (832x1216), square (1024x1024) или landscape (1216x832)")
    p.add_argument("--count", type=bounded(int, 1, 100), default=1, help="сколько картинок на каждый промпт")
    p.add_argument("--seed", type=int, default=None,
                   help="начальный seed; без него — случайный у каждой картинки, с ним — seed, seed+1, … "
                        "для --count картинок одного промпта")
    p.add_argument("--keep-card", action="store_true", help="не освобождать карту в конце")
    p.add_argument("--url", default=URL, help="база API картинок шлюза")
    p.add_argument("--token", default="", help="ключ шлюза; по умолчанию — вписанный командой --make-local")
    p.add_argument("--make-local", action="store_true",
                   help=f"собрать рядом {LOCAL_NAME} с ключом {VAULT_KEY} из vault и выйти")
    p.add_argument("--vault-path", default=VAULT_PATH, help="файл vault для --make-local")
    p.add_argument("--gateway-url", default="",
                   help="адрес шлюза (база API, как --url): --make-local впишет его в копию умолчанием --url; "
                        "без него — нынешнее умолчание")
    args = p.parse_args(argv)
    if not args.make_local and not args.prompts_file:
        p.error("нужен --prompts-file (или --make-local)")
    if args.gateway_url and not args.make_local:
        p.error("--gateway-url — только с --make-local (на один запуск — --url)")
    if args.resume and not args.out_dir:
        p.error("--resume — только с --out-dir прежнего запуска")
    return args


def make_local(vault_path, gateway_url=""):
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
    what = "ключ вписан"
    if gateway_url:
        if not re.fullmatch(r"https?://[^\s'\"`]+", gateway_url):
            raise Fail(f"Адрес шлюза — http(s)://хост[:порт]/путь без пробелов и кавычек: {gateway_url}")
        gateway_url = gateway_url.rstrip("/")
        urls = list(re.finditer(r"^URL = .*$", text, re.M))
        if len(urls) != 1:
            raise Fail(f"В {script} не найдена строка умолчания URL")
        text = text[:urls[0].start()] + "URL = " + repr(gateway_url) + text[urls[0].end():]
        what = f"ключ и адрес шлюза {gateway_url} вписаны"
    target = script.with_name(LOCAL_NAME)
    # Ключ — только владельцу файла; LF — иначе shebang на Linux не найдёт python3
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700), "w",
                   encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(target, 0o700)
    say(f"Готово: {target} — {what} (файл в .gitignore). Дальше запускайте его.")


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


def call(url, token, path, body, timeout, image=None):
    """POST к шлюзу: (код ответа, тело текстом). Сбой сети — Fail с понятной причиной."""
    headers = {"Authorization": "Bearer " + token}
    if image is not None:
        data, headers["Content-Type"] = multipart(body, "image", image)
    else:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        e.close()
        return e.code, text
    except socket.timeout:
        raise Fail(f"шлюз не ответил за {timeout} с")
    except urllib.error.URLError as e:
        if isinstance(e.reason, socket.timeout):
            raise Fail(f"шлюз не ответил за {timeout} с")
        raise Fail(f"шлюз недоступен ({url}): {e.reason}")
    except (OSError, http.client.HTTPException) as e:
        raise Fail(f"шлюз недоступен ({url}): {e}")


def reason(text):
    """Причина отказа: error.message (формат OpenAI), error строкой или detail; иначе — начало тела."""
    try:
        j = json.loads(text)
    except ValueError:
        j = None
    if isinstance(j, dict):
        error = j.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if error:
            return str(error)
        if j.get("detail"):
            return str(j["detail"])
    return text[:300] + "…" if len(text) > 300 else text


def fail_text(status, text):
    why = reason(text)
    return {
        400: f"запрос отклонён (400): {why}",
        401: "шлюз не принял ключ (401): ключ неверный или отозван — пересоберите копию через --make-local",
        403: f"ключу не разрешена эта модель или маршрут (403): {why}",
        429: f"превышен лимит запросов ключа (429): {why}",
        503: f"карта или модель недоступна (503): {why}",
    }.get(status, f"шлюз ответил {status}: {why}")


def slug(text):
    s = re.sub(r"\s+", "_", BAD_NAME.sub("", text)).strip("_.")
    if len(s) > 40:
        s = s[:40].rstrip("_.")
    return s or "prompt"


def write_exclusively(path, content):
    stream = path.open("xb")
    try:
        with stream:
            stream.write(content)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def write_picture(folder, stem, content):
    """Готовое имя появляется целиком, если файловая система поддерживает публикацию без замены."""
    part = folder / f".studio-batch-{uuid.uuid4().hex}.part"
    write_exclusively(part, content)
    try:
        publish = os.rename if os.name == "nt" else os.link
        file, n = f"{stem}.png", 1
        while True:
            path = folder / file
            try:
                try:
                    publish(part, path)
                except OSError as e:
                    if os.name == "nt" or e.errno not in (errno.EOPNOTSUPP, errno.ENOSYS, errno.EPERM):
                        raise
                    # Например, exFAT на Linux не поддерживает жёстких ссылок.
                    write_exclusively(path, content)
            except FileExistsError:
                n += 1
                file = f"{stem}-{n}.png"
            else:
                return file
    finally:
        part.unlink(missing_ok=True)


def run(args):
    """Прогон по файлу промптов; возвращает число не вышедших картинок."""
    token = args.token or EMBEDDED_KEY
    if not token:
        raise Fail("Нет ключа шлюза. Соберите локальную копию с ключом из vault: python3 studio-batch.py "
                   f"--make-local, затем запускайте {LOCAL_NAME} (или передайте ключ параметром --token)")
    url = args.url.rstrip("/")
    model_id = MODELS[args.model]
    # FineView и Jib занимают слот ComfyUI модели SNOFS: release — по слоту, как у студии
    release_id = MODELS["flux"] if args.model == "flux" else MODELS["snofs"]
    timeout = MEDIA_TIMEOUT if args.model != "flux" else TIMEOUT
    image = None
    if args.image_path:
        image = Path(args.image_path).resolve()
        if not image.is_file() or not 0 < image.stat().st_size <= MAX_IMAGE:
            raise Fail("Фото должно быть непустым файлом до 10 МиБ")

    # Строки — только по CR/LF, как Get-Content у PowerShell-версии: splitlines резал бы промпт и по
    # U+2028/VT/FF/NEL (копия с веба)
    lines = re.split(r"\r\n|\r|\n", Path(args.prompts_file).read_text(encoding="utf-8-sig", errors="replace"))
    prompts = [s for s in (line.strip() for line in lines) if s and not s.startswith("#")]
    if not prompts:
        raise Fail(f"В файле {args.prompts_file} нет промптов")
    if args.seed is not None and (args.seed < 0 or args.seed + args.count - 1 > MAX_SEED):
        raise Fail(f"Seed — от 0 до {MAX_SEED}")

    out_dir = Path(args.out_dir or "studio-" + datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = out_dir / "manifest.tsv"
    try:
        with open(manifest, "x", encoding="utf-8", newline="\n") as f:
            f.write("n\tfile\tseed\tsize\tmodel\tprompt\n")
    except FileExistsError:
        pass

    # --resume: готовые пары «промпт × вариант» той же модели и размера; счётчик — на повторы промпта
    recorded = Counter()
    if args.resume:
        rows = re.split(r"\r\n|\r|\n", manifest.read_text(encoding="utf-8-sig", errors="replace"))
        for row in rows[1:]:
            c = row.split("\t")
            if len(c) < 6 or "-" not in c[0]:
                continue
            try:
                saved = (out_dir / c[1]).stat()
            except OSError:
                continue
            if stat.S_ISREG(saved.st_mode) and saved.st_size > 0:
                recorded[(c[0].rsplit("-", 1)[1], c[3], c[4], c[5])] += 1

    todo = []                                       # (номер промпта, промпт, варианты к работе)
    skipped = 0
    for p, prompt in enumerate(prompts, 1):
        variants = []
        for k in range(1, args.count + 1):
            key = (str(k), args.size, args.model, SEPARATORS.sub(" ", prompt))
            if recorded[key]:
                recorded[key] -= 1
                skipped += 1
            else:
                variants.append(k)
        if variants:
            todo.append((p, prompt, variants))
    total = sum(len(variants) for _, _, variants in todo)
    done = failed = 0
    started = time.monotonic()
    if skipped:
        say(f"Уже в manifest.tsv — пропущено: {skipped}")
    say(f"Шлюз: {total} картинок ({len(prompts)} промптов × {args.count}), {args.model}, "
        f"размер {args.size} → {out_dir}")
    say("Если карту держит другой потребитель, первая картинка ждёт её освобождения")
    try:
        for p, prompt, variants in todo:
            preview = prompt[:60] + "…" if len(prompt) > 60 else prompt
            if len(prompt) > MAX_PROMPT:
                warn(f"[{p}] промпт длиннее {MAX_PROMPT} символов — пропущен: {preview}")
                failed += len(variants)
                continue
            for k in variants:
                say(f"[{done + failed + 1}/{total}] {preview}")
                seed = args.seed + k - 1 if args.seed is not None else secrets.randbits(32)
                t0 = time.monotonic()
                try:
                    body = {"model": model_id, "prompt": prompt, "size": SIZES[args.size], "n": 1,
                            "seed": seed, "response_format": "b64_json"}
                    if image is not None and args.model in STRENGTH_MODELS:
                        body["strength"] = args.strength
                    # 429 и 503 — шлюз запрос не выполнил: повтор безопасен, до SEND_TRIES попыток
                    for attempt in range(1, SEND_TRIES + 1):
                        if image is not None:
                            status, text = call(url, token, "/images/edits", body, timeout, image)
                        else:
                            status, text = call(url, token, "/images/generations", body, timeout)
                        if status not in (429, 503) or attempt == SEND_TRIES:
                            break
                        say(f"  {fail_text(status, text)} — повторю через {PAUSE} с")
                        time.sleep(PAUSE)
                    if status != 200:
                        raise Fail(fail_text(status, text))
                    data = json.loads(text).get("data") or [{}]
                    item = data[0]
                    if not item.get("b64_json"):
                        raise Fail("в ответе шлюза нет картинки (data[0].b64_json)")
                    # FLUX может не вернуть seed — тогда в manifest запрошенный
                    seed_out = int(item["seed"]) if item.get("seed") is not None else seed
                    stem = f"{p:03d}-{k:02d}_{slug(prompt)}_s{seed_out}"
                    file = write_picture(out_dir, stem, base64.b64decode(item["b64_json"]))
                    with open(manifest, "a", encoding="utf-8", newline="\n") as f:
                        f.write(f"{p}-{k}\t{file}\t{seed_out}\t{args.size}\t{args.model}\t"
                                f"{SEPARATORS.sub(' ', prompt)}\n")
                    done += 1
                    say(f"  готово за {time.monotonic() - t0:.0f} с → {file}")
                except Exception as e:      # картинка не вышла — следующая; Ctrl+C сюда не попадает
                    failed += 1
                    warn(f"  не вышло: {e}")
    finally:
        # Освобождение — в своём try: сетевой сбой не должен затереть исходную ошибку и итог
        if not args.keep_card:
            try:
                status, text = call(url, token, "/images/release", {"model": release_id}, 60)
                if status == 200:
                    say("Карта освобождена — вернётся gemma4")
                elif status == 409:
                    warn("Карта занята генерацией, освободится сама через 10 минут")
                else:
                    warn(f"Карту освободить не удалось: {fail_text(status, text)}")
            except Fail as e:
                warn(f"Карту освободить не удалось: {e}")
        say(f"Итого: {done} готово, {failed} не вышло, {(time.monotonic() - started) / 60:.0f} мин. "
            f"Папка: {out_dir}")
    return failed


def main(argv=None):
    # в трубу и файл Python пишет кодировкой локали (Windows — cp1251): «×» и «→» сообщений
    # не должны ронять пакет посреди работы
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = parse_args(argv)
    try:
        if args.make_local:
            make_local(args.vault_path, args.gateway_url)
        elif run(args):
            return 3                    # хотя бы одна картинка не вышла: внешний запуск должен это видеть
    except (Fail, OSError) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("прервано", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
