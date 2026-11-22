"""Один автономный PKG для Intel/Apple Silicon. Сборка на Mac локально или через SSH."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
import zipfile

HERE = Path(__file__).resolve().parent
IDENTIFIER = "com.studio.agent.installer"
CACHE_NAME = "studio-agent-macos"
RUNTIME_MAX_BYTES = 256 * 1024 * 1024
CACHE_LOCK_TIMEOUT = 900
WHEEL_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*\.whl")
WHEEL_PLATFORM_PREFIX = "macosx_13_0_"


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


@contextmanager
def cache_lock(cache_dir, name):
    if cache_dir is None:
        yield
        return
    import fcntl  # Only the Mac builder uses the shared cache.

    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(cache_dir / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        deadline = time.monotonic() + CACHE_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"кэш сборки занят: {name}") from None
                time.sleep(0.25)
        yield


def copy_runtime(incoming, expected, output=None):
    digest = hashlib.sha256()
    size = 0
    while block := incoming.read(1024 * 1024):
        size += len(block)
        if size > RUNTIME_MAX_BYTES:
            return False
        digest.update(block)
        if output is not None:
            output.write(block)
    return digest.hexdigest() == expected


def fetch_runtime(lock, arch, archive):
    triple = "aarch64" if arch == "arm64" else "x86_64"
    name = "cpython-{}+{}-{}-apple-darwin-install_only_stripped.tar.gz".format(
        lock["version"], lock["release"], triple)
    url = "https://github.com/astral-sh/python-build-standalone/releases/download/{}/{}".format(
        lock["release"], name)
    with urllib.request.urlopen(url, timeout=60) as incoming, archive.open("wb") as output:
        if not copy_runtime(incoming, lock[arch], output):
            raise ValueError("не совпала SHA-256 или превышен размер runtime " + arch)


@contextmanager
def runtime_archive(lock, arch, work, cache_dir):
    stamp = lock[arch]
    if not re.fullmatch(r"[0-9a-f]{64}", stamp):
        raise ValueError("неверный SHA-256 runtime " + arch)
    if cache_dir is None:
        archive = work / (arch + ".tar.gz")
        fetch_runtime(lock, arch, archive)
        with archive.open("rb") as stream:
            yield stream
        return
    with cache_lock(cache_dir, f"runtime-{stamp}.lock"):
        folder = cache_dir / "runtime"
        archive = folder / (stamp + ".tar.gz")
        if folder.is_symlink() or archive.is_symlink():
            raise ValueError("ссылка в кэше runtime")
        folder.mkdir(mode=0o700, exist_ok=True)
        if archive.is_file():
            with archive.open("rb") as stream:
                if copy_runtime(stream, stamp):
                    print("==> runtime из кэша " + arch, flush=True)
                    stream.seek(0)
                    yield stream
                    return
        with tempfile.TemporaryDirectory(prefix="incoming-", dir=folder) as temporary:
            staged = Path(temporary) / "runtime.tar.gz"
            fetch_runtime(lock, arch, staged)
            os.replace(staged, archive)
        with archive.open("rb") as stream:
            yield stream


def download_runtime(lock, arch, target, work, cache_dir=None):
    # Доверенный архив поставщика с закреплённой SHA-256; сохраняем относительные ссылки Python.
    folder = work / ("unpack-" + arch)
    folder.mkdir()
    with runtime_archive(lock, arch, work, cache_dir) as archive:
        with tarfile.open(fileobj=archive) as source:
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


def wheelhouse_path(cache_dir, lock, arch, source):
    inputs = [1, WHEEL_PLATFORM_PREFIX + arch, lock["version"], lock[arch]]
    for name in ("requirements.txt", "constraints.txt"):
        inputs.append(hashlib.sha256((source / name).read_bytes()).hexdigest())
    stamp = hashlib.sha256(json.dumps(inputs).encode("ascii")).hexdigest()
    return cache_dir / "wheels" / f"{arch}-{stamp}.zip"


def cache_wheelhouse(wheels, path):
    """Атомарно сохранить готовые колёса. Вызывается под блокировкой pip.lock."""
    files = sorted(wheels.iterdir())
    if not files or any(not WHEEL_NAME.fullmatch(file.name) or not file.is_file() or file.is_symlink()
                        for file in files):
        raise ValueError("неверные файлы колёс моста")
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("ссылка в кэше колёс")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Заменяется целый проверяемый ZIP; другие сборки не увидят частичную запись.
    with tempfile.TemporaryDirectory(prefix="incoming-", dir=path.parent) as temporary:
        staged = Path(temporary) / path.name
        with zipfile.ZipFile(staged, "w", strict_timestamps=False) as archive:
            for file in files:
                archive.write(file, file.name)
        os.replace(staged, path)


def restore_wheelhouse(path, wheels):
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("ссылка в кэше колёс")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if (not members or len({m.filename.casefold() for m in members}) != len(members)
                    or any(m.compress_type != zipfile.ZIP_STORED or not WHEEL_NAME.fullmatch(m.filename)
                           for m in members)):
                return False
            with tempfile.TemporaryDirectory(prefix="wheels-", dir=wheels.parent) as temporary:
                top = Path(temporary) / "wheelhouse"
                archive.extractall(top)
                os.replace(top, wheels)
    except (FileNotFoundError, zipfile.BadZipFile, EOFError):
        return False
    return True


def download_wheels(native, lock, arch, source, wheels, work, env, cache_dir):
    wheels.parent.mkdir(parents=True, exist_ok=True)
    # Оба pip используют один HTTP-кэш, доступ к нему на время загрузки сериализован.
    with cache_lock(cache_dir, "pip.lock"):
        cached = wheelhouse_path(cache_dir, lock, arch, source) if cache_dir is not None else None
        if cached is not None and restore_wheelhouse(cached, wheels):
            print("==> зависимости из кэша " + arch, flush=True)
            return
        if cache_dir is not None and (cache_dir / "pip").is_symlink():
            raise ValueError("ссылка в кэше pip")
        wheels.mkdir()
        files = []
        for name in ("requirements.txt", "constraints.txt"):
            filtered = work / (arch + "-" + name)
            run([native, __file__, "--filter-requirements", source / name, filtered, arch], env=env)
            files.append(filtered)
        print("==> зависимости " + arch, flush=True)
        py_version = ".".join(lock["version"].split(".")[:2])
        run([native, "-m", "pip", "download", "--only-binary=:all:", "--platform", WHEEL_PLATFORM_PREFIX + arch,
             "--python-version", py_version, "--implementation", "cp", "--abi", "cp" + py_version.replace(".", ""),
             "-r", files[0], "-c", files[1], "--dest", wheels], env=env)
        if cached is not None:
            cache_wheelhouse(wheels, cached)


def build(archive, output, lock_path, work, launcher_output=None, signing_identity=None, capture_profile=None,
          installer_identity=None, cache_dir=None):
    scripts = work / "scripts"
    scripts.mkdir()
    source = unpack_source(archive, scripts / "source")
    if (capture_profile or installer_identity) and not signing_identity:
        raise ValueError("Для подписанного выпуска нужен Developer ID Application")
    if signing_identity:
        signing = runpy.run_path(str(source / 'macos/sign_release.py'))
        team, grants = signing['preflight'](signing_identity, capture_profile)
        if installer_identity:
            listing = subprocess.check_output(['/usr/bin/security', 'find-identity', '-v'], text=True, timeout=20)
            if signing['signing_team'](installer_identity, listing, 'Installer') != team:
                raise ValueError('Сертификаты приложения и установщика принадлежат разным командам')
    match = re.search(r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$',
                      (source / "studio_agent/__init__.py").read_text(encoding="utf-8"), re.M)
    if match is None:
        raise ValueError("нет версии моста")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    runtimes = scripts / "runtime"
    runtimes.mkdir()
    for arch in ("arm64", "x86_64"):
        print("==> runtime " + arch, flush=True)
        download_runtime(lock, arch, runtimes / arch, work, cache_dir)
    native = runtimes / platform.machine() / "bin/python3"
    # Native identity is shared by GUI, LaunchAgent and updater. It never execs Python.
    launchers = []
    compiler_env = dict(os.environ, TMPDIR=str(work), CLANG_MODULE_CACHE_PATH=str(work / 'clang-cache'))
    for arch in ("arm64", "x86_64"):
        binary = work / ("studio-agent-" + arch)
        headers = runtimes / arch / "include" / ("python" + ".".join(lock["version"].split(".")[:2]))
        run(["/usr/bin/clang", "-arch", arch, "-mmacosx-version-min=11.0", "-framework", "Foundation",
             "-I", headers, source / "macos/launcher.m", "-o", binary], timeout=120, env=compiler_env)
        launchers.append(binary)
    launcher = source / "macos/studio-agent"
    run(["/usr/bin/lipo", "-create", *launchers, "-output", launcher], timeout=30)
    launcher.chmod(0o755)
    if launcher_output is not None:
        launcher_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(launcher, launcher_output)
        launcher_output.chmod(0o755)
    pip_cache = work / "pip-cache" if cache_dir is None else cache_dir / "pip"
    env = dict(os.environ, TMPDIR=str(work), PIP_CACHE_DIR=str(pip_cache),
               PYTHONDONTWRITEBYTECODE="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONPYCACHEPREFIX"):
        env.pop(name, None)
    run([native, "-c", "import tkinter, ssl, venv"], env=env)
    for arch in ("arm64", "x86_64"):
        download_wheels(native, lock, arch, source, scripts / "wheels" / arch, work, env, cache_dir)
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
    if signing_identity:
        signed = signing['build_app'](source, scripts, work, lock, acceptance / 'Applications/Studio Agent.app',
                                      match[1], signing_identity, capture_profile, team, grants, env)
        verifier = runpy.run_path(str(source / 'studio_agent/macos_signed.py'))['verify']
        verifier(signed, team, match[1], capture=bool(grants))
        result = run([signed / 'Contents/MacOS/studio-agent', '--check-runtime'], env=probe_env,
                     capture_output=True, text=True, timeout=180)
        if result.stdout.strip() != match[1]:
            raise ValueError('Неверная версия подписанного приложения')
        scripts = signed.parent
        postinstall = scripts / 'postinstall'
        postinstall.write_text('''#!/bin/sh
set -eu
[ "$(/usr/bin/id -u)" != 0 ] || exit 1
app="$(dirname -- "$0")/Studio Agent.app"
arch=$(/usr/bin/uname -m)
exec "$app/Contents/Resources/runtime/$arch/bin/python3" -I -B \\
    "$app/Contents/Resources/agent/studio_agent/macos_signed.py" --install "$app"
''', encoding='utf-8')
        postinstall.chmod(0o755)
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
        "welcome": "<h1>Studio Agent</h1><p>Мост Claude Code, Codex, OpenCode и DeepSeek Harness для Studio.</p>"
                   "<p>Установщик содержит Python и все зависимости для Intel и Apple Silicon. Интернет для установки не нужен.</p>"
                   "<p>Перед повторной установкой дождитесь завершения заданий и выйдите из моста через меню.</p>",
        "conclusion": "<h1>Мост установлен</h1><p>В окне Studio Agent войдите в свою учётную запись Studio.</p>"
                      "<p>Откройте Studio Agent в «Программах». Если общая папка недоступна для записи,"
                      " приложение устанавливается в Applications домашней папки.</p>"
    }.items():
        (resources / (name + ".html")).write_text('<!doctype html><meta charset="utf-8">' + body, encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    built = work / "studio-agent-setup.pkg"
    sign_pkg = ['--sign', installer_identity] if installer_identity else []
    run(["/usr/bin/productbuild", *sign_pkg, "--distribution", distribution, "--package-path", work,
         "--resources", resources, built])
    if signing_identity:
        if installer_identity:
            run(['/usr/sbin/pkgutil', '--check-signature', built])
        expanded = work / 'expanded'
        run(['/usr/sbin/pkgutil', '--expand-full', built, expanded])
        verifier(expanded / 'bridge.pkg/Scripts/Studio Agent.app', team, match[1], capture=bool(grants))
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
        cache_dir = root / ".cache" / CACHE_NAME
        command = ["/usr/bin/python3", folder + "/build-agent-macos.py", "--archive", folder + "/" + args.archive.name,
                   "--lock", folder + "/" + args.lock.name, "--output", folder + "/studio-agent-setup.pkg",
                   "--cache-dir", str(cache_dir)]
        if getattr(args, 'launcher_output', None) is not None:
            command += ['--launcher-output', folder + '/studio-agent-macos']
        if getattr(args, 'signing_identity', None):
            command += ['--signing-identity', args.signing_identity]
        if getattr(args, 'capture_profile', None):
            command += ['--capture-profile', str(args.capture_profile)]
        if getattr(args, 'installer_identity', None):
            command += ['--installer-identity', args.installer_identity]
        # Полная сборка включает обе архитектуры, загрузки и автономную установку.
        run([*ssh, shlex.join(command)], timeout=3600)
        staged = work / "studio-agent-setup.pkg"
        run([*scp, args.host + ":" + folder + "/studio-agent-setup.pkg", staged])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, args.output)
        if getattr(args, 'launcher_output', None) is not None:
            staged_launcher = work / 'studio-agent-macos'
            run([*scp, args.host + ':' + folder + '/studio-agent-macos', staged_launcher])
            args.launcher_output.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_launcher, args.launcher_output)
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
    parser.add_argument("--launcher-output", type=Path)
    parser.add_argument("--signing-identity", default=os.environ.get("STUDIO_MACOS_SIGNING_IDENTITY"))
    parser.add_argument("--capture-profile", default=os.environ.get("STUDIO_MACOS_CAPTURE_PROFILE"))
    parser.add_argument("--installer-identity", default=os.environ.get("STUDIO_MACOS_INSTALLER_IDENTITY"))
    parser.add_argument("--lock", type=Path, default=HERE / "macos-runtime.json")
    parser.add_argument("--cache-dir", type=Path, help="абсолютный путь к кэшу проекта на Mac (локальная сборка)")
    parser.add_argument("--host", default=os.environ.get("STUDIO_MACOS_BUILD_SSH"))
    parser.add_argument("--port", default=os.environ.get("STUDIO_MACOS_BUILD_PORT", "22"))
    parser.add_argument("--key", default=os.environ.get("STUDIO_MACOS_BUILD_KEY"))
    parser.add_argument("--remote-root", default=os.environ.get("STUDIO_MACOS_BUILD_ROOT"))
    args = parser.parse_args()
    if args.installer_identity and not args.signing_identity:
        parser.error("подпись установщика требует подписи приложения")
    if args.capture_profile and (not args.signing_identity or not PurePosixPath(args.capture_profile).is_absolute()):
        parser.error("нужны Developer ID Application и абсолютный путь профиля на Mac")
    if not args.host and sys.platform != "darwin":
        parser.error("нужен Mac: задайте STUDIO_MACOS_BUILD_SSH и STUDIO_MACOS_BUILD_ROOT в локальном профиле")
    if args.cache_dir is not None:
        if args.host or not args.cache_dir.is_absolute() or args.cache_dir.resolve() != args.cache_dir:
            parser.error("задайте абсолютный путь без ссылок к кэшу проекта для локальной сборки")
    # Вызывающий deploy уже создал собственную папку .tmp; промежуточные файлы остаются рядом с ZIP.
    with tempfile.TemporaryDirectory(prefix="macos-pkg-", dir=args.archive.resolve().parent) as temporary:
        work = Path(temporary)
        if args.host:
            remote(args, work)
        else:
            build(args.archive, args.output, args.lock, work, args.launcher_output, args.signing_identity,
                  args.capture_profile, args.installer_identity, args.cache_dir)


if __name__ == "__main__":
    main()
