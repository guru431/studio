"""Наложение настроек DeepSeek Harness человека на профиль `acp` (решение А, спека §6.1).

Источник — патч профиля `web` или `desktop` (тот, что менялся последним). Из него берутся только строки
известных видов (`KINDS`) и переводятся для `acp` по таблице «Строки наложения» отчёта пробы 2026-11-22;
остальное попадает в «не применено» — только имя строки и код причины (`REASONS`), без значений, путей и имён
переменных окружения. YAML разбирает `js-yaml` из установки DSH (Node этой установки, без shell, с пределом
времени): PyYAML мосту не нужен, наложение пишется JSON-файлом — DSH принимает его в `--patch`.

Файлы DSH человека только читаются. Наложение — `<каталог моста>/deepseek/overlay-<16 hex>.json`: вызывающий
удаляет его после disconnect, остатки после падения стирает `sweep` при старте ядра.

Причины: `unknown` — вид строки мосту незнаком; `acp_own` — строку задал сам человек в
`profiles/acp/cordis.patch.yml` (`--patch` перекрыл бы её); `env_key` — ключ провайдера из переменной окружения,
которой нет ни в `.credentials.yaml`, ни у процесса моста, ни в `$DSH_HOME/.env`; `unreadable` — файл или
строка не разбирается (битый YAML, `!!js`, нет Node установки) или не переводится без вычислений."""

import json
import os
import secrets
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from . import host, store
from .deepseek_rpc import ACCESS, Install

PROFILES = ("web", "desktop")
PATCH = "cordis.patch.yml"
CREDENTIALS = ".credentials.yaml"
MCP_MANAGER = "@deepseek-ai/dsh-mcp-manager"
MCP_CLIENT = "@deepseek-ai/dsh-mcp-client"
LLM = ("llm-pi-ai", "llm-deepseek", "llm-deepseek-account")
KINDS = frozenset({*LLM, "agent-default-model", "mcp-manager", MCP_CLIENT, "permission"})
REASONS = ("unknown", "acp_own", "env_key", "unreadable")
NAME_MAX = 100
LIST_MAX = 100
FILE_MAX = 1024 * 1024
OUTPUT_MAX = 8 * 1024 * 1024
PARSE_SEC = 20
JS = "\u0000js"  # маркер значения `!!js` из помощника Node

# Помощник Node: js-yaml установки DSH (`createRequire` от входа CLI), `!!js` → маркер без вычисления; задание —
# JSON в stdin (тексты, а не пути), ответ — JSON в stdout. Таблица `presets` профиля `acp` — из бандла dsh-base.
HELPER = r"""
const {createRequire} = require('module');
const fs = require('fs');
const req = createRequire(process.argv[process.argv.length - 1]);
const yaml = req('js-yaml');
const mark = () => ({'\u0000js': 1});
const schema = yaml.DEFAULT_SCHEMA.extend(['scalar', 'sequence', 'mapping'].map(
  (kind) => new yaml.Type('tag:yaml.org,2002:js', {kind, construct: mark})));
const load = (text) => yaml.load(text, {schema});
const find = (value) => {
  if (Array.isArray(value)) {
    for (const item of value) { const found = find(item); if (found) return found; }
  } else if (value && typeof value === 'object') {
    if (value.id === 'permission' && value.name === '@deepseek-ai/dsh-permission-presets' && value.config
        && value.config.presets && typeof value.config.presets === 'object') return value.config.presets;
    for (const key of Object.keys(value)) { const found = find(value[key]); if (found) return found; }
  }
  return null;
};
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', () => {
  const job = JSON.parse(input);
  const docs = job.texts.map((text) => { try { return {ok: load(text) ?? null}; } catch (e) { return {error: 1}; } });
  let presets = null;
  if (job.presets) {
    try { presets = find(load(fs.readFileSync(req.resolve('@deepseek-ai/dsh-base/cordis.patch.yml'), 'utf8'))); }
    catch (e) { presets = null; }
  }
  process.stdout.write(JSON.stringify({docs, presets}));
});
"""

_BROKEN = object()  # документ не разобран


def dsh_home(environ: Mapping[str, str] | None = None) -> Path:
    """`DSH_HOME` или умолчание DSH (`os.homedir()/.dsh`: Windows — `%USERPROFILE%`, иначе `$HOME`)."""
    environ = os.environ if environ is None else environ
    if value := (environ.get("DSH_HOME") or "").strip():
        return Path(value).expanduser()
    user = environ.get("HOME" if host.MACOS or host.LINUX else "USERPROFILE")
    return (Path(user) if user else Path.home()) / ".dsh"


def source(home: Path) -> Literal["web", "desktop"] | None:
    """Профиль, чей `cordis.patch.yml` менялся последним; нет ни одного — `None` (только консоль: наложения нет)."""
    best = None
    for name in PROFILES:
        try:
            changed = (Path(home) / "profiles" / name / PATCH).stat().st_mtime_ns
        except OSError:
            continue
        if best is None or changed > best[0]:
            best = (changed, name)
    return best[1] if best else None


def _text(path: Path) -> str | None:
    """Текст файла; нет файла — `None`; не читается или больше `FILE_MAX` — `ValueError`."""
    try:
        if path.stat().st_size > FILE_MAX:
            raise ValueError("too large")
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(type(error).__name__) from None


def _helper(install: Install | None) -> list[str] | None:
    """Команда Node установки с помощником; у Python-пакета своего Node для скриптов нет — `None`."""
    if install is None or install.reason is not None:
        return None
    if install.kind == "npm":
        return [install.command[0], "-e", HELPER, install.command[1]]
    if install.kind == "desktop":  # Electron в режиме RunAsNode (`install.env`), вход CLI из app.asar
        return [install.command[0], "-e", HELPER, install.command[-1]]
    return None


def _parse(install: Install | None, texts: list[str], environ: Mapping[str, str],
           run: Callable) -> tuple[list, dict | None] | None:
    """Разбор текстов YAML помощником Node: документы (`_BROKEN` — не разобран) и `presets` бандла `acp`."""
    command = _helper(install)
    if command is None:
        return None
    job = json.dumps({"texts": texts, "presets": True}, ensure_ascii=True).encode("ascii")
    try:
        result = run(command, input=job, capture_output=True, timeout=PARSE_SEC, creationflags=host.NO_WINDOW,
                     env={**environ, **install.env})
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or len(result.stdout or b"") > OUTPUT_MAX:
        return None
    try:
        data = json.loads(result.stdout.decode("utf-8"))
    except (ValueError, AttributeError):
        return None
    docs = data.get("docs") if isinstance(data, dict) else None
    if not isinstance(docs, list) or len(docs) != len(texts):
        return None
    parsed = [doc["ok"] if isinstance(doc, dict) and "ok" in doc else _BROKEN for doc in docs]
    presets = data.get("presets")
    return parsed, presets if isinstance(presets, dict) and presets else None


def _has_js(value) -> bool:
    if isinstance(value, dict):
        return JS in value or any(_has_js(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_js(item) for item in value)
    return False


def _dotenv(path: Path) -> set[str]:
    """Имена непустых переменных `$DSH_HOME/.env` (значения не нужны и не хранятся)."""
    try:
        text = _text(path) or ""
    except ValueError:
        return set()
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        if sep and not name.startswith("#") and value.strip().strip("'\""):
            names.add(name.strip())
    return names


def _entries(document) -> list[tuple[str, dict, bool]]:
    """Строки патча: (имя, запись, вставка ли это) — для `insert` по одной на элемент."""
    found = []
    for entry in document if isinstance(document, list) else []:
        if not isinstance(entry, dict):
            found.append(("?", {}, False))
        elif "insert" in entry:
            items = entry["insert"] if isinstance(entry["insert"], list) else [entry["insert"]]
            for item in items:
                item = item if isinstance(item, dict) else {}
                name = item.get("id") if isinstance(item.get("id"), str) else item.get("name")
                found.append((name if isinstance(name, str) and name else "?", item, True))
        else:
            name = entry.get("id")
            found.append((name if isinstance(name, str) and name else "?", entry, False))
    return found


def _own(document) -> set[str]:
    """id строк и вставок, которые человек сам задал в `profiles/acp/cordis.patch.yml`."""
    return {entry["id"] for _, entry, _ in _entries(document) if isinstance(entry.get("id"), str)}


def _kind(name: str, entry: dict, inserted: bool) -> str | None:
    if inserted:
        return MCP_CLIENT if entry.get("name") == MCP_CLIENT else None
    return name if name in KINDS and name != MCP_CLIENT else None


def _targets(kind: str, name: str, entry: dict) -> set[str]:
    """id строк профиля `acp`, которые запишет перевод строки."""
    if kind == "agent-default-model":
        return {kind, "acp"}
    if kind == MCP_CLIENT:
        return {entry["id"]} if isinstance(entry.get("id"), str) else set()
    return {name}


def _config(entry: dict) -> dict:
    return entry["config"] if isinstance(entry.get("config"), dict) else {}


class _Summary:
    def __init__(self, profile):
        self.profile, self.applied, self.skipped = profile, [], []

    def apply(self, name: str) -> None:
        name = name[:NAME_MAX]
        if name not in self.applied and len(self.applied) < LIST_MAX:
            self.applied.append(name)

    def skip(self, name: str, reason: str) -> None:
        item = {"name": name[:NAME_MAX], "reason": reason}
        if item not in self.skipped and len(self.skipped) < LIST_MAX:
            self.skipped.append(item)

    def result(self) -> dict:
        return {"profile": self.profile, "applied": self.applied, "skipped": self.skipped}


def _translate(entries, own: set[str], keys: Callable[[str], bool], presets: dict | None, summary: _Summary,
               *, mcp: bool, permission: bool) -> list:
    """Строки источника → строки наложения `acp` по таблице «Строки наложения»."""
    rows, dropped = [], set()
    for name, entry, inserted in entries:
        kind = _kind(name, entry, inserted)
        if kind is None:
            summary.skip(name, "unknown")
            continue
        if _has_js(entry):
            summary.skip(name, "unreadable")
            continue
        if _targets(kind, name, entry) & own:
            summary.skip(name, "acp_own")
            continue
        if kind == "llm-pi-ai":
            config = _config(entry)
            providers = config.get("providers")
            if isinstance(providers, dict):
                kept = {}
                for provider, spec in providers.items():
                    env = spec.get("apiKeyEnv") if isinstance(spec, dict) else None
                    if isinstance(env, str) and env and not keys(env):
                        dropped.add(provider)
                        summary.skip(f"{name}:{provider}", "env_key")
                    else:
                        kept[provider] = spec
                if providers and not kept and set(config) == {"providers"}:
                    continue
                entry = {**entry, "config": {**config, "providers": kept}}
            rows.append(entry)
        elif kind == "agent-default-model":
            config = _config(entry)
            provider, model = config.get("provider"), config.get("model")
            if provider in dropped:
                summary.skip(name, "env_key")
                continue
            rows.append(entry)
            if isinstance(provider, str) and isinstance(model, str):
                rows.append({"id": "acp", "config": {"provider": provider, "model": model}})
        elif kind == "mcp-manager":
            if mcp:
                item = {"id": "mcp-manager", "name": MCP_MANAGER, "inject": ["credentials"],
                        "config": _config(entry)}
                if isinstance(entry.get("disabled"), bool):
                    item["disabled"] = entry["disabled"]
                rows.append({"insert": [item]})
        elif kind == MCP_CLIENT:
            if mcp:
                rows.append({"insert": [entry]})
        elif kind == "permission":
            config = _config(entry)
            if isinstance(config.get("presets"), dict):
                row = {"id": "permission", "config": config}
            else:
                default = config.get("defaultPreset")
                if presets is None or not isinstance(default, str) or default not in presets:
                    summary.skip(name, "unreadable")
                    continue
                row = {"id": "permission", "config": {"presets": presets, "defaultPreset": default}}
            if permission:
                rows.append(row)
        else:  # llm-deepseek, llm-deepseek-account
            rows.append(entry)
        summary.apply(name)
    return rows


def build(home: Path, environ: Mapping[str, str], private: Path, install: Install | None, *,
          scratch: Path | None = None, access: str | None = None,
          run: Callable = subprocess.run) -> tuple[Path | None, dict]:
    """Наложение для `dsh --profile acp --patch <файл>` и сводка `{"profile", "applied", "skipped"}`.

    `install` — установка, чьим Node разбирается YAML (`deepseek_rpc.pick`); без пригодной — источник «unreadable».
    `scratch` — пробный процесс каталога (D4): хранилища сессий и проекций — во временный каталог, без MCP.
    `access` — права чата: явные (`deepseek_rpc.ACCESS`) сильнее `defaultPreset` профиля, строка `permission`
    в файл не пишется. Ни `scratch`, ни `access` сводку не меняют. Файла нет, если записывать нечего."""
    home = Path(home)
    profile = source(home)
    summary = _Summary(profile)
    rows = []
    if profile is not None:
        try:
            text = _text(home / "profiles" / profile / PATCH)
        except ValueError:
            text = None
        try:
            acp_text = _text(home / "profiles" / "acp" / PATCH)
        except ValueError:
            summary.skip("acp", "unreadable")
            return None, summary.result()
        try:
            credentials = _text(home / CREDENTIALS)
        except ValueError:
            credentials = None
        texts = [text or "", acp_text or "", credentials or ""]
        parsed = _parse(install, texts, environ, run) if text is not None else None
        document = parsed[0][0] if parsed else _BROKEN
        if document is _BROKEN or not isinstance(document, (list, type(None))):
            summary.skip(profile, "unreadable")
            return None, summary.result()
        (_, own_doc, secrets_doc), presets = parsed
        if own_doc is _BROKEN or not isinstance(own_doc, (list, type(None))):
            summary.skip("acp", "unreadable")
            return None, summary.result()
        refs = secrets_doc.get("refs") if isinstance(secrets_doc, dict) else None
        refs = refs if isinstance(refs, dict) else {}
        dotenv = None

        def keys(name: str) -> bool:
            nonlocal dotenv
            if isinstance(refs.get(name), str) and refs[name] or environ.get(name):
                return True
            if dotenv is None:
                dotenv = _dotenv(home / ".env")
            return name in dotenv

        rows = _translate(_entries(document), _own(own_doc), keys, presets, summary,
                          mcp=scratch is None, permission=access not in ACCESS)
    if scratch is not None:
        rows += [{"id": "session-persistence-jsonl", "config": {"root": str(Path(scratch) / "sessions")}},
                 {"id": "storage-json", "config": {"root": str(Path(scratch) / "storages")}}]
    if not rows:
        return None, summary.result()
    folder = Path(private) / "deepseek"
    host.private_dir(folder, parents=True, exist_ok=True)
    path = folder / f"overlay-{secrets.token_hex(8)}.json"
    store.write_atomic(path, json.dumps(rows, ensure_ascii=True, allow_nan=False).encode("ascii"))
    return path, summary.result()


def sweep(bridge_home: Path) -> int:
    """При старте ядра, до первого хода: наложения (`deepseek/overlay-*`) процессов, оборванных падением моста
    или выключением компьютера, — в них MCP и параметры провайдеров человека."""
    gone = 0
    try:
        stale = [p for p in (Path(bridge_home) / "deepseek").glob("overlay-*") if not p.is_symlink()]
    except OSError:
        return 0
    for path in stale:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink()
        except OSError:
            continue
        gone += not path.exists()
    return gone
