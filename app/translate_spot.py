"""Текст снимка с координатами (спека 2026-10-28-studio-translator-design, 6.5, 6.6): разбор ответа HunyuanOCR
и сборка строк в блоки. Без состояния, без базы и без журнала — запрос к шлюзу делает `ocr.recognize` с PROMPT.

1. Ответ на PROMPT — подряд `текст(x1,y1),(x2,y2)`: левый верхний и правый нижний углы строки в долях снимка
   0…1000 по каждой оси (живая проба 2026-10-28). Текст сам может содержать скобки, поэтому строка — всё до
   ближайшей пары точек (`parse`). Наклонная строка может прийти четырьмя точками — рамка тогда объединяет их.
   Координаты зажаты в 0…1000 и упорядочены; пустой текст и хвост без координат (обрезанный ответ, мусор) —
   мимо, а не падение: строк нет — «текст на снимке не найден» решает вызывающий.
2. Переводится блок, а не строка (`group`): фраза, разорванная переносом, не переводится по половинкам. Строка
   присоединяется к блоку, чья последняя строка — прямо над ней (зазор не больше 0,8 её высоты), с пересечением
   по x и похожей высотой (не больше чем в 1,6 раза): так колонки и заголовки остаются своими блоками. Блок
   ищется с конца — у двух колонок строки идут вперемешку. Текст блока — строки через пробел, перенос слова
   («слово-» и строчная буква следом) — без пробела и дефиса; рамка — объединение рамок строк.
"""
import re
from dataclasses import dataclass

PROMPT = "Detect and recognize text in the image, and output the text coordinates in a formatted manner."
SCALE = 1000
GAP = 0.8                        # зазор до строки выше — доля высоты строки
HEIGHTS = 1.6                    # высоты строк одного блока отличаются не больше чем во столько раз

_POINT = r"\((\d+),(\d+)\)"
_LINE = re.compile(rf"(.*?){_POINT},{_POINT}((?:,\(\d+,\d+\))*)", re.S)
_NUMBERS = re.compile(r"\d+")


@dataclass(frozen=True)
class Line:
    text: str
    box: tuple[int, int, int, int]


def _clamp(v: str) -> int:
    return min(SCALE, max(0, int(v)))


def parse(content: str) -> list[Line]:
    """Строки ответа по порядку; без координат — мимо."""
    lines = []
    for m in _LINE.finditer(content):
        text = m.group(1).strip()
        if not text:
            continue
        values = [_clamp(v) for v in m.groups()[1:5]] + [_clamp(v) for v in _NUMBERS.findall(m.group(6))]
        xs, ys = values[0::2], values[1::2]
        lines.append(Line(text, (min(xs), min(ys), max(xs), max(ys))))
    return lines


def _fits(last: Line, line: Line) -> bool:
    """`line` — следующая строка того же блока, что `last`."""
    lx1, ly1, lx2, ly2 = last.box
    x1, y1, x2, y2 = line.box
    h, lh = y2 - y1, ly2 - ly1
    return (ly1 < y1 and y1 - ly2 <= GAP * h and x1 < lx2 and lx1 < x2
            and max(h, lh) <= HEIGHTS * min(h, lh))


def _join(text: str, more: str) -> str:
    if text.endswith("-") and more[:1].islower():
        return text[:-1] + more
    return f"{text} {more}"


def group(lines: list[Line]) -> list[dict]:
    """Блоки `{"source", "box": [x1, y1, x2, y2]}` в порядке первых строк."""
    blocks: list[dict] = []
    for line in lines:
        block = next((b for b in reversed(blocks) if _fits(b["last"], line)), None)
        if block is None:
            blocks.append({"source": line.text, "box": list(line.box), "last": line})
            continue
        box = block["box"]
        block["source"] = _join(block["source"], line.text)
        block["box"] = [min(box[0], line.box[0]), min(box[1], line.box[1]),
                        max(box[2], line.box[2]), max(box[3], line.box[3])]
        block["last"] = line
    return [{"source": b["source"], "box": b["box"]} for b in blocks]
