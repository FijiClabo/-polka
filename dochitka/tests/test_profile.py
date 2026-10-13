"""Профиль: какое фото видят другие и ссылка на Telegram."""

import io

from PIL import Image
from sqlalchemy import select

from db.models import User
from db.session import session_scope
from services.common import Outbox
from services.social import add_friend_by_code
from tests.test_api import TOKEN, client, make_init_data  # noqa: F401 — фикстура клиента API

A, B = 801, 802


def hdr(tg_id: int, name: str, username: str | None = None, photo: str | None = None) -> dict:
    user = {"id": tg_id, "first_name": name}
    if username:
        user["username"] = username
    if photo:
        user["photo_url"] = photo
    return {"Authorization": "tma " + make_init_data(user, TOKEN)}


def png(w=600, h=400) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 120, 90)).save(buf, "PNG")
    return buf.getvalue()


async def test_photo_choice_and_telegram_link(client, tmp_path, monkeypatch):  # noqa: F811
    from settings import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    ha = hdr(A, "Аня", "anya_reads", "https://t.me/i/userpic/320/anya.svg")
    hb = hdr(B, "Боря", "borya")
    for h in (ha, hb):
        await client.get("/api/me", headers=h)
        await client.post("/api/consent", headers=h)
    async with session_scope() as s:
        a = await s.scalar(select(User).where(User.tg_id == A))
        b = await s.scalar(select(User).where(User.tg_id == B))
        out = Outbox()
        await add_friend_by_code(s, b, a.friend_code, is_new_user=False, outbox=out)
        # в сообщении о новом друге — имя и username, чтобы было понятно, кто это
        assert "Боря (@borya)" in out.messages[0].text
        a_id = a.id

    # по умолчанию друг видит фото из Telegram и ссылку
    f = (await client.get(f"/api/friends/{a_id}", headers=hb)).json()["status"]
    assert f["photo_url"].startswith("https://t.me/") and f["tg"] == "anya_reads"

    # своё фото: квадрат 320×320 по случайному адресу
    me = (await client.post("/api/me/avatar", files={"photo": ("a.png", png(), "image/png")}, headers=ha)).json()
    url = me["user"]["photo_url"]
    assert me["user"]["avatar"] == "custom" and url.startswith("/a/") and url.endswith(".jpg")
    r = await client.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(r.content)).size == (320, 320)
    assert (await client.get("/a/../../etc/passwd.jpg")).status_code == 404
    assert (await client.post("/api/me/avatar", files={"photo": ("a.png", b"not an image", "image/png")},
                              headers=ha)).status_code == 400
    f = (await client.get(f"/api/friends/{a_id}", headers=hb)).json()["status"]
    assert f["photo_url"] == url

    # без фото и без ссылки
    me = (await client.patch("/api/me", json={"avatar": "none", "show_username": False}, headers=ha)).json()
    assert me["user"]["photo_url"] is None and me["user"]["tg"] is None and me["user"]["custom_photo_url"] == url
    f = (await client.get(f"/api/friends/{a_id}", headers=hb)).json()["status"]
    assert f["photo_url"] is None and f["tg"] is None
    friends = (await client.get("/api/friends", headers=hb)).json()
    row = next(x for x in friends["items"] if x["user_id"] == a_id)
    assert row["tg"] is None and row["photo_url"] is None

    # удалить своё фото — файл исчезает
    token_path = tmp_path / "avatars" / url.split("/")[-1]
    assert token_path.exists()
    me = (await client.delete("/api/me/avatar", headers=ha)).json()
    assert me["user"]["custom_photo_url"] is None and not token_path.exists()
    assert (await client.patch("/api/me", json={"avatar": "custom"}, headers=ha)).status_code == 400
