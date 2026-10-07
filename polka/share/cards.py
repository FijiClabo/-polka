"""Карточки для шеринга: стрик, финиш, пара. Размеры 1080×1920 (сторис) и 1080×1350 (пост).

Стиль повторяет макет v3: тёмный фон, тёплое свечение, Unbounded для цифр,
Cormorant для названий книг, Onest для подписей, полка с цветными корешками.
"""

from __future__ import annotations

import io
import math
import random
from datetime import date
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from settings import get_settings

FONTS = Path(__file__).parent / "fonts"
SIZES = {"story": (1080, 1920), "post": (1080, 1350)}
BG = (17, 17, 19)
CARD = (30, 30, 34)
TEXT = (245, 241, 234)
MUTED = (150, 145, 138)
ORANGE = (255, 122, 61)
AMBER = (255, 180, 67)
VIOLET = (184, 168, 255)
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]


@lru_cache(maxsize=32)
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


def F_NUM(size):  # noqa: N802
    return font("Unbounded-Bold.ttf", size)


def F_TITLE(size):  # noqa: N802
    return font("Cormorant-SemiBoldItalic.ttf", size)


def F_UI(size, bold=False):  # noqa: N802
    return font("Onest-SemiBold.ttf" if bold else "Onest-Regular.ttf", size)


def hex_rgb(h: str | None, default=(226, 85, 63)) -> tuple[int, int, int]:
    try:
        h = (h or "").lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return default


# --------------------------------------------------------------------------- примитивы


def _background(w: int, h: int, glow=(120, 52, 22), glow_y: float = 0.28) -> Image.Image:
    img = Image.new("RGB", (w, h), BG)
    glow_layer = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(glow_layer)
    cx, cy, r = w // 2, int(h * glow_y), int(w * 0.48)
    d.ellipse((cx - r, cy - int(r * 0.9), cx + r, cy + int(r * 0.9)), fill=glow)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=w // 5))
    return Image.blend(img, glow_layer, 0.75)


def _wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int, max_lines: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
            while draw.textlength(cur, font=fnt) > max_w and len(cur) > 1:
                cur = cur[:-1]
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while draw.textlength(last + "…", font=fnt) > max_w and len(last) > 1:
            last = last[:-1].rstrip()
        lines[-1] = last + "…"
    return lines


def _center(draw, y: int, text: str, fnt, fill, w: int) -> int:
    tw = draw.textlength(text, font=fnt)
    draw.text(((w - tw) / 2, y), text, font=fnt, fill=fill)
    bbox = fnt.getbbox(text)
    return y + bbox[3]


def _center_lines(draw, y: int, lines: list[str], fnt, fill, w: int, gap: int) -> int:
    for line in lines:
        y = _center(draw, y, line, fnt, fill, w) + gap
    return y


def _flame(img: Image.Image, cx: int, cy: int, size: int) -> None:
    """Пламя: два вложенных «капли» с градиентом."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    def drop(scale: float, color):
        pts = []
        for i in range(120):
            t = i / 119 * 2 * math.pi
            x = math.sin(t) * (0.55 + 0.15 * math.cos(t))
            y = -math.cos(t) * 1.0 - 0.25 * (math.cos(t) ** 2)
            if y < -0.2:
                x *= 1 - (abs(y) - 0.2) * 0.55
            pts.append((cx + x * size * scale, cy + y * size * scale * 1.15))
        d.polygon(pts, fill=color)

    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((cx - size * 1.6, cy - size * 1.8, cx + size * 1.6, cy + size * 1.2), fill=(255, 110, 40, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(size // 2))
    img.paste(glow, (0, 0), glow)
    drop(1.0, (255, 106, 61, 255))
    drop(0.62, (255, 180, 67, 255))
    drop(0.3, (255, 236, 190, 255))
    img.paste(layer, (0, 0), layer)


def _shelf(img: Image.Image, x0: int, base_y: int, width: int, spines: list[tuple[tuple[int, int, int], float]],
           new_last: bool = True) -> None:
    d = ImageDraw.Draw(img)
    n = max(len(spines), 1)
    gap = 14
    sw = min(110, (width - gap * (n - 1)) // n)
    total = sw * n + gap * (n - 1)
    x = x0 + (width - total) // 2
    max_h = int(width * 0.42)
    for i, (color, hfrac) in enumerate(spines):
        h = int(max_h * hfrac)
        r = 10
        d.rounded_rectangle((x, base_y - h, x + sw, base_y), radius=r, fill=color)
        # блик и полоски на корешке
        d.rectangle((x + 8, base_y - h + 30, x + sw - 8, base_y - h + 36), fill=tuple(min(255, c + 30) for c in color))
        d.rectangle((x + 8, base_y - 40, x + sw - 8, base_y - 34), fill=tuple(max(0, c - 30) for c in color))
        if new_last and i == len(spines) - 1:
            pill_w, pill_h = 92, 54
            px, py = x + sw // 2 - pill_w // 2, base_y - h - pill_h - 18
            d.rounded_rectangle((px, py, px + pill_w, py + pill_h), radius=27, fill=ORANGE)
            f = F_UI(30, bold=True)
            tw = d.textlength("+1", font=f)
            d.text((px + (pill_w - tw) / 2, py + 8), "+1", font=f, fill=(20, 16, 14))
        x += sw + gap
    d.rectangle((x0, base_y + 2, x0 + width, base_y + 6), fill=(235, 230, 222))


def _stat_boxes(img: Image.Image, y: int, w: int, stats: list[tuple[str, str, tuple]]) -> int:
    d = ImageDraw.Draw(img)
    margin, gap = 70, 22
    n = len(stats)
    bw = (w - 2 * margin - gap * (n - 1)) // n
    bh = 190
    x = margin
    for value, label, color in stats:
        d.rounded_rectangle((x, y, x + bw, y + bh), radius=34, fill=CARD)
        fv = F_NUM(58 if len(value) <= 4 else 44)
        tw = d.textlength(value, font=fv)
        d.text((x + (bw - tw) / 2, y + 36), value, font=fv, fill=color)
        fl = F_UI(30)
        lines = _wrap(d, label, fl, bw - 30, 2)
        ly = y + 120
        for line in lines:
            tw = d.textlength(line, font=fl)
            d.text((x + (bw - tw) / 2, ly), line, font=fl, fill=MUTED)
            ly += 34
        x += bw + gap
    return y + bh


def _confetti(img: Image.Image, seed: int = 7) -> None:
    ImageDraw.Draw(img)
    rnd = random.Random(seed)
    w, h = img.size
    colors = [AMBER, ORANGE, VIOLET, (110, 220, 160), (120, 160, 255), (255, 140, 120)]
    for _ in range(40):
        x, y = rnd.randint(30, w - 30), rnd.randint(40, int(h * 0.6))
        if w * 0.08 < x < w * 0.92 and h * 0.07 < y < h * 0.27:
            continue  # не закрываем заголовок
        cw, ch = rnd.randint(10, 18), rnd.randint(22, 34)
        piece = Image.new("RGBA", (cw * 3, ch * 3), (0, 0, 0, 0))
        ImageDraw.Draw(piece).rounded_rectangle((cw, ch, cw * 2, ch * 2), radius=4, fill=rnd.choice(colors) + (230,))
        piece = piece.rotate(rnd.randint(0, 180), expand=False, resample=Image.BICUBIC)
        img.paste(piece, (x - cw, y - ch), piece)


def _footer(img: Image.Image, text: str) -> None:
    d = ImageDraw.Draw(img)
    w, h = img.size
    f = F_UI(32, bold=True)
    _center(d, h - 110, text, f, MUTED, w)


def _avatar(img: Image.Image, cx: int, cy: int, r: int, name: str, color, ring) -> None:
    d = ImageDraw.Draw(img)
    d.ellipse((cx - r - 8, cy - r - 8, cx + r + 8, cy + r + 8), outline=ring, width=6)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    letter = (name or "?").strip()[:1].upper()
    f = F_UI(int(r * 0.9), bold=True)
    tw = d.textlength(letter, font=f)
    bbox = f.getbbox(letter)
    d.text((cx - tw / 2, cy - (bbox[3] + bbox[1]) / 2), letter, font=f, fill=(20, 20, 22))


def _fmt_range(start: date | None, end: date | None) -> str:
    if not start or not end:
        return ""
    return f"{start.day} {MONTHS[start.month - 1]} — {end.day} {MONTHS[end.month - 1]}".upper()


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- шаблоны


def card_streak(*, name: str, streak: int, book_title: str, spine: str | None, size: str = "story") -> bytes:
    w, h = SIZES[size]
    img = _background(w, h, glow=(110, 46, 16), glow_y=0.3)
    d = ImageDraw.Draw(img)
    story = size == "story"
    top = int(h * (0.16 if story else 0.1))
    _flame(img, w // 2, top + 150, 120)
    d = ImageDraw.Draw(img)
    y = top + 330
    num = str(streak)
    fnum = F_NUM(300 if len(num) < 3 else 230)
    y = _center(d, y, num, fnum, TEXT, w) + 50
    y = _center(d, y, _days_label(streak), F_UI(56, bold=True), AMBER, w) + 90
    if book_title:
        y = _center(d, y, "читаю каждый день", F_UI(40), MUTED, w) + 30
        lines = _wrap(d, f"«{book_title}»", F_TITLE(84), w - 160, 3)
        _center_lines(d, y, lines, F_TITLE(84), TEXT, w, 18)
    _footer(img, f"{name} · {get_settings().project_name}")
    return _png(img)


def _days_label(n: int) -> str:
    n2 = abs(n) % 100
    n1 = n2 % 10
    if 11 <= n2 <= 14:
        word = "дней"
    elif n1 == 1:
        word = "день"
    elif 2 <= n1 <= 4:
        word = "дня"
    else:
        word = "дней"
    return f"{word} подряд"


def card_finish(*, name: str, book_title: str, plan_days: int, start: date | None, end: date | None,
                best_streak: int, retells: int, partner: str | None, shelf: list[tuple[str, int]],
                size: str = "story") -> bytes:
    w, h = SIZES[size]
    img = _background(w, h, glow=(100, 40, 20), glow_y=0.22)
    _confetti(img)
    d = ImageDraw.Draw(img)
    story = size == "story"
    y = int(h * (0.13 if story else 0.07))
    rng = _fmt_range(start, end)
    if rng:
        y = _center(d, y, rng, F_UI(34, bold=True), MUTED, w) + 40
    y = _center(d, y, "Дочитано!", F_NUM(112), TEXT, w) + 44
    lines = _wrap(d, f"«{book_title}» за {plan_days} {_days_word(plan_days)}", F_UI(44), w - 160, 2)
    y = _center_lines(d, y, lines, F_UI(44), (205, 200, 192), w, 14)
    # полка: прошлые книги + новая
    spines = []
    rnd = random.Random(len(shelf))
    for color, pages in shelf[-5:]:
        spines.append((hex_rgb(color), min(1.0, 0.55 + min(pages, 700) / 1600 + rnd.random() * 0.1)))
    base = int(h * (0.63 if story else 0.66))
    _shelf(img, 90, base, w - 180, spines or [((226, 85, 63), 0.8)], new_last=True)
    stats = [(str(best_streak), "дней подряд", AMBER), (str(retells), "пересказов", TEXT)]
    if partner:
        stats.append((partner[:8], "напарник", VIOLET))
    y = _stat_boxes(img, base + 70, w, stats)
    if story:
        d = ImageDraw.Draw(img)
        lines = _wrap(d, "Конспект собран из моих пересказов", F_TITLE(56), w - 200, 2)
        _center_lines(d, y + 90, lines, F_TITLE(56), (205, 200, 192), w, 10)
    _footer(img, f"{name} · {get_settings().project_name}")
    return _png(img)


def _days_word(n: int) -> str:
    return _days_label(n).split()[0]


def card_pair(*, name: str, partner: str, pair_streak: int, size: str = "story") -> bytes:
    w, h = SIZES[size]
    img = _background(w, h, glow=(64, 44, 120), glow_y=0.38)
    d = ImageDraw.Draw(img)
    story = size == "story"
    y0 = int(h * (0.18 if story else 0.12))
    _center(d, y0, "НАПАРНИКИ", F_UI(38, bold=True), VIOLET, w)
    cy = y0 + 330
    _avatar(img, w // 2 - 290, cy, 120, name, (62, 116, 96), (120, 116, 112))
    _avatar(img, w // 2 + 290, cy, 120, partner, (184, 168, 255), (110, 220, 160))
    _flame(img, w // 2, cy - 140, 60)
    d = ImageDraw.Draw(img)
    num = str(pair_streak)
    _center(d, cy - 70, num, F_NUM(200 if len(num) < 3 else 150), TEXT, w)
    _center(d, cy + 150, "общий стрик", F_UI(40), MUTED, w)
    f = F_UI(44, bold=True)
    for nm, cx in ((name, w // 2 - 290), (partner, w // 2 + 290)):
        nm = nm[:14]
        tw = d.textlength(nm, font=f)
        d.text((cx - tw / 2, cy + 160), nm, font=f, fill=TEXT)
    y = cy + 300
    lines = _wrap(d, "Читаем каждый свою книгу. Стрик растёт, только если сдали оба.", F_TITLE(64), w - 180, 3)
    _center_lines(d, y, lines, F_TITLE(64), (225, 220, 212), w, 14)
    _footer(img, get_settings().project_name)
    return _png(img)


# --------------------------------------------------------------------------- общий вход


def render_card(kind: str, *, user, enr, book, partner=None, retells: int = 0, pair_streak: int = 0,
                shelf: list[tuple[str, int]] | None = None, size: str = "story") -> bytes:
    size = size if size in SIZES else "story"
    name = user.display_name if user else ""
    if kind == "streak":
        return card_streak(name=name, streak=enr.streak if enr else 0, book_title=book.title if book else "",
                           spine=book.spine_color if book else None, size=size)
    if kind == "finish":
        end = enr.finished_at.date() if enr and enr.finished_at else None
        shelf_items = list(shelf or [])
        if book and not shelf_items:
            shelf_items = [(book.spine_color, book.total_pages)]
        return card_finish(name=name, book_title=book.title if book else "", plan_days=enr.plan_days or 0,
                           start=enr.plan_start_date, end=end, best_streak=enr.best_streak, retells=retells,
                           partner=partner.display_name if partner else None, shelf=shelf_items, size=size)
    if kind == "pair":
        return card_pair(name=name, partner=partner.display_name if partner else "Напарник", pair_streak=pair_streak,
                         size=size)
    raise ValueError(kind)
