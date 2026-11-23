"""Документация сверяется с кодом: `модуль.символ`, пары «`модуль.КОНСТАНТА` (значение)», относительные ссылки.

Код читается разбором AST, без импорта: мост (`agent/studio_agent`) тянет Windows-зависимости, а импорт модулей
службы — её состояние. Значение константы с модулем сверяется с этим модулем. Голое имя бывает в нескольких
модулях с разными значениями (`TEXT_MAX`, `QUEUE_MAX`): его значение должно совпасть хотя бы с одним из них, а имя,
которого в коде нет (константа JS, поле протокола), не сверяется. Новый текст пишет значения с модулем.
Ссылка на столбец базы (`users.timezone`, `images.chain`) проходит, если такой столбец есть в `app/db.py`.
"""
import ast
import re
import unicodedata
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
AGENT = ROOT / "agent" / "studio_agent"
ARCH = ROOT / "docs" / "architecture"
CLAUDE = ROOT / "CLAUDE.md"
AGENT_DOCS = {ROOT / "agent" / "CLAUDE.md", ARCH / "agent-bridge.md"}
SYMBOL_DOCS = sorted({*ARCH.glob("*.md"), *AGENT_DOCS})
LINK_DOCS = sorted({CLAUDE, *SYMBOL_DOCS, ROOT / "AGENTS.md", *ROOT.glob("README*.md"),
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
def _source(package: Path, name: str) -> tuple[str, str]:
    """Исходник и форма имён Python: NFKC сохраняет поиск Unicode-идентификаторов до разбора AST."""
    text = (package / f"{name}.py").read_text(encoding="utf-8")
    return text, unicodedata.normalize("NFKC", text)


@cache
def _module(package: Path, name: str) -> "tuple[dict, dict, dict] | None":
    """Имена модуля: верхний уровень (с объявленными global), поля и методы классов, узлы значений.
    Ключ кэша — имя строкой: пути Windows равны без учёта регистра."""
    if name not in _modules(package):
        return None
    source = _source(package, name)[0]
    tree = ast.parse(source)
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
    if "global" in source:
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
        if name not in _source(package, module)[1]:
            continue                    # нет имени — не нужен полный разбор классов/методов модуля
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


def _symbol_problems(doc: Path, text: str | None = None) -> list[str]:
    package = _package(doc)
    problems = []
    if text is None:
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


@pytest.mark.xdist_group("docs")  # общий AST-кэш, без повторного разбора пакета в каждом worker
@pytest.mark.parametrize("text", re.split(r"(?=^## )", _text(CLAUDE), flags=re.M),
                         ids=lambda text: text.splitlines()[0].lstrip("# "))
def test_code_references_in_claude_section_exist(text):
    assert _symbol_problems(CLAUDE, text) == []


@pytest.mark.xdist_group("docs")
@pytest.mark.parametrize("doc", SYMBOL_DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_code_references_in_docs_exist(doc):
    assert _symbol_problems(doc) == []


@pytest.mark.parametrize("doc", LINK_DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_in_docs_resolve(doc):
    assert _link_problems(doc) == []


@pytest.mark.xdist_group("docs")  # тот же AST-кэш, что у проверок ссылок на символы
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


#: Адреса в коде моста, к которым он сам не ходит: студия по умолчанию и ссылки для браузера (`subscription.LINKS`)
LINK_ONLY = {"studio.example.com", "claude.ai", "chatgpt.com"}


def _bridge_hosts() -> set:
    tree = ast.parse((AGENT / "limits.py").read_text(encoding="utf-8"))
    node = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "HOSTS" for t in n.targets))
    return set(ast.literal_eval(node).values())


def test_bridge_hosts_are_listed_in_both_claude_md_and_cover_the_code():
    """Внешние адреса моста — `limits.HOSTS`: перечни инварианта называют каждый, литерала мимо константы нет."""
    hosts = _bridge_hosts()
    for doc in (ROOT / "CLAUDE.md", ROOT / "agent" / "CLAUDE.md"):
        text = _text(doc)
        assert [host for host in sorted(hosts)
                if not re.search(rf"(?<![\w.-]){re.escape(host)}(?![\w-])", text)] == [], doc.name
    literal = set()  # имена с доменом верхнего уровня: `http://127\.0\.0\.1` (loopback OpenCode) и `https://h` — не адреса
    for path in AGENT.glob("*.py"):
        literal.update(re.findall(r"\bhttps?://((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})", path.read_text(encoding="utf-8")))
    assert literal - hosts - LINK_ONLY == set()


#: Поля `hello` базы v1 — до таблицы «capability → версия»
HELLO_BASE = {"t", "v", "cc_version", "projects_root", "has_pin", "models", "efforts"}


def _hello_keys() -> set:
    """Ключи `Agent.hello`: словарь-литерал и присваивания `hello["…"]` (AST, без импорта)."""
    tree = ast.parse((AGENT / "agent.py").read_text(encoding="utf-8"))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "hello")
    keys = set()
    for node in ast.walk(method):
        if isinstance(node, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "t" for k in node.keys):
            keys.update(k.value for k in node.keys)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) \
                and isinstance(node.slice, ast.Constant):
            keys.add(node.slice.value)
    return keys


def test_capability_table_names_every_hello_field():
    """Каждое необязательное поле `hello` моста — в таблице «Capability и версии моста» `agent-bridge.md`."""
    text = _text(ARCH / "agent-bridge.md")
    start = text.index("\n## Capability и версии моста")
    table = "\n".join(line for line in text[start:text.index("\n## ", start + 1)].splitlines()
                      if line.startswith("|"))
    listed = set(re.findall(r"`(?:hello\.)?([a-z_]+)", table))
    assert sorted(_hello_keys() - HELLO_BASE - listed) == []


def _claude_section(title: str) -> str:
    text = _text(ROOT / "CLAUDE.md")
    start = text.index(f"\n## {title}")
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else None]


def test_rollback_table_covers_every_schema_step_from_16():
    """Каждый шаг `db.STEPS` от 16 — строка «Откат по шагам схемы» (первый столбец — номера через запятую)."""
    steps = {key.value for key in _module(APP, "db")[2]["STEPS"].keys if key.value >= 16}
    rows = re.findall(r"^\|\s*(\d+(?:\s*,\s*\d+)*)\s*\|", _claude_section("Откат по шагам схемы"), re.M)
    listed = {int(n) for row in rows for n in row.split(",")}
    assert sorted(steps - listed) == [] and sorted(listed - steps) == []


def test_module_map_lists_every_app_module():
    listed = set(re.findall(r"`(\w+)`", _claude_section("Карта модулей")))
    assert sorted(_modules(APP) - listed) == []


GUIDE = ROOT / "docs" / "guide.ru.md"
CLIENT = ROOT / "client"
#: имя скрипта или значение `-Model`/`--model` после него: значение относится к последнему названному скрипту
SCRIPT_MODEL = re.compile(r"(studio-(?:batch|video))|(?:-Model|--model)\s+([\w|.-]+)")


def _batch_models() -> tuple[set, set]:
    """Модели картинок скриптов: ValidateSet `$Model` версии PowerShell и ключи `MODELS` (choices) версии Python."""
    ps1 = (CLIENT / "studio-batch.ps1").read_text(encoding="utf-8-sig")
    validate = re.search(r"\[ValidateSet\(([^)]*)\)\]\[string\]\$Model\b", ps1).group(1)
    tree = ast.parse((CLIENT / "studio-batch.py").read_text(encoding="utf-8"))
    node = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "MODELS" for t in n.targets))
    return set(re.findall(r"'([^']+)'", validate)), set(ast.literal_eval(node))


def test_guide_batch_script_models_are_accepted_by_both_scripts():
    """Значения `-Model`/`--model` у studio-batch в руководстве — из ValidateSet и choices скриптов."""
    ps1, py = _batch_models()
    written, script = set(), None
    for match in SCRIPT_MODEL.finditer(GUIDE.read_text(encoding="utf-8")):
        if match.group(1):
            script = match.group(1)
        elif script == "studio-batch":
            written.update(filter(None, match.group(2).split("|")))
    assert written, "руководство не показывает -Model/--model скрипта картинок"
    assert sorted(written - ps1) == [] and sorted(written - py) == []


def _model_labels() -> dict:
    """Подписи моделей страницы — `LABELS` из models.js (одна таблица на настройки, разделы и «Поиск»)."""
    body = re.search(r"const LABELS = \{(.*?)\};", (APP / "static" / "js" / "models.js").read_text(encoding="utf-8"),
                     re.S).group(1)
    return {key or bare: label for key, bare, label in re.findall(r"(?:'([^']+)'|([\w-]+))\s*:\s*'([^']+)'", body)}


def test_server_model_names_copy_the_page_labels():
    """«В работе» (`jobs.MODEL_NAMES`) называет картинки так же, как разделы и «Поиск», — каждую модель фото шлюза
    (`sections.CATALOG` и снятые `sections.RETIRED_PHOTO_MODELS`)."""
    names = ast.literal_eval(_module(APP, "jobs")[2]["MODEL_NAMES"])
    labels = _model_labels()
    tree = _module(APP, "sections")[2]
    photo = set(ast.literal_eval(tree["CATALOG"])["photo"]) | ast.literal_eval(tree["RETIRED_PHOTO_MODELS"])
    assert set(names) == photo
    assert {model: labels.get(model) for model in names} == names


def test_guide_names_every_srvllm_catalog_model():
    """Каждая модель встроенного перечня srvllm (`sections.CATALOG`) названа в руководстве — подписью настроек или id."""
    catalog = ast.literal_eval(_module(APP, "sections")[2]["CATALOG"])
    labels, text = _model_labels(), GUIDE.read_text(encoding="utf-8")
    missing = sorted({model for models in catalog.values() for model in models
                      if labels.get(model, model) not in text and f"`{model}`" not in text})
    assert missing == []


def _account_settings() -> dict:
    """Группа «Учётка» настроек: `ACCOUNT_IDS` и разделы администратора (`smtp`) → подпись из `names` settingsView."""
    text = (APP / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    ids = re.findall(r"'(\w+)'", re.search(r"const ACCOUNT_IDS = \[([^\]]*)\]", text).group(1))
    ids += re.findall(r"is_admin \? \['(\w+)'\]", re.search(r"const accountIds = (.*)", text).group(1))
    names = re.search(r"const names = \{(.*?)\};", text, re.S).group(1)
    return {key: re.search(rf"\b{key}:[^,]*?[\"']([^\"']+)[\"']", names).group(1) for key in ids}


def test_guide_shows_where_every_account_settings_section_is():
    """Каждый раздел группы «Учётка» (с SMTP администратора) руководство называет путём «Настройки» → «…»."""
    sections, text = _account_settings(), GUIDE.read_text(encoding="utf-8")
    assert {"security", "smtp"} <= sections.keys()
    assert sorted(name for name in sections.values() if f"«Настройки» → «{name}»" not in text) == []


def test_constant_lookup_preserves_python_identifier_normalization(tmp_path):
    (tmp_path / "sample.py").write_text("ＦＡＩＬＳ_MAX = 3\n", encoding="utf-8")
    assert _values_anywhere(tmp_path, "FAILS_MAX") == {3}


def test_guide_and_architecture_do_not_use_retired_section_names():
    """Прежние имена разделов из глоссария («Речь» (стало «Озвучка») …) не пишутся ни в руководстве, ни в
    docs/architecture: ни в кавычках, ни заголовком главы. «Распознавание «…»» — имя действия, не раздела."""
    glossary = (ROOT / "docs" / "glossary.md").read_text(encoding="utf-8")
    retired = re.findall(r"«([^«»]+)»(?: как имя раздела)? \(стало", glossary)
    assert {"Речь", "Разговоры", "Распознавание"} <= set(retired)
    found = []
    for path in [GUIDE, *sorted((ROOT / "docs" / "architecture").glob("*.md"))]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in retired:
                if re.search(rf"«{name}»|^#+ {name}\s*$", line):
                    found.append(f"{path.name}:{number}: {name}")
    assert found == []
