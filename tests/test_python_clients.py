"""Python-версии клиентов client/studio-batch.py и studio-video.py — то, чего нет у PowerShell.

Поведение обеих версий сверяют общие сценарии в test_studio_batch_client.py и
test_studio_video_client.py. Здесь — формат файла (shebang на Linux), пустая заглушка ключа,
умолчания по контракту шлюза и локальная копия с ключом (*.local.py — в .gitignore).
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys

import pytest

from clients import UTF8_ENV
from conftest import CLIENT, ROOT

BATCH = CLIENT / "studio-batch.py"
VIDEO = CLIENT / "studio-video.py"
SCRIPTS = pytest.mark.parametrize("script", [BATCH, VIDEO], ids=lambda p: p.name)
URL = "http://10.77.1.15:4000/slot"
VAULT = r"C:\work\projects\local-config\secrets\vault.env"


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_").replace(".", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@SCRIPTS
def test_file_runs_by_shebang_on_linux(script):
    raw = script.read_bytes()
    # BOM и CRLF ломают shebang: «env: 'python3\r': No such file or directory»
    assert raw.startswith(b"#!/usr/bin/env python3\n") and b"\r" not in raw


@SCRIPTS
def test_key_placeholder_is_empty_in_repo(script):
    slots = re.findall(r"^EMBEDDED_KEY = (.*)$", script.read_text(encoding="utf-8"), re.M)
    assert slots == ['""'], "строка-заглушка ключа одна и пустая: ключ вписывает только --make-local"


def test_batch_defaults_match_the_gateway_contract():
    m = load(BATCH)
    assert m.MODELS == {"lustify": "lustify-v10-krea2-turbo", "flux": "flux-3-image-klein"}
    assert m.SIZES == {"portrait": "832x1216", "square": "1024x1024", "landscape": "1216x832"}
    a = m.parse_args(["--prompts-file", "p.txt"])
    assert (a.model, a.size, a.count, a.strength, a.url, a.token, a.vault_path) == (
        "lustify", "portrait", 1, 0.65, URL, "", VAULT)
    assert (m.VAULT_KEY, m.LOCAL_NAME) == ("GATEWAY_KEY_OWNER_IMAGES", "studio-batch.local.py")


def test_video_defaults_match_the_gateway_contract():
    m = load(VIDEO)
    a = m.parse_args(["--folder", "pics"])
    assert (a.model, a.seconds, a.quality, a.count, a.prompt, a.url, a.token, a.vault_path) == (
        "wan2.2-remix-i2v-v3", 5, "standard", 1, "", URL, "", VAULT)
    assert (m.VAULT_KEY, m.LOCAL_NAME, m.MANIFEST) == (
        "GATEWAY_KEY_OWNER_IMAGES", "studio-video.local.py", "video-manifest.tsv")


@pytest.mark.parametrize("script, args", [(BATCH, ["--prompts-file", "p.txt"]), (VIDEO, ["--folder", "."])],
                         ids=["batch", "video"])
def test_without_key_refuses_and_points_to_make_local(script, args, capsys):
    assert load(script).main(args) == 1
    assert "--make-local" in capsys.readouterr().err


@pytest.mark.integration  # настоящий CLI-процесс с файловой копией: > 1 с под нагрузкой Windows
@SCRIPTS
def test_make_local_writes_a_copy_with_the_vault_key(script, tmp_path):
    copy = tmp_path / script.name
    shutil.copy(script, copy)
    vault = tmp_path / "vault.env"
    vault.write_text('OTHER=x\nGATEWAY_KEY_OWNER_IMAGES="sk-owner-1"\n', encoding="utf-8")

    r = subprocess.run([sys.executable, str(copy), "--make-local", "--vault-path", str(vault)],
                       capture_output=True, timeout=20, env=UTF8_ENV)

    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert b"sk-owner-1" not in r.stdout + r.stderr, "ключ не печатается"
    local = tmp_path / script.name.replace(".py", ".local.py")
    assert local.read_bytes() == script.read_bytes().replace(
        b'\nEMBEDDED_KEY = ""\n', b"\nEMBEDDED_KEY = 'sk-owner-1'\n")
    assert load(local).EMBEDDED_KEY == "sk-owner-1"
    if os.name == "posix":
        assert local.stat().st_mode & 0o777 == 0o700, "ключ — только владельцу файла"


@pytest.mark.parametrize("name", ["studio-batch.local.py", "studio-video.local.py"])
def test_local_copy_is_gitignored(name):
    git = shutil.which("git")
    if not git:
        pytest.skip("нет git — проверка .gitignore пропущена")
    r = subprocess.run([git, "-C", str(ROOT), "check-ignore", "-q", f"client/{name}"],
                       capture_output=True, timeout=20)
    assert r.returncode == 0, f"{name} с ключом не покрыт .gitignore"
