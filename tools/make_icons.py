"""Иконки студии «Облачко с искрой» из геометрии `app/static/icons/icon.svg` (спека 2026-10-20, решение 14).

    python tools/make_icons.py [каталог]        # по умолчанию app/static/icons

Пишет `icon-192.png`, `icon-512.png` (квадрат со скруглением, углы прозрачные), `apple-touch-icon.png`
(180) и `icon-maskable-512.png` (фон на всю площадь, без альфы; у maskable рисунок уменьшен до 0,75 —
безопасная зона 80 %), `favicon.ico` (16, 32, 48). Рисует в 4× и уменьшает LANCZOS. Правка рисунка —
в `icon.svg` и здесь вместе; PNG коммитятся, на хосте скрипт не запускается.
"""
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

TOP, BOTTOM = (0xe2, 0x82, 0x5c), (0xb2, 0x4f, 0x2b)      # градиент от левого верхнего к правому нижнему
BUBBLE, SPARK = (0xff, 0xff, 0xff), (0xc9, 0x64, 0x42)
RADIUS = 112                                               # скругление квадрата (rx в icon.svg)
SPARK_CURVES = [((256, 158), (266, 222), (286, 236), (340, 246)),
                ((340, 246), (286, 256), (266, 270), (256, 334)),
                ((256, 334), (246, 270), (226, 256), (172, 246)),
                ((172, 246), (226, 236), (246, 222), (256, 158))]
SUPER = 4


def gradient(s: int) -> Image.Image:
    """Цвет по t = (x + y) / (2·s): как linearGradient 0,0 → 1,1 у квадрата."""
    v = Image.linear_gradient("L").resize((s, s))                 # сверху вниз 0 → 255
    t = ImageChops.add(v, v.transpose(Image.Transpose.ROTATE_90), scale=2)       # + слева направо 0 → 255
    return Image.merge("RGB", [t.point(lambda x, a=a, b=b: round(a + (b - a) * x / 255))
                               for a, b in zip(TOP, BOTTOM)])


def cubic(p0, p1, p2, p3, n=48):
    for i in range(n):
        u = i / n
        yield tuple((1 - u) ** 3 * a + 3 * (1 - u) ** 2 * u * b + 3 * (1 - u) * u ** 2 * c + u ** 3 * d
                    for a, b, c, d in zip(p0, p1, p2, p3))


def render(size: int, *, rounded: bool, shrink: float = 1.0) -> Image.Image:
    s = size * SUPER
    k = s / 512
    at = lambda x, y: ((256 + (x - 256) * shrink) * k, (256 + (y - 256) * shrink) * k)
    im = gradient(s)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((*at(84, 108), *at(428, 384)), radius=84 * shrink * k, fill=BUBBLE)
    d.polygon([at(236, 384), at(152, 446), at(168, 384)], fill=BUBBLE)          # хвост облачка
    d.polygon([at(x, y) for curve in SPARK_CURVES for x, y in cubic(*curve)], fill=SPARK)
    if rounded:
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=RADIUS * k, fill=255)
        im.putalpha(mask)
    return im.resize((size, size), Image.Resampling.LANCZOS)


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    big = render(512, rounded=True)
    big.save(out / "icon-512.png")
    render(192, rounded=True).save(out / "icon-192.png")
    render(180, rounded=False).save(out / "apple-touch-icon.png")
    render(512, rounded=False, shrink=0.75).save(out / "icon-maskable-512.png")
    big.save(out / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "app" / "static" / "icons")
