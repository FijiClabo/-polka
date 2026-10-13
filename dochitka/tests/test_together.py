"""«Читаем вместе»: друг берёт план книги напарника без текста и читает свой экземпляр."""

from sqlalchemy import select

from core import clock
from db.models import Book, Enrollment, Pair, Retelling, Segment, User
from db.session import session_scope
from services.books import attach_book, confirm_plan, save_parsed
from services.common import Outbox
from services.runs import ensure_enrollment
from services.social import ensure_pair_code, join_pair_by_code
from services.users import get_or_create_user
from tests.helpers import set_now
from tests.test_api import H, client  # noqa: F401 — фикстура клиента API

A_TG, B_TG = 301, 302


async def _reader_with_plan() -> tuple[int, str]:
    """A: книга из файла, план на 21 день. Возвращает id A и код приглашения в пару."""
    from books.parse import parse_book
    from books.samples import sample_set

    async with session_scope() as s:
        a, _ = await get_or_create_user(s, A_TG, first_name="Аня")
        enr = await ensure_enrollment(s, a)
        book = Book(owner_user_id=a.id, source="epub", title="x", parse_status="pending")
        s.add(book)
        await s.flush()
        await save_parsed(s, book, parse_book("b.epub", sample_set()["01_clean_with_toc.epub"]), "h1")
        await attach_book(s, enr, book)
        await confirm_plan(s, a, enr, 21)
        return a.id, await ensure_pair_code(s, enr)


async def test_read_together_flow(client):  # noqa: F811
    day0 = clock.real_now().date()
    set_now(day0, 10)
    a_id, code = await _reader_with_plan()
    ha, hb = H(A_TG, "Аня"), H(B_TG, "Боря")
    await client.get("/api/me", headers=hb)
    await client.post("/api/consent", headers=hb)

    # A зовёт читать вместе: ссылка t_, в тексте — книга
    inv = (await client.get("/api/pair/invite", headers=ha)).json()
    assert inv["kind"] == "together" and "?start=t_" in inv["link"] and "Аккуратная книга" in inv["text"]

    # обычная ссылка p_ книгу напарнику не раскрывает и план не предлагает
    async with session_scope() as s:
        b = await s.scalar(select(User).where(User.tg_id == B_TG))
        assert (await join_pair_by_code(s, b, code, Outbox()))[0] == "ok"
    assert (await client.get("/api/book", headers=hb)).json()["together"] is None
    # та же пара по ссылке «читать вместе» — теперь можно взять план
    async with session_scope() as s:
        b = await s.scalar(select(User).where(User.tg_id == B_TG))
        assert (await join_pair_by_code(s, b, code, Outbox(), together=True))[0] == "ok"
    offer = (await client.get("/api/book", headers=hb)).json()["together"]
    assert offer["title"] == "Аккуратная книга" and offer["plan_days"] == 21 and offer["partner_name"] == "Аня"

    r = await client.post("/api/book/together", headers=hb)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["book"]["source"] == "shared" and j["plan_days"] == 21
    # повторно взять нельзя — уже та же книга
    assert (await client.post("/api/book/together", headers=hb)).status_code == 409

    async with session_scope() as s:
        b = await s.scalar(select(User).where(User.tg_id == B_TG))
        b_enr = await s.scalar(select(Enrollment).where(Enrollment.user_id == b.id, Enrollment.book_id.is_not(None)))
        b_book = await s.get(Book, b_enr.book_id)
        a_book_id = b_book.shared_from_id
        mine = list(await s.scalars(select(Segment).where(Segment.book_id == b_book.id).order_by(Segment.day_number)))
        src = list(await s.scalars(select(Segment).where(Segment.book_id == a_book_id).order_by(Segment.day_number)))
        # план тот же: дни, главы, проценты; текста у друга нет, а метки начала и конца — есть
        assert [(x.day_number, x.title, x.pos_to) for x in mine] == [(x.day_number, x.title, x.pos_to) for x in src]
        assert all(x.text is None for x in mine) and all(x.start_words and x.end_words for x in mine)
        assert not b_book.has_text
        # для проверки пересказа друга — краткое содержание исходного отрезка, не текст
        src[0].summary = "Краткое содержание первого отрезка."

    # срок плана напарника не пересобрать
    r = await client.post("/api/book/plan", json={"days": 30}, headers=hb)
    assert r.status_code in (400, 409, 422)

    # друг видит план с метками, но не может открыть текст отрезка
    async with session_scope() as s:
        b = await s.scalar(select(User).where(User.tg_id == B_TG))
        b_enr = await s.scalar(select(Enrollment).where(Enrollment.user_id == b.id, Enrollment.book_id.is_not(None)))
        from services.runs import grant

        await grant(s, b, b_enr.run)
    today = (await client.get("/api/today", headers=hb)).json()
    seg = today["segment"]
    assert today["state"] == "to_read" and seg["day_number"] == 1
    assert seg["anchors"] and seg["place"].endswith("% книги") and not seg["can_read"]
    assert (await client.get("/api/read/1", headers=hb)).status_code in (403, 404)

    # напарник видит, что читаете одну книгу, и прогресс обоих
    pa = (await client.get("/api/pair", headers=ha)).json()
    assert pa["has_pair"] and pa["together"]["title"] == "Аккуратная книга"
    assert set(pa["together"]) == {"title", "me", "partner"}

    # проверка пересказа друга опирается на краткое содержание исходной книги
    from services.retell import build_check_input

    async with session_scope() as s:
        b = await s.scalar(select(User).where(User.tg_id == B_TG))
        b_enr = await s.scalar(select(Enrollment).where(Enrollment.user_id == b.id, Enrollment.book_id.is_not(None)))
        seg1 = await s.scalar(select(Segment).where(Segment.book_id == b_enr.book_id, Segment.day_number == 1))
        r = Retelling(enrollment_id=b_enr.id, segment_id=seg1.id, user_day=clock.now().date(),
                      raw_text="пересказ", source="text")
        s.add(r)
        await s.flush()
        inp = await build_check_input(s, r)
        assert inp.segment_text is None and inp.segment_summary == "Краткое содержание первого отрезка."
        pair = await s.get(Pair, b_enr.pair_id)
        assert pair.together
    clock.set_fixed(None)


async def test_together_needs_confirmed_plan(client):  # noqa: F811
    """Пока у напарника нет срока, читать вместе нечего: обычная пара, ссылка p_."""
    async with session_scope() as s:
        a, _ = await get_or_create_user(s, A_TG, first_name="Аня")
        enr = await ensure_enrollment(s, a)
        code = await ensure_pair_code(s, enr)
    inv = (await client.get("/api/pair/invite", headers=H(A_TG, "Аня"))).json()
    assert inv["kind"] == "pair" and "?start=p_" in inv["link"]
    hb = H(B_TG, "Боря")
    await client.get("/api/me", headers=hb)
    await client.post("/api/consent", headers=hb)
    async with session_scope() as s:
        b = await s.scalar(select(User).where(User.tg_id == B_TG))
        assert (await join_pair_by_code(s, b, code, Outbox(), together=True))[0] == "ok"
    assert (await client.get("/api/book", headers=hb)).json()["together"] is None
    assert (await client.post("/api/book/together", headers=hb)).status_code == 409
