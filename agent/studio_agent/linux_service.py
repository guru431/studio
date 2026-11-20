"""Headless bridge supervised across releases by the same user service."""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import host, journal, single, store, update_helper, updater
from .atomic import write_atomic
from .i18n import tr
from .linux_autostart import switch_current

_handover = None
_next_release = None


def schedule_update(stage: Path) -> None:
    global _next_release
    if _handover is None or update_helper._managed_base(stage) is None:
        raise RuntimeError("update requires the installed bridge service")   # внутреннее: журнал — только тип
    _next_release = stage


def supervise(h: Path) -> int:
    """The owner lock covers staging, confirmation, rollback and the next process."""
    mutex = single.acquire()
    if mutex is None:
        print(tr("Мост уже запущен"))
        return 1
    repo = updater.REPO
    previous = None
    old_handler = signal.signal(signal.SIGINT, lambda *_: None)  # child handles terminal Ctrl+C after idle
    try:
        update_helper.acquire_runtime_lease(repo)
        while True:
            outcome, next_name = _run_release(repo, previous, h)
            if outcome == "failed" and previous is not None:
                (repo / "_update_result").write_text("rolled_back", encoding="ascii")
                repo, previous = previous, None
                continue
            if outcome != "update":
                return int(outcome != "done")
            if not update_helper.RELEASE_NAME.fullmatch(next_name):
                return 1
            stage = host.install_home() / "releases" / next_name
            if update_helper._managed_base(stage) is None or stage == repo:
                return 1
            write_atomic(stage / "_update_plan.json", json.dumps({"old_repo": str(repo)}).encode())
            repo, previous = stage, repo
    finally:
        signal.signal(signal.SIGINT, old_handler)
        mutex.close()


def _run_release(repo, previous, h):
    reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    env = dict(os.environ, STUDIO_AGENT_HANDOVER_FD=str(writer.fileno()), STUDIO_AGENT_HOME=str(h))
    env.pop("NOTIFY_SOCKET", None)
    interpreter = host.python(repo)
    if repo == updater.REPO and not interpreter.is_file():
        interpreter = Path(sys.executable)
    with reader, writer:
        try:
            child = subprocess.Popen([str(interpreter), "-m", "studio_agent", "_service"], cwd=repo, env=env,
                                     pass_fds=(writer.fileno(),), stdin=subprocess.DEVNULL)
        except OSError:
            return "failed", ""
        writer.close()
        if reader.recv(32) != b"ready":
            child.wait()
            return "failed", ""
        try:
            if previous is not None:
                switch_current(host.install_home(), repo, previous)
                (repo / "_update_result").write_text("complete", encoding="ascii")
            address = os.environ.get("NOTIFY_SOCKET")
            if address:
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
                    notifier.sendto(b"READY=1", "\0" + address[1:] if address.startswith("@") else address)
            reader.sendall(b"ok")
        except (OSError, ValueError):
            reader.close()
            child.wait()
            raise
        packet = reader.recv(128)
        status = child.wait()
        if status == 0 and packet.startswith(b"update "):
            return "update", packet[7:].decode("ascii")
        # A confirmed release is already current. systemd applies its normal restart policy.
        return ("done" if status == 0 else "crashed"), ""


def serve(h: Path) -> int:
    global _handover
    from . import app, linux_menu

    descriptor = os.environ.pop("STUDIO_AGENT_HANDOVER_FD", "")
    if not descriptor.isdecimal():
        return 1
    with socket.socket(fileno=int(descriptor)) as handover:
        handover.set_inheritable(False)
        _handover = handover
        app.prepare_env()
        journal.setup(h, console=False)
        ctrl = app.Controller(h)
        stopped = threading.Event()
        ctrl.on_idle_quit = ctrl.on_update_quit = stopped.set
        signal.signal(signal.SIGINT, lambda *_: ctrl.set_quit_when_idle(True))
        try:
            with linux_menu.listener(h) as server:
                ctrl.start()
                deadline = time.monotonic() + 90
                while not ctrl._built.is_set():
                    if not ctrl._thread.is_alive() or time.monotonic() >= deadline:
                        return 1
                    stopped.wait(0.1)
                handover.sendall(b"ready")
                if handover.recv(32) != b"ok":
                    return 1
                while not stopped.is_set() and ctrl._thread.is_alive():
                    linux_menu.handle_one(server, ctrl)
                if not stopped.is_set():
                    return 1
        finally:
            ctrl.quit()
            _handover = None
        if _next_release is not None:
            handover.sendall(b"update " + _next_release.name.encode("ascii"))
        return 0
