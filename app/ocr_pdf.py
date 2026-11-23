"""«PDF со слоем» скана (05:I9): страница выглядит как исходник, поверх — невидимый текст на местах строк.

1. Страница PDF-исходника — сама страница исходника (pypdf: содержимое и размер как есть, поворот переведён в
   содержимое), страница фото — JPEG (Pillow) на странице с длинной стороной PHOTO_SIDE pt. Текст — режимом 3
   (невидимый): поиск, выделение и копирование работают, вид страницы не меняется.
2. Строка — `(текст, (x1, y1, x2, y2))` в долях 0…SCALE видимой страницы, как у `translate_spot.parse`: высота
   шрифта — высота рамки, ширина подогнана горизонтальным масштабом (Tz) к ширине рамки. Текст без рамок
   (правленая страница, s06, ответ без координат) — `flow`: строки сверху вниз по левому краю.
3. Шрифт — Type3 без начертаний: невидимому тексту форма знаков не нужна, поэтому не нужен и встроенный шрифт с
   кириллицей. Знак — код 0…255 своего шрифта, Unicode — таблицей ToUnicode (любая письменность, вне BMP —
   парой UTF-16): по ней поиск и копирование у Acrobat, pdf.js и PDFium (Chrome; сверяют тесты). Шрифтов — по
   256 разных знаков документа на каждый.
4. Страница текстового слоя (`layer`) и страница без текста — страница исходника как есть.
Всё синхронно (через asyncio.to_thread); байты и текст в журнал не идут.
"""
import io
from dataclasses import dataclass, field

from PIL import Image, ImageOps
from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject,
                           NumberObject)

SCALE = 1000                     # доли страницы по каждой оси — как translate_spot.SCALE
PHOTO_SIDE = 842                 # длинная сторона страницы фото, pt (A4)
JPEG_QUALITY = 80
GLYPH = 500                      # ширина знака шрифта, 1/1000 кегля
BASELINE = 0.2                   # базовая линия — над низом рамки на долю её высоты
FLOW_TOP, FLOW_STEP, FLOW_CHAR = 50, 25, 9      # `flow`: верх, шаг строк и ширина знака в долях SCALE
CODES = 256


@dataclass
class Page:
    """`index` — страница исходника PDF (с 0) или `image` — фото страницы (PNG/JPEG); `lines` — строки слоя."""
    index: "int | None" = None
    image: "bytes | None" = None
    lines: list = field(default_factory=list)


def flow(text: str) -> list:
    """Строки текста без рамок — полосами сверху вниз по левому краю; не уместились по высоте — полосы тоньше."""
    rows = [" ".join(line.split()) for line in text.splitlines()]
    rows = [r for r in rows if r]
    if not rows:
        return []
    step = min(FLOW_STEP, (SCALE - 2 * FLOW_TOP) / len(rows))
    return [(r, (FLOW_TOP, FLOW_TOP + k * step, min(SCALE - FLOW_TOP, FLOW_TOP + FLOW_CHAR * len(r)),
                 FLOW_TOP + (k + 1) * step)) for k, r in enumerate(rows)]


class _Fonts:
    """Знаки документа → (шрифт, код); шрифты пишутся в конце (`write`), когда известны все знаки."""

    def __init__(self):
        self.codes: dict[str, tuple[int, int]] = {}

    def code(self, char: str) -> tuple[int, int]:
        if char not in self.codes:
            n = len(self.codes)
            self.codes[char] = (n // CODES, n % CODES)
        return self.codes[char]

    def count(self) -> int:
        return (len(self.codes) + CODES - 1) // CODES

    def write(self, writer: PdfWriter, refs: list) -> None:
        glyph = DecodedStreamObject()
        glyph.set_data(f"{GLYPH} 0 d0".encode("ascii"))
        glyph_ref = writer._add_object(glyph)
        for k, ref in enumerate(refs):
            chars = sorted((code, char) for char, (font, code) in self.codes.items() if font == k)
            last = chars[-1][0]
            font = ref.get_object()
            font.update({
                NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type3"),
                NameObject("/FontBBox"): ArrayObject(NumberObject(v) for v in (0, -200, GLYPH, 800)),
                NameObject("/FontMatrix"): ArrayObject(FloatObject(v) for v in (0.001, 0, 0, 0.001, 0, 0)),
                NameObject("/CharProcs"): DictionaryObject({NameObject("/g"): glyph_ref}),
                NameObject("/Encoding"): DictionaryObject({
                    NameObject("/Type"): NameObject("/Encoding"),
                    NameObject("/Differences"): ArrayObject([NumberObject(0)] + [NameObject("/g")] * (last + 1))}),
                NameObject("/FirstChar"): NumberObject(0), NameObject("/LastChar"): NumberObject(last),
                NameObject("/Widths"): ArrayObject([NumberObject(GLYPH)] * (last + 1)),
                NameObject("/Resources"): DictionaryObject(),
                NameObject("/ToUnicode"): writer._add_object(_cmap(chars)),
            })


def _utf16(char: str) -> str:
    return char.encode("utf-16-be").hex().upper()


def _cmap(chars: list) -> DecodedStreamObject:
    """ToUnicode: код знака → его Unicode (блоками по 100 — предел bfchar)."""
    blocks = []
    for at in range(0, len(chars), 100):
        part = chars[at:at + 100]
        blocks.append(f"{len(part)} beginbfchar\n"
                      + "".join(f"<{code:02X}> <{_utf16(char)}>\n" for code, char in part) + "endbfchar\n")
    stream = DecodedStreamObject()
    stream.set_data(("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
                     "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
                     "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
                     "1 begincodespacerange\n<00> <FF>\nendcodespacerange\n" + "".join(blocks)
                     + "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n").encode("ascii"))
    return stream


def _num(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _text(lines: list, box: tuple, fonts: _Fonts) -> str:
    """Операторы невидимого текста: `box` — видимая область страницы (left, bottom, right, top) в pt."""
    left, bottom, right, top = box
    width, height = right - left, top - bottom
    out = ["BT 3 Tr"]
    for text, (x1, y1, x2, y2) in lines:
        chars = [c for c in text if c.isprintable() or c == " "]
        if not chars or x2 <= x1 or y2 <= y1:
            continue
        size = (y2 - y1) / SCALE * height
        x, y = left + x1 / SCALE * width, top - y2 / SCALE * height + BASELINE * size
        out.append(f"{_num(100 * (x2 - x1) / SCALE * width / (len(chars) * GLYPH / 1000 * size))} Tz"
                   f" 1 0 0 1 {_num(x)} {_num(y)} Tm")
        run, font = [], None
        for c in chars:
            k, code = fonts.code(c)
            if k != font and run:
                out.append(f"/StudioOcr{font} {_num(size)} Tf <{''.join(run)}> Tj")
                run = []
            font = k
            run.append(f"{code:02X}")
        out.append(f"/StudioOcr{font} {_num(size)} Tf <{''.join(run)}> Tj")
    out.append("ET")
    return "\n".join(out) if len(out) > 2 else ""


def _jpeg(image: bytes) -> tuple[bytes, int, int]:
    with Image.open(io.BytesIO(image)) as img:
        rgb = ImageOps.exif_transpose(img).convert("RGB")     # как ocr_render.photo_png: рамки — по повёрнутой
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue(), rgb.width, rgb.height


def build(source: "bytes | None", pages: list) -> bytes:
    """PDF со слоем: `source` — PDF-исходник документа (у документа из фото — None), `pages` — Page по порядку."""
    writer, fonts = PdfWriter(), _Fonts()
    reader = PdfReader(io.BytesIO(source)) if source is not None else None
    texts = []                                       # (страница writer, операторы) — шрифты известны в конце
    for page in pages:
        if page.index is not None:
            out = writer.add_page(reader.pages[page.index])
            if out.get("/Rotate"):
                out.transfer_rotation_to_content()
            crop = out.cropbox
            texts.append((out, _text(page.lines, (float(crop.left), float(crop.bottom), float(crop.right),
                                                  float(crop.top)), fonts), None))
            continue
        data, w, h = _jpeg(page.image)
        k = PHOTO_SIDE / max(w, h)
        out = writer.add_blank_page(w * k, h * k)
        picture = DecodedStreamObject()
        picture._data = data                         # уже JPEG: пишется как есть, с /Filter /DCTDecode
        picture.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                        NameObject("/Width"): NumberObject(w), NameObject("/Height"): NumberObject(h),
                        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                        NameObject("/BitsPerComponent"): NumberObject(8),
                        NameObject("/Filter"): NameObject("/DCTDecode")})
        draw = f"q {_num(w * k)} 0 0 {_num(h * k)} 0 0 cm /StudioPage Do Q\n"
        texts.append((out, draw + _text(page.lines, (0, 0, w * k, h * k), fonts), writer._add_object(picture)))
    refs = [writer._add_object(DictionaryObject()) for _ in range(fonts.count())]
    fonts.write(writer, refs)
    for out, ops, picture in texts:
        if not ops:
            continue
        resources = DictionaryObject({NameObject("/Font"): DictionaryObject(
            {NameObject(f"/StudioOcr{k}"): ref for k, ref in enumerate(refs)})})
        if picture is not None:
            resources[NameObject("/XObject")] = DictionaryObject({NameObject("/StudioPage"): picture})
        stream = DecodedStreamObject()
        stream.set_data(ops.encode("ascii"))
        if picture is not None:
            out[NameObject("/Resources")] = resources
            out[NameObject("/Contents")] = writer._add_object(stream)
            continue
        overlay = PageObject.create_blank_page(writer, 1, 1)     # слой поверх страницы исходника
        overlay[NameObject("/Resources")] = resources
        overlay[NameObject("/Contents")] = writer._add_object(stream)
        out.merge_page(overlay)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()
