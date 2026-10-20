"""Клиент пакетного видео client/studio-video.ps1 и его Python-версия studio-video.py — без сети.

Скрипт ходит в API видео шлюза (форма OpenAI Videos). Его интерфейс зафиксирован спекой
2026-10-19-srvllm-studio-video-design.md (раздел 7 и «Решения исполнителя»: контракт API
видео и параметры скрипта). Ключ в репозитории пустой, а локальная копия с ключом
(studio-video.local.ps1) — в .gitignore. Прогоны обеих версий идут против подставной службы
на 127.0.0.1: она отвечает «готово» с первого опроса, поэтому скрипт не ждёт свои 5 с.
"""
import json
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email import policy
from email.parser import BytesParser

import pytest

from clients import RUNNERS, run_client
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


class FakeVideos:
    """Подставная служба видео: задание готово с первого опроса, журнал всех запросов."""

    def __init__(self, busy_posts=(), fail_names=()):
        self.log = []            # (метод, путь) по порядку
        self.posts = []          # поля multipart принятых заданий
        self.post_attempts = 0
        self.busy_posts = set(busy_posts)    # номера попыток POST, на которые 429
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


def run_script(runner, fake, folder, *extra):
    r = run_client(runner, SCRIPT, ["-Folder", str(folder), "-Token", "test-key", "-Url", fake.url, *extra])
    out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, out
    assert b"test-key" not in r.stdout + r.stderr, "ключ не печатается"
    return out


def manifest_rows(folder):
    lines = (folder / "video-manifest.tsv").read_text(encoding="utf-8-sig").splitlines()
    header = lines[0].split("\t")
    assert header == ["image", "video", "seed", "seconds", "quality", "smooth", "variant", "prompt_source", "prompt"]
    return [dict(zip(header, line.split("\t"))) for line in lines[1:]]


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

    run_script(runner, fake, tmp_path)

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

    names = [f"a_5s_s{seeds[0]}.mp4", f"B_5s_s{seeds[1]}.mp4", f"c_5s_s{seeds[2]}.mp4"]
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
    assert {(r["seconds"], r["quality"], r["smooth"], r["variant"]) for r in rows} == {("5", "standard", "true", "1")}
    assert fake.log[-1] == ("POST", "/slot/images/release") and fake.release == [{"model": MODEL}]

    # Повторный запуск: всё в журнале и файлы на месте — ни одного запроса, карта не трогается
    fake.log.clear()
    run_script(runner, fake, tmp_path)
    assert fake.log == []

    # -Redo: снова все три; у B и c теперь свой промпт из .auto.txt
    run_script(runner, fake, tmp_path, "-Redo")
    assert len(fake.posts) == 6
    assert [p.get("prompt") for p in fake.posts[3:]] == [b"slow turn\tto camera", b"auto motion 2", b"auto motion 3"]
    rows = manifest_rows(tmp_path)
    assert len(rows) == 6 and [r["prompt_source"] for r in rows[3:]] == ["file", "auto", "auto"]
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


@pytest.mark.parametrize("runner", RUNNERS)
def test_variants_use_consecutive_seeds_and_options(runner, service, tmp_path):
    fake = service()
    (tmp_path / "x.png").write_bytes(b"img-x")

    out = run_script(runner, fake, tmp_path, "-Count", "2", "-Seed", "10", "-Seconds", "3", "-Quality", "fast",
                     "-NoSmooth", "-Prompt", "common move")

    assert [(p["seed"], p["seconds"], p["quality"], p["smooth"], p["prompt"]) for p in fake.posts] == [
        (b"10", b"3", b"fast", b"false", b"common move"), (b"11", b"3", b"fast", b"false", b"common move")]
    assert (tmp_path / "x_3s_s10.mp4").exists() and (tmp_path / "x_3s_s11.mp4").exists()
    rows = manifest_rows(tmp_path)
    assert [(r["variant"], r["seed"], r["smooth"], r["prompt_source"]) for r in rows] == [
        ("1", "10", "false", "common"), ("2", "11", "false", "common")]
    assert "auto.txt" not in out


@pytest.mark.parametrize("runner", RUNNERS)
def test_failed_job_is_deleted_and_not_recorded(runner, service, tmp_path):
    fake = service(fail_names={b"bad"})
    (tmp_path / "bad.png").write_bytes(b"bad")
    (tmp_path / "good.png").write_bytes(b"good")

    run_script(runner, fake, tmp_path)

    assert ("DELETE", "/slot/videos/video_%024x" % 1) in fake.log
    assert ("GET", "/slot/videos/video_%024x/content" % 1) not in fake.log
    assert [r["image"] for r in manifest_rows(tmp_path)] == ["good.png"]
    assert [p.name for p in tmp_path.glob("*.mp4")] == [f"good_5s_s{int(fake.posts[1]['seed'])}.mp4"]
    assert fake.jobs == {}
