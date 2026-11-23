"""Клиент пакетной генерации client/studio-batch.ps1 и его Python-версия studio-batch.py — без сети.

Скрипт ходит в API картинок шлюза. Его интерфейс зафиксирован спекой
2026-10-19-images-api-snofs-design.md (раздел 5) и описан в Manual: умолчания -Model и
-Url, имена моделей и размеры у шлюза расходиться с ним не должны. Ключ в репозитории
пустой, а локальная копия с ключом (studio-batch.local.ps1) — в .gitignore.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import pytest

from clients import RUNNERS, flat, run_client
from conftest import CLIENT, ROOT

SCRIPT = CLIENT / "studio-batch.ps1"
REPO = ROOT


def test_script_is_utf8_with_bom():
    # Windows PowerShell 5.1 читает файл без BOM как CP1251 — кириллица ломает разбор строк
    assert SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf")


def test_key_placeholder_is_empty_in_repo():
    text = SCRIPT.read_text(encoding="utf-8-sig")
    slots = re.findall(r"^\$EmbeddedKey = '(.*)'\s*$", text, re.M)
    assert slots == [""], "строка-заглушка ключа одна и пустая: ключ вписывает только -MakeLocal"


def test_models_and_sizes_match_the_gateway_contract():
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert "$Sizes = @{ portrait = '832x1216'; square = '1024x1024'; landscape = '1216x832' }" in text
    assert "'/images/generations'" in text and "'/images/release'" in text
    assert "'GATEWAY_KEY_OWNER_IMAGES'" in text and "'studio-batch.local.ps1'" in text


def test_script_models_and_strength_match_the_server():
    """Контракт шлюза в сессии виден через то, что шлёт ему страница (app/images.py): имена моделей,
    размеры и модели с силой изменения у обеих версий скрипта — те же."""
    import importlib.util

    import images
    spec = importlib.util.spec_from_file_location("studio_batch_client", SCRIPT.with_suffix(".py"))
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    assert client.MODELS == images.MODELS and client.SIZES == images.SIZES
    assert client.STRENGTH_MODELS == images.STRENGTH_MODELS
    assert client.MEDIA_TIMEOUT == images.MEDIA_TIMEOUT_SEC
    text = SCRIPT.read_text(encoding="utf-8-sig")
    for name, model in images.MODELS.items():
        key = name if name.isidentifier() else f"'{name}'"
        assert f"{key} = '{model}'" in text and f"'{name}'" in text, model
    strength = re.search(r"^\$StrengthModels = @\(([^)]*)\)", text, re.M)
    assert strength and set(re.findall(r"'([^']+)'", strength[1])) == images.STRENGTH_MODELS
    assert f"$MediaTimeoutSec = {images.MEDIA_TIMEOUT_SEC}" in text


def test_local_copy_is_gitignored():
    git = shutil.which("git")
    if not git:
        pytest.skip("нет git — проверка .gitignore пропущена")
    r = subprocess.run([git, "-C", str(REPO), "check-ignore", "-q", "client/studio-batch.local.ps1"],
                       capture_output=True, timeout=20)
    assert r.returncode == 0, "studio-batch.local.ps1 с ключом не покрыт .gitignore"


_PARSE = r"""
$e = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$null, [ref]$e)
$defaults = @{}
foreach ($p in $ast.ParamBlock.Parameters) {
    if ($p.DefaultValue -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
        $defaults[$p.Name.VariablePath.UserPath] = $p.DefaultValue.Value
    }
}
@{ errors = @($e | ForEach-Object { $_.Message }); defaults = $defaults } | ConvertTo-Json -Compress
"""


@pytest.mark.integration      # старт PowerShell — больше секунды
def test_powershell_parses_script_and_defaults_match_contract():
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        pytest.skip("нет powershell — разбор скрипта парсером пропущен")
    r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command",
                        "& { " + _PARSE + " }", str(SCRIPT)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["errors"] == []
    d = out["defaults"]
    assert d["Model"] == "snofs"
    assert d["Url"] == "http://10.77.1.15:4000/slot"
    assert d["Size"] == "portrait"
    assert d["Token"] == ""
    assert d["VaultPath"] == r"C:\work\projects\local-config\secrets\vault.env"


@pytest.fixture
def images():
    """Подставной API картинок на 127.0.0.1: журнал (путь, Authorization, поля или JSON).
    generations отдаёт «png-<номер>» без seed (так отвечает FLUX), на промпт «bad» — 400;
    edits — «result-png» и seed 7. files — (поле, имя файла, тип) файловых частей multipart.
    statuses — коды отказа очередных generations (429, 503) до обычного ответа."""
    log = []
    files = []
    seeds = []
    statuses = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers["Content-Type"].startswith("multipart/form-data"):
                mime = BytesParser(policy=policy.default).parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
                payload = {p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
                           for p in mime.iter_parts()}
                files.extend((p.get_param("name", header="content-disposition"), p.get_filename(),
                              p.get_content_type()) for p in mime.iter_parts() if p.get_filename())
            else:
                payload = json.loads(raw)
            log.append((self.path, self.headers["Authorization"], payload))
            code = 200
            if self.path == "/slot/images/generations" and payload.get("prompt") == "bad":
                code, reply = 400, {"error": {"message": "промпт не принят"}}
            elif self.path == "/slot/images/generations" and statuses:
                code, reply = statuses.pop(0), {"error": {"message": "карта занята"}}
            elif self.path == "/slot/images/edits":
                reply = {"data": [{"b64_json": base64.b64encode(b"result-png").decode(), "seed": 7}]}
            elif self.path == "/slot/images/generations":
                reply = {"data": [{"b64_json": base64.b64encode(b"png-%d" % len(log)).decode()}]}
                if seeds:
                    reply["data"][0]["seed"] = seeds.pop(0)
            else:
                reply = {"ok": True}
            body = json.dumps(reply).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield SimpleNamespace(log=log, files=files, seeds=seeds, statuses=statuses,
                          url=f"http://127.0.0.1:{server.server_port}/slot")
    server.shutdown()
    server.server_close()
    thread.join(1)


def run_script(runner, images, *args, code=0):
    """code — ожидаемый код выхода: 0 — все картинки готовы, 3 — хотя бы одна не вышла."""
    r = run_client(runner, SCRIPT, [*args, "-Token", "test-key", "-Url", images.url])
    out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
    assert r.returncode == code, out
    assert b"test-key" not in r.stdout + r.stderr, "ключ не печатается"
    return flat(out)


@pytest.mark.integration  # настоящий CLI и HTTP: > 1 с под нагрузкой Windows
def test_python_run_survives_a_pipe_in_a_non_utf8_encoding(images, tmp_path):
    """В трубу и файл Python пишет кодировкой локали (Windows — cp1251): «×» и «→» сообщений
    роняли пакет посреди работы."""
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("first\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT.with_suffix(".py")), "--prompts-file", str(prompts),
                        "--token", "test-key", "--url", images.url, "--out-dir", str(tmp_path / "out")],
                       capture_output=True, timeout=25, env={**os.environ, "PYTHONIOENCODING": "cp1251"})
    assert r.returncode == 0, r.stderr.decode("cp1251", "replace")


@pytest.mark.parametrize("runner", RUNNERS)
def test_photo_upload_uses_multipart_and_writes_result(runner, images, tmp_path):
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("change background\n", encoding="utf-8")
    photo = tmp_path / "photo.JPG"
    photo.write_bytes(b"source-photo")
    out = tmp_path / "out"

    run_script(runner, images, "-PromptsFile", str(prompts), "-ImagePath", str(photo), "-Strength", "0.4",
               "-Seed", "7", "-KeepCard", "-OutDir", str(out))

    assert len(images.log) == 1
    path, auth, fields = images.log[0]
    assert path == "/slot/images/edits" and auth == "Bearer test-key"
    assert fields["image"] == b"source-photo" and fields["strength"] == b"0.4"
    assert fields["model"] == b"snofs-krea2-v1.6" and fields["seed"] == b"7"
    # Имя файла — своё (имя фото владельца в журналы службы не идёт), тип — по расширению
    assert images.files == [("image", "input.jpg", "image/jpeg")]
    assert [p.read_bytes() for p in out.glob("*.png")] == [b"result-png"]


@pytest.mark.parametrize("runner", RUNNERS)
def test_fineview_and_jib_send_strength_like_the_page_and_release_the_comfy_slot(runner, images, tmp_path):
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("change background\n", encoding="utf-8")
    photo = tmp_path / "photo.png"
    photo.write_bytes(b"source-photo")
    for model in ("jib-moss", "fineview"):
        run_script(runner, images, "-PromptsFile", str(prompts), "-ImagePath", str(photo), "-Strength", "0.3",
                   "-Model", model, "-Seed", "7", "-OutDir", str(tmp_path / model))

    edits = [fields for path, _, fields in images.log if path == "/slot/images/edits"]
    # Сила — только у Jib (как у SNOFS); FineView берёт фото референсом
    assert [(f["model"], f.get("strength")) for f in edits] == [(b"jib-moss-v2", b"0.3"), (b"fineview-qw21", None)]
    # Обе занимают слот ComfyUI SNOFS: release — по слоту, как у студии (images.RELEASE_MODELS)
    releases = [body for path, _, body in images.log if path == "/slot/images/release"]
    assert releases == [{"model": "snofs-krea2-v1.6"}] * 2


@pytest.mark.parametrize("runner", RUNNERS)
def test_resume_skips_recorded_pairs_with_the_file_in_place(runner, images, tmp_path):
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("first\nsecond\nFirst\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.png").write_bytes(b"a")
    (out / "b.png").write_bytes(b"b")
    (out / "c.png").write_bytes(b"c")
    rows = ["n\tfile\tseed\tsize\tmodel\tprompt",
            "1-1\ta.png\t1\tportrait\tsnofs\tfirst",          # готово — пропуск
            "2-1\tgone.png\t1\tportrait\tsnofs\tsecond",      # файла нет — заново
            "2-1\tb.png\t1\tsquare\tsnofs\tsecond",           # другой размер — заново
            "3-1\tc.png\t1\tportrait\tsnofs\tfirst"]          # «first» второй раз: «First» — другой промпт
    (out / "manifest.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    text = run_script(runner, images, "-PromptsFile", str(prompts), "-Seed", "5", "-KeepCard", "-OutDir", str(out),
                      "-Resume")

    gens = [body["prompt"] for path, _, body in images.log if path == "/slot/images/generations"]
    assert gens == ["second", "First"]
    assert "пропущено: 1" in text and "2 готово, 0 не вышло" in text
    lines = (out / "manifest.tsv").read_text(encoding="utf-8").splitlines()
    assert lines == [*rows, "2-1\t002-01_second_s5.png\t5\tportrait\tsnofs\tsecond",
                     "3-1\t003-01_First_s5.png\t5\tportrait\tsnofs\tFirst"]


@pytest.mark.parametrize("runner", RUNNERS)
def test_busy_gateway_is_retried_up_to_three_times(runner, images, tmp_path):
    """429 и 503 — шлюз картинку не делал: повтор через 5 с, всего до трёх попыток (как у роликов)."""
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("first\n", encoding="utf-8")
    images.statuses.extend([503, 429])
    out = tmp_path / "out"

    text = run_script(runner, images, "-PromptsFile", str(prompts), "-Seed", "5", "-KeepCard", "-OutDir", str(out))

    assert [path for path, _, _ in images.log] == ["/slot/images/generations"] * 3
    assert "(503): карта занята — повторю через 5 с" in text and "(429): карта занята — повторю через 5 с" in text
    assert [p.name for p in out.glob("*.png")] == ["001-01_first_s5.png"]


@pytest.mark.integration
@pytest.mark.parametrize("runner", RUNNERS)
def test_same_seed_keeps_the_image_from_an_earlier_run(runner, images, tmp_path):
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("same prompt\n", encoding="utf-8")
    out = tmp_path / "out"
    args = ("-PromptsFile", str(prompts), "-Seed", "17", "-KeepCard", "-OutDir", str(out))
    run_script(runner, images, *args, "-Size", "portrait")
    run_script(runner, images, *args, "-Size", "square")

    rows = (out / "manifest.tsv").read_text(encoding="utf-8-sig").splitlines()[1:]
    assert len(rows) == 2
    paths = [out / row.split("\t")[1] for row in rows]
    assert paths[0] != paths[1]
    assert [path.read_bytes() for path in paths] == [b"png-1", b"png-2"]


@pytest.mark.parametrize("runner", RUNNERS)
def test_prompts_file_run_names_files_writes_manifest_and_releases(runner, images, tmp_path):
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("﻿# заметка\n\n  first prompt \nsecond: a/b\n", encoding="utf-8")
    out = tmp_path / "out"

    run_script(runner, images, "-PromptsFile", str(prompts), "-Model", "flux", "-Size", "square",
               "-Count", "2", "-Seed", "5", "-OutDir", str(out))

    gens = [body for path, _, body in images.log if path == "/slot/images/generations"]
    assert [(b["prompt"], b["seed"]) for b in gens] == [
        ("first prompt", 5), ("first prompt", 6), ("second: a/b", 5), ("second: a/b", 6)]
    assert all((b["model"], b["size"], b["n"], b["response_format"]) == ("flux-3-image-klein", "1024x1024", 1, "b64_json")
               and "strength" not in b for b in gens)
    # Без seed в ответе (FLUX) — в имени и журнале запрошенный; из имени выпали «:» и «/»
    names = ["001-01_first_prompt_s5.png", "001-02_first_prompt_s6.png",
             "002-01_second_ab_s5.png", "002-02_second_ab_s6.png"]
    assert sorted(p.name for p in out.glob("*.png")) == names
    assert [(out / n).read_bytes() for n in names] == [b"png-1", b"png-2", b"png-3", b"png-4"]
    assert (out / "manifest.tsv").read_text(encoding="utf-8").splitlines() == [
        "n\tfile\tseed\tsize\tmodel\tprompt",
        f"1-1\t{names[0]}\t5\tsquare\tflux\tfirst prompt", f"1-2\t{names[1]}\t6\tsquare\tflux\tfirst prompt",
        f"2-1\t{names[2]}\t5\tsquare\tflux\tsecond: a/b", f"2-2\t{names[3]}\t6\tsquare\tflux\tsecond: a/b"]
    assert images.log[-1] == ("/slot/images/release", "Bearer test-key", {"model": "flux-3-image-klein"})


@pytest.mark.parametrize("runner", RUNNERS)
def test_response_seed_is_normalized_before_naming_and_manifest(runner, images, tmp_path):
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("first\n", encoding="utf-8")
    images.seeds.append("00042")
    out = tmp_path / "out"

    run_script(runner, images, "-PromptsFile", str(prompts), "-KeepCard", "-OutDir", str(out))

    assert [p.name for p in out.glob("*.png")] == ["001-01_first_s42.png"]
    assert (out / "manifest.tsv").read_text(encoding="utf-8").splitlines() == [
        "n\tfile\tseed\tsize\tmodel\tprompt", "1-1\t001-01_first_s42.png\t42\tportrait\tsnofs\tfirst"]


@pytest.mark.parametrize("runner", RUNNERS)
def test_partly_failed_run_exits_with_failure_and_still_releases(runner, images, tmp_path):
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("good\nbad\n", encoding="utf-8")
    out = tmp_path / "out"

    text = run_script(runner, images, "-PromptsFile", str(prompts), "-Seed", "1", "-OutDir", str(out), code=3)

    assert "промпт не принят" in text and "1 готово, 1 не вышло" in text
    assert [p.name for p in out.glob("*.png")] == ["001-01_good_s1.png"]
    assert images.log[-1][0] == "/slot/images/release"


@pytest.mark.parametrize("runner", RUNNERS)
def test_fully_failed_run_exits_with_failure(runner, images, tmp_path):
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("bad\n", encoding="utf-8")
    out = tmp_path / "out"

    text = run_script(runner, images, "-PromptsFile", str(prompts), "-KeepCard", "-OutDir", str(out), code=3)

    assert "0 готово, 1 не вышло" in text and not list(out.glob("*.png"))
