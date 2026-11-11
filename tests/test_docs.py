"""Документация сверяется с кодом: `модуль.символ`, пары «`модуль.КОНСТАНТА` (значение)», относительные ссылки.

Код читается разбором AST, без импорта: мост (`agent/studio_agent`) тянет Windows-зависимости, а импорт модулей
службы — её состояние. Значение константы с модулем сверяется с этим модулем. Голое имя бывает в нескольких
модулях с разными значениями (`TEXT_MAX`, `QUEUE_MAX`): его значение должно совпасть хотя бы с одним из них, а имя,
которого в коде нет (константа JS, поле протокола), не сверяется. Новый текст пишет значения с модулем.
Ссылка на столбец базы (`users.timezone`, `images.chain`) проходит, если такой столбец есть в `app/db.py`.
"""
import ast
import re
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
AGENT = ROOT / "agent" / "studio_agent"
ARCH = ROOT / "docs" / "architecture"
AGENT_DOCS = {ROOT / "agent" / "CLAUDE.md", ARCH / "agent-bridge.md"}
SYMBOL_DOCS = sorted({ROOT / "CLAUDE.md", *ARCH.glob("*.md"), *AGENT_DOCS})
LINK_DOCS = sorted({*SYMBOL_DOCS, ROOT / "AGENTS.md", *ROOT.glob("README*.md"),
                    ROOT / "docs" / "guide.ru.md", ROOT / "docs" / "screenshots" / "README.md", ROOT / "agent" / "README.md",
                    ROOT / "agent" / "AGENTS.md", ROOT / "docs" / "glossary.md"})

#: `имя.расширение` — файл, а не модуль с символом
EXT = {"js", "mjs", "css", "py", "pyw", "md", "json", "html", "ps1", "sh", "cmd", "txt", "zip", "exe", "db", "svg",
       "png", "toml", "yaml", "yml", "ini", "cs", "srt", "docx", "pdf", "ico", "webmanifest", "env", "service", "timer",
       "conf", "log", "bin", "sha256", "tsv", "sock", "local", "jpg", "mp3", "mp4", "wav", "gz", "lock", "cfg"}
REF = re.compile(r"`([A-Za-z_]\w*)\.([A-Za-z_]\w*)(?:\.([A-Za-z_]\w*))?(\([^`]*\))?`(\s*\(([^)]*)\))?")
FILE_REF = re.compile(r"`([\w./-]+\.(?:js|py))::([A-Za-z_$][\w$]*)`")
LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)\)")
VALUE = re.compile(r"^\s*(\d[\d\u00a0\u2009\u202f ]*)\s*([^\s\d;,:)]*)")
BARE = re.compile(r"`([A-Z][A-Z0-9_]+)`\s*\(([^)]*)\)")
UNITS = {"": 1, "с": 1, "мин": 60, "ч": 3600, "день": 86400, "дня": 86400, "дней": 86400, "сут": 86400,
         "МиБ": 1 << 20, "КиБ": 1 << 10, "байт": 1, "знаков": 1, "строк": 1}
FENCE = re.compile(r"^ {0,3}```.*?^ {0,3}```", re.M | re.S)


#: API браузера, а не модуль `app/history.py`
BROWSER = {("history", "replaceState"), ("history", "pushState"), ("history", "state")}


def _package(doc: Path) -> Path:
    return AGENT if doc in AGENT_DOCS else APP


@cache
def _modules(package: Path) -> frozenset:
    """Имена модулей с учётом регистра: файловая система Windows нашла бы `Config.py` как `config.py`."""
    return frozenset(p.stem for p in package.glob("*.py"))


@cache
def _module(package: Path, name: str) -> "tuple[dict, dict, dict] | None":
    """Имена модуля: верхний уровень (с объявленными global), поля и методы классов, узлы значений.
    Ключ кэша — имя строкой: пути Windows равны без учёта регистра."""
    if name not in _modules(package):
        return None
    tree = ast.parse((package / f"{name}.py").read_text(encoding="utf-8"))
    top, classes, values = {}, {}, {}

    def names(target):
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                yield from names(item)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            top[node.name] = node
        elif isinstance(node, ast.ClassDef):
            top[node.name] = node
            members = classes.setdefault(node.name, set())
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    members.add(item.name)
                elif isinstance(item, ast.Assign):
                    for target in item.targets:
                        members.update(names(target))
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    members.add(item.target.id)
            for sub in ast.walk(node):          # self.поле = … в методах
                if (isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Store)
                        and isinstance(sub.value, ast.Name) and sub.value.id == "self"):
                    members.add(sub.attr)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for name in names(target):
                    top[name] = node
                    if isinstance(target, ast.Name):
                        values[name] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            top[node.target.id] = node
            if node.value is not None:
                values[node.target.id] = node.value
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                top[(alias.asname or alias.name).split(".")[0]] = node
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            for name in node.names:
                top.setdefault(name, node)
    return top, classes, values


def _evaluate(package: Path, module: str, node, depth=0):
    """Значение простой константы: числа, строки, арифметика, имена того же или соседнего модуля."""
    if depth > 10:
        raise ValueError
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_evaluate(package, module, node.operand, depth + 1)
    if isinstance(node, ast.BinOp):
        left = _evaluate(package, module, node.left, depth + 1)
        right = _evaluate(package, module, node.right, depth + 1)
        ops = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
               ast.FloorDiv: lambda a, b: a // b, ast.Div: lambda a, b: a / b, ast.Pow: lambda a, b: a ** b,
               ast.LShift: lambda a, b: a << b}
        if type(node.op) in ops:
            return ops[type(node.op)](left, right)
    if isinstance(node, ast.Name):
        return _constant(package, module, node.id, depth + 1)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return _constant(package, node.value.id, node.attr, depth + 1)
    raise ValueError


def _constant(package: Path, module: str, name: str, depth=0):
    info = _module(package, module)
    if info is None or name not in info[2]:
        raise ValueError
    return _evaluate(package, module, info[2][name], depth)


@cache
def _columns() -> dict:
    """Таблица → столбцы по SQL в app/db.py (CREATE TABLE и ALTER TABLE … ADD COLUMN)."""
    text = (APP / "db.py").read_text(encoding="utf-8")
    tables: dict[str, set] = {}
    for name, body in re.findall(r"CREATE TABLE (\w+)\s*\((.*?)\);", text, re.S):
        cols = re.findall(r"(?:^|,|\()\s*(\w+)\s+(?:TEXT|INTEGER|REAL|BLOB)", body, re.M)
        tables.setdefault(re.sub(r"_\d+$", "", name), set()).update(cols)
    for name, col in re.findall(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", text):
        tables.setdefault(name, set()).add(col)
    return tables


def _written(value: str) -> "int | None":
    """Число из скобок после имени, в единицах кода (секунды, байты); незнакомая единица — None."""
    number = VALUE.match(value)
    if number is None or number.group(2) not in UNITS:
        return None
    return int(re.sub(r"\D", "", number.group(1))) * UNITS[number.group(2)]


@cache
def _values_anywhere(package: Path, name: str) -> frozenset:
    """Числовые значения константы `name` во всех модулях пакета."""
    found = set()
    for module in _modules(package):
        info = _module(package, module)
        if info is not None and name in info[2]:
            try:
                value = _constant(package, module, name)
            except (ValueError, TypeError, ZeroDivisionError):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                found.add(value)
    return frozenset(found)


def _text(doc: Path) -> str:
    return FENCE.sub("", doc.read_text(encoding="utf-8"))


def _symbol_problems(doc: Path) -> list[str]:
    package = _package(doc)
    problems = []
    text = _text(doc)
    for match in REF.finditer(text):
        module, name, attr, _args, _paren, value = match.groups()
        if name in EXT or (attr is None and name.lower() in EXT) or (module, name) in BROWSER:
            continue
        ref = f"{module}.{name}" + (f".{attr}" if attr else "")
        info = _module(package, module)
        if info is None:
            columns = _columns().get(module) if package == APP else None
            if columns is not None and attr is None and name not in columns:
                problems.append(f"{ref}: в таблице {module} нет столбца {name}")
            continue
        top, classes, _values = info
        known = name in top or any(name in members for members in classes.values())
        if not known and package == APP and name in _columns().get(module, set()):
            continue
        if not known:
            problems.append(f"{ref}: в {module}.py нет {name}")
            continue
        if attr and name in classes and attr not in classes[name]:
            problems.append(f"{ref}: в классе {module}.{name} нет {attr}")
            continue
        if value is not None and attr is None and name in top:
            written = _written(value)
            try:
                actual = _constant(package, module, name)
            except (ValueError, TypeError, ZeroDivisionError):
                continue
            if written is not None and isinstance(actual, (int, float)) and actual != written:
                problems.append(f"{ref} ({value}): в коде {actual}")
    for match in BARE.finditer(text):
        name, value = match.groups()
        written = _written(value)
        if written is None:
            continue
        actual = _values_anywhere(package, name)
        if actual and written not in actual:
            problems.append(f"{name} ({value}): в коде {sorted(actual)}")
    for match in FILE_REF.finditer(text):
        path, symbol = match.groups()
        candidates = [ROOT / path, APP / "static" / "js" / path, APP / path, package / path, ROOT / "agent" / path]
        found = next((c for c in candidates if c.is_file()), None)
        if found is not None and not re.search(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])",
                                               found.read_text(encoding="utf-8")):
            problems.append(f"{path}::{symbol}: в файле нет {symbol}")
    return problems


def _slug(heading: str) -> str:
    """Якорь заголовка, как его строит GitHub."""
    text = re.sub(r"[`*_]|\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) or "", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


@cache
def _anchors(doc: Path) -> set:
    return {_slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.+?)\s*#*$", _text(doc), re.M)}


def _link_problems(doc: Path) -> list[str]:
    problems = []
    for match in LINK.finditer(_text(doc)):
        target = match.group(1)
        if re.match(r"^[a-z]+:", target):
            continue
        path, _, anchor = target.partition("#")
        dest = (doc.parent / path).resolve() if path else doc
        if not dest.exists():
            problems.append(f"{target}: нет файла")
        elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
            problems.append(f"{target}: нет заголовка #{anchor}")
    return problems


@pytest.mark.parametrize("doc", SYMBOL_DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_code_references_in_docs_exist(doc):
    assert _symbol_problems(doc) == []


@pytest.mark.parametrize("doc", LINK_DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_in_docs_resolve(doc):
    assert _link_problems(doc) == []


def test_the_checker_catches_a_wrong_constant_and_a_missing_symbol(tmp_path, monkeypatch):
    doc = tmp_path / "doc.md"
    doc.write_text("`chat.RETRY_SEC` (16) с, `chat.NO_SUCH_THING`, `users.no_such_column`, `chat.WAIT_MAX_SEC` (30 мин),"
                   " `FAILS_MAX` (51), `NOT_IN_CODE_MAX` (7), [x](missing.md), [y](doc.md#нет)\n\n# Есть\n",
                   encoding="utf-8")
    problems = _symbol_problems(doc)
    assert any("RETRY_SEC" in p for p in problems)
    assert any(p.startswith("FAILS_MAX") for p in problems)      # chat_shares.FAILS_MAX = 50
    assert not any("NOT_IN_CODE_MAX" in p for p in problems)
    assert any("NO_SUCH_THING" in p for p in problems)
    assert any("no_such_column" in p for p in problems)
    assert not any("WAIT_MAX_SEC" in p for p in problems)        # 30 мин = 1800 с
    assert [p.split(":")[0] for p in _link_problems(doc)] == ["missing.md", "doc.md#нет"]
