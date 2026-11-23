"""Build the bridge bundle once and reuse the verified artifacts across Studio deployments."""
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path


ARTIFACTS = ("studio-agent.zip", "studio-agent-setup.exe", "studio-agent-setup.pkg", "studio-agent-setup.sh")
BUILD_FILES = ("build-agent-installer.ps1", "agent-installer.cs", "build-agent-launcher.ps1", "agent-launcher.cs",
               "build-agent-macos.py", "build-agent-linux.py", "macos-runtime.json")
WINDOWS_LAUNCHER = "studio-agent/Studio Agent.exe"  # `shortcut.LAUNCHER` в копии моста
SIGNING = ("STUDIO_MACOS_SIGNING_IDENTITY", "STUDIO_MACOS_CAPTURE_PROFILE", "STUDIO_MACOS_INSTALLER_IDENTITY")
BUILD_TIMEOUT = 3800


def git(repo, *args):
    return ["git", "-C", str(repo), "-c", "core.autocrlf=false", *args]


def git_file(repo, revision, path, destination):
    with destination.open("wb") as output:
        subprocess.run(git(repo, "show", f"{revision}:{path}"), stdout=output, check=True, timeout=30)


def cache_key(repo, revision):
    paths = ("agent", "deploy/build-agent-bundle.py", *(f"deploy/{name}" for name in BUILD_FILES))
    objects = subprocess.check_output(git(repo, "rev-parse", *(f"{revision}:{path}" for path in paths)), timeout=30)
    return hashlib.sha256(objects).hexdigest()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def hashes(directory):
    if any(not (directory / name).is_file() or (directory / name).is_symlink() for name in ARTIFACTS):
        return None
    return {name: digest(directory / name) for name in ARTIFACTS}


@contextlib.contextmanager
def lease(path):
    # The OS drops the lock when the caller exits, including on an interrupted build.
    with path.open("a+b") as held:
        if held.tell() == 0:
            held.write(b"\0")
        held.flush()
        held.seek(0)
        if os.name == "nt":
            import msvcrt
            limit = time.monotonic() + BUILD_TIMEOUT
            while True:
                try:
                    msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if time.monotonic() >= limit:
                        raise TimeoutError("Studio bridge bundle cache is busy") from error
                    time.sleep(1)
        else:
            import fcntl
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        yield


def build(repo, revision, work):
    archive, windows, macos, linux = (work / name for name in ARTIFACTS)
    with archive.open("wb") as output:
        subprocess.run(git(repo, "archive", "--format=zip", "--prefix=studio-agent/", f"{revision}:agent"),
                       stdout=output, check=True, timeout=60)
    for name in BUILD_FILES:
        git_file(repo, revision, f"deploy/{name}", work / name)
    subprocess.run([sys.executable, work / "build-agent-linux.py", "--archive", archive, "--output", linux],
                   check=True, timeout=60)
    print("==> сборка автономного установщика macOS", flush=True)
    launcher = work / "studio-agent-macos"
    subprocess.run([sys.executable, work / "build-agent-macos.py", "--archive", archive, "--output", macos,
                    "--lock", work / "macos-runtime.json", "--launcher-output", launcher], check=True, timeout=BUILD_TIMEOUT)
    with zipfile.ZipFile(archive, "a") as bundle:
        name = "studio-agent/macos/studio-agent"
        if name in bundle.namelist():
            raise ValueError("Bridge source already contains a compiled launcher")
        bundle.write(launcher, name)
        if os.environ.get("STUDIO_MACOS_SIGNING_IDENTITY"):
            bundle.writestr("studio-agent/macos/signed-package.json",
                            json.dumps({"sha256": digest(macos), "size": macos.stat().st_size}))
    print("==> сборка запускателя и установщика Windows", flush=True)
    executable = work / "studio-agent-launcher.exe"
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", work / "build-agent-launcher.ps1", "-Archive", archive, "-Output", executable],
                   check=True, timeout=180)
    with zipfile.ZipFile(archive, "a") as bundle:
        if WINDOWS_LAUNCHER in bundle.namelist():
            raise ValueError("Bridge source already contains a compiled launcher")
        bundle.write(executable, WINDOWS_LAUNCHER)
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", work / "build-agent-installer.ps1", "-Archive", archive, "-Output", windows],
                   check=True, timeout=180)


def prepare(repo, revision, work):
    # Signed packages are built with a fresh check of the builder's certificate and profile.
    if any(os.environ.get(name) for name in SIGNING):
        build(repo, revision, work)
        return
    key = cache_key(repo, revision)
    cache = repo / ".cache" / "studio-agent-bundle"
    if cache.resolve() != cache or (cache / "lock").is_symlink():
        raise ValueError("Invalid Studio bridge bundle cache path")
    cache.mkdir(parents=True, exist_ok=True)
    with lease(cache / "lock"):
        marker = cache / "manifest.json"
        try:
            record = json.loads(marker.read_text(encoding="utf-8")) if not marker.is_symlink() else None
        except (OSError, ValueError):
            record = None
        if isinstance(record, dict) and record.get("key") == key and record.get("sha256"):
            if hashes(cache) == record["sha256"]:
                for file in ARTIFACTS:
                    shutil.copyfile(cache / file, work / file)
                print("==> общий комплект моста из кэша", flush=True)
                return

        build(repo, revision, work)
        staged = work / "bundle-cache"
        staged.mkdir()
        for file in ARTIFACTS:
            shutil.copyfile(work / file, staged / file)
        (staged / marker.name).write_text(json.dumps({"key": key, "sha256": hashes(staged)}), encoding="utf-8")
        for file in (*ARTIFACTS, marker.name):
            (staged / file).replace(cache / file)


if __name__ == "__main__":
    repo, revision, work = sys.argv[1:]
    prepare(Path(repo).resolve(strict=True), revision, Path(work).resolve(strict=True))
