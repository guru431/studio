"""Сторож перевода слов сервера и моста: каждый русский текст `app/*.py` и `agent/studio_agent/*.py`, который
уходит на страницу (отказ, слова задания, итог проверки, отказ моста), переводится `StudioI18n.message` — строкой
`app/static/en.js` или шаблоном `StudioEnglishMessages`. Тексты собирает AST: аргументы вызовов-отказов
(`SINK_CALLS`), значения ключей ответа (`SINK_KEYS`), возвраты функций-слов (`WORD_FUNCTIONS`) и константы модуля,
на которые они ссылаются. Вставка f-строки проверяется значением `7`: шаблон должен перевести всё, кроме подстановки."""
import ast
import functools
import itertools
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import APP, ROOT
import i18n_words

CYRILLIC = re.compile('[А-Яа-яЁё]')

#: вызовы, все строковые аргументы которых видит пользователь: HTTP-отказы, ошибки заданий и обёртки над ними
SINK_CALLS = {
    'HTTPException', 'JSONResponse', '_refuse', 'refused', '_bad', '_gone', '_series_refused', 'UploadError',
    'JobError', 'ToolError', 'DocumentError', 'TranslateError', 'UpstreamRefused', 'OcrError', 'SpeechError',
    'AudioError', 'PageFailed', 'Busy', '_fail', '_settle', 'StorageError', 'TooBig', '_Missing',
    '_item', '_login_page', 'ConnectError',
}

#: ключи ответа API и записей заданий, значение которых страница показывает словами
SINK_KEYS = {'message', 'error', 'words', 'detail', 'problems', 'reason', 'title', 'label'}

#: имена переменных и полей, присваивание которым — слова для страницы (состояние задания, причина)
SINK_NAMES = SINK_KEYS | {'stage', 'why', 'hint'}

#: строковые константы модулей с кириллицей, которые страница не показывает: `модуль.ИМЯ` → причина
EXEMPT_CONSTANTS = {
    'chat.WEB_NOTE': 'подсказка модели',
    'chat_system.MCP_NOTE': 'подсказка модели',
    'chat_system.MCP_FRAME': 'рамка правил MCP для модели',
    'chat_system.ROLE_HEAD': 'заголовок роли для модели',
    'chat_system.CUT': 'пометка обрезки для модели',
    'checks.PROBE_TEXT': 'проба модели',
    'web.NOTHING': 'результат инструмента для модели',
    'db.YO': 'SQL',
    'videos.MCP_VIDEOS': 'SQL ищет фрагмент сводки Spark для старых роликов',
    'manage.USAGE': 'командная строка администратора',
    'takeout.README': 'файл внутри выгрузки',
    'upstream.KEY_PLACE': 'часть отказа gateway_error, сверяется целым отказом',
    'chat.REFUSED': 'пометка в контексте модели',
    'mcp_files.NOT_SENT': 'заметка модели о файлах диалога',
    'mcp_files.ON_SERVER': 'заметка модели о файлах диалога',
    'mcp_files.TOO_BIG': 'заметка модели о файлах диалога',
    'mcp_files.LEFT': 'заметка модели о файлах диалога',
    'mcp_files.CHANGED': 'заметка модели о файлах диалога',
    'mcp_files.FAILED': 'заметка модели о файлах диалога',
    'mcp_files.MISSING': 'заметка модели о файлах диалога',
    'mcp_files.BLOCKED_WEB': 'заметка модели о файлах диалога',
    'mcp_files.BLOCKED_CONFIRM': 'заметка модели о файлах диалога',
    'mcp_files.BLOCKED_CLEANER': 'заметка модели о файлах диалога',
    'chat_pending.REFUSED': 'результат инструмента для модели',
    'chat_tools.NO_ANSWER': 'результат инструмента для модели',
    'knowledge.NOTHING': 'результат инструмента для модели',
    'chat.DUPLICATE': 'результат инструмента для модели',
    'chat.HISTORY_CUT': 'пометка выжимки результата инструмента для модели',
    'chat.RESULT_IMAGES_NOTE': 'пометка картинок результата инструмента для модели',
    'chat_system.MEMORY_HEAD': 'заголовок памяти для модели',
    'chat_tools.TODO_SAVED': 'результат инструмента для модели',
    'chat_tools.MEMORY_SAVED': 'результат инструмента для модели',
    'chat_tools.MEMORY_FORGOTTEN': 'результат инструмента для модели',
    'chat_tools.PART_CUT': 'результат инструмента для модели (шаг части без итога)',
    'transcripts.MARKER': 'служебная реплика внутри текста расшифровки — содержимое, не слова',
    'translate.SCANNING': 'начало этапа «распознаю · стр. {0} из {1}» — сверяется целым этапом',
}

#: функции, возвращающие слова для пользователя: (модуль, имя)
WORD_FUNCTIONS = {
    ('upstream', 'gateway_error'), ('upstream', 'refused_words'), ('upstream', '_reason'),
    ('checks', 'refused_words'), ('checks', '_down'), ('checks', '_refusal'), ('checks', '_changed'),
    ('images', 'release_after_cancel'), ('videos', '_unreachable'),
    ('videos', '_failure_words'), ('web', '_exa_error'), ('web', '_yt_words'), ('translate', '_unread'),
    ('translate', '_failed'), ('transcripts', '_busy'), ('mcp_client', '_list'), ('ocr', 'release_now'),
    ('chat', '_read_model'), ('chat', '_round'), ('chat', '_too_big'), ('admin', 'get_storage'),
    ('admin', '_taken'), ('sections', 'unlisted'), ('connections', '_gone'), ('chat_shares', 'public_guard'),
    ('cleaner_route', 'words'), ('cleaner_route', 'forced_refusal'),
}

#: тексты, которые нашёл сторож, но переводить не нужно: текст → причина
EXEMPT = {
    'привязка': 'имя действия для журнала (`agents._refuse`, параметр what)',
    'отвязка': 'имя действия для журнала (`agents._refuse`, параметр what)',
    'убран': 'внутренняя причина UpstreamRefused: пользователь видит refused_words',
}

# ---- мост ------------------------------------------------------------------------------------------------------
# Слова моста студия передаёт странице «Агентов» как есть: отказ `Refused` — ответом `claude_code` (`words(err)`),
# итог хода — `done.message` (`failure`), итог обновления — `message` статуса (`updater.FAILED_WORDS`), подписи
# лимитов — `label`/`reason`/`source` (`subscription`), имена подзадач — `title`. Окно, трей и консоль моста — свой
# словарь `agent/studio_agent/i18n.py` и свой сторож (`agent/tests/test_i18n.py`), сюда не входят.

AGENT = ROOT / 'agent' / 'studio_agent'

#: вызовы моста, строковые аргументы которых уходят в студию: отказ и слова «файл больше предела» (`files._read`)
AGENT_SINK_CALLS = {'Refused', '_read'}

#: ключи кадров моста, значение которых страница показывает словами: и имя шага (`tool`, «Рассуждения» OpenCode)
AGENT_SINK_KEYS = SINK_KEYS | {'tool'}

#: имена моста, присваивание которым — слова итога хода (`turns._done` → `done.message`)
AGENT_SINK_NAMES = AGENT_SINK_KEYS | {'failure'}

#: модули моста, чьих слов студия не видит: модуль → причина
AGENT_LOCAL = {
    'app': 'окно и трей моста (`Controller`): слова — в окне на компьютере',
    'enroll': 'привязка из окна или консоли моста',
    'setup': 'первичная настройка в консоли моста',
    'tray': 'подсказка значка в трее',
    'window': 'окно моста',
}

#: константы моста с кириллицей, которых страница не показывает словами: `модуль.ИМЯ` → причина
AGENT_EXEMPT_CONSTANTS = {
    'sessions.STEPS_CUT': 'пометка внутри текста сообщения истории — содержимое, не слова',
    'sessions.HEAD_CUT': 'пометка внутри текста сообщения истории — содержимое, не слова',
}

#: тексты моста, которые нашёл сторож, но переводить не нужно: текст → причина
AGENT_EXEMPT = {
    'Чтение состояния': 'ответ отказа инструмента для модели при фоновом чтении (`PermissionResultDeny.message`)',
    'Пользователь отказал': 'ответ отказа инструмента для модели (`turns.REFUSED`, `PermissionResultDeny.message`)',
}

SAMPLE = '7'

#: вызовы, слова которых — их первый аргумент: урезка и чистка строки
PASSTHROUGH = {'replace', 'strip', 'rstrip', 'capitalize', 'clip'}


class Root:
    """Корень обхода: каталог модулей и что в нём считается словами для страницы."""

    def __init__(self, name, path, calls, keys, names, words, exempt, exempt_constants, local=()):
        self.name, self.path, self.calls, self.keys, self.names = name, path, calls, keys, names
        self.words, self.exempt, self.exempt_constants = words, exempt, exempt_constants
        self.modules = sorted(p.stem for p in path.glob('*.py') if p.stem not in local)
        #: строка «интересна», если в ней кириллица, имя-константа (ВЕРХНИЙ_РЕГИСТР) или имя отказа, поля слов,
        #: функции-слов: операторы без таких строк обход пропускает — разбор всех модулей и так занимает почти секунду
        self.interesting = re.compile(r'[А-Яа-яЁё]|\b[A-Z][A-Z0-9_]{2,}\b|\b(?:%s)\b' % '|'.join(
            sorted(calls | names | {name for _, name in words})))


ROOTS = {
    'app': Root('app', APP, SINK_CALLS, SINK_KEYS, SINK_NAMES, WORD_FUNCTIONS, EXEMPT, EXEMPT_CONSTANTS),
    'agent': Root('agent', AGENT, AGENT_SINK_CALLS, AGENT_SINK_KEYS, AGENT_SINK_NAMES, set(), AGENT_EXEMPT,
                  AGENT_EXEMPT_CONSTANTS, AGENT_LOCAL),
}
MODULES = ROOTS['app'].modules


@functools.cache
def _tree(path: Path, module: str) -> ast.Module | None:
    file = path / f'{module}.py'
    return ast.parse(file.read_text(encoding='utf-8')) if file.is_file() else None


@functools.cache
def _lines(root: str, module: str) -> list[int]:
    """Префиксные суммы интересных строк модуля: диапазон оператора проверяется за O(1)."""
    prefix = [0]
    for line in (ROOTS[root].path / f'{module}.py').read_text(encoding='utf-8').splitlines():
        prefix.append(prefix[-1] + bool(ROOTS[root].interesting.search(line)))
    return prefix


@functools.cache
def _constants(path: Path, module: str) -> dict[str, ast.expr]:
    found = {}
    tree = _tree(path, module)
    for node in tree.body if tree is not None else []:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value
    return found


def _locals(function) -> dict[str, list[ast.expr]]:
    """Присваивания имён внутри функции (без вложенных функций): имя → все присвоенные значения."""
    found: dict[str, list[ast.expr]] = {}
    stack = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).append(node.value)
                elif isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    for left, right in zip(target.elts, node.value.elts):
                        if isinstance(left, ast.Name):
                            found.setdefault(left.id, []).append(right)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _name(node, names) -> str | None:
    """Имя цели присваивания или вызова: `x`, `obj.x`, `obj["x"]` (и `obj["x" if … else "y"]` — оба)."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        keys = (node.slice.body, node.slice.orelse) if isinstance(node.slice, ast.IfExp) else (node.slice,)
        for key in keys:
            if isinstance(key, ast.Constant) and key.value in names:
                return key.value
    return None


class Collector:
    def __init__(self, root: str = 'app'):
        self.root = ROOTS[root]
        self.found: dict[str, set[str]] = {}
        self.functions: list = []
        self.locals: dict[int, dict[str, list[ast.expr]]] = {}

    def consts(self, module: str) -> dict[str, ast.expr]:
        return _constants(self.root.path, module) if module in self.root.modules else {}

    def _local(self, name: str) -> list[ast.expr] | None:
        for function in reversed(self.functions):
            if id(function) not in self.locals:
                self.locals[id(function)] = _locals(function)
            if name in self.locals[id(function)]:
                return self.locals[id(function)][name]
        return None

    def _container(self, module: str, expr, depth: int) -> list:
        """Значения словаря, списка или кортежа, к которому ведёт выражение (константа модуля, индекс)."""
        if depth > 6:
            return []
        if isinstance(expr, ast.Dict):
            return list(expr.values)
        if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
            return list(expr.elts)
        if isinstance(expr, ast.Name) and expr.id in self.consts(module):
            return self._container(module, self.consts(module)[expr.id], depth + 1)
        if isinstance(expr, ast.Name):
            return [item for value in self._local(expr.id) or [] for item in self._container(module, value, depth + 1)]
        if (isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name)
                and expr.attr in self.consts(expr.value.id)):
            return self._container(expr.value.id, self.consts(expr.value.id)[expr.attr], depth + 1)
        if isinstance(expr, ast.Subscript):
            return [inner for item in self._container(module, expr.value, depth + 1)
                    for inner in (self._container(module, item, depth + 1) or [item])]
        return []

    def samples(self, module: str, expr, depth: int = 0) -> list[str]:
        """Варианты текста выражения: изменяемые части — `SAMPLE`; пусто — выражение не строка."""
        if depth > 6:
            return []
        if isinstance(expr, ast.Subscript):
            if isinstance(expr.slice, ast.Slice):
                return self.samples(module, expr.value, depth + 1)
            return [s for item in self._container(module, expr, depth + 1)
                    for s in self.samples(module, item, depth + 1)]
        if isinstance(expr, ast.Call):
            func = expr.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
            if name in PASSTHROUGH:
                source = func.value if isinstance(func, ast.Attribute) and name != 'clip' else (
                    expr.args[0] if expr.args else None)
                return self.samples(module, source, depth + 1) if source is not None else []
            if name == 'get' and isinstance(func, ast.Attribute) and len(expr.args) == 2:
                # `СЛОВА.get(code, по_умолчанию)` — любое значение словаря или запасное
                return [s for item in self._container(module, func.value, depth + 1)
                        for s in self.samples(module, item, depth + 1)] + self.samples(module, expr.args[1], depth + 1)
            return []
        if isinstance(expr, ast.Name) and expr.id not in self.consts(module):
            values = self._local(expr.id) or []
            return [s for value in values for s in self.samples(module, value, depth + 1)]
        if isinstance(expr, ast.Constant):
            return [expr.value] if isinstance(expr.value, str) else []
        if isinstance(expr, ast.JoinedStr):
            parts = []
            for part in expr.values:
                if isinstance(part, ast.Constant):
                    parts.append([part.value])
                else:
                    inner = [s for s in self.samples(module, part.value, depth + 1) if CYRILLIC.search(s)]
                    parts.append(inner or [SAMPLE])
            return [''.join(chosen) for chosen in itertools.islice(itertools.product(*parts), 32)]
        if isinstance(expr, ast.IfExp):
            return self.samples(module, expr.body, depth + 1) + self.samples(module, expr.orelse, depth + 1)
        if isinstance(expr, ast.BoolOp):
            return [s for value in expr.values for s in self.samples(module, value, depth + 1)]
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            left = self.samples(module, expr.left, depth + 1) or [SAMPLE]
            right = self.samples(module, expr.right, depth + 1) or [SAMPLE]
            return [a + b for a, b in itertools.islice(itertools.product(left, right), 32)]
        if isinstance(expr, ast.Name) and expr.id in self.consts(module):
            return self.samples(module, self.consts(module)[expr.id], depth + 1)
        if (isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name)
                and expr.attr in self.consts(expr.value.id)):
            return self.samples(expr.value.id, self.consts(expr.value.id)[expr.attr], depth + 1)
        return []

    def add(self, module: str, expr, line: int):
        if isinstance(expr, ast.Dict):
            for key, value in zip(expr.keys, expr.values):
                if isinstance(key, ast.Constant) and key.value in self.root.keys:
                    self.add(module, value, line)
            return
        if isinstance(expr, (ast.List, ast.Tuple)):
            for item in expr.elts:
                self.add(module, item, line)
            return
        for text in self.samples(module, expr):
            if CYRILLIC.search(text):
                self.found.setdefault(text, set()).add(f'{self.root.name}/{module}.py:{line}')

    def visit(self, module: str, node):
        root = self.root
        end = getattr(node, 'end_lineno', None)
        if end is not None and _lines(root.name, module)[end] == _lines(root.name, module)[node.lineno - 1]:
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.functions.append(node)
            if node.name in root.calls:
                for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                    self.add(module, default, node.lineno)
            if (module, node.name) in root.words:
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Return) and inner.value is not None:
                        self.add(module, inner.value, inner.lineno)
            for child in ast.iter_child_nodes(node):
                self.visit(module, child)
            self.functions.pop()
            return
        if isinstance(node, ast.ClassDef) and node.name in root.calls:
            for inner in node.body:
                if isinstance(inner, ast.FunctionDef) and inner.name == '__init__':
                    for default in inner.args.defaults + [d for d in inner.args.kw_defaults if d]:
                        self.add(module, default, inner.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                pairs = (zip(target.elts, node.value.elts) if isinstance(target, ast.Tuple)
                         and isinstance(node.value, ast.Tuple) else [(target, node.value)])
                for left, right in pairs:
                    if _name(left, root.names) in root.names:
                        self.add(module, right, node.lineno)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
            listed = (isinstance(func, ast.Attribute) and name in {'append', 'extend'}
                      and _name(func.value, root.names) in root.names)
            if name in root.calls or listed:
                for arg in [*node.args, *(k.value for k in node.keywords)]:
                    self.add(module, arg, node.lineno)
            for keyword in node.keywords:
                if keyword.arg in root.keys:
                    self.add(module, keyword.value, node.lineno)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in root.keys:
                    self.add(module, value, node.lineno)
        for child in ast.iter_child_nodes(node):
            self.visit(module, child)

    def collect(self, module: str) -> dict[str, set[str]]:
        for name, value in self.consts(module).items():
            if f'{module}.{name}' not in self.root.exempt_constants and isinstance(value, (ast.Constant, ast.JoinedStr)):
                self.add(module, value, value.lineno)
        self.visit(module, _tree(self.root.path, module))
        return self.found


@functools.cache
def collected(root: str, module: str) -> dict[str, set[str]]:
    return Collector(root).collect(module)


def every(root: str | None = None) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for name in [root] if root else ROOTS:
        for module in ROOTS[name].modules:
            for text, places in collected(name, module).items():
                found.setdefault(text, set()).update(places)
    return found


def untranslated(root: str | None = None, module: str | None = None) -> dict[str, set[str]]:
    exempt = ROOTS[root].exempt if root else EXEMPT | AGENT_EXEMPT
    return {text: places for text, places in (collected(root, module) if module else every(root)).items()
            if text not in exempt and CYRILLIC.search(i18n_words.message(text))}


@pytest.mark.parametrize('module', MODULES)
def test_every_server_word_shown_on_the_page_has_an_english_translation(module):
    missing = untranslated('app', module)
    assert not missing, '\n'.join(f'{sorted(places)[0]}: {text!r}' for text, places in sorted(missing.items()))


@pytest.mark.parametrize('module', ROOTS['agent'].modules)
def test_every_bridge_word_shown_on_the_page_has_an_english_translation(module):
    missing = untranslated('agent', module)
    assert not missing, '\n'.join(f'{sorted(places)[0]}: {text!r}' for text, places in sorted(missing.items()))


@pytest.mark.parametrize('root', ROOTS)
def test_exemptions_are_still_found_and_explained(root):
    config = ROOTS[root]
    assert all(config.exempt.values()) and all(config.exempt_constants.values())
    found = every(root)
    assert not set(config.exempt) - set(found), set(config.exempt) - set(found)
    collector = Collector(root)
    for name in config.exempt_constants:
        module, _, constant = name.partition('.')
        assert constant in collector.consts(module), name
    assert all(AGENT_LOCAL.values()) and all((AGENT / f'{module}.py').is_file() for module in AGENT_LOCAL)


def test_bridge_refusals_and_turn_failures_are_collected():
    """Сторож видит отказы моста по всем путям: литерал, константа, словарь кодов, `.get` с запасным, итог хода."""
    found = every('agent')
    for text in ('нет такого проекта', 'компьютер закрыт', 'Сеанс рабочего стола завершён',
                 'Неверные параметры', 'Codex не ответил — проверьте вход и журнал Codex на компьютере',
                 'Новая версия моста не запустилась — работает прежняя', 'Визуализация больше 10 МиБ'):
        assert text in found, text


@pytest.mark.integration
def test_page_runtime_translates_the_same_server_words():
    """Сторож и английские выгрузки переводят слова `i18n_words.message`; настоящий `i18n.js` должен дать то же."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    texts = sorted(text for text in every() if text not in EXEMPT | AGENT_EXEMPT)
    program = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const context = vm.createContext({ localStorage: { getItem: () => 'en' } });
for (const file of ['en.js', 'i18n.js']) vm.runInContext(fs.readFileSync(process.argv[1] + '/' + file, 'utf8'), context);
const texts = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(texts.map((text) => context.StudioI18n.message(text))));
"""
    result = subprocess.run([node, '-e', program, str(APP / 'static')], input=json.dumps(texts),
                            capture_output=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    assert [i18n_words.message(text) for text in texts] == json.loads(result.stdout)
