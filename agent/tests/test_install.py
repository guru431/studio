"""Установка из архива студии (`install.cmd` → `install.ps1`) — `integration`: настоящий PowerShell и git.

«Архив» — рабочее дерево без того, что `.gitattributes` помечает `export-ignore` (как `git archive` выкладки после
коммита), в папке без `.git`; `LOCALAPPDATA` — временная. Среда и пакеты (`-NoVenv`) и запуск (`-NoStart`) не
делаются: тест не ходит в сеть."""

import os
import shutil
import subprocess
import sys
import winreg

import pytest

from studio_agent import autostart

pytestmark = pytest.mark.integration

REPO = autostart.REPO
TEST_ROOT = r"Software\studio-agent-test"
RUN_KEY = TEST_ROOT + r"\Run"  # настоящий Run установка в тестах не трогает
IGNORED = ["tests/test_app.py", ".claude/claude-switch.ps1", "CLAUDE.md", "AGENTS.md", "FINDINGS.md",
           "FINDINGS-archive.md", "IDEAS.md", "pytest.ini", "requirements-dev.txt", ".gitignore", ".gitattributes"]
SHIPPED = ["install.cmd", "install.ps1", "requirements.txt", "studio_agent.pyw", "README.md", "studio_agent/app.py",
           "studio_agent/theme.py"]


def git(*args: str, stdin: str | None = None) -> str:
    """Ввод — байтами: текстовый режим Windows превратил бы `
` в `
`, и git искал бы пути с ``."""
    r = subprocess.run(["git", "-C", str(REPO), *args], input=stdin.encode("utf-8") if stdin is not None else None,
                       capture_output=True, check=True, timeout=60)
    return r.stdout.decode("utf-8")


def archive(dest):
    """Файлы будущего архива из рабочего дерева: отслеживаемые и новые, кроме `export-ignore`."""
    files = [f for f in git("ls-files", "-co", "--exclude-standard").splitlines() if f]
    marks = git("check-attr", "export-ignore", "--stdin", stdin="\n".join(files) + "\n").splitlines()
    ignored = {line.split(": ")[0] for line in marks if line.endswith(": set")}
    for f in files:
        if f in ignored or not (REPO / f).is_file():
            continue
        (dest / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / f, dest / f)
    return dest


def mark_of_the_web(path) -> None:
    with open(f"{path}:Zone.Identifier", "w", encoding="ascii") as f:
        f.write("[ZoneTransfer]\nZoneId=3\n")


def has_mark(path) -> bool:
    try:
        with open(f"{path}:Zone.Identifier", "rb"):
            return True
    except OSError:
        return False


@pytest.fixture(autouse=True)
def run_key():
    yield RUN_KEY
    for k in (RUN_KEY, TEST_ROOT):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)
        except FileNotFoundError:
            pass


def install(src, local, *, success=True):
    env = {**os.environ, "LOCALAPPDATA": str(local)}
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                        str(src / "install.ps1"), "-NoVenv", "-NoStart", "-RunKey", "HKCU:\\" + RUN_KEY], env=env,
                       capture_output=True, timeout=120)
    assert (r.returncode == 0) == success, (r.stdout + r.stderr).decode("cp866", "replace")


def test_install_cmd_is_ascii_crlf():
    data = (REPO / "install.cmd").read_bytes()
    data.decode("ascii")
    assert data.count(b"\n") == data.count(b"\r\n") > 0
    text = data.decode("ascii")
    assert 'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*' in text and "pause" in text


def test_archive_keeps_only_what_installs():
    marks = git("check-attr", "export-ignore", "--stdin", stdin="\n".join(IGNORED + SHIPPED) + "\n").splitlines()
    got = {line.split(": ")[0]: line.rsplit(": ", 1)[1] for line in marks}
    assert all(got[f] == "set" for f in IGNORED), got
    assert all(got[f] == "unspecified" for f in SHIPPED), got
    assert git("check-attr", "eol", "install.cmd", "install.ps1").split() == [
        "install.cmd:", "eol:", "crlf", "install.ps1:", "eol:", "crlf"]
    assert git("check-attr", "eol", "README.md").split()[-1] == "lf"


def test_install_ps1_installs_into_programs_from_archive(tmp_path):
    src = archive(tmp_path / "Downloads" / "studio-agent")
    assert not (src / ".git").exists() and not (src / "tests").exists() and (src / "install.cmd").is_file()
    for f in ("install.ps1", "studio_agent/app.py"):
        mark_of_the_web(src / f)
    local = tmp_path / "Local"
    target = local / "Programs" / "studio-agent"
    install(src, local)
    for f in SHIPPED:
        assert (target / f).read_bytes() == (src / f).read_bytes(), f
        assert not has_mark(target / f), f                              # «Mark of the Web» снят
    assert not (target / "tests").exists() and not (target / "CLAUDE.md").exists()

    venv = target / ".venv"                                             # среда прежней установки — остаётся
    (venv / "Scripts").mkdir(parents=True)
    (venv / "marker").write_text("среда", encoding="utf-8")
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                str(target / "studio_agent.pyw")])      # «мост» этой копии
    try:
        (src / "README.md").write_text("новая версия\n", encoding="utf-8")
        (src / ".venv").mkdir()                                         # своя .venv архива не копируется
        (src / ".venv" / "foreign").write_text("x", encoding="utf-8")
        previous = (target / "README.md").read_bytes()
        install(src, local, success=False)
        assert running.poll() is None                                 # установка не прерывает задания
        assert (target / "README.md").read_bytes() == previous
        running.terminate()
        running.wait(timeout=10)
        install(src, local)                                            # обновление после явного выхода
    finally:
        if running.poll() is None:
            running.kill()
    assert (target / "README.md").read_text(encoding="utf-8") == "новая версия\n"
    assert (venv / "marker").read_text(encoding="utf-8") == "среда" and not (venv / "foreign").exists()
    r = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                        "from studio_agent import autostart; print(autostart.command())", str(target)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.stdout.strip() == f'"{target}\\.venv\\Scripts\\pythonw.exe" "{target}\\studio_agent.pyw"'
    with pytest.raises(FileNotFoundError):                              # автозапуска не было — и не заведён
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY)


def test_install_moves_an_enabled_autostart_to_the_installed_copy(tmp_path):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, autostart.VALUE, 0, winreg.REG_SZ, autostart.command())   # включён git-копией
    src = archive(tmp_path / "Downloads" / "studio-agent")
    local = tmp_path / "Local"
    target = local / "Programs" / "studio-agent"
    install(src, local)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        value, kind = winreg.QueryValueEx(k, autostart.VALUE)
    r = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                        "from studio_agent import autostart; print(autostart.command())", str(target)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert (value, kind) == (r.stdout.strip(), winreg.REG_SZ)          # окно этой копии увидит галочку


def test_install_from_the_installed_copy_stays_in_place(tmp_path):
    local = tmp_path / "Local"
    target = local / "Programs" / "studio-agent"
    archive(target)
    install(target, local)                                              # install.cmd из установленной папки
    assert sorted(p.name for p in (local / "Programs").iterdir()) == ["studio-agent"]
    assert (target / "install.ps1").is_file()


def test_install_from_studio_checkout_keeps_the_component_in_place(tmp_path):
    src = archive(tmp_path / "studio" / "agent")
    # Файл .git, как у worktree; отдельного .git у компонента нет.
    (src.parent / ".git").write_text("gitdir: elsewhere\n")
    local = tmp_path / "Local"
    install(src, local)
    assert not (local / "Programs").exists()
    assert (src / "studio_agent.pyw").is_file()

    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                str(src / "studio_agent.pyw")])
    try:
        install(src, local, success=False)
        assert running.poll() is None
        assert not (local / "Programs").exists()
    finally:
        running.terminate()
        running.wait(timeout=10)
