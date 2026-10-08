import { useEffect, useRef, useState } from "react";
import { api, type Achievement, type Billing, type Me } from "../api";
import { AchIcon, IBook, IChevron, IEnvelope } from "../components/Icons";
import { Avatar, Skeleton, Switch, toast } from "../components/ui";
import { useApi } from "../hooks";
import { useNav } from "../nav";
import { closeApp, openExternal, openTgLink } from "../tg";
import { dayMonth } from "../format";

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
  ["Как проверяется пересказ?", "ИИ сверяет твой пересказ с текстом отрезка и проверяет одно: прочитан ли отрезок. Не оценивает стиль и грамотность. Если неясно — задаст один вопрос. Сомнение всегда в твою пользу."],
  ["Что если пропущу день?", "Раз в неделю срабатывает заморозка — стрик не сгорит. Пропущенный отрезок можно догнать на следующий день, а в последние дни и в дни отсрочки — сдавать по два."],
  ["Когда начинается и заканчивается день?", "В 04:00 по твоему времени. Всё, что сдано до четырёх утра, относится к прошедшему дню."],
  ["Кто видит мои пересказы?", "Никто из людей. Пересказ проходит только автоматическую проверку и сразу удаляется — остаётся лишь отметка, что день сдан. Напарник видит твой прогресс, друзья — книгу, стрик, значки и полку."],
  ["Сколько стоит?", "Одна книга — разовая оплата за забег до финиша. Абонемент на месяц или год — книга за книгой без доплат. Оплата разовая, без автопродления. Первый раз можно попробовать бесплатный спринт на 7 дней."],
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
        <h2 className="h-title" style={{ margin: "10px 0 0", fontSize: 30 }}>{u.name}</h2>
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
        <IEnvelope size={22} />
        <div className="grow">
          <b>Позвать друга</b>
          <div className="small muted">По личной ссылке — сразу в друзья, а новичку — бесплатный спринт</div>
        </div>
        <IChevron size={18} />
      </button>

      <AccessCard />

      <div className="section-title" ref={achRef}>
        <span>Значки</span>
        <span className="tiny muted">{ach.data ? `${ach.data.items.filter((a) => a.earned).length} из 9` : ""}</span>
      </div>
      {ach.data ? (
        <div className="ach-grid">
          {ach.data.items.map((a) => (
            <div key={a.code} className={`ach${a.earned ? " on" : ""}`}>
              <div className="e"><AchIcon code={a.code} /></div>
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

function AccessCard() {
  const nav = useNav();
  const { data } = useApi<Billing>("/billing");
  if (!data) return <Skeleton h={90} />;
  const sub = data.subscription;
  return (
    <>
      <div className="section-title">Доступ</div>
      <div className="card sub-card">
        <div className="row between">
          <span>Абонемент</span>
          <b className="small">{sub.active ? `до ${dayMonth(sub.until)}` : "нет"}</b>
        </div>
        {data.credits > 0 && (
          <div className="row between">
            <span>Оплаченные забеги</span>
            <b className="small">{data.credits}</b>
          </div>
        )}
        {data.purchases.length > 0 && (
          <div className="tiny muted mt-8">
            {data.purchases.slice(0, 3).map((p) => (
              <div key={p.id}>
                {dayMonth(p.date)} · {p.product === "run" ? "одна книга" : p.product === "month" ? "месяц" : "год"} · {p.amount}
              </div>
            ))}
          </div>
        )}
        <div className="btn-row mt-12">
          <button className="btn secondary" onClick={() => nav.push({ name: "pay" })}>{sub.active ? "Тарифы" : "Открыть доступ"}</button>
        </div>
        {(data.offer_url || data.privacy_url) && (
          <div className="tiny muted mt-8">
            {data.offer_url && <button className="link tiny" onClick={() => openExternal(data.offer_url!)}>Оферта</button>}
            {data.offer_url && data.privacy_url && " · "}
            {data.privacy_url && <button className="link tiny" onClick={() => openExternal(data.privacy_url!)}>Политика данных</button>}
          </div>
        )}
      </div>
    </>
  );
}
