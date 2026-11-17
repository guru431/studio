"""Живая приёмка macOS без учёток LLM: Keychain, процессные группы, окно и установка."""
import ctypes
import os
import sys
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.skipif(sys.platform != 'darwin', reason='macOS')]


@pytest.mark.timeout(300)
def test_pkg_installs_offline_preserves_settings_and_reuses_private_python(tmp_path, monkeypatch):
    """Payload получают через pkgutil --expand-full; тест не пишет в настоящий HOME/LaunchAgents."""
    import install_macos
    import platform
    from studio_agent import macos_autostart
    payload = os.environ.get('STUDIO_AGENT_MACOS_PAYLOAD')
    if not payload:
        pytest.skip('нужен распакованный PKG: STUDIO_AGENT_MACOS_PAYLOAD=<bridge.pkg/Scripts>')
    source = Path(payload)
    monkeypatch.setattr(install_macos, 'SOURCE', source / 'source/studio-agent')
    monkeypatch.setenv('PIP_NO_INDEX', '1')
    with tempfile.TemporaryDirectory(dir=tmp_path) as folder:
        root = Path(folder)
        home, base = root / 'settings', root / 'app'
        monkeypatch.setattr(macos_autostart, 'path', lambda: root / 'LaunchAgents/com.studio.agent.plist')
        kwargs = dict(start=False, runtime_source=source / 'runtime' / platform.machine(),
                      wheelhouse=source / 'wheels' / platform.machine())
        first = install_macos.install(base, home, root / 'Applications', **kwargs)
        (home / 'key.bin').write_bytes(b'preserved-key')
        second = install_macos.install(base, home, root / 'Applications', **kwargs)
        assert first != second and (base / 'current').resolve() == second
        assert (home / 'key.bin').read_bytes() == b'preserved-key'
        assert len(list((base / 'runtimes').iterdir())) == 1
        result = subprocess.run([str(second / '.venv/bin/python'), '-c',
                                 'import sys,tkinter,studio_agent.app; assert "/runtimes/" in sys._base_executable'],
                                cwd=second, capture_output=True, check=True, timeout=30)
        assert result.returncode == 0


def test_native_keychain_encrypts_and_recovers_after_cache_reset(tmp_path, monkeypatch):
    from studio_agent import macos_keychain as secrets
    lib = secrets._security()
    ptr, uint = ctypes.c_void_p, ctypes.c_uint32
    lib.SecKeychainCreate.argtypes = [ctypes.c_char_p, uint, ptr, ctypes.c_bool, ptr, ctypes.POINTER(ptr)]
    lib.SecKeychainCreate.restype = ctypes.c_int32
    lib.SecKeychainDelete.argtypes = [ptr]
    lib.SecKeychainDelete.restype = ctypes.c_int32
    keychain = ptr()
    password = os.urandom(32).hex().encode()
    assert lib.SecKeychainCreate(os.fsencode(tmp_path / 'probe.keychain'), len(password), password,
                                  False, None, ctypes.byref(keychain)) == 0

    class Scoped:
        def SecKeychainFindGenericPassword(self, ignored, *args):
            return lib.SecKeychainFindGenericPassword(keychain, *args)
        def SecKeychainAddGenericPassword(self, ignored, *args):
            return lib.SecKeychainAddGenericPassword(keychain, *args)
        SecKeychainItemFreeContent = staticmethod(lib.SecKeychainItemFreeContent)

    monkeypatch.setattr(secrets, '_security', lambda: Scoped())
    monkeypatch.setenv('STUDIO_AGENT_HOME', str(tmp_path))
    secrets._key.cache_clear()
    try:
        blob = secrets.protect(b'local device key')
        secrets._key.cache_clear()
        assert secrets.unprotect(blob) == b'local device key'
        assert b'local device key' not in blob
        with pytest.raises(OSError):
            secrets.unprotect(blob[:-2] + b'xx')
    finally:
        secrets._key.cache_clear()
        assert lib.SecKeychainDelete(keychain) == 0
        core = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        core.CFRelease.argtypes = [ptr]
        core.CFRelease(keychain)


def test_gui_and_menubar_share_the_main_runloop(home, monkeypatch):
    import tkinter as tk
    from studio_agent import app, tray
    root = tk.Tk()
    seen = []
    original_start = tray.Tray.start

    def start(icon):
        seen.append('menubar')
        original_start(icon)

    def make_root():
        def check():
            seen.append('window' if any(isinstance(child, tk.Toplevel) for child in root.winfo_children()) else 'missing')
            root.quit()
        root.after(500, check)
        return root

    monkeypatch.setattr(tk, 'Tk', make_root)
    monkeypatch.setattr(tray.Tray, 'start', start)
    assert app.main([]) == 0
    assert seen == ['menubar', 'window']


@pytest.mark.timeout(300)
def test_installer_builds_a_copy_and_refuses_a_running_bridge(tmp_path):
    import fcntl
    import hashlib
    import install_macos
    with tempfile.TemporaryDirectory(dir=tmp_path) as folder:
        root = Path(folder)
        home, base, applications = root / 'settings', root / 'app', root / 'Applications'
        # Первый запуск идёт базовым Python без site-packages моста, с включённым автозапуском.
        code = ('import sys; from pathlib import Path; import install_macos; '
                'from studio_agent import macos_autostart; '
                'macos_autostart.path=lambda: Path(sys.argv[4]); '
                'install_macos.install(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), start=False)')
        subprocess.run([sys._base_executable, '-c', code, str(base), str(home), str(applications),
                        str(root / 'LaunchAgents/com.studio.agent.plist')], cwd=install_macos.SOURCE,
                       check=True, timeout=240)
        stage = (base / 'current').resolve()
        assert (base / 'current').resolve() == stage
        assert (applications / 'Studio Agent.app/Contents/MacOS/studio-agent').stat().st_mode & 0o111
        result = subprocess.run([str(stage / '.venv/bin/python'), '-m', 'studio_agent', '--help'],
                                cwd=stage, capture_output=True, timeout=30, check=True)
        assert b'enroll' in result.stdout
        name = '.instance-' + hashlib.sha256(b'Local\\StudioAgent').hexdigest()[:16]
        with (home / name).open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(RuntimeError, match='Дождитесь'):
                install_macos.install(base, home, applications, start=False, autostart=False)
        assert list((base / 'releases').iterdir()) == [stage]


def test_macos_update_switches_only_its_launchagent_and_preserves_rollback(tmp_path, monkeypatch):
    from studio_agent import macos_autostart, update_helper as helper
    user = tmp_path / 'user'
    monkeypatch.setattr(Path, 'home', lambda: user)
    base = helper.install_home() / 'releases'
    old, current, stale = (base / ('release-' + name) for name in ('old', 'current', 'stale'))
    for stage in (old, current, stale):
        stage.mkdir(parents=True)
        (stage / '_update_result').write_text('complete')
        # Настоящая venv содержит ссылки: уборка удаляет ссылки, не их внешние цели.
        (stage / 'python-link').symlink_to(sys.executable)
    (base.parent / 'current').symlink_to(old, target_is_directory=True)
    macos_autostart.set_enabled(True, old)
    helper.switch_macos(current, old)
    assert (base.parent / 'current').resolve() == current
    assert macos_autostart.read()['ProgramArguments'] == macos_autostart.arguments(current)
    assert helper.prune(current, {'old_repo': str(old)}, helper._autostart(),
                        processes=lambda ignored: helper.ProcessReleases()) == [stale]
    assert old.exists() and current.exists() and Path(sys.executable).exists()


def test_native_memory_and_system_snapshot():
    from studio_agent import macos_info
    data = macos_info.system_data()
    assert data['os'] == 'macOS' and data['version']
    assert data['memory_total'] > 0 and 0 <= data['memory_free'] <= data['memory_total']
    assert data['disks'][0]['path'] == '/' and data['disks'][0]['total'] > 0


def test_update_waits_for_exit_and_rolls_back_a_failed_new_copy(tmp_path):
    from studio_agent import update_helper as helper
    old = subprocess.Popen([sys.executable, '-c', 'import sys; print("ready",flush=True); sys.stdin.read()'],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert old.stdout.readline().strip() == b'ready'
        assert helper.wait_old(old.pid, timeout=20) is False
        old.stdin.close()
        old.wait(5)
        assert helper.wait_old(old.pid, timeout=20) is True
    finally:
        if old.poll() is None:
            old.kill()
        old.wait(5)
        old.stdout.close()
        old.stdin.close()
    new, previous = tmp_path / 'new', tmp_path / 'previous'
    (new / '.venv/bin').mkdir(parents=True)
    (new / '.venv/bin/python').symlink_to(sys.executable)
    (new / 'studio_agent.pyw').write_text('raise SystemExit(1)')
    previous.mkdir()
    (previous / 'studio_agent.pyw').write_text('from pathlib import Path; Path("rolled-back").touch()')
    processes = []
    def start(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        processes.append(process)
        return process
    try:
        result = helper.activate(new, {'pid': old.pid, 'home': str(tmp_path), 'old_repo': str(previous),
                                     'old_python': sys.executable}, start=start)
        assert result == 'rolled_back'
        for process in processes:
            process.wait(5)
        assert (previous / 'rolled-back').is_file()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(5)
