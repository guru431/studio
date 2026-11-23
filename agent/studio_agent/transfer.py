"""Перенос моста на новый компьютер: `export-chats` / `import-chats` (IDEAS 09:I8).

Архив ZIP несёт только файлы соответствий «UUID студии → сессия помощника» и выбор модели по чатам (`FILES`) и
`manifest.json` (формат, платформа и папки проектов прежнего компьютера). Ключа устройства, PIN, `config.json` и
журнала в нём нет: ключ и PIN привязаны к учётке ОС, а новый компьютер получает свой ключ привязкой и наследует
id прежнего через «Заменить компьютер» в Студии. Импорт дополняет файлы нового компьютера (его записи сильнее);
путь проекта в соответствиях переводится на папку проектов нового компьютера, где есть тот же путь внутри корня.
Архив читается только по известным именам — распаковки в произвольные пути нет."""

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import zipfile
import zlib

from . import host, store
from .atomic import write_atomic

FORMAT = 1
MANIFEST = "manifest.json"
#: соответствия UUID студии → сессия (`codex.Aliases`, `acp.Aliases`): в записи — настоящий путь проекта
ALIASES = ("codex-chats.json", "opencode-chats.json", "deepseek-chats.json")
#: выбор модели, усилия и доступа по чатам (`store.Choices` Claude Code и Codex, OpenCode, DeepSeek Harness)
CHOICES = (store.CHOICES, "opencode/" + store.CHOICES, "deepseek/" + store.CHOICES)
FILES = ALIASES + CHOICES
FILE_MAX = 8 * 1024 * 1024
MANIFEST_MAX = 1024 * 1024  # формат, платформа и корни — заявленный размер больше значит чужой архив (zip-бомба)


class TransferError(Exception):
    """Не архив переноса этого формата или файл в нём — не JSON-объект."""


def _read(path: Path) -> dict | None:
    """Файл каталога моста: объект JSON; нет файла или он битый — None."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def export(h: Path, target: Path) -> int:
    """Архив `target` из файлов каталога моста `h`; итог — сколько файлов в нём (без `manifest.json`)."""
    cfg = store.load_config(h)
    roots = [r for r in dict.fromkeys([*cfg.projects_roots, cfg.projects_legacy_root]) if r]
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST, json.dumps({"format": FORMAT, "platform": host.platform_id(), "roots": roots}))
        for name in FILES:
            data = _read(h / name)
            if data is not None:  # ASCII, как сам мост: имена Windows бывают с одиночными суррогатами
                archive.writestr(name, json.dumps(data, ensure_ascii=True))
                count += 1
    return count


def _json(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int):
    """Запись архива как JSON; больше `limit`, не JSON, слишком глубокая вложенность (`RecursionError`), шифр или
    неизвестное сжатие (`RuntimeError`, `NotImplementedError`), битый поток — `TransferError`, без трассировки."""
    if info.file_size > limit:
        raise TransferError(info.filename)
    try:
        return json.loads(archive.read(info))
    except (ValueError, RuntimeError, zlib.error, EOFError):
        raise TransferError(info.filename) from None


def _load(source: Path) -> tuple[dict, dict[str, dict]]:
    with zipfile.ZipFile(source) as archive:
        try:
            info = archive.getinfo(MANIFEST)
        except KeyError:
            raise TransferError(MANIFEST) from None
        manifest = _json(archive, info, MANIFEST_MAX)
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise TransferError(MANIFEST)
        found = {}
        for name in FILES:
            try:
                info = archive.getinfo(name)
            except KeyError:
                continue
            data = _json(archive, info, FILE_MAX)
            if not isinstance(data, dict):
                raise TransferError(name)
            found[name] = data
    return manifest, found


def _mover(manifest: dict, roots: list[str]):
    """Путь проекта прежнего компьютера → та же папка внутри одного из корней нового (если она есть)."""
    old_roots = [r for r in manifest.get("roots", []) if isinstance(r, str) and r] \
        if isinstance(manifest.get("roots"), list) else []
    flavour = PureWindowsPath if manifest.get("platform") == "win32" else PurePosixPath

    def move(item):
        if not isinstance(item, dict) or not isinstance(item.get("project"), str):
            return item
        old = flavour(item["project"])
        for root in old_roots:
            try:
                inner = old.relative_to(flavour(root))
            except ValueError:
                continue
            for new_root in roots:
                candidate = Path(new_root, *inner.parts)
                if candidate.is_dir():
                    return {**item, "project": str(candidate.resolve())}
        return item
    return move


def import_(h: Path, source: Path) -> int:
    """Записи архива `source` — в файлы каталога моста `h`, где их ещё нет; итог — сколько записей добавлено."""
    manifest, found = _load(source)
    move = _mover(manifest, store.load_config(h).projects_roots)
    added = 0
    for name, data in found.items():
        if name in ALIASES:
            data = {chat: move(item) for chat, item in data.items()}
        path = h / name
        current = _read(path) or {}
        merged = {**{k: v for k, v in data.items() if k not in current}, **current}  # свои — свежее, в конце
        if name in CHOICES:
            merged = dict(list(merged.items())[-store.CHOICES_MAX:])
        if len(merged) == len(current):
            continue
        added += len(merged) - len(current)
        host.private_dir(path.parent, parents=True, exist_ok=True)
        write_atomic(path, json.dumps(merged, ensure_ascii=True).encode("utf-8"))  # `\udc80` из архива — не сбой
    return added
