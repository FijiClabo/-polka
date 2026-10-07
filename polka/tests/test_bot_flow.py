"""Сквозной прогон бота без Telegram: подменённая сессия aiogram записывает исходящие запросы."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetFile, GetMe, SendDocument, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Document, File, Message, Update, User

from books.samples import sample_set
from core import clock
from tests.helpers import RETELL_OK, install_fake_llm, set_now, setup_db

_ids = itertools.count(1)


class MockSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list[TelegramMethod] = []
        self.files: dict[str, bytes] = {}

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="Dochitka", username="dochitka_test_bot")
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id=method.file_id, file_path=method.file_id)
        if isinstance(method, (SendMessage, SendPhoto, SendDocument)):
            return Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=method.chat_id, type="private"),
                           text=getattr(method, "text", None))
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        key = url.rsplit("/", 1)[-1]
        yield self.files.get(key, b"")

    async def close(self):
        pass

    def texts(self) -> list[str]:
        return [m.text for m in self.sent if isinstance(m, SendMessage)]

    def last_markup_callbacks(self) -> list[str]:
        for m in reversed(self.sent):
            if isinstance(m, SendMessage) and m.reply_markup is not None and hasattr(m.reply_markup, "inline_keyboard"):
                return [b.callback_data for row in m.reply_markup.inline_keyboard for b in row if b.callback_data]
        return []


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TG_IDS", "900")
    monkeypatch.setenv("WEBAPP_URL", "https://dochitka.example.com/app")
    from settings import get_settings

    get_settings.cache_clear()
    await setup_db(tmp_path)
    install_fake_llm()
    from ai.stt import set_stt

    set_stt([])
    from bot.app import create_dispatcher
    from bot.ui import set_bot_username

    set_bot_username("dochitka_test_bot")
    session = MockSession()
    bot = Bot("123456:TEST-TOKEN", session=session)
    from aiogram.client.default import DefaultBotProperties

    bot.default = DefaultBotProperties(parse_mode="HTML")
    dp = create_dispatcher()
    yield bot, dp, session
    clock.set_fixed(None)
    get_settings.cache_clear()


def tg_user(uid: int, name: str = "Саша") -> User:
    return User(id=uid, is_bot=False, first_name=name, language_code="ru")


async def send_text(bot, dp, uid, text, name="Саша"):
    msg = Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=uid, type="private"),
                  from_user=tg_user(uid, name), text=text)
    await dp.feed_update(bot, Update(update_id=next(_ids), message=msg))


async def press(bot, dp, uid, data, name="Саша"):
    bot_msg = Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=uid, type="private"),
                      from_user=User(id=1, is_bot=True, first_name="Dochitka"), text="…")
    cq = CallbackQuery(id=str(next(_ids)), from_user=tg_user(uid, name), chat_instance="x", data=data, message=bot_msg)
    await dp.feed_update(bot, Update(update_id=next(_ids), callback_query=cq))


async def send_doc(bot, dp, session, uid, filename, data):
    fid = f"file{next(_ids)}"
    session.files[fid] = data
    msg = Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=uid, type="private"),
                  from_user=tg_user(uid), document=Document(file_id=fid, file_unique_id=fid, file_name=filename,
                                                            file_size=len(data)))
    await dp.feed_update(bot, Update(update_id=next(_ids), message=msg))


async def test_full_flow(env):
    bot, dp, session = env
    start = datetime.now().date() + timedelta(days=2)
    set_now(start - timedelta(days=2), 12)
    ADMIN, U = 900, 501

    # ведущий создаёт забег
    await send_text(bot, dp, ADMIN, "/start", "Ведущий")
    await send_text(bot, dp, ADMIN, f"/run_new Осенний забег | {start.isoformat()} | 990", "Ведущий")
    assert any("создан" in t for t in session.texts())

    # онбординг участника
    await send_text(bot, dp, U, "/start")
    assert "Привет" in session.texts()[-1]
    await press(bot, dp, U, "ob:1")
    await press(bot, dp, U, "ob:2")
    await press(bot, dp, U, "ob:go")
    assert "согласие на обработку данных" in session.texts()[-1]
    assert "consent:pd" in session.last_markup_callbacks()
    await press(bot, dp, U, "consent:pd")
    assert any("Где ты живёшь" in t for t in session.texts())
    await press(bot, dp, U, "tz:Asia/Yekaterinburg")
    await press(bot, dp, U, "mt:08:00")
    await press(bot, dp, U, "et:other")
    await send_text(bot, dp, U, "22:30")
    assert any("Чехов" in t or "Червяков" in t for t in session.texts())
    await send_text(bot, dp, U, "Червяков в театре чихнул на лысину генерала Бризжалова и сильно смутился, начал думать.")
    texts = session.texts()
    assert any("Засчитано" in t for t in texts)
    assert any("Теперь книга" in t for t in texts)

    # книга файлом
    await send_doc(bot, dp, session, U, "book.epub", sample_set()["01_clean_with_toc.epub"])
    assert any("Готово" in t and "Аккуратная книга" in t for t in session.texts())
    cbs = session.last_markup_callbacks()
    assert "plan:21" in cbs
    await press(bot, dp, U, "plan:21")
    assert any("План готов" in t for t in session.texts())
    assert "Осталось открыть доступ" in session.texts()[-1]  # пейвол
    assert "pay:run:stars" in session.last_markup_callbacks()

    # плохой файл
    await send_doc(bot, dp, session, U, "scan.epub", sample_set()["07_scanned_images.epub"])
    assert any("картинок" in t for t in session.texts())

    # оплата
    await send_text(bot, dp, ADMIN, "/grant 501", "Ведущий")
    assert any("Оплата получена" in t for t in session.texts())

    # старт: пересказ
    set_now(start, 10)
    await send_text(bot, dp, U, "/today")
    assert any("День 1 из 21" in t for t in session.texts())
    await send_text(bot, dp, U, "коротко")
    assert "от 50 знаков" in session.texts()[-1]
    await send_text(bot, dp, U, RETELL_OK)
    assert any("Засчитано" in t and "Стрик: <b>1</b>" in t for t in session.texts())
    await send_text(bot, dp, U, RETELL_OK)
    assert "На сегодня хватит" in session.texts()[-1]

    # социальное
    await send_text(bot, dp, U, "/friends")
    assert any("личная ссылка" in t for t in session.texts())
    await send_text(bot, dp, U, "/pair")
    assert any("Позови напарника" in t for t in session.texts())
    await send_text(bot, dp, U, "/settings")
    await press(bot, dp, U, "cfg:nudges")
    assert "Толчки выключены" in session.texts()[-1]

    # админ
    await send_text(bot, dp, ADMIN, "/stats", "Ведущий")
    assert any("Средний стрик" in t for t in session.texts())
    await send_text(bot, dp, ADMIN, "/user 501", "Ведущий")
    assert any("Последние пересказы" in t for t in session.texts())
    await send_text(bot, dp, ADMIN, "/export", "Ведущий")
    assert any(isinstance(m, SendDocument) for m in session.sent)
    await send_text(bot, dp, U, "/stats")
    assert "только для ведущего" in session.texts()[-1]

    # удаление данных
    await send_text(bot, dp, U, "/delete_me")
    await press(bot, dp, U, "del:yes")
    assert "всё удалено" in session.texts()[-1]


async def test_paper_book_flow(env):
    bot, dp, session = env
    set_now(datetime.now().date(), 12)
    from db.models import Run
    from db.session import session_scope

    async with session_scope() as s:
        s.add(Run(title="T", kind="main", start_date=datetime.now().date() + timedelta(days=3), status="open"))
    await send_text(bot, dp, 601, "/start")
    await press(bot, dp, 601, "book:paper")
    await send_text(bot, dp, 601, "Мастер и Маргарита")
    await send_text(bot, dp, 601, "Михаил Булгаков")
    await send_text(bot, dp, 601, "десять")
    assert "Нужно число" in session.texts()[-1]
    await send_text(bot, dp, 601, "480")
    assert "Выбери срок" in session.texts()[-1]
    assert "plan:45" in session.last_markup_callbacks()
    await press(bot, dp, 601, "plan:45")
    assert any("План готов" in t for t in session.texts())


async def test_self_serve_paid_flow(env):
    """Без ведущего: книга → план → пейвол → счёт в Stars → оплата → день 1 → возврат по гарантии."""
    from aiogram.methods import AnswerPreCheckoutQuery, RefundStarPayment, SendInvoice
    from aiogram.types import PreCheckoutQuery, SuccessfulPayment

    bot, dp, session = env
    day = datetime.now().date()
    set_now(day, 10)
    U = 777
    await send_text(bot, dp, U, "/start")
    for cb in ("ob:1", "ob:2", "ob:go", "consent:pd", "tz:Europe/Moscow", "mt:08:00", "et:21:00"):
        await press(bot, dp, U, cb)
    await send_text(bot, dp, U, "Червяков в театре чихнул на лысину генерала Бризжалова и сильно смутился, начал думать.")
    assert any("Теперь книга" in t for t in session.texts())

    await send_doc(bot, dp, session, U, "book.epub", sample_set()["01_clean_with_toc.epub"])
    await press(bot, dp, U, "plan:21")
    assert "Осталось открыть доступ" in session.texts()[-1]

    await press(bot, dp, U, "pay:run:stars")
    inv = next(m for m in reversed(session.sent) if isinstance(m, SendInvoice))
    assert inv.currency == "XTR" and inv.prices[0].amount > 0

    q = PreCheckoutQuery(id="pc1", from_user=tg_user(U), currency="XTR", total_amount=inv.prices[0].amount,
                         invoice_payload=inv.payload)
    await dp.feed_update(bot, Update(update_id=next(_ids), pre_checkout_query=q))
    ans = next(m for m in reversed(session.sent) if isinstance(m, AnswerPreCheckoutQuery))
    assert ans.ok is True

    sp = SuccessfulPayment(currency="XTR", total_amount=inv.prices[0].amount, invoice_payload=inv.payload,
                           telegram_payment_charge_id="tg-charge-1", provider_payment_charge_id="")
    msg = Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=U, type="private"), from_user=tg_user(U),
                  successful_payment=sp)
    await dp.feed_update(bot, Update(update_id=next(_ids), message=msg))
    assert any("Оплата прошла" in t for t in session.texts())
    assert any("День 1 из 21" in t for t in session.texts())

    # повторная доставка того же платежа ничего не дублирует
    await dp.feed_update(bot, Update(update_id=next(_ids), message=msg))
    from sqlalchemy import func, select

    from db.models import Purchase
    from db.session import session_scope

    async with session_scope() as s:
        assert await s.scalar(select(func.count(Purchase.id))) == 1

    await send_text(bot, dp, U, "/money_back")
    assert "mb:yes" in session.last_markup_callbacks()
    await press(bot, dp, U, "mb:yes")
    assert any(isinstance(m, RefundStarPayment) and m.telegram_payment_charge_id == "tg-charge-1" for m in session.sent)
    assert "деньги возвращены" in session.texts()[-1]
    await send_text(bot, dp, U, "/today")
    assert "День 1 из 21" not in session.texts()[-1]
