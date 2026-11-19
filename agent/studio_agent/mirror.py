"""Манифест проекта и приём копии в разрешённой папке моста."""

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import files, gitinfo, projects
from .errors import Refused

CHUNK = 4 * 1024 * 1024
MANIFEST_MAX = 20 * 1024 * 1024  # вместе два манифеста могут поместиться в кадр
ENTRY_MAX = 150_000
EXCLUDE_MAX = 64
REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT
DIRECTORY = ["dir", "", 0, 0]
SKIPPED = ["skip", "", 0, 0]
REMOTE_KEY = r"^remote\.(origin|github)\.url$"
DISCOVERY_SEC = 20
DISCOVERY_MAX = 100
REPOSITORY_ID = re.compile(r"[0-9a-f]{64}")


def serial(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def fingerprint(entries) -> str:
    return hashlib.sha256(serial(entries)).hexdigest()


def _repository_id(url: str) -> str | None:
    """Идентичность сетевого remote без имени пользователя, пароля, параметров и транспорта."""
    if not 0 < len(url) <= 4096 or any(c.isspace() or ord(c) < 32 or c == "\\" for c in url):
        return None
    if "://" not in url:
        scp = re.fullmatch(r"(?:[^@/:]+@)?([A-Za-z0-9.-]{2,}):([^/].*)", url)
        if scp is None:
            return None
        url = f"ssh://{scp[1]}/{scp[2]}"
    try:
        parsed = urlsplit(url)
        default_port = {"http": 80, "https": 443, "ssh": 22, "git": 9418}.get(parsed.scheme)
        hostname = parsed.hostname
        if not default_port or not hostname:
            return None
        port = parsed.port
        path = unquote(parsed.path, errors="strict").strip("/").removesuffix(".git")
    except ValueError:
        return None
    if any(not part or part in (".", "..") for part in path.split("/")) or any(ord(c) < 32 for c in path):
        return None
    hostname = hostname.rstrip(".")
    if hostname == "github.com":
        path = path.casefold()
    return fingerprint([hostname, None if port in (None, default_port) else port, path])


def _repositories(project: Path) -> set[str]:
    try:
        # Читается только собственный обычный .git/config. Include и сетевые операции не нужны.
        config = files._mutation_path(project, ".git/config")
        info = config.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            return set()
        result = gitinfo.run(project, ["config", "--no-includes", "--file", str(config), "-z",
                                       "--get-regexp", REMOTE_KEY], timeout=2)
    except (OSError, Refused, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    repositories = set()
    for row in result.stdout.split(b"\0"):
        key, sep, value = row.partition(b"\n")
        if sep and key in (b"remote.origin.url", b"remote.github.url"):
            ident = _repository_id(value.decode("utf-8", "replace"))
            if ident:
                repositories.add(ident)
    return repositories


def identity(project: Path) -> dict:
    return {"name": project.name, "repositories": sorted(_repositories(project))[:16]}


def valid_repositories(value) -> bool:
    return (isinstance(value, list) and len(value) <= 16
            and all(isinstance(item, str) and REPOSITORY_ID.fullmatch(item) for item in value))


def candidates(config, name: str, repositories: list[str], *, now=time.monotonic) -> dict:
    """Похожие папки в локальном каталоге. Известные разные remotes исключают совпадение только по имени."""
    deadline = now() + DISCOVERY_SEC
    roots = {Path(root["path"]): root for root in projects.list_roots(config)}
    wanted = unicodedata.normalize("NFC", name).casefold()
    catalog, complete = projects.scan_catalog(config)
    catalog.sort(key=lambda entry: unicodedata.normalize("NFC", entry[1].name).casefold() != wanted)
    source_ids = set(repositories)
    items = []
    for row, path in catalog:
        if now() >= deadline or len(items) == DISCOVERY_MAX:
            return {"items": items, "complete": False}
        if path.parent not in roots or not projects.valid_name(path.name):
            continue
        same_name = unicodedata.normalize("NFC", path.name).casefold() == wanted
        if not same_name and not source_ids:
            continue
        local = _repositories(path) if source_ids else set()
        if source_ids & local:
            match = "repository"
        elif same_name and not (source_ids and local):
            match = "name"
        else:
            continue
        root = roots[path.parent]
        items.append({"pid": row["pid"], "root": root["id"], "folder": root["path"], "name": path.name, "match": match})
    return {"items": items, "complete": complete}


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


def destination(config, root_id, name, *, metadata_only=False, exclude=()):
    path = target(config, root_id, name)
    if metadata_only:
        _standalone_git(path)
    data = {"exists": _stat(path) is not None} if metadata_only else scan(path, exclude)
    return {**data, "pid": projects.project_id(config, path.parent, path.name), "folder": str(path.parent)}


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _reparse(info):
    return getattr(info, "st_file_attributes", 0) & REPARSE


def _standalone_git(root):
    git = _stat(root / ".git")
    if git is not None and (not stat.S_ISDIR(git.st_mode) or _reparse(git)):
        raise Refused("conflict", "Папка использует внешний каталог Git. Выберите основной репозиторий или отдельный клон.")


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


def valid_exclude(value) -> bool:
    """Постоянные исключения зеркала: точные относительные пути, без шаблонов; `.git` целиком переносится всегда."""
    if not isinstance(value, list) or len(value) > EXCLUDE_MAX:
        return False
    for path in value:
        try:
            parts = files._parts(path)
        except Refused:
            return False
        if not parts or parts[0] == ".git":
            return False
    return True


def scan(root: Path, exclude=()) -> dict:
    """Все доступные обычные файлы и пустые папки, в том числе .git и gitignored. Исключённый путь — запись
    «пропущен» без чтения и обхода: план его не переносит и в копии не удаляет."""
    exclude = set(exclude or ())
    try:
        info = _stat(root)
        if info is None:
            return {"entries": None}
        if _reparse(info) or not stat.S_ISDIR(info.st_mode) or root.resolve() != root:
            raise Refused("outside", "путь назначения должен быть обычной папкой проекта")
        _standalone_git(root)
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
                    entry = SKIPPED if relative in exclude else _entry(root, path, path.lstat())
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
        _standalone_git(root)
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

    def apply(self, expected, updates, remove, exclude=()):
        dest = self.destination
        before = scan(dest, exclude)["entries"]
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
                    mode = 0o700
                    if path in before and before[path][0] == "file" and before[path][3]:
                        old = files._mutation_path(dest, path)
                        mode = old.lstat().st_mode & 0o777
                    candidate.chmod(mode)
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
                    destination = target(config, frame["root"], frame["name"])
                    _standalone_git(destination)
                    self.transfer = Transfer(destination, session, ticket)
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
                            active.apply(frame["expected"], frame.get("updates"), frame.get("remove"),
                                         frame.get("exclude") or ())
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
