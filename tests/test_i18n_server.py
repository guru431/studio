"""Сторож перевода слов сервера: каждый русский текст `app/*.py`, который уходит на страницу (отказ, слова
задания, итог проверки), переводится `StudioI18n.message` — строкой `app/static/en.js` или шаблоном
`StudioEnglishMessages`. Тексты собирает AST: аргументы вызовов-отказов (`SINK_CALLS`), значения ключей
ответа (`SINK_KEYS`), возвраты функций-слов (`WORD_FUNCTIONS`) и константы модуля, на которые они ссылаются.
Вставка f-строки проверяется значением `7`: шаблон должен перевести всё, кроме подстановки."""
import ast
import functools
import itertools
import json
import re
import shutil
import subprocess

import pytest

from conftest import APP

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
    'checks.PROBE_TEXT': 'проба модели',
    'web.NOTHING': 'результат инструмента для модели',
    'db.YO': 'SQL',
    'manage.USAGE': 'командная строка администратора',
    'takeout.README': 'файл внутри выгрузки',
    'upstream.KEY_PLACE': 'часть отказа gateway_error, сверяется целым отказом',
    'chat.REFUSED': 'пометка в контексте модели',
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
}

#: тексты, которые нашёл сторож, но переводить не нужно: текст → причина
EXEMPT = {
    'привязка': 'имя действия для журнала (`agents._refuse`, параметр what)',
    'отвязка': 'имя действия для журнала (`agents._refuse`, параметр what)',
    'убран': 'внутренняя причина UpstreamRefused: пользователь видит refused_words',
}

SAMPLE = '7'


MODULES = sorted(path.stem for path in APP.glob('*.py'))
#: строка «интересна», если в ней кириллица, имя-константа (ВЕРХНИЙ_РЕГИСТР) или имя отказа, поля слов,
#: функции-слов: операторы без таких строк обход пропускает — разбор всех модулей и так занимает почти секунду
INTERESTING = re.compile(r'[А-Яа-яЁё]|\b[A-Z][A-Z0-9_]{2,}\b|\b(?:%s)\b' % '|'.join(
    sorted(SINK_CALLS | SINK_NAMES | {name for _, name in WORD_FUNCTIONS})))


@functools.cache
def _tree(module: str) -> ast.Module | None:
    path = APP / f'{module}.py'
    return ast.parse(path.read_text(encoding='utf-8')) if path.is_file() else None


@functools.cache
def _lines(module: str) -> list[int]:
    """Префиксные суммы интересных строк модуля: диапазон оператора проверяется за O(1)."""
    prefix = [0]
    for line in (APP / f'{module}.py').read_text(encoding='utf-8').splitlines():
        prefix.append(prefix[-1] + bool(INTERESTING.search(line)))
    return prefix


@functools.cache
def _constants(module: str) -> dict[str, ast.expr]:
    found = {}
    tree = _tree(module)
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


def _name(node) -> str | None:
    """Имя цели присваивания или вызова: `x`, `obj.x`, `obj["x"]` (и `obj["x" if … else "y"]` — оба)."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        keys = (node.slice.body, node.slice.orelse) if isinstance(node.slice, ast.IfExp) else (node.slice,)
        for key in keys:
            if isinstance(key, ast.Constant) and key.value in SINK_NAMES:
                return key.value
    return None


class Collector:
    def __init__(self):
        self.found: dict[str, set[str]] = {}
        self.functions: list = []
        self.locals: dict[int, dict[str, list[ast.expr]]] = {}

    @staticmethod
    def consts(module: str) -> dict[str, ast.expr]:
        return _constants(module) if module in MODULES else {}

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
        if (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
                and expr.func.attr in {'replace', 'strip', 'rstrip', 'capitalize'}):
            return self.samples(module, expr.func.value, depth + 1)
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
                if isinstance(key, ast.Constant) and key.value in SINK_KEYS:
                    self.add(module, value, line)
            return
        if isinstance(expr, (ast.List, ast.Tuple)):
            for item in expr.elts:
                self.add(module, item, line)
            return
        for text in self.samples(module, expr):
            if CYRILLIC.search(text):
                self.found.setdefault(text, set()).add(f'{module}.py:{line}')

    def visit(self, module: str, node):
        end = getattr(node, 'end_lineno', None)
        if end is not None and _lines(module)[end] == _lines(module)[node.lineno - 1]:
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.functions.append(node)
            if node.name in SINK_CALLS:
                for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                    self.add(module, default, node.lineno)
            if (module, node.name) in WORD_FUNCTIONS:
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Return) and inner.value is not None:
                        self.add(module, inner.value, inner.lineno)
            for child in ast.iter_child_nodes(node):
                self.visit(module, child)
            self.functions.pop()
            return
        if isinstance(node, ast.ClassDef) and node.name in SINK_CALLS:
            for inner in node.body:
                if isinstance(inner, ast.FunctionDef) and inner.name == '__init__':
                    for default in inner.args.defaults + [d for d in inner.args.kw_defaults if d]:
                        self.add(module, default, inner.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                pairs = (zip(target.elts, node.value.elts) if isinstance(target, ast.Tuple)
                         and isinstance(node.value, ast.Tuple) else [(target, node.value)])
                for left, right in pairs:
                    if _name(left) in SINK_NAMES:
                        self.add(module, right, node.lineno)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
            listed = isinstance(func, ast.Attribute) and name in {'append', 'extend'} and _name(func.value) in SINK_NAMES
            if name in SINK_CALLS or listed:
                for arg in [*node.args, *(k.value for k in node.keywords)]:
                    self.add(module, arg, node.lineno)
            for keyword in node.keywords:
                if keyword.arg in SINK_KEYS:
                    self.add(module, keyword.value, node.lineno)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in SINK_KEYS:
                    self.add(module, value, node.lineno)
        for child in ast.iter_child_nodes(node):
            self.visit(module, child)

    def collect(self, module: str) -> dict[str, set[str]]:
        for name, value in self.consts(module).items():
            if f'{module}.{name}' not in EXEMPT_CONSTANTS and isinstance(value, (ast.Constant, ast.JoinedStr)):
                self.add(module, value, value.lineno)
        self.visit(module, _tree(module))
        return self.found


def _catalog():
    source = (APP / 'static' / 'en.js').read_text(encoding='utf-8')
    english, patterns, verbatim = (json.JSONDecoder().raw_decode(part)[0]
                                   for part in source.split('Object.freeze(')[1:4])
    return english, patterns, set(verbatim)


@functools.cache
def translator():
    """`StudioI18n.message` (`app/static/i18n.js`) на Python: целиком, с малой буквы или шаблоном; голая
    подстановка шаблона — тоже слова сервера, подстановка в «» и шаблоны `StudioEnglishVerbatim` — как есть."""
    english, keys, verbatim = _catalog()
    compiled = []
    for key in keys:
        slots, auto, parts = [], 0, []
        pieces = re.split(r'(\{\d*\})', key)
        for at, part in enumerate(pieces):
            if re.fullmatch(r'\{\d*\}', part):
                named = key in verbatim or (pieces[at - 1].endswith(('«', '“')) and pieces[at + 1].startswith(('»', '”')))
                slots.append((auto if part == '{}' else int(part[1:-1]), named))
                auto += part == '{}'
                parts.append(r'([\s\S]*?)')
            else:
                parts.append(re.escape(part))
        compiled.append((re.compile('^' + ''.join(parts) + '$', re.I), key, slots))

    def message(source: str, depth: int = 0) -> str:
        if source in english:
            return english[source]
        lower = source[:1].lower() + source[1:]
        if lower in english:
            out = english[lower]
            return out if source == lower else out[:1].upper() + out[1:]
        if len(source) > 4096 or depth > 3:
            return source
        for pattern, key, slots in compiled:
            match = pattern.match(source)
            if match:
                values = {}
                for n, (index, named) in enumerate(slots):
                    part = match.group(n + 1)
                    quoted = not named and re.fullmatch('«([^«»]*)»', part)
                    values[index] = (part if named else f'“{message(quoted[1], depth + 1)}”' if quoted
                                     else message(part, depth + 1))
                counter = itertools.count()
                text = re.sub(r'\{\}', lambda _: '{%d}' % next(counter), english[key])
                return re.sub(r'\{(\d+)\}', lambda m: values.get(int(m.group(1)), m.group(0)), text)
        return source
    return message


@functools.cache
def collected(module: str) -> dict[str, set[str]]:
    return Collector().collect(module)


def every() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for module in MODULES:
        for text, places in collected(module).items():
            found.setdefault(text, set()).update(places)
    return found


def untranslated(module: str | None = None) -> dict[str, set[str]]:
    message = translator()
    return {text: places for text, places in (collected(module) if module else every()).items()
            if text not in EXEMPT and CYRILLIC.search(message(text))}


@pytest.mark.parametrize('module', MODULES)
def test_every_server_word_shown_on_the_page_has_an_english_translation(module):
    missing = untranslated(module)
    assert not missing, '\n'.join(f'{sorted(places)[0]}: {text!r}' for text, places in sorted(missing.items()))


def test_exemptions_are_still_found_and_explained():
    assert all(EXEMPT.values()) and all(EXEMPT_CONSTANTS.values())
    assert not set(EXEMPT) - set(every()), set(EXEMPT) - set(every())
    for name in EXEMPT_CONSTANTS:
        module, _, constant = name.partition('.')
        assert constant in Collector.consts(module), name


def test_page_runtime_translates_the_same_server_words():
    """Сторож переводит слова Python-копией `message()`; настоящий `i18n.js` должен дать то же."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    texts = sorted(text for text in every() if text not in EXEMPT)
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
    message = translator()
    assert [message(text) for text in texts] == json.loads(result.stdout)
