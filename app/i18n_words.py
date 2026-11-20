"""Слова сервера по-английски так же, как их переводит страница: `message` — `StudioI18n.message`
(`static/i18n.js`) на Python по словарю `static/en.js`. Нужен английским выгрузкам (`exports.server_word`): причина
нераспознанной страницы, ошибка ответа и шага чата приходят из базы русскими, страница переводит их на лету.
`en.js` разбирается один раз при первом обращении; сверку с настоящим `i18n.js` в node держит
tests/test_i18n_server.py."""
import functools
import itertools
import json
import re
from pathlib import Path

EN_JS = Path(__file__).resolve().parent / "static" / "en.js"


@functools.cache
def _catalog() -> tuple[dict, list]:
    """`StudioEnglish` (текст → перевод) и шаблоны `StudioEnglishMessages` регулярными выражениями: голая
    подстановка — тоже слова сервера, подстановка в «» и шаблоны `StudioEnglishVerbatim` — как есть."""
    source = EN_JS.read_text(encoding="utf-8")
    english, keys, verbatim = (json.JSONDecoder().raw_decode(part)[0]
                               for part in source.split("Object.freeze(")[1:4])
    verbatim = set(verbatim)
    compiled = []
    for key in keys:
        slots, auto, parts = [], 0, []
        pieces = re.split(r"(\{\d*\})", key)
        for at, part in enumerate(pieces):
            if re.fullmatch(r"\{\d*\}", part):
                named = key in verbatim or (pieces[at - 1].endswith(("«", "“")) and pieces[at + 1].startswith(("»", "”")))
                slots.append((auto if part == "{}" else int(part[1:-1]), named))
                auto += part == "{}"
                parts.append(r"([\s\S]*?)")
            else:
                parts.append(re.escape(part))
        compiled.append((re.compile("^" + "".join(parts) + "$", re.I), key, slots))
    return english, compiled


def message(source: str, depth: int = 0) -> str:
    """Текст целиком, с малой буквы или шаблоном — по-английски; незнакомый — как есть."""
    english, compiled = _catalog()
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
                quoted = not named and re.fullmatch("«([^«»]*)»", part)
                values[index] = (part if named else f"“{message(quoted[1], depth + 1)}”" if quoted
                                 else message(part, depth + 1))
            counter = itertools.count()
            text = re.sub(r"\{\}", lambda _: "{%d}" % next(counter), english[key])
            out = re.sub(r"\{(\d+)\}", lambda m: values.get(int(m.group(1)), m.group(0)), text)
            return out[:1].upper() + out[1:] if source[:1].isupper() else out
    return source
