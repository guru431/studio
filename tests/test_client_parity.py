"""Паритет пакетных клиентов: одна папка крайних входов через обе версии studio-batch и studio-video.

Каждая версия гоняется на одной и той же папке-наборе, а ожидания — общие: план заданий (что ушло
в шлюз и с каким промптом), журнал и код выхода. Расхождение `.ps1` и `.py` на этих входах — это
расхождение с общим ожиданием. Python — в быстром наборе, PowerShell — `integration` (`clients.RUNNERS`).

Набор: служебные файлы macOS с точкой в имени («._IMG.jpg» с флешки), журнал с BOM в формате
прежней версии, строки журнала и промпты с U+2028 / VT / FF / NEL (копия с веба) — .NET
`ReadAllLines` и `Get-Content` режут строки только по CR/LF, так же должна и Python-версия.
"""
import shutil

import pytest

from clients import RUNNERS, local_copy_drift, ps_encoding, run_client
from conftest import CLIENT
from test_studio_batch_client import images                                   # noqa: F401 (фикстура)
from test_studio_batch_client import run_script as run_batch
from test_studio_video_client import HEADER, MODEL, fingerprint, service       # noqa: F401 (фикстура)
from test_studio_video_client import run_script as run_video

# Разделители строк Unicode, которых нет у .NET: Python `str.splitlines` режет и по ним
ODD = "\u2028\x0b\x0c\x85"
OLD_HEADER = "image\tvideo\tseed\tseconds\tquality\tsmooth\tvariant\tprompt_source\tprompt"


def manifest_rows(folder):
    """Строки журнала по CR/LF, как их читают обе версии (splitlines порезал бы по разделителям Unicode)."""
    lines = (folder / "video-manifest.tsv").read_bytes().decode("utf-8-sig").replace("\r\n", "\n").split("\n")
    assert lines[0].split("\t") == HEADER and lines[-1] == ""
    return [dict(zip(HEADER, line.split("\t"))) for line in lines[1:-1]]


def video_folder(path):
    """Папка роликов с крайними входами; возвращает отпечатки a и b для сверки."""
    (path / "a.png").write_bytes(b"img-a")
    (path / "a.txt").write_text(f"turn{ODD}left", encoding="utf-8")
    (path / "._a.png").write_bytes(b"macos-junk")        # AppleDouble с флешки: не картинка
    (path / ".hidden.jpg").write_bytes(b"dot-file")
    # b: сделан раньше с промптом, где был U+2028 (так писала прежняя версия журнала), теперь промпт другой
    (path / "b.png").write_bytes(b"img-b")
    (path / "b.txt").write_text("new b", encoding="utf-8")
    old_b = fingerprint(b"img-b", "old\u2028b")
    (path / "b_old.mp4").write_bytes(b"old-b")
    # c: строка прежней версии без отпечатка — сверяется по имени и параметрам
    (path / "c.png").write_bytes(b"img-c")
    (path / "c_old.mp4").write_bytes(b"old-c")
    rows = [OLD_HEADER,
            f"b.png\tb_old.mp4\t1\t5\tstandard\ttrue\t1\tfile\told\u2028b\t{MODEL}\t{old_b}",
            "c.png\tc_old.mp4\t1\t5\tstandard\ttrue\t1\tcommon\told\x85move"]
    (path / "video-manifest.tsv").write_bytes(b"\xef\xbb\xbf" + "\n".join(rows).encode() + b"\n")
    return fingerprint(b"img-a", f"turn{ODD}left"), fingerprint(b"img-b", "new b")


@pytest.mark.parametrize("runner", RUNNERS)
def test_video_clients_plan_the_same_jobs_on_edge_inputs(runner, service, tmp_path):
    fake = service()
    key_a, key_b = video_folder(tmp_path)

    out = run_video(runner, fake, tmp_path, "-KeepCard", code=0)

    # Служебные файлы с точкой не уходят; b — заново (промпт сменился), c — пропущен по строке без отпечатка
    assert [(p["input_reference"], p.get("prompt")) for p in fake.posts] == [
        (b"img-a", f"turn{ODD}left".encode()), (b"img-b", b"new b")]
    assert "без отпечатка" in out and "пропущено: 1" in out
    rows = manifest_rows(tmp_path)
    assert [(r["image"], r.get("fingerprint")) for r in rows] == [
        ("b.png", fingerprint(b"img-b", "old\u2028b")), ("c.png", None), ("a.png", key_a), ("b.png", key_b)]
    # Новые строки журнала — без разделителей строк Unicode: иначе их порежет любой читатель TSV
    assert rows[2]["prompt"] == "turn left"
    raw = (tmp_path / "video-manifest.tsv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and raw.count(b"\xef\xbb\xbf") == 1


@pytest.mark.parametrize("runner", RUNNERS)
def test_batch_clients_read_the_same_prompts_on_edge_inputs(runner, images, tmp_path):
    prompts = tmp_path / "prompts.txt"
    prompts.write_bytes(b"\xef\xbb\xbf" + f"# note\r\nturn{ODD}left\r\n\r\n  plain  \n".encode())
    out = tmp_path / "out"

    run_batch(runner, images, "-PromptsFile", str(prompts), "-Seed", "5", "-KeepCard", "-OutDir", str(out), code=0)

    gens = [body["prompt"] for path, _, body in images.log if path == "/slot/images/generations"]
    assert gens == [f"turn{ODD}left", "plain"]
    lines = (out / "manifest.tsv").read_bytes().decode().split("\n")
    assert lines[1:] == ["1-1\t001-01_turn_left_s5.png\t5\tportrait\tlustify\tturn left",
                         "2-1\t002-01_plain_s5.png\t5\tportrait\tlustify\tplain", ""]


GATEWAY = "http://gw.example.net:4000/slot"


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("name", ["studio-batch", "studio-video"])
def test_make_local_embeds_the_key_and_the_gateway_address(runner, name, tmp_path):
    """После очистки публикации адрес шлюза в скрипте — пример: копия, где вписан только ключ, по умолчанию
    не работала. -GatewayUrl вписывает адрес умолчанием -Url; остальные строки — как у скрипта."""
    for suffix in (".ps1", ".py"):
        shutil.copy(CLIENT / (name + suffix), tmp_path / (name + suffix))
    (tmp_path / "vault.env").write_text('OTHER=x\nGATEWAY_KEY_OWNER_IMAGES="sk-owner-1"\n', encoding="utf-8")

    r = run_client(runner, tmp_path / (name + ".ps1"),
                   ["-MakeLocal", "-VaultPath", str(tmp_path / "vault.env"), "-GatewayUrl", GATEWAY + "/"])

    out = r.stdout.decode(ps_encoding() if runner == "ps" else "utf-8", "replace")
    assert r.returncode == 0, out + r.stderr.decode("utf-8", "replace")
    assert b"sk-owner-1" not in r.stdout + r.stderr and GATEWAY in out
    suffix = ".ps1" if runner == "ps" else ".py"
    script, local = CLIENT / (name + suffix), tmp_path / (name + ".local" + suffix)
    assert local_copy_drift(script, local) == []
    text = local.read_bytes().decode("utf-8-sig")
    if runner == "ps":
        assert "$EmbeddedKey = 'sk-owner-1'" in text and f"[string]$Url = '{GATEWAY}'," in text
    else:
        assert "\nEMBEDDED_KEY = 'sk-owner-1'\n" in text and f"\nURL = '{GATEWAY}'\n" in text


def test_local_copy_drift_ignores_only_the_key_and_the_address(tmp_path):
    script, local = tmp_path / "s.ps1", tmp_path / "s.local.ps1"
    lines = ["param(", "    [Parameter(ParameterSetName = 'Run')][string]$Url = 'http://10.77.1.15:4000/slot',", ")",
             "$EmbeddedKey = ''", "Write-Host 1"]
    script.write_bytes(b"\xef\xbb\xbf" + "\r\n".join(lines).encode())
    copy = [*lines]
    copy[1], copy[3] = copy[1].replace("10.77.1.15", "gw"), "$EmbeddedKey = 'secret'"
    local.write_text("\n".join(copy), encoding="utf-8")
    assert local_copy_drift(script, local) == []
    local.write_text("\n".join([*copy[:4], "Write-Host 2"]), encoding="utf-8")
    assert local_copy_drift(script, local) == [5]
    local.write_text("\n".join(copy[:4]), encoding="utf-8")
    assert local_copy_drift(script, local) == [5]
