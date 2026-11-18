"""Манифест проекта и приём копии в разрешённой папке моста."""

import base64
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

from . import files, projects
from .errors import Refused

CHUNK = 4 * 1024 * 1024
MANIFEST_MAX = 20 * 1024 * 1024  # вместе два манифеста могут поместиться в кадр
ENTRY_MAX = 150_000
REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT
DIRECTORY = ["dir", "", 0, 0]
SKIPPED = ["skip", "", 0, 0]


def serial(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def fingerprint(entries) -> str:
    return hashlib.sha256(serial(entries)).hexdigest()


def target(config, root_id, name) -> Path:
    for root in projects.list_roots(config):
        if root["id"] == root_id and projects.valid_name(name):
            path = Path(root["path"]) / name
            if not path.parent.is_dir() or path.parent.resolve() != path.parent:
                raise Refused("outside")
            info = _stat(path)
            if info is not None and (_reparse(info) or not stat.S_ISDIR(info.st_mode) or path.resolve() != path):
                raise Refused("outside", "путь назначения должен быть обычной папкой проекта")
            if info is None:
                return path
            # На томах без учёта регистра pid должен соответствовать настоящему имени папки.
            wanted = unicodedata.normalize("NFC", name).casefold()
            with os.scandir(path.parent) as siblings:
                for entry in siblings:
                    if entry.name == name:
                        return path
                    if unicodedata.normalize("NFC", entry.name).casefold() == wanted:
                        actual = Path(entry.path)
                        if info.st_ino and os.path.samestat(info, actual.lstat()):
                            return actual
            raise Refused("conflict", "папка зеркала была заменена")
    raise Refused("not_found", "папка проектов больше не разрешена — обновите список")


def _stat(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _reparse(info):
    return getattr(info, "st_file_attributes", 0) & REPARSE


def _safe_link(root, path, link):
    if not link or len(link) > 1024 or any(ord(c) < 32 or c == ":" for c in link) or Path(link).anchor:
        return False
    try:
        return files.inside(str(root), os.path.realpath(path.parent / link))
    except (OSError, ValueError):
        return False


def _entry(root, path, info):
    if stat.S_ISLNK(info.st_mode):
        value = os.readlink(path)
        if not _safe_link(root, path, value):
            return SKIPPED
        return ["link", value, 0, int(path.is_dir())]
    if _reparse(info):
        return SKIPPED
    if stat.S_ISDIR(info.st_mode):
        return DIRECTORY
    if not stat.S_ISREG(info.st_mode):
        return SKIPPED
    if path.name.endswith(("-wal", "-shm", "-journal")):
        suffix = next(suffix for suffix in ("-wal", "-shm", "-journal") if path.name.endswith(suffix))
        db = path.with_name(path.name[:-len(suffix)]) if len(path.name) > len(suffix) else None
        db_info = _stat(db) if db is not None else None
        if db_info and stat.S_ISREG(db_info.st_mode) and not _reparse(db_info):
            with db.open("rb") as stream:
                if stream.read(16) == b"SQLite format 3\x00":
                    raise Refused("busy", "обнаружена открытая база SQLite — закройте её и повторите операцию")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        # Windows fstat и lstat могут возвращать разные st_ctime для одного файла.
        if _stamp(info)[:4] != _stamp(opened)[:4]:
            raise Refused("conflict", "файлы проекта изменились во время проверки")
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    if _stamp(info) != _stamp(path.lstat()):
        raise Refused("conflict", "файлы проекта изменились во время проверки")
    return ["file", sha, info.st_size, int(bool(info.st_mode & 0o111)) if os.name != "nt" else 0]


def scan(root: Path) -> dict:
    """Все доступные обычные файлы и пустые папки, в том числе .git и gitignored."""
    try:
        info = _stat(root)
        if info is None:
            return {"entries": None}
        if _reparse(info) or not stat.S_ISDIR(info.st_mode) or root.resolve() != root:
            raise Refused("outside", "путь назначения должен быть обычной папкой проекта")
        entries = {}
        folders = [root]
        while folders:
            folder = folders.pop()
            if folder.resolve(strict=True) != folder or _reparse(folder.lstat()):
                raise Refused("outside")
            with os.scandir(folder) as children:
                for child in children:
                    path = Path(child.path)
                    relative = path.relative_to(root).as_posix()
                    files._parts(relative)
                    # DirEntry.stat на Windows может не заполнять inode и номер тома.
                    entry = _entry(root, path, path.lstat())
                    entries[relative] = entry
                    if entry == DIRECTORY:
                        folders.append(path)
                    if len(entries) > ENTRY_MAX:
                        raise Refused("too_large", "в проекте слишком много файлов для зеркала")
        if len(serial(entries)) > MANIFEST_MAX:
            raise Refused("too_large", "список файлов проекта слишком велик для зеркала")
        return {"entries": entries, "name": root.name}
    except OSError:
        raise Refused("busy", "не удалось прочитать все файлы проекта") from None


def read(root, reads):
    if not isinstance(reads, list) or len(reads) > 128:
        raise Refused("invalid")
    payload, total = [], 0
    try:
        for path, offset, count in reads:
            if type(offset) is not int or offset < 0 or type(count) is not int or not 0 <= count <= CHUNK - total:
                raise Refused("invalid")
            full = files._mutation_path(root, path)
            info = full.lstat()
            if _reparse(info) or not stat.S_ISREG(info.st_mode):
                raise Refused("outside")
            with full.open("rb") as stream:
                if not os.path.samestat(info, os.fstat(stream.fileno())):
                    raise Refused("conflict", "файл изменился во время передачи")
                stream.seek(offset)
                data = stream.read(count)
            if len(data) != count:
                raise Refused("conflict", "размер файла изменился во время передачи")
            payload.append([path, offset, base64.b64encode(data).decode("ascii")])
            total += count
    except (ValueError, TypeError):
        raise Refused("invalid") from None
    except OSError:
        raise Refused("busy", "не удалось прочитать файл проекта") from None
    return {"chunks": payload}


class Transfer:
    def __init__(self, destination, session, ticket):
        self.destination = destination
        self.session = session
        self.ticket = ticket
        self.temp = Path(tempfile.mkdtemp(dir=destination.parent, prefix=".studio-mirror-"))
        self.incoming = self.temp / "files"
        self.incoming.mkdir()
        self.preserve = False

    def close(self):
        if not self.preserve:
            temp = files._mutation_path(self.temp.parent, self.temp.name)
            files._delete(self.temp.parent, temp)

    def write(self, chunks):
        total = 0
        if not isinstance(chunks, list) or not 1 <= len(chunks) <= 128:
            raise Refused("invalid")
        for path, offset, data in chunks:
            parts = files._parts(path)
            if not parts or type(offset) is not int or offset < 0 or not isinstance(data, str):
                raise Refused("invalid")
            raw = base64.b64decode(data, validate=True)
            total += len(raw)
            if total > CHUNK:
                raise Refused("too_large")
            # Имена файлов хранилища независимы от путей проекта и регистра ФС.
            staged = self.incoming / hashlib.sha256(path.encode()).hexdigest()
            mode = "xb" if offset == 0 else "r+b"
            with open(staged, mode, opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
                if output.seek(0, os.SEEK_END) != offset:
                    raise Refused("conflict")
                output.write(raw)

    def apply(self, expected, updates, remove):
        dest = self.destination
        before = scan(dest)["entries"]
        if fingerprint(before) != expected:
            raise Refused("conflict", "зеркало изменилось — обновите сравнение")
        new_project = before is None
        root_info = None if new_project else dest.lstat()
        before = before or {}
        staged = {}
        for path, entry in updates.items():
            parts = files._parts(path)
            if not parts:
                raise Refused("invalid")
            if entry[0] == "file":
                candidate = self.incoming / hashlib.sha256(path.encode()).hexdigest()
                with candidate.open("rb") as stream:
                    if os.fstat(stream.fileno()).st_size != entry[2] or (
                            hashlib.file_digest(stream, "sha256").hexdigest() != entry[1]):
                        raise Refused("conflict", "содержимое файла изменилось во время передачи")
                if os.name != "nt" and entry[3]:
                    candidate.chmod(0o700)
                staged[path] = candidate
            elif entry[0] == "link":
                if not _safe_link(dest, dest.joinpath(*parts), entry[1]):
                    raise Refused("outside", "ссылка ведёт за пределы проекта")
            elif entry != DIRECTORY:
                raise Refused("invalid")
        for path in remove:
            if not files._parts(path) or path not in before or before[path] == SKIPPED:
                raise Refused("invalid")
        backup = self.temp / "previous"
        backup.mkdir()
        undo = []
        try:
            if new_project:
                dest.mkdir(mode=0o700)
                root_info = dest.lstat()
                undo.append((dest, None, root_info))
            if not stat.S_ISDIR(root_info.st_mode) or _reparse(root_info):
                raise Refused("outside")
            for path in sorted(remove, key=lambda p: p.count("/"), reverse=True):
                self._same_root(dest, root_info)
                self._save(dest, backup, before, path, undo)
            for path, entry in sorted(updates.items(), key=lambda item: item[0].count("/")):
                self._same_root(dest, root_info)
                full = dest.joinpath(*files._parts(path))
                parent = full.parent
                if parent.resolve(strict=True) != parent or not parent.is_dir():
                    raise Refused("outside")
                if path in before and path not in remove:
                    self._save(dest, backup, before, path, undo)
                if _stat(full) is not None:
                    raise Refused("conflict", "в зеркале появился новый файл — обновите сравнение")
                if entry == DIRECTORY:
                    full.mkdir()
                elif entry[0] == "link":
                    if not _safe_link(dest, full, entry[1]):
                        raise Refused("outside")
                    full.symlink_to(entry[1], target_is_directory=bool(entry[3]))
                elif os.name == "nt":
                    os.rename(staged[path], full)  # На Windows rename отказывает, если путь уже появился.
                else:
                    os.link(staged[path], full)  # Создаёт имя атомарно и отказывает при существующем пути.
                undo.append((full, None, full.lstat()))
        except BaseException:
            recovery = {}
            for full, saved, created in reversed(undo):
                try:
                    if full.parent.resolve(strict=True) != full.parent:
                        raise OSError
                    if saved is not None:
                        if _stat(full) is not None:
                            raise FileExistsError
                        os.rename(saved, full)
                    else:
                        info = full.lstat()
                        if stat.S_ISDIR(created.st_mode):
                            if not os.path.samestat(created, info):
                                raise OSError
                            full.rmdir()
                        elif _stamp(created) == _stamp(info):
                            full.unlink()
                        else:
                            raise OSError
                except OSError:
                    self.preserve = True
                    if saved is not None:
                        recovery[saved.name] = full.relative_to(dest).as_posix()
            if self.preserve:
                (self.temp / "recovery.json").write_bytes(serial({"project": dest.name, "files": recovery}))
                raise Refused("busy", "откат прерван локальными изменениями — прежние файлы сохранены в .studio-mirror-*") from None
            raise

    @staticmethod
    def _same_root(dest, info):
        current = dest.lstat()
        if _reparse(current) or not os.path.samestat(info, current) or dest.resolve(strict=True) != dest:
            raise Refused("conflict", "папка зеркала была заменена")

    @staticmethod
    def _save(dest, backup, before, path, undo):
        full = dest.joinpath(*files._parts(path))
        if full.parent.resolve(strict=True) != full.parent:
            raise Refused("outside")
        info = _stat(full)
        if info is None or _entry(dest, full, info) != before[path]:
            raise Refused("conflict", "зеркало изменилось — обновите сравнение")
        if before[path] == DIRECTORY and any(full.iterdir()):
            raise Refused("conflict", "в зеркале появились новые файлы — обновите сравнение")
        saved = backup / str(len(undo))
        os.rename(full, saved)
        undo.append((full, saved, None))


class Transfers:
    """Одна входящая передача на компьютер, только из открывшей её сессии."""

    def __init__(self):
        self.lock = threading.Lock()
        self.transfer = None
        self.timer = None
        self.deadline = 0.0

    @property
    def active(self):
        return self.timer is not None

    def _close(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if self.transfer:
            self.transfer.close()
            self.transfer = None

    def _expire(self):
        with self.lock:
            if self.timer is threading.current_thread():
                self.timer = None
                try:
                    if self.deadline > time.monotonic():
                        self._schedule()
                    else:
                        self._close()
                except OSError:
                    # На следующем запросе повторим уборку перед началом новой передачи.
                    pass

    def _schedule(self):
        timer = threading.Timer(max(0, self.deadline - time.monotonic()), self._expire)
        timer.daemon = True
        self.timer = timer
        timer.start()

    def _renew(self):
        self.deadline = time.monotonic() + 300
        if self.timer is None:
            self._schedule()

    def run(self, op, frame, config):
        with self.lock:
            try:
                session, ticket = frame["session"], frame["ticket"]
                if op == "mirror_begin":
                    if self.transfer and self.timer:
                        raise Refused("busy", "на компьютер уже передаётся зеркало")
                    self._close()
                    self.transfer = Transfer(target(config, frame["root"], frame["name"]), session, ticket)
                    self._renew()
                else:
                    active = self.transfer
                    if not active or (active.session, active.ticket) != (session, ticket):
                        raise Refused("conflict", "передача зеркала прервана — повторите операцию")
                    if op == "mirror_chunk":
                        active.write(frame.get("chunks"))
                        self._renew()
                    elif op == "mirror_apply":
                        destination = target(config, frame["root"], frame["name"])
                        if destination != active.destination:
                            raise Refused("outside")
                        try:
                            active.apply(frame["expected"], frame.get("updates"), frame.get("remove"))
                            return {"pid": projects.project_id(config, destination.parent, destination.name)}
                        finally:
                            self._close()
                    elif op == "mirror_abort":
                        self._close()
            except (ValueError, TypeError, AttributeError, IndexError):
                raise Refused("invalid") from None
            except OSError:
                raise Refused("busy", "не удалось записать зеркало — проверьте диск и права доступа") from None
            return {}
