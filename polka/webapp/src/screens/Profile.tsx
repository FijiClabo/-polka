import { useEffect, useRef, useState } from "react";
import { api, type Achievement, type Me } from "../api";
import { IBook, IChevron } from "../components/Icons";
import { Avatar, Skeleton, Switch, toast } from "../components/ui";
import { useApi } from "../hooks";
import { useNav } from "../nav";
import { closeApp, openTgLink } from "../tg";

const ZONES: [string, string][] = [
  ["Europe/Kaliningrad", "Калининград (МСК−1)"],
  ["Europe/Moscow", "Москва"],
  ["Europe/Samara", "Самара (+1)"],
  ["Asia/Yekaterinburg", "Екатеринбург (+2)"],
  ["Asia/Omsk", "Омск (+3)"],
  ["Asia/Novosibirsk", "Новосибирск (+4)"],
  ["Asia/Krasnoyarsk", "Красноярск (+4)"],
  ["Asia/Irkutsk", "Иркутск (+5)"],
  ["Asia/Yakutsk", "Якутск (+6)"],
  ["Asia/Vladivostok", "Владивосток (+7)"],
  ["Asia/Magadan", "Магадан (+8)"],
  ["Asia/Kamchatka", "Камчатка (+9)"],
];

const FAQ: [string, string][] = [
  ["Как проверяется пересказ?", "ИИ сверяет твой пересказ с текстом отрезка и проверяет одно: читал ли ты это. Не оценивает стиль и грамотность. Если неясно — задаст один вопрос. Сомнение всегда в твою пользу."],
  ["Что если пропущу день?", "Раз в неделю срабатывает заморозка — стрик не сгорит. Пропущенный отрезок можно догнать на следующий день, а в последние дни и в дни отсрочки — сдавать по два."],
  ["Когда начинается и заканчивается день?", "В 04:00 по твоему времени. Всё, что сдано до четырёх утра, относится к прошедшему дню."],
  ["Кто видит мои пересказы?", "Только ты. Напарник — если вы читаете одну книгу, и только ту часть, которую он уже прочитал сам. Друзья видят книгу, стрик и полку, но не пересказы."],
  ["Бумажная книга?", "Можно. Отрезки — по страницам, проверка мягче: без сверки с текстом, максимум два уточняющих вопроса."],
];

export default function Profile() {
  const nav = useNav();
  const [me, setMe] = useState<Me>(nav.me);
  const ach = useApi<{ items: Achievement[] }>("/achievements");
  const achRef = useRef<HTMLDivElement>(null);
  const deviceTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const [openFaq, setOpenFaq] = useState<number | null>(null);

  useEffect(() => {
    if (nav.route.params?.focus === "ach") setTimeout(() => achRef.current?.scrollIntoView({ behavior: "smooth" }), 300);
  }, [nav.route.params]);

  const save = async (patch: Record<string, unknown>) => {
    try {
      const m = await api.patch<Me>("/me", patch);
      setMe(m);
      nav.refreshMe();
      toast("Сохранено");
    } catch (e) {
      toast((e as Error).message);
    }
  };

  const u = me.user;
  const zones = ZONES.some(([z]) => z === u.timezone) ? ZONES : [[u.timezone, u.timezone] as [string, string], ...ZONES];
  const deviceDiffers = deviceTz && deviceTz !== u.timezone;

  return (
    <div className="screen no-tabs">
      <div className="col" style={{ alignItems: "center", textAlign: "center", marginTop: 6 }}>
        <Avatar name={u.name} url={u.photo_url} size={84} seed={u.id} ring="accent" />
        <h2 style={{ margin: "10px 0 0" }}>{u.name}</h2>
        {me.access.run && <div className="small muted">{me.access.run.title}</div>}
      </div>

      <div className="section-title">Настройки</div>
      <div className="card" style={{ padding: "6px 16px" }}>
        <div className="settings-row">
          <span>Часовой пояс</span>
          <select value={u.timezone} onChange={(e) => save({ timezone: e.target.value })}>
            {zones.map(([z, l]) => (
              <option key={z} value={z}>{l}</option>
            ))}
          </select>
        </div>
        {deviceDiffers && (
          <div className="settings-row">
            <span className="small muted">На телефоне: {deviceTz}</span>
            <button className="link" onClick={() => save({ timezone: deviceTz })}>Взять его</button>
          </div>
        )}
        <div className="settings-row">
          <span>Отрезок утром</span>
          <input type="time" value={u.morning_time} onChange={(e) => e.target.value && save({ morning_time: e.target.value })} />
        </div>
        <div className="settings-row">
          <span>Напоминание вечером</span>
          <input type="time" value={u.evening_time} onChange={(e) => e.target.value && save({ evening_time: e.target.value })} />
        </div>
        <div className="settings-row">
          <span>Пересказ по умолчанию</span>
          <div className="seg-switch" style={{ margin: 0 }}>
            <button className={u.retell_format === "voice" ? "on" : ""} onClick={() => save({ retell_format: "voice" })}>Голос</button>
            <button className={u.retell_format === "text" ? "on" : ""} onClick={() => save({ retell_format: "text" })}>Текст</button>
          </div>
        </div>
        <div className="settings-row">
          <div>
            Толчки от друзей
            <div className="tiny muted">не больше двух в день</div>
          </div>
          <Switch on={u.nudges_enabled} onChange={(v) => save({ nudges_enabled: v })} />
        </div>
      </div>

      <button className="card row mt-12" style={{ width: "100%", textAlign: "left" }} onClick={() => nav.push({ name: "book" })}>
        <IBook size={22} />
        <div className="grow"><b>Книга и план</b></div>
        <IChevron size={18} />
      </button>
      <button
        className="card row"
        style={{ width: "100%", textAlign: "left" }}
        onClick={async () => {
          try {
            const r = await api.get<{ share_url: string }>("/friends/invite");
            openTgLink(r.share_url);
          } catch (e) {
            toast((e as Error).message);
          }
        }}
      >
        <span style={{ fontSize: 20 }}>💌</span>
        <div className="grow">
          <b>Позвать друга</b>
          <div className="small muted">По личной ссылке — сразу в друзья, а новичку — бесплатный спринт</div>
        </div>
        <IChevron size={18} />
      </button>

      <div className="section-title" ref={achRef}>
        <span>Значки</span>
        <span className="tiny muted">{ach.data ? `${ach.data.items.filter((a) => a.earned).length} из 9` : ""}</span>
      </div>
      {ach.data ? (
        <div className="ach-grid">
          {ach.data.items.map((a) => (
            <div key={a.code} className={`ach${a.earned ? " on" : ""}`}>
              <div className="e">{a.emoji}</div>
              <div className="t">{a.title}</div>
              <div className="d">{a.description}</div>
            </div>
          ))}
        </div>
      ) : (
        <Skeleton h={240} />
      )}

      <div className="section-title">Как это работает</div>
      <div className="card" style={{ padding: "4px 16px" }}>
        {FAQ.map(([q, a], i) => (
          <div key={q} className="settings-row" style={{ flexDirection: "column", alignItems: "stretch" }} onClick={() => setOpenFaq(openFaq === i ? null : i)}>
            <div className="row between">
              <span>{q}</span>
              <IChevron size={16} style={{ transform: openFaq === i ? "rotate(90deg)" : undefined, transition: "transform .2s" }} />
            </div>
            {openFaq === i && <p className="small muted" style={{ margin: "8px 0 4px" }}>{a}</p>}
          </div>
        ))}
      </div>

      <p className="tiny muted center mt-24">
        Удалить все данные: команда /delete_me в чате с ботом.{" "}
        <button className="link tiny" onClick={closeApp}>Открыть чат</button>
      </p>
    </div>
  );
}
