"""Все тексты бота в одном месте. Тон: дружелюбный, на «ты», короткие фразы, лёгкая ирония.

Пол собеседника Telegram не сообщает, поэтому формулировки нейтральные («у Ани день сдан»).
Разметка — HTML; всё, что пришло от пользователей (имена, названия), экранируется.
"""

from __future__ import annotations

from datetime import date
from html import escape

from core.achievements import ACH_BY_CODE
from settings import get_settings

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def P() -> str:
    return get_settings().project_name


def e(s: str | None) -> str:
    return escape(s or "")


def d(day: date | None) -> str:
    if not day:
        return "дата уточняется"
    return f"{day.day} {MONTHS[day.month - 1]}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def days_word(n: int) -> str:
    return f"{n} {plural(n, 'день', 'дня', 'дней')}"


# --------------------------------------------------------------------------- онбординг

def welcome() -> list[str]:
    return [
        f"Привет! Я — {P()}. Помогаю дочитывать книги, которые обычно остаются на середине.\n\n"
        "Как это устроено: ты берёшь свою книгу и вступаешь в <b>забег</b> — план на 21–60 дней, "
        "по 15 минут чтения в день. В конце книга встаёт на полку вместе с конспектом.",
        "Каждый день читаешь свой отрезок — в приложении, в любой читалке или на бумаге — "
        "и за 30–60 секунд <b>пересказываешь</b> его мне голосом или текстом.\n\n"
        "Я не экзаменатор: просто убеждаюсь, что отрезок прочитан. Если что-то неясно — задам один вопрос.",
        "День засчитывается только после пересказа. Дни подряд — это <b>стрик</b> 🔥\n\n"
        "Раз в неделю есть заморозка: если пропустишь день, стрик не сгорит. "
        "А с напарником стрик общий — подводить друг друга не хочется.",
    ]


ASK_TZ = "Где ты живёшь? Это нужно, чтобы присылать отрезок утром, а не в три ночи. День у нас длится с 04:00 до 04:00."
ASK_TZ_OTHER = (
    "Напиши смещение от Москвы (например, <code>+4</code> или <code>-1</code>) "
    "или от UTC в формате <code>UTC+5</code>. Можно просто отправить геопозицию."
)
TZ_BAD = "Не понял пояс. Пример: <code>+2</code> (от Москвы) или <code>UTC+5</code>."
ASK_MORNING = "Во сколько присылать отрезок на день?"
ASK_EVENING = "А во сколько напомнить вечером, если день ещё не сдан? Напоминание одно, без занудства."
TIME_BAD = "Не понял время. Напиши в формате <code>08:30</code>."


def tz_set(label: str) -> str:
    return f"Записал: {e(label)}."


def trial_intro(title: str, author: str, text: str) -> str:
    return (
        "Давай попробуем механику, пока ничего не считается.\n\n"
        f"Вот кусочек рассказа «{e(title)}» ({e(author)}). Прочитай и перескажи своими словами — "
        "голосовым или текстом, 2–4 предложения.\n\n"
        f"<blockquote expandable>{e(text)}</blockquote>"
    )


TRIAL_SKIP = "Пропустить"
TRIAL_DONE = "Вот так это и работает. Дальше — твоя книга."
TRIAL_AI_OFF = "Пересказ принят. (Проверка ИИ сейчас не настроена, но в забеге она будет работать.)"


def add_book_prompt() -> str:
    return (
        "Теперь книга. Пришли сюда файл <b>epub</b> или <b>fb2</b> — я разобью его на отрезки по дням.\n\n"
        "Читаешь на бумаге? Нажми «У меня бумажная книга»."
    )


BOOK_RECEIVED = "Получил, разбираю. Это займёт до минуты ⏳"
BOOK_TOO_BIG = "Файл больше {mb} МБ — Telegram не даст мне его скачать. Попробуй другой файл или добавь книгу как бумажную."
BOOK_UNSUPPORTED = "Пока умею только epub и fb2. Можно найти книгу в другом формате или добавить её как бумажную."
BOOK_NOT_A_BOOK = "Это не похоже на книгу. Я жду файл epub или fb2."


def book_parsed(title: str, author: str, pages: int, chapters: int, minutes: int) -> str:
    ch = f" · {chapters} {plural(chapters, 'глава', 'главы', 'глав')}" if chapters else ""
    return (
        f"Готово: <b>{e(title)}</b>{(' — ' + e(author)) if author else ''}\n"
        f"{pages} {plural(pages, 'страница', 'страницы', 'страниц')}{ch} · около {_hm(minutes)} чтения.\n\n"
        "Осталось выбрать срок — нажми «Настроить план»."
    )


def _hm(minutes: int) -> str:
    h, m = divmod(max(minutes, 1), 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


def book_failed(reason: str) -> str:
    return f"Не получилось: {e(reason)}\n\nМожно прислать другой файл или добавить книгу как бумажную — тогда сверка будет по общему знанию о книге."


def book_replace_confirm(title: str) -> str:
    return (f"Сейчас ты читаешь «{e(title)}», и прогресс уже есть. Заменить книгу? "
            "План начнётся заново со следующего дня, стрик сохранится.")


def plan_ready(title: str, days: int, start: date | None, ppd: float, minutes: int, *,
               run_start: date | None = None, awaiting_payment: bool = False) -> str:
    if start:
        when = f"Старт — {d(start)}."
    elif run_start:
        when = f"Старт забега — {d(run_start)}." + (" План включится после оплаты." if awaiting_payment else "")
    else:
        when = "Старт — в день начала забега."
    ppd_s = f"{ppd:g}".replace(".", ",")
    return f"План готов: «{e(title)}» за {days_word(days)}, примерно {ppd_s} стр. и {minutes} мин в день.\n{when}"


PAPER_ASK_TITLE = "Как называется книга?"
PAPER_ASK_AUTHOR = "Кто автор? (Если не знаешь — отправь «-».)"
PAPER_ASK_PAGES = "Сколько в ней страниц? Только число."
PAPER_BAD_PAGES = "Нужно число от 20 до 3000."


# --------------------------------------------------------------------------- статус


def status_waiting_payment() -> str:
    s = get_settings()
    return f"Ты в списке забега. Ожидаем оплату 💳\n\n{e(s.payment_info)}"


def status_in_list(start: date | None) -> str:
    return f"Забег стартует {d(start)}. Ты в списке ✓" if start else "Ты в списке ✓ Дату старта сообщу отдельно."


STATUS_NO_RUN = "Сейчас нет открытого забега. Как только ведущий его откроет — напишу."


def sprint_offer(inviter: str | None) -> str:
    who = f"{e(inviter)} зовёт тебя читать вместе. " if inviter else ""
    return (f"{who}Хочешь попробовать бесплатно? <b>Спринт</b> — 7 дней на короткую книгу или рассказ. "
            "Всё как в забеге, только короче.")


SPRINT_STARTED = "Спринт открыт. Пришли файл короткой книги или рассказа (epub/fb2) — или выбери бумажную."


# --------------------------------------------------------------------------- день

def _seg_line(title: str, pages: str, minutes: int) -> str:
    return f"<b>{e(title)}</b>\n{e(pages)} · ≈ {minutes} мин"


def morning(name: str, day_n: int, plan_days: int, title: str, pages: str, minutes: int, *, catching_up: bool,
            yesterday: str | None, streak: int, partner: str | None, run_started: bool) -> str:
    lines = []
    if run_started:
        lines.append("Забег начался 🏁")
    if yesterday == "frozen":
        lines.append("Вчера сработала заморозка — стрик цел.")
    elif yesterday == "missed":
        lines.append("Вчера день не засчитан, стрик обнулился. Ничего — сегодня новый старт.")
    head = f"День {day_n} из {plan_days}"
    if catching_up:
        head += " · догоняем вчерашний отрезок"
    lines.append(f"{head}\n{_seg_line(title, pages, minutes)}")
    tail = []
    if streak:
        tail.append(f"Стрик: {streak} 🔥")
    if partner:
        tail.append(f"Напарник: {e(partner)}")
    if tail:
        lines.append(" · ".join(tail))
    lines.append("Прочитаешь — расскажи мне голосом или текстом, что там было.")
    return "\n\n".join(lines)


def evening(partner_name: str | None, partner_done: bool) -> str:
    base = "Сегодняшний отрезок ещё ждёт пересказа. Минуты хватит."
    if partner_name and partner_done:
        base = f"У {e(partner_name)} день уже сдан. Твоя очередь — минуты хватит."
    return base


def partner_done(name: str) -> str:
    return f"У {e(name)} день сдан ✓ Твоя очередь."


def nudge_received(name: str) -> str:
    return f"👋 {e(name)} ждёт твой пересказ."


def friend_joined(name: str) -> str:
    return f"{e(name)} теперь с тобой 🙌"


def friends_now(name: str) -> str:
    return f"Вы с {e(name)} теперь друзья."


def pair_joined(name: str) -> str:
    return f"{e(name)} — твой напарник. Общий стрик растёт, только если сдали оба."


NO_BOOK_REMINDER = "Забег уже идёт, а книги у тебя пока нет. Пришли файл epub/fb2 или добавь бумажную — начнём со следующего дня."


# --------------------------------------------------------------------------- вердикты

def heard(text: str) -> str:
    short = text if len(text) <= 300 else text[:300] + "…"
    return f"🎙 Я услышал: <i>{e(short)}</i>"


def accepted(reply: str, question: str | None, streak: int, streak_grew: bool, *, verified: bool,
             partner_name: str | None, partner_done: bool | None, pair_streak: int | None,
             can_more: bool, next_title: str | None) -> str:
    parts = [f"✅ <b>Засчитано.</b> {e(reply)}"]
    if question:
        parts.append(f"<i>Подумать:</i> {e(question)}")
    if streak_grew:
        parts.append(f"Стрик: <b>{streak}</b> 🔥")
    if partner_name is not None:
        if partner_done:
            parts.append(f"У {e(partner_name)} день тоже сдан — общий стрик растёт 🤝")
        else:
            parts.append(f"{e(partner_name)} получит весточку — теперь очередь напарника.")
    if can_more and next_title:
        parts.append(f"Финишная прямая: сегодня можно сдать ещё один отрезок — «{e(next_title)}».")
    elif next_title:
        parts.append(f"Завтра: «{e(next_title)}».")
    return "\n\n".join(parts)


def clarify(reply: str, question: str | None) -> str:
    q = f"\n\n❓ <b>{e(question)}</b>" if question else ""
    return f"{e(reply)}{q}\n\nОтветь одним сообщением — можно голосом."


def rejected(reply: str) -> str:
    return f"🤔 {e(reply)}\n\nПопробуй ещё раз — расскажи пару конкретных моментов из отрезка."


QUEUED = "Принял. Проверю чуть позже — день не сгорит, пришлю ответ сюда."
CHECKING = "Ещё проверяю прошлый пересказ — секунду."
RATE_LIMITED = "Многовато попыток за час. Давай сделаем паузу и вернёмся чуть позже."
VOICE_TOO_LONG = "Голосовое длиннее 3 минут — я столько не осилю. Достаточно 30–60 секунд."
VOICE_FAILED = "Не разобрал голосовое. Попробуй ещё раз или напиши текстом."
STT_OFF = "Голосовые пока не настроены — напиши, пожалуйста, текстом."


def pending_resolved(out) -> str:
    return "Вернулся к твоему пересказу: засчитано ✅ День на месте."


def override_notice() -> str:
    return "Ведущий пересмотрел твой пересказ: засчитано ✅ Стрик пересчитан."


def too_short_hint(seg_title: str | None) -> str:
    seg = f" «{e(seg_title)}»" if seg_title else ""
    return (f"Чтобы сдать отрезок{seg}, расскажи о нём хотя бы в паре предложений — текстом от 50 знаков "
            "или голосовым на 30–60 секунд.")


def state_message(state: str, *, next_title: str | None = None, start: date | None = None) -> str:
    return {
        "no_run": "Сейчас ты не в забеге. Загляни в приложение — там видно, что дальше.",
        "awaiting_payment": status_waiting_payment(),
        "no_book": add_book_prompt(),
        "parsing": "Ещё разбираю твою книгу — минутку.",
        "parse_failed": "С файлом не вышло. Пришли другой или добавь книгу как бумажную.",
        "plan_needed": "Книга есть, осталось выбрать срок. Нажми «Настроить план».",
        "waiting_start": status_in_list(start),
        "not_started": f"План стартует {d(start)}. Пересказы — с первого дня.",
        "finished": "Книга дочитана 🎉 Она уже на полке.",
        "expired": "Срок забега вышел. Пересказы и конспект сохранились — дочитать можно в следующем забеге.",
        "refunded": "Участие отменено, деньги возвращены. Будем рады видеть снова.",
        "done_today": (f"На сегодня хватит — завтра следующий: «{e(next_title)}»." if next_title
                       else "На сегодня хватит — завтра следующий отрезок."),
    }.get(state, "Загляни в приложение — там всё видно.")


# --------------------------------------------------------------------------- ачивки и финиш

def achievements_message(codes: list[str]) -> str:
    if len(codes) == 1:
        c = ACH_BY_CODE[codes[0]]
        return f"{c[3]} Новый значок: <b>{e(c[1])}</b> — {e(c[2].lower())}."
    items = ", ".join(f"{ACH_BY_CODE[c][3]} {e(ACH_BY_CODE[c][1])}" for c in codes)
    return f"Новые значки: {items}."


def finished(title: str, days: int, streak: int, retells: int, partner: str | None) -> str:
    who = f" Вместе с {e(partner)}." if partner else ""
    return (f"🏁 <b>Дочитано!</b>\n«{e(title)}» за {days_word(days)}. Лучший стрик — {streak}, "
            f"пересказов — {retells}.{who}\n\nКнига встала на полку, конспект собран из твоих пересказов.")


# --------------------------------------------------------------------------- прочее

def help_text() -> str:
    s = get_settings()
    contact = f"\n\nВопросы — {e(s.support_contact)}" if s.support_contact else ""
    return (
        "<b>Как это работает</b>\n"
        "• Утром приходит отрезок на день.\n"
        "• Прочитано — перескажи мне голосом или текстом (от 50 знаков).\n"
        "• Засчитанный день = +1 к стрику. Раз в неделю — заморозка.\n"
        "• Вчерашний отрезок можно догнать сегодня.\n\n"
        "<b>Команды</b>\n"
        "/today — отрезок на сегодня\n"
        "/book — моя книга\n"
        "/pair — позвать напарника\n"
        "/friends — друзья и личная ссылка\n"
        "/settings — время и пояс\n"
        "/delete_me — удалить все мои данные"
        f"{contact}"
    )


DELETE_CONFIRM = "Удалить профиль, книги и все пересказы? Это необратимо."
DELETED = "Готово, всё удалено. Если захочешь вернуться — просто нажми /start."
UNKNOWN_SHORT = "Чтобы сдать отрезок, перескажи его подробнее (от 50 знаков) или пришли голосовое. Всё остальное — в приложении."


def friend_invite_text(name: str) -> str:
    return f"{name} зовёт тебя дочитать книгу вместе: 15 минут чтения и минута пересказа в день."


def pair_invite_text(name: str) -> str:
    return f"{name} зовёт тебя в напарники: читаем каждый свою книгу, а стрик — общий."
