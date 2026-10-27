"""Клиент пакетного видео client/studio-video.ps1 и его Python-версия studio-video.py — без сети.

Скрипт ходит в API видео шлюза (форма OpenAI Videos). Его интерфейс зафиксирован спекой
2026-10-19-srvllm-studio-video-design.md (раздел 7 и «Решения исполнителя»: контракт API
видео и параметры скрипта). Ключ в репозитории пустой, а локальная копия с ключом
(studio-video.local.ps1) — в .gitignore. Прогоны обеих версий идут против подставной службы
на 127.0.0.1: она отвечает «готово» с первого опроса, поэтому скрипт не ждёт свои 5 с.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email import policy
from email.parser import BytesParser

import pytest

from clients import RUNNERS, flat, ps_encoding, run_client
from conftest import CLIENT, ROOT

SCRIPT = CLIENT / "studio-video.ps1"
REPO = ROOT
MODEL = "wan2.2-remix-i2v-v3"


def test_script_is_utf8_with_bom():
    # Windows PowerShell 5.1 читает файл без BOM как CP1251 — кириллица ломает разбор строк
    assert SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf")


def test_key_placeholder_is_empty_in_repo():
    text = SCRIPT.read_text(encoding="utf-8-sig")
    slots = re.findall(r"^\$EmbeddedKey = '(.*)'\s*$", text, re.M)
    assert slots == [""], "строка-заглушка ключа одна и пустая: ключ вписывает только -MakeLocal"


def test_paths_and_key_match_the_gateway_contract():
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert "'/videos'" in text and '"/videos/$($Job.Id)/content"' in text
    assert "'/images/release'" in text and "'input_reference'" in text
    assert "'GATEWAY_KEY_OWNER_IMAGES'" in text and "'studio-video.local.ps1'" in text
    assert "'video-manifest.tsv'" in text


def test_local_copy_is_gitignored():
    git = shutil.which("git")
    if not git:
        pytest.skip("нет git — проверка .gitignore пропущена")
    r = subprocess.run([git, "-C", str(REPO), "check-ignore", "-q", "client/studio-video.local.ps1"],
                       capture_output=True, timeout=20)
    assert r.returncode == 0, "studio-video.local.ps1 с ключом не покрыт .gitignore"


def _powershell():
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        pytest.skip("нет powershell — прогон скрипта пропущен")
    return ps


_PARSE = r"""
$e = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$null, [ref]$e)
$defaults = @{}
foreach ($p in $ast.ParamBlock.Parameters) {
    if ($p.DefaultValue -is [System.Management.Automation.Language.ConstantExpressionAst]) {
        $defaults[$p.Name.VariablePath.UserPath] = $p.DefaultValue.Value
    }
}
@{ errors = @($e | ForEach-Object { $_.Message }); defaults = $defaults } | ConvertTo-Json -Compress
"""


@pytest.mark.integration      # старт PowerShell — больше секунды
def test_powershell_parses_script_and_defaults_match_contract():
    ps = _powershell()
    r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command",
                        "& { " + _PARSE + " }", str(SCRIPT)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["errors"] == []
    d = out["defaults"]
    assert d["Model"] == MODEL
    assert d["Url"] == "http://10.77.1.15:4000/slot"
    assert d["Seconds"] == 5 and d["Quality"] == "standard" and d["Count"] == 1
    assert d["Token"] == "" and d["Prompt"] == ""
    assert d["VaultPath"] == r"C:\work\projects\local-config\secrets\vault.env"


def test_python_help_survives_a_pipe_in_a_non_utf8_encoding():
    """В трубу и файл Python пишет кодировкой локали (Windows — cp1251): «→» справки ронял скрипт."""
    r = subprocess.run([sys.executable, str(SCRIPT.with_suffix(".py")), "--help"], capture_output=True,
                       timeout=25, env={**os.environ, "PYTHONIOENCODING": "cp1251"})
    assert r.returncode == 0, r.stderr.decode("cp1251", "replace")


class FakeVideos:
    """Подставная служба видео: задание готово с первого опроса, журнал всех запросов."""

    def __init__(self, busy_posts=(), fail_names=(), reject_posts=False, unavailable=0):
        self.log = []            # (метод, путь) по порядку
        self.posts = []          # поля multipart принятых заданий
        self.post_attempts = 0
        self.busy_posts = set(busy_posts)    # номера попыток POST, на которые 429
        self.unavailable = unavailable       # столько первых попыток POST — 503
        self.reject_posts = reject_posts     # все создания — 400
        self.fail_names = set(fail_names)    # картинки (по содержимому), задания которых падают
        self.jobs = {}
        self.release = []
        self.lock = threading.Lock()

    def obj(self, job, status):
        # Порядок полей намеренно не «естественный»: скрипт не должен на него полагаться
        failed = status == "failed"
        return {"seed": job["seed"], "status": status, "detail": "сбой" if failed else "готово",
                "prompt_source": job["source"], "prompt": job["prompt"], "seconds": job["seconds"],
                "id": job["id"], "object": "video", "model": MODEL, "progress": 100,
                "error": {"code": "x", "message": "граф не принят"} if failed else None}

    def handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, code, payload, ctype="application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_POST(self):
                raw = self.body()
                with fake.lock:
                    fake.log.append(("POST", self.path))
                    if self.path == "/slot/images/release":
                        fake.release.append(json.loads(raw))
                        return self.reply(200, {"ok": True, "slot": "comfy"})
                    assert self.path == "/slot/videos", self.path
                    assert self.headers["Authorization"] == "Bearer test-key"
                    fake.post_attempts += 1
                    if fake.post_attempts in fake.busy_posts:
                        return self.reply(429, {"error": {"message": "очередь полна", "type": "rate_limit"}})
                    if fake.post_attempts <= fake.unavailable:
                        return self.reply(503, {"error": {"message": "карта занята", "type": "unavailable"}})
                    if fake.reject_posts:
                        return self.reply(400, {"error": {"message": "модель не та", "type": "invalid_request"}})
                    mime = BytesParser(policy=policy.default).parsebytes(
                        b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
                    fields = {p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
                              for p in mime.iter_parts()}
                    fake.posts.append(fields)
                    n = len(fake.posts)
                    given = fields.get("prompt", b"").decode()
                    job = {"id": "video_%024x" % n, "seed": int(fields["seed"]), "seconds": fields["seconds"].decode(),
                           "prompt": given or "auto motion %d" % n, "source": "given" if given else "auto",
                           "fail": fields["input_reference"] in fake.fail_names}
                    fake.jobs[job["id"]] = job
                    return self.reply(200, fake.obj(job, "queued"))

            def do_GET(self):
                with fake.lock:
                    fake.log.append(("GET", self.path))
                    if self.path == "/slot/videos":
                        data = [fake.obj(j, "failed" if j["fail"] else "completed") for j in fake.jobs.values()]
                        return self.reply(200, {"object": "list", "data": data, "has_more": False})
                    m = re.fullmatch(r"/slot/videos/(video_\w+)(/content)?", self.path)
                    job = fake.jobs.get(m.group(1)) if m else None
                    if not job:
                        return self.reply(404, {"error": {"message": "нет такого"}})
                    if m.group(2):
                        return self.reply(200, ("mp4-" + job["id"]).encode(), "video/mp4")
                    return self.reply(200, fake.obj(job, "failed" if job["fail"] else "completed"))

            def do_DELETE(self):
                with fake.lock:
                    fake.log.append(("DELETE", self.path))
                    vid = self.path.rsplit("/", 1)[-1]
                    if fake.jobs.pop(vid, None) is None:
                        return self.reply(404, {"error": {"message": "нет такого"}})
                    return self.reply(200, {"id": vid, "object": "video.deleted", "deleted": True})

            def log_message(self, *_args):
                pass

        return Handler


@pytest.fixture
def service():
    fakes = []

    def start(**kw):
        fake = FakeVideos(**kw)
        server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        fake.url = f"http://127.0.0.1:{server.server_port}/slot"
        fakes.append((server, thread))
        return fake

    yield start
    for server, thread in fakes:
        server.shutdown()
        server.server_close()
        thread.join(1)


def run_script(runner, fake, folder, *extra, code=0):
    """code — ожидаемый код выхода: 0 — всё готово или уже было, 3 — хотя бы один ролик не вышел."""
    r = run_client(runner, SCRIPT, ["-Folder", str(folder), "-Token", "test-key", "-Url", fake.url, *extra])
    enc = ps_encoding() if runner == "ps" else "utf-8"      # Windows PowerShell пишет в трубу кодировкой консоли
    out = r.stdout.decode(enc, "replace") + r.stderr.decode(enc, "replace")
    assert r.returncode == code, out
    assert b"test-key" not in r.stdout + r.stderr, "ключ не печатается"
    return flat(out)


HEADER = ["image", "video", "seed", "seconds", "quality", "smooth", "variant", "prompt_source", "prompt", "model",
          "fingerprint"]


def manifest_rows(folder):
    lines = (folder / "video-manifest.tsv").read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].split("\t") == HEADER
    return [dict(zip(HEADER, line.split("\t"))) for line in lines[1:]]


def fingerprint(image, prompt="", seed=None, seconds=5, quality="standard", smooth="true", variant=1, model=MODEL):
    """Отпечаток запроса по формуле шапки скрипта — независимо от обеих версий: так они сходятся."""
    parts = [hashlib.sha256(image).hexdigest(), model, "" if seed is None else str(seed), str(seconds), quality,
             smooth, str(variant), prompt]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


@pytest.mark.parametrize("runner", RUNNERS)
def test_folder_run_sends_downloads_records_and_releases(runner, service, tmp_path):
    fake = service()
    (tmp_path / "a.png").write_bytes(b"img-a")
    (tmp_path / "a.txt").write_text("\ufeff  slow turn\tto camera \n", encoding="utf-8")
    (tmp_path / "B.jpg").write_bytes(b"img-b")
    (tmp_path / "c.webp").write_bytes(b"img-c")
    (tmp_path / "notes.txt").write_text("not an image", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "d.png").write_bytes(b"img-d")
    with open(tmp_path / "huge.png", "wb") as f:     # больше 20 МБ — пропуск с предупреждением
        f.truncate(21 * 1024 * 1024)

    out = run_script(runner, fake, tmp_path, code=3)    # huge.png не вышла

    assert "не вышло 1" in out
    assert [p["input_reference"] for p in fake.posts] == [b"img-a", b"img-b", b"img-c"]
    first = fake.posts[0]
    assert first["prompt"] == b"slow turn\tto camera"
    assert first["model"] == MODEL.encode() and first["seconds"] == b"5"
    assert first["quality"] == b"standard" and first["smooth"] == b"true"
    assert "prompt" not in fake.posts[1] and "prompt" not in fake.posts[2]
    seeds = [int(p["seed"]) for p in fake.posts]

    # Придуманный службой промпт сохранён, без BOM; у картинки со своим .txt — нет
    assert (tmp_path / "B.auto.txt").read_bytes() == b"auto motion 2"
    assert (tmp_path / "c.auto.txt").read_bytes() == b"auto motion 3"
    assert not (tmp_path / "a.auto.txt").exists()

    # Отпечаток: свой промпт входит, промпт службы — нет; seed — только заданный явно
    keys = [fingerprint(b"img-a", "slow turn\tto camera"), fingerprint(b"img-b"), fingerprint(b"img-c")]
    names = [f"a_5s_s{seeds[0]}_{keys[0][:8]}.mp4", f"B_5s_s{seeds[1]}_{keys[1][:8]}.mp4",
             f"c_5s_s{seeds[2]}_{keys[2][:8]}.mp4"]
    for n, name in enumerate(names, 1):
        assert (tmp_path / name).read_bytes() == b"mp4-video_%024x" % n
        content = fake.log.index(("GET", "/slot/videos/video_%024x/content" % n))
        assert fake.log.index(("DELETE", "/slot/videos/video_%024x" % n)) > content
    assert fake.jobs == {}, "все задания удалены у службы"

    rows = manifest_rows(tmp_path)
    assert [(r["image"], r["video"], r["seed"], r["prompt_source"]) for r in rows] == [
        ("a.png", names[0], str(seeds[0]), "file"), ("B.jpg", names[1], str(seeds[1]), "auto"),
        ("c.webp", names[2], str(seeds[2]), "auto")]
    assert rows[0]["prompt"] == "slow turn to camera" and rows[1]["prompt"] == "auto motion 2"
    assert {(r["seconds"], r["quality"], r["smooth"], r["variant"], r["model"]) for r in rows} == {
        ("5", "standard", "true", "1", MODEL)}
    assert [r["fingerprint"] for r in rows] == keys
    assert fake.log[-1] == ("POST", "/slot/images/release") and fake.release == [{"model": MODEL}]

    # Повторный запуск: всё в журнале и файлы на месте — ни одного запроса, карта не трогается;
    # промпт B и c теперь из .auto.txt, но отпечаток тот же. Пропуск — не отказ: код 0
    fake.log.clear()
    (tmp_path / "huge.png").unlink()
    run_script(runner, fake, tmp_path)
    assert fake.log == []


@pytest.mark.parametrize("runner", RUNNERS)
def test_redo_makes_everything_again_with_the_saved_auto_prompt(runner, service, tmp_path):
    """Отдельно от прогона папки: три запуска скрипта в одном тесте выходили за секунду."""
    fake = service()
    (tmp_path / "a.png").write_bytes(b"img-a")
    (tmp_path / "a.txt").write_text("slow turn", encoding="utf-8")
    (tmp_path / "B.jpg").write_bytes(b"img-b")
    run_script(runner, fake, tmp_path)

    # -Redo: снова обе; у B теперь свой промпт из .auto.txt
    run_script(runner, fake, tmp_path, "-Redo")
    assert len(fake.posts) == 4
    assert [p.get("prompt") for p in fake.posts[2:]] == [b"slow turn", b"auto motion 2"]
    rows = manifest_rows(tmp_path)
    assert len(rows) == 4 and [r["prompt_source"] for r in rows[2:]] == ["file", "auto"]
    # BOM (для Excel) — только у PowerShell-версии и только в начале; на Linux журнал без BOM
    assert (tmp_path / "video-manifest.tsv").read_bytes().count(b"\xef\xbb\xbf") == (1 if runner == "ps" else 0)


@pytest.mark.parametrize("runner", RUNNERS)
def test_full_queue_waits_for_sent_jobs_and_resends(runner, service, tmp_path):
    fake = service(busy_posts={3})
    for name in ("a.png", "b.png", "c.png"):
        (tmp_path / name).write_bytes(name.encode())

    run_script(runner, fake, tmp_path, "-KeepCard")

    posts = [i for i, entry in enumerate(fake.log) if entry == ("POST", "/slot/videos")]
    assert len(posts) == 4, "третья отправка получила 429 и ушла снова"
    resend = posts[3]
    assert fake.log.index(("DELETE", "/slot/videos/video_%024x" % 1)) < resend
    assert fake.log.index(("DELETE", "/slot/videos/video_%024x" % 2)) < resend
    assert [p["input_reference"] for p in fake.posts] == [b"a.png", b"b.png", b"c.png"]
    assert len(list(tmp_path.glob("*.mp4"))) == 3
    assert fake.release == [], "-KeepCard: карта не освобождается"


@pytest.mark.integration      # ждёт паузу перед повтором по-настоящему (5 с)
@pytest.mark.parametrize("runner", RUNNERS)
def test_unavailable_service_gets_the_job_again_after_a_pause(runner, service, tmp_path):
    # 503 — служба задание не приняла: повтор безопасен, ролик выходит со второй попытки
    fake = service(unavailable=1)
    (tmp_path / "a.png").write_bytes(b"img-a")

    out = run_script(runner, fake, tmp_path, "-KeepCard")

    assert fake.log.count(("POST", "/slot/videos")) == 2 and "карта занята" in out
    assert [r["image"] for r in manifest_rows(tmp_path)] == ["a.png"] and len(list(tmp_path.glob("*.mp4"))) == 1


@pytest.mark.integration      # две паузы перед повтором по-настоящему
@pytest.mark.parametrize("runner", RUNNERS)
def test_always_unavailable_service_gives_up_after_three_tries(runner, service, tmp_path):
    fake = service(unavailable=99)
    (tmp_path / "a.png").write_bytes(b"img-a")

    out = run_script(runner, fake, tmp_path, code=3)

    assert fake.log == [("POST", "/slot/videos")] * 3, "три попытки, карту не трогает: ничего не отправлено"
    assert "не отправлен — карта или служба недоступна (503): карта занята" in out
    assert manifest_rows(tmp_path) == [] and not list(tmp_path.glob("*.mp4"))


@pytest.mark.parametrize("content", [b"", b"\xef\xbb\xbf"], ids=["empty", "bom"])
@pytest.mark.parametrize("runner", RUNNERS)
def test_empty_manifest_gets_the_header(runner, content, service, tmp_path):
    # Пустой журнал (0 байт или один BOM): раньше шапка писалась, только если файла не было
    fake = service()
    (tmp_path / "a.png").write_bytes(b"img-a")
    (tmp_path / "video-manifest.tsv").write_bytes(content)

    run_script(runner, fake, tmp_path, "-KeepCard")

    assert [r["image"] for r in manifest_rows(tmp_path)] == ["a.png"]


@pytest.mark.parametrize("runner", RUNNERS)
def test_variants_use_consecutive_seeds_and_options(runner, service, tmp_path):
    fake = service()
    (tmp_path / "x.png").write_bytes(b"img-x")

    out = run_script(runner, fake, tmp_path, "-Count", "2", "-Seed", "10", "-Seconds", "3", "-Quality", "fast",
                     "-NoSmooth", "-Prompt", "common move")

    assert [(p["seed"], p["seconds"], p["quality"], p["smooth"], p["prompt"]) for p in fake.posts] == [
        (b"10", b"3", b"fast", b"false", b"common move"), (b"11", b"3", b"fast", b"false", b"common move")]
    keys = [fingerprint(b"img-x", "common move", seed, 3, "fast", "false", k) for k, seed in ((1, 10), (2, 11))]
    assert (tmp_path / f"x_3s_s10_{keys[0][:8]}.mp4").exists() and (tmp_path / f"x_3s_s11_{keys[1][:8]}.mp4").exists()
    rows = manifest_rows(tmp_path)
    assert [(r["variant"], r["seed"], r["smooth"], r["prompt_source"]) for r in rows] == [
        ("1", "10", "false", "common"), ("2", "11", "false", "common")]
    assert "auto.txt" not in out


@pytest.mark.parametrize("runner", RUNNERS)
def test_failed_job_is_deleted_and_not_recorded(runner, service, tmp_path):
    fake = service(fail_names={b"bad"})
    (tmp_path / "bad.png").write_bytes(b"bad")
    (tmp_path / "good.png").write_bytes(b"good")

    run_script(runner, fake, tmp_path, code=3)      # частичный провал — не успех

    assert ("DELETE", "/slot/videos/video_%024x" % 1) in fake.log
    assert ("GET", "/slot/videos/video_%024x/content" % 1) not in fake.log
    assert [r["image"] for r in manifest_rows(tmp_path)] == ["good.png"]
    assert [p.name for p in tmp_path.glob("*.mp4")] == [
        f"good_5s_s{int(fake.posts[1]['seed'])}_{fingerprint(b'good')[:8]}.mp4"]
    assert fake.jobs == {}


@pytest.mark.parametrize("runner", RUNNERS)
def test_failed_write_of_one_video_does_not_stop_the_run(runner, service, tmp_path):
    # Ролик не записался (здесь имя .part занято каталогом — отказ записи и на Windows, и на Linux):
    # раньше исключение роняло прогон и снимало у шлюза уже принятые задания
    fake = service()
    (tmp_path / "a.png").write_bytes(b"img-a")
    (tmp_path / "b.png").write_bytes(b"img-b")
    blocked = tmp_path / f"a_5s_s42_{fingerprint(b'img-a', seed=42)[:8]}.mp4.part"
    blocked.mkdir()

    out = run_script(runner, fake, tmp_path, "-Seed", "42", code=3)

    assert "ролик 1/2 a.png: не записался —" in out and "готово 1, не вышло 1" in out
    assert ("DELETE", "/slot/videos/video_%024x" % 1) in fake.log and fake.jobs == {}
    assert [r["image"] for r in manifest_rows(tmp_path)] == ["b.png"]
    assert [p.name for p in tmp_path.glob("*.mp4")] == [f"b_5s_s42_{fingerprint(b'img-b', seed=42)[:8]}.mp4"]
    assert blocked.is_dir(), "чужой каталог не трогается: убирается только свой недописанный файл"
    assert fake.release == [{"model": MODEL}]


@pytest.mark.parametrize("runner", RUNNERS)
def test_all_jobs_rejected_exits_with_failure(runner, service, tmp_path):
    fake = service(reject_posts=True)
    (tmp_path / "a.png").write_bytes(b"img-a")

    out = run_script(runner, fake, tmp_path, code=3)

    assert "модель не та" in out and "готово 0, не вышло 1" in out
    assert manifest_rows(tmp_path) == [] and not list(tmp_path.glob("*.mp4"))


@pytest.mark.parametrize("runner", RUNNERS)
def test_same_name_images_and_taken_names_keep_every_result(runner, service, tmp_path):
    # Две картинки с одним именем и один seed: раньше обе ложились в photo_5s_s42.mp4
    fake = service()
    (tmp_path / "photo.jpg").write_bytes(b"img-jpg")
    (tmp_path / "photo.png").write_bytes(b"img-png")
    keys = [fingerprint(b"img-jpg", seed=42), fingerprint(b"img-png", seed=42)]
    # Имя уже занято (так выходит у -Redo с тем же seed): прежний файл не перезаписывается, новому — «-2»
    taken = tmp_path / f"photo_5s_s42_{keys[0][:8]}.mp4"
    taken.write_bytes(b"earlier")

    run_script(runner, fake, tmp_path, "-Seed", "42", "-KeepCard")

    names = [f"photo_5s_s42_{keys[0][:8]}-2.mp4", f"photo_5s_s42_{keys[1][:8]}.mp4"]
    rows = manifest_rows(tmp_path)
    assert [(r["image"], r["video"], r["fingerprint"]) for r in rows] == [
        ("photo.jpg", names[0], keys[0]), ("photo.png", names[1], keys[1])]
    assert [(tmp_path / n).read_bytes() for n in names] == [b"mp4-video_%024x" % n for n in (1, 2)]
    assert taken.read_bytes() == b"earlier" and not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("runner", RUNNERS)
def test_changed_request_is_made_again(runner, service, tmp_path):
    # Пропуск — только по отпечатку: новое содержимое картинки под тем же именем — новый ролик
    fake = service()
    image = tmp_path / "x.png"
    image.write_bytes(b"img-old")
    args = ("-Seed", "7", "-Prompt", "move", "-Model", "wan-other", "-Quality", "fast", "-KeepCard")

    run_script(runner, fake, tmp_path, *args)
    image.write_bytes(b"img-new")
    run_script(runner, fake, tmp_path, *args)

    keys = [fingerprint(b"img-old", "move", 7, quality="fast", model="wan-other"),
            fingerprint(b"img-new", "move", 7, quality="fast", model="wan-other")]
    rows = manifest_rows(tmp_path)
    assert [(r["video"], r["model"], r["fingerprint"]) for r in rows] == [
        (f"x_5s_s7_{keys[0][:8]}.mp4", "wan-other", keys[0]), (f"x_5s_s7_{keys[1][:8]}.mp4", "wan-other", keys[1])]
    assert [p["input_reference"] for p in fake.posts] == [b"img-old", b"img-new"]


@pytest.mark.parametrize("runner", RUNNERS)
def test_old_manifest_rows_are_matched_by_name_with_a_warning(runner, service, tmp_path):
    # Журнал прежней версии: 9 столбцов, без модели и отпечатка
    fake = service()
    (tmp_path / "x.png").write_bytes(b"img-x")
    (tmp_path / "x_5s_s1.mp4").write_bytes(b"old-mp4")
    old = ["image", "video", "seed", "seconds", "quality", "smooth", "variant", "prompt_source", "prompt"]
    (tmp_path / "video-manifest.tsv").write_text(
        "\t".join(old) + "\n" + "x.png\tx_5s_s1.mp4\t1\t5\tstandard\ttrue\t1\tcommon\told move\n", encoding="utf-8")

    out = run_script(runner, fake, tmp_path, "-Prompt", "new move")

    assert fake.log == [], "строка без отпечатка сверяется по имени картинки и параметрам"
    assert "без отпечатка" in out and "-redo" in out.lower()
    rows = manifest_rows(tmp_path)     # шапка дополнена новыми столбцами, строка — как была
    assert [(r["video"], r["prompt"], r.get("fingerprint")) for r in rows] == [("x_5s_s1.mp4", "old move", None)]
