"""Карточки для шеринга: стрик, финиш, пара. Размеры 1080×1920 (сторис) и 1080×1350 (пост).

Спокойный «книжный» стиль, как в мини-приложении: кремовая бумага, тёплый графит, приглушённые
терракота, горчица, шалфей и пыльно-синий. Без свечения и градиентов. Cormorant — для цифр и названий,
Onest — для подписей, полка с цветными корешками.
"""

from __future__ import annotations

import io
import math
import random
from datetime import date
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from services.books import calm_color
from settings import get_settings

FONTS = Path(__file__).parent / "fonts"
SIZES = {"story": (1080, 1920), "post": (1080, 1350)}
BG = (243, 238, 230)
PAPER = (251, 248, 243)
CARD = PAPER
LINE = (222, 214, 203)
TEXT = (42, 38, 35)
TEXT_2 = (74, 67, 61)
MUTED = (120, 111, 103)
TERRACOTTA = (201, 100, 79)
MUSTARD = (226, 184, 79)
SAGE = (126, 156, 122)
BLUE = (79, 111, 159)
SOFT = {TERRACOTTA: (244, 227, 220), SAGE: (227, 236, 224), BLUE: (228, 234, 243)}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]


@lru_cache(maxsize=32)
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


def F_NUM(size):  # noqa: N802
    return font("Cormorant-SemiBold.ttf", size)


def F_TITLE(size):  # noqa: N802
    return font("Cormorant-SemiBoldItalic.ttf", size)


def F_UI(size, bold=False):  # noqa: N802
    return font("Onest-SemiBold.ttf" if bold else "Onest-Regular.ttf", size)


def hex_rgb(h: str | None, default=TERRACOTTA) -> tuple[int, int, int]:
    try:
        h = (h or "").lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return default


# --------------------------------------------------------------------------- примитивы


def _background(w: int, h: int, arch_top: float = 0.08, arch_bottom: float = 0.9) -> Image.Image:
    """Кремовый лист и светлая «арка» — как страница в книжном переплёте."""
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    m = int(w * 0.07)
    top, bottom = int(h * arch_top), int(h * arch_bottom)
    r = (w - 2 * m) // 2
    d.rounded_rectangle((m, top, w - m, bottom), radius=r, fill=PAPER)
    d.rectangle((m, top + r, w - m, bottom), fill=PAPER)
    d.line((m, bottom, w - m, bottom), fill=LINE, width=3)
    return img


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


LNUM = ["lnum"]  # цифры одной высоты: у Cormorant по умолчанию «старинные» с выносными элементами


def _center(draw, y: int, text: str, fnt, fill, w: int, features: list[str] | None = None) -> int:
    tw = draw.textlength(text, font=fnt, features=features)
    draw.text(((w - tw) / 2, y), text, font=fnt, fill=fill, features=features)
    bbox = fnt.getbbox(text, features=features)
    return y + bbox[3]


def _center_lines(draw, y: int, lines: list[str], fnt, fill, w: int, gap: int) -> int:
    for line in lines:
        y = _center(draw, y, line, fnt, fill, w) + gap
    return y


def _flame(img: Image.Image, cx: int, cy: int, size: int) -> None:
    """Пламя: три вложенные «капли» спокойных тонов, без свечения."""
    d = ImageDraw.Draw(img)

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

    drop(1.0, TERRACOTTA)
    drop(0.62, MUSTARD)
    drop(0.3, PAPER)


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
        # тонкие полоски на корешке, как тиснение
        stripe = tuple(min(255, c + 40) for c in color)
        d.rectangle((x + 12, base_y - h + 30, x + sw - 12, base_y - h + 33), fill=stripe)
        d.rectangle((x + 12, base_y - 38, x + sw - 12, base_y - 35), fill=stripe)
        if new_last and i == len(spines) - 1:
            pill_w, pill_h = 96, 56
            px, py = x + sw // 2 - pill_w // 2, base_y - h - pill_h - 18
            d.rounded_rectangle((px, py, px + pill_w, py + pill_h), radius=28, fill=PAPER, outline=LINE, width=3)
            f = F_UI(30, bold=True)
            tw = d.textlength("+1", font=f)
            d.text((px + (pill_w - tw) / 2, py + 9), "+1", font=f, fill=TERRACOTTA)
        x += sw + gap
    d.rectangle((x0, base_y + 2, x0 + width, base_y + 5), fill=TEXT)


def _stat_boxes(img: Image.Image, y: int, w: int, stats: list[tuple[str, str, tuple]]) -> int:
    d = ImageDraw.Draw(img)
    margin, gap = 70, 22
    n = len(stats)
    bw = (w - 2 * margin - gap * (n - 1)) // n
    bh = 190
    x = margin
    for value, label, color in stats:
        d.rounded_rectangle((x, y, x + bw, y + bh), radius=30, fill=BG, outline=LINE, width=3)
        fv = F_NUM(76 if len(value) <= 4 else 52)
        tw = d.textlength(value, font=fv, features=LNUM)
        d.text((x + (bw - tw) / 2, y + 22), value, font=fv, fill=color, features=LNUM)
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
    colors = [TERRACOTTA, MUSTARD, SAGE, BLUE, (212, 154, 140)]
    for _ in range(28):
        x, y = rnd.randint(30, w - 30), rnd.randint(40, int(h * 0.6))
        if w * 0.08 < x < w * 0.92 and h * 0.07 < y < h * 0.27:
            continue  # не закрываем заголовок
        cw, ch = rnd.randint(10, 18), rnd.randint(22, 34)
        piece = Image.new("RGBA", (cw * 3, ch * 3), (0, 0, 0, 0))
        ImageDraw.Draw(piece).rounded_rectangle((cw, ch, cw * 2, ch * 2), radius=4, fill=rnd.choice(colors) + (200,))
        piece = piece.rotate(rnd.randint(0, 180), expand=False, resample=Image.BICUBIC)
        img.paste(piece, (x - cw, y - ch), piece)


def _footer(img: Image.Image, text: str) -> None:
    d = ImageDraw.Draw(img)
    w, h = img.size
    f = F_UI(32)
    _center(d, h - 100, text, f, MUTED, w)


def _avatar(img: Image.Image, cx: int, cy: int, r: int, name: str, color, ring) -> None:
    d = ImageDraw.Draw(img)
    d.ellipse((cx - r - 10, cy - r - 10, cx + r + 10, cy + r + 10), outline=ring, width=4)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    letter = (name or "?").strip()[:1].upper()
    f = F_UI(int(r * 0.9), bold=True)
    tw = d.textlength(letter, font=f)
    bbox = f.getbbox(letter)
    d.text((cx - tw / 2, cy - (bbox[3] + bbox[1]) / 2), letter, font=f, fill=PAPER)


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
    story = size == "story"
    img = _background(w, h, arch_bottom=0.76 if story else 0.88)
    d = ImageDraw.Draw(img)
    top = int(h * (0.16 if story else 0.1))
    _flame(img, w // 2, top + 150, 120)
    d = ImageDraw.Draw(img)
    y = top + 330
    num = str(streak)
    fnum = F_NUM(340 if len(num) < 3 else 260)
    y = _center(d, y - 30, num, fnum, TEXT, w, LNUM) + 40
    y = _center(d, y, _days_label(streak), F_TITLE(72), TERRACOTTA, w) + 90
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
    story = size == "story"
    base = int(h * (0.63 if story else 0.66))
    img = _background(w, h, arch_top=0.05, arch_bottom=(base + 4) / h)
    _confetti(img)
    d = ImageDraw.Draw(img)
    y = int(h * (0.13 if story else 0.07))
    rng = _fmt_range(start, end)
    if rng:
        y = _center(d, y, rng, F_UI(34, bold=True), MUTED, w) + 40
    y = _center(d, y, "Дочитано!", F_NUM(140), TEXT, w) + 44
    lines = _wrap(d, f"«{book_title}» за {plan_days} {_days_word(plan_days)}", F_UI(44), w - 160, 2)
    y = _center_lines(d, y, lines, F_UI(44), TEXT_2, w, 14)
    # полка: прошлые книги + новая
    spines = []
    rnd = random.Random(len(shelf))
    for color, pages in shelf[-5:]:
        spines.append((hex_rgb(calm_color(color)), min(1.0, 0.55 + min(pages, 700) / 1600 + rnd.random() * 0.1)))
    _shelf(img, 90, base, w - 180, spines or [(TERRACOTTA, 0.8)], new_last=True)
    stats = [(str(best_streak), _days_label(best_streak), TERRACOTTA),
             (str(retells), _plural(retells, "пересказ", "пересказа", "пересказов"), TEXT)]
    if partner:
        stats.append((partner[:8], "напарник", BLUE))
    y = _stat_boxes(img, base + 70, w, stats)
    if story:
        d = ImageDraw.Draw(img)
        lines = _wrap(d, "Пятнадцать минут в день — и книга дочитана", F_TITLE(56), w - 200, 2)
        _center_lines(d, y + 90, lines, F_TITLE(56), TEXT_2, w, 10)
    _footer(img, f"{name} · {get_settings().project_name}")
    return _png(img)


def _plural(n: int, one: str, few: str, many: str) -> str:
    n2, n1 = abs(n) % 100, abs(n) % 10
    if 11 <= n2 <= 14:
        return many
    return one if n1 == 1 else few if 2 <= n1 <= 4 else many


def _days_word(n: int) -> str:
    return _days_label(n).split()[0]


def card_pair(*, name: str, partner: str, pair_streak: int, size: str = "story") -> bytes:
    w, h = SIZES[size]
    story = size == "story"
    img = _background(w, h, arch_bottom=0.7 if story else 0.88)
    d = ImageDraw.Draw(img)
    y0 = int(h * (0.18 if story else 0.12))
    _center(d, y0, "НАПАРНИКИ", F_UI(36, bold=True), BLUE, w)
    cy = y0 + 330
    _avatar(img, w // 2 - 290, cy, 120, name, SAGE, LINE)
    _avatar(img, w // 2 + 290, cy, 120, partner, BLUE, LINE)
    _flame(img, w // 2, cy - 150, 52)
    d = ImageDraw.Draw(img)
    num = str(pair_streak)
    _center(d, cy - 100, num, F_NUM(210 if len(num) < 3 else 160), TEXT, w, LNUM)
    _center(d, cy + 110, "общий стрик", F_UI(36), MUTED, w)
    f = F_UI(44, bold=True)
    for nm, cx in ((name, w // 2 - 290), (partner, w // 2 + 290)):
        nm = nm[:14]
        tw = d.textlength(nm, font=f)
        d.text((cx - tw / 2, cy + 160), nm, font=f, fill=TEXT)
    y = cy + 300
    lines = _wrap(d, "Читаем каждый свою книгу. Стрик растёт, только если день сдан у нас двоих.", F_TITLE(64), w - 180, 3)
    _center_lines(d, y, lines, F_TITLE(64), TEXT_2, w, 14)
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
