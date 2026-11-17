"""Один автономный PKG для Intel/Apple Silicon. Сборка на Mac локально или через SSH."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile

HERE = Path(__file__).resolve().parent
IDENTIFIER = "com.studio.agent.installer"


def run(argv, *, timeout=900, **kwargs):
    return subprocess.run(list(map(str, argv)), check=True, timeout=timeout, **kwargs)


def unpack_source(archive, target):
    """ZIP исходников не может писать вне своей папки или подменять ссылки."""
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > 3000 or sum(m.file_size for m in members) > 64 * 1024 * 1024:
            raise ValueError("слишком большой архив моста")
        seen = set()
        for member in members:
            path = PurePosixPath(member.filename)
            if (not member.filename.startswith("studio-agent/") or path.is_absolute()
                    or "\\" in member.orig_filename
                    or member.filename.rstrip("/") != str(path)
                    or any(p in ("..", ".") or "\\" in p or ":" in p for p in path.parts)
                    or member.filename.casefold() in seen
                    or (member.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("неверный путь в архиве моста")
            seen.add(member.filename.casefold())
        source.extractall(target)
    return target / "studio-agent"


def download_runtime(lock, arch, target, work):
    triple = "aarch64" if arch == "arm64" else "x86_64"
    name = "cpython-{}+{}-{}-apple-darwin-install_only_stripped.tar.gz".format(
        lock["version"], lock["release"], triple)
    archive = work / (arch + ".tar.gz")
    url = "https://github.com/astral-sh/python-build-standalone/releases/download/{}/{}".format(
        lock["release"], name)
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as incoming, archive.open("wb") as output:
        size = 0
        while block := incoming.read(1024 * 1024):
            size += len(block)
            if size > 256 * 1024 * 1024:
                raise ValueError("слишком большой runtime")
            digest.update(block)
            output.write(block)
    if digest.hexdigest() != lock[arch]:
        raise ValueError("не совпала SHA-256 runtime " + arch)
    # Доверенный архив поставщика с закреплённой SHA-256; сохраняем относительные ссылки Python.
    folder = work / ("unpack-" + arch)
    folder.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(folder)
    shutil.move(str(folder / "python"), target)
    (target / ".studio-runtime-id").write_text(lock[arch] + "\n", encoding="ascii")


def filter_requirements(source, destination, arch):
    # Выполняется встроенным Python с pip, не требует пакетов у системного Python сборщика.
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import Requirement
    environment = dict(default_environment(), sys_platform="darwin", platform_system="Darwin",
                       platform_machine=arch)
    lines = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker is None or requirement.marker.evaluate(environment):
            requirement.marker = None
            lines.append(str(requirement))
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build(archive, output, lock_path, work):
    scripts = work / "scripts"
    scripts.mkdir()
    source = unpack_source(archive, scripts / "source")
    match = re.search(r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$',
                      (source / "studio_agent/__init__.py").read_text(encoding="utf-8"), re.M)
    if match is None:
        raise ValueError("нет версии моста")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    runtimes = scripts / "runtime"
    runtimes.mkdir()
    for arch in ("arm64", "x86_64"):
        print("==> runtime " + arch, flush=True)
        download_runtime(lock, arch, runtimes / arch, work)
    native = runtimes / platform.machine() / "bin/python3"
    env = dict(os.environ, TMPDIR=str(work), PIP_CACHE_DIR=str(work / "pip-cache"),
               PYTHONDONTWRITEBYTECODE="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONPYCACHEPREFIX"):
        env.pop(name, None)
    run([native, "-c", "import tkinter, ssl, venv"], env=env)
    for arch in ("arm64", "x86_64"):
        wheels = scripts / "wheels" / arch
        wheels.mkdir(parents=True)
        files = []
        for name in ("requirements.txt", "constraints.txt"):
            filtered = work / (arch + "-" + name)
            run([native, __file__, "--filter-requirements", source / name, filtered, arch], env=env)
            files.append(filtered)
        print("==> зависимости " + arch, flush=True)
        run([native, "-m", "pip", "download", "--only-binary=:all:", "--platform", "macosx_13_0_" + arch,
             "--python-version", ".".join(lock["version"].split(".")[:2]), "--implementation", "cp",
             "--abi", "cp" + "".join(lock["version"].split(".")[:2]),
             "-r", files[0], "-c", files[1], "--dest", wheels], env=env)
    postinstall = scripts / "postinstall"
    postinstall.write_text('''#!/bin/sh
set -eu
# Только пользовательская установка: мост и Keychain никогда не запускаются от root.
if [ "$(/usr/bin/id -u)" = 0 ]; then
    echo 'Studio Agent: выберите установку только для текущего пользователя.' >&2
    exit 1
fi
PAYLOAD=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ARCH=$(/usr/bin/uname -m)
case "$ARCH" in arm64|x86_64) ;; *) exit 1 ;; esac
exec "$PAYLOAD/runtime/$ARCH/bin/python3" "$PAYLOAD/source/studio-agent/install_macos.py" \\
    --runtime-source "$PAYLOAD/runtime/$ARCH" --wheelhouse "$PAYLOAD/wheels/$ARCH"
''', encoding="utf-8")
    postinstall.chmod(0o755)
    # Живая установка без сети и системного Python, полностью внутри папки этой сборки.
    acceptance = work / "acceptance"
    acceptance.mkdir()
    probe_env = dict(env, HOME=str(acceptance), STUDIO_AGENT_HOME=str(acceptance / "settings"),
                     PIP_NO_INDEX="1")
    run([native, source / "install_macos.py", "--no-start", "--no-autostart",
         "--runtime-source", runtimes / platform.machine(),
         "--wheelhouse", scripts / "wheels" / platform.machine()], env=probe_env,
        stdout=subprocess.DEVNULL)
    installed = acceptance / "settings/app/current/.venv/bin/python"
    run([installed, "-c", "import sys, tkinter, studio_agent.app; "
         "assert '/runtimes/' in sys._base_executable"], cwd=source, env=probe_env)
    # nopayload: вся запись и проверка замка идут через общий install_macos, до смены приложения.
    component = work / "bridge.pkg"
    run(["/usr/bin/pkgbuild", "--nopayload", "--scripts", scripts, "--identifier", IDENTIFIER,
         "--version", match[1], component])
    distribution = work / "Distribution.xml"
    distribution.write_text('''<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
  <title>Studio Agent</title>
  <welcome file="welcome.html"/>
  <conclusion file="conclusion.html"/>
  <options customize="never" require-scripts="true" hostArchitectures="arm64,x86_64"/>
  <domains enable_anywhere="false" enable_currentUserHome="true" enable_localSystem="false"/>
  <allowed-os-versions><os-version min="13.0"/></allowed-os-versions>
  <choices-outline><line choice="bridge"/></choices-outline>
  <choice id="bridge" visible="false"><pkg-ref id="com.studio.agent.installer"/></choice>
  <pkg-ref id="com.studio.agent.installer" version="''' + match[1] + '''" onConclusion="none">bridge.pkg</pkg-ref>
</installer-gui-script>
''', encoding="utf-8")
    resources = work / "resources"
    resources.mkdir()
    for name, body in {
        "welcome": "<h1>Studio Agent</h1><p>Мост Claude Code, Codex и OpenCode для Studio.</p>"
                   "<p>Установщик содержит Python и все зависимости для Intel и Apple Silicon. Интернет для установки не нужен.</p>"
                   "<p>Перед повторной установкой дождитесь завершения заданий и выйдите из моста через меню.</p>",
        "conclusion": "<h1>Мост установлен</h1><p>В окне Studio Agent войдите в свою учётную запись Studio.</p>"
                      "<p>Приложение находится в папке Applications вашего пользователя и запускается при входе в macOS.</p>"
    }.items():
        (resources / (name + ".html")).write_text('<!doctype html><meta charset="utf-8">' + body, encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    built = work / "studio-agent-setup.pkg"
    run(["/usr/bin/productbuild", "--distribution", distribution, "--package-path", work,
         "--resources", resources, built])
    os.replace(built, output)


def remote(args, work):
    """Удалённые файлы только в .tmp этого же проекта; сборщик получает тот же ZIP и lock."""
    root = PurePosixPath(args.remote_root or "")
    if (not root.is_absolute() or ".." in root.parts or len(root.parts) < 4
            or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(root))):
        raise ValueError("задайте --remote-root: абсолютный корень проекта Studio на Mac")
    folder = str(root / ".tmp" / ("studio-agent-pkg-" + uuid.uuid4().hex))
    options = ["-i", args.key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"] if args.key else ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
    ssh = ["ssh", *options, "-p", args.port, args.host]
    scp = ["scp", *options, "-P", args.port]
    run([*ssh, "mkdir -p " + shlex.quote(folder)])
    try:
        run([*scp, args.archive, HERE / "build-agent-macos.py", args.lock, args.host + ":" + folder + "/"])
        command = ["/usr/bin/python3", folder + "/build-agent-macos.py", "--archive", folder + "/" + args.archive.name,
                   "--lock", folder + "/" + args.lock.name, "--output", folder + "/studio-agent-setup.pkg"]
        # Полная сборка включает обе архитектуры, загрузки и автономную установку.
        run([*ssh, shlex.join(command)], timeout=3600)
        staged = work / "studio-agent-setup.pkg"
        run([*scp, args.host + ":" + folder + "/studio-agent-setup.pkg", staged])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, args.output)
    finally:
        run([*ssh, shlex.join(["/usr/bin/python3", folder + "/build-agent-macos.py", "--validate-cleanup", folder])])
        run([*ssh, "rm -rf -- " + shlex.quote(folder)])


def main():
    if sys.argv[1:2] == ["--validate-cleanup"]:
        target = Path(sys.argv[2])
        if (target.parent.name != ".tmp" or not target.name.startswith("studio-agent-pkg-")
                or target.resolve() != target):
            raise ValueError("неверная папка уборки")
        pending = [target]
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if not Path(entry.path).resolve().is_relative_to(target):
                        raise ValueError("ссылка за пределы папки сборки")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
        return
    if sys.argv[1:2] == ["--filter-requirements"]:
        filter_requirements(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=HERE / "macos-runtime.json")
    parser.add_argument("--host", default=os.environ.get("STUDIO_MACOS_BUILD_SSH"))
    parser.add_argument("--port", default=os.environ.get("STUDIO_MACOS_BUILD_PORT", "22"))
    parser.add_argument("--key", default=os.environ.get("STUDIO_MACOS_BUILD_KEY"))
    parser.add_argument("--remote-root", default=os.environ.get("STUDIO_MACOS_BUILD_ROOT"))
    args = parser.parse_args()
    if not args.host and sys.platform != "darwin":
        parser.error("нужен Mac: задайте STUDIO_MACOS_BUILD_SSH и STUDIO_MACOS_BUILD_ROOT в локальном профиле")
    # Вызывающий deploy уже создал собственную папку .tmp; промежуточные файлы остаются рядом с ZIP.
    with tempfile.TemporaryDirectory(prefix="macos-pkg-", dir=args.archive.resolve().parent) as temporary:
        work = Path(temporary)
        if args.host:
            remote(args, work)
        else:
            build(args.archive, args.output, args.lock, work)


if __name__ == "__main__":
    main()
