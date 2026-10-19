"""Клиент пакетной генерации client/studio-batch.ps1 — без сети.

Скрипт ходит в API картинок шлюза. Его интерфейс зафиксирован спекой
2026-10-19-images-api-lustify-design.md (раздел 5) и описан в Manual: умолчания -Model и
-Url, имена моделей и размеры у шлюза расходиться с ним не должны. Ключ в репозитории
пустой, а локальная копия с ключом (studio-batch.local.ps1) — в .gitignore.
"""
import json
import re
import shutil
import subprocess
import threading
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email import policy
from email.parser import BytesParser

import pytest

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
    assert "$Models = @{ lustify = 'lustify-v10-krea2-turbo'; flux = 'flux.2-klein-4b' }" in text
    assert "$Sizes = @{ portrait = '832x1216'; square = '1024x1024'; landscape = '1216x832' }" in text
    assert "'/images/generations'" in text and "'/images/release'" in text
    assert "'GATEWAY_KEY_OWNER_IMAGES'" in text and "'studio-batch.local.ps1'" in text


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
    assert d["Model"] == "lustify"
    assert d["Url"] == "http://10.77.1.15:4000/slot"
    assert d["Size"] == "portrait"
    assert d["Token"] == ""
    assert d["VaultPath"] == r"C:\work\projects\local-config\secrets\vault.env"


@pytest.mark.integration
def test_powershell_photo_upload_uses_multipart_and_writes_result(tmp_path):
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        pytest.skip("PowerShell unavailable")
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            mime = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
            fields = {p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
                      for p in mime.iter_parts()}
            received.append((self.path, self.headers["Authorization"], fields))
            body = json.dumps({"data": [{"b64_json": base64.b64encode(b"result-png").decode(), "seed": 7}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    prompts = tmp_path / "prompts.txt"
    prompts.write_text("change background\n", encoding="utf-8")
    photo = tmp_path / "photo.png"
    photo.write_bytes(b"source-photo")
    out = tmp_path / "out"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        result = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(SCRIPT), "-PromptsFile", str(prompts), "-ImagePath", str(photo),
                                 "-Strength", "0.4", "-Seed", "7", "-Token", "test-key", "-KeepCard",
                                 "-OutDir", str(out), "-Url", f"http://127.0.0.1:{server.server_port}/slot"],
                                capture_output=True, timeout=25)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)
    assert result.returncode == 0, result.stderr
    assert len(received) == 1, result.stdout
    path, auth, fields = received[0]
    assert path == "/slot/images/edits" and auth == "Bearer test-key"
    assert fields["image"] == b"source-photo" and fields["strength"] == b"0.4"
    assert fields["model"] == b"lustify-v10-krea2-turbo" and fields["seed"] == b"7"
    assert [p.read_bytes() for p in out.glob("*.png")] == [b"result-png"]
