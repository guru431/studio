"""Linux platform paths, CLI updates and native filesystem contracts."""
import concurrent.futures
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from studio_agent import cli, codex, codex_rpc, host, linux_info, linux_secrets, opencode_rpc, software


def test_linux_layout_cli_and_architecture(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "MACOS", False)
    monkeypatch.setattr(host, "LINUX", True)
    monkeypatch.setattr(host.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", "relative/ignored")
    assert host.platform_id() == "linux"
    assert host.vscode_platform() == "linux-arm64"
    assert host.data_home() == tmp_path / ".local/share/studio-agent"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert host.install_home() == tmp_path / "data/studio-agent/app"
    assert host.python(tmp_path) == tmp_path / ".venv/bin/python"
    monkeypatch.setattr(host, "find_tool", lambda name: None)
    codex = tmp_path / ".vscode/extensions/openai.chatgpt-1/bin/linux-aarch64/codex"
    codex.parent.mkdir(parents=True)
    codex.touch()
    assert codex_rpc.find_cli() == codex
    monkeypatch.setattr(host, "find_tool", lambda name: tmp_path / "bin" / name)
    assert cli.find_cli(tmp_path) == tmp_path / "bin/claude"
    assert opencode_rpc.find_cli() == tmp_path / "bin/opencode"


def test_linux_update_uses_existing_global_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "LINUX", True)
    modules = tmp_path / "lib/node_modules"
    binary = modules / "opencode-linux-x64/bin/opencode"
    binary.parent.mkdir(parents=True)
    binary.touch()
    (binary.parent.parent / "package.json").write_text('{"name":"opencode-linux-x64"}')
    (modules / "opencode-ai").mkdir()
    (modules / "opencode-ai/package.json").write_text('{"name":"opencode-ai","version":"1.2.3"}')
    monkeypatch.setattr(opencode_rpc, "find_cli", lambda: binary)
    monkeypatch.setattr(software, "_npm", lambda: ["node", "npm-cli.js"])
    monkeypatch.setattr(software.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b"1.2.3"))
    item = software.inspect("opencode")
    command, _ = software.command(item, "1.2.4")
    assert item["source"] == "npm" and command[command.index("--prefix") + 1] == str(tmp_path.resolve())


def test_linux_session_access_distinguishes_project_directory_case(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "LINUX", True)
    assert not codex.same_project(str(tmp_path / "Private"), tmp_path / "private")


def test_linux_card_handles_missing_memory_and_lists_local_disks(monkeypatch):
    files = {"meminfo": "MemTotal:   1200 kB\nMemAvailable: 300 kB\n",
             "mountinfo": "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
                          "2 1 8:2 / /mnt/disk\\040one rw - ext4 /dev/sdb rw\n"
                          "3 1 0:3 / /network rw - nfs server:/vol rw\n"}
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **kw: files[p.name])
    monkeypatch.setattr(linux_info.shutil, "disk_usage", lambda path: SimpleNamespace(total=2000, free=400))
    assert linux_info.memory_status() == {"total": 1200 * 1024, "available": 300 * 1024}
    assert [disk["path"] for disk in linux_info.local_disks()] == ["/", "/mnt/disk one"]
    files["meminfo"] = "MemTotal: 1 kB"
    assert linux_info.memory_status() is None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux secret file permissions")
def test_linux_user_key_is_private_and_published_once(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        blobs = list(pool.map(linux_secrets.protect, [b"private-test" for _ in range(8)]))
    key = tmp_path / "studio-agent/secrets.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
    assert all(b"private-test" not in blob and linux_secrets.unprotect(blob) == b"private-test" for blob in blobs)
    with pytest.raises(OSError):
        linux_secrets.unprotect(blobs[0][:-2] + b"xx")
    key.chmod(0o644)
    with pytest.raises(PermissionError):
        linux_secrets.protect(b"test")
    key.unlink()
    with pytest.raises(FileNotFoundError):
        linux_secrets.unprotect(blobs[0])
    assert not key.exists()
    dest = tmp_path / "elsewhere"
    dest.write_bytes(b"untouched")
    key.symlink_to(dest)
    with pytest.raises(OSError):
        linux_secrets.protect(b"test")
    assert dest.read_bytes() == b"untouched"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux renameat2")
def test_linux_rename_never_clobbers_a_destination(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_text("a")
    second.write_text("b")
    with pytest.raises(FileExistsError):
        host.rename(first, second)
    assert first.read_text() == "a" and second.read_text() == "b"
