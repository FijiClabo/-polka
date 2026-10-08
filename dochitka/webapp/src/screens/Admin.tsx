import { useState } from "react";
import { api, type AdminOverview, type Product, type PromoRow } from "../api";
import { IRefresh } from "../components/Icons";
import { ErrorState, ScreenSkeleton, toast } from "../components/ui";
import { dayMonth, plural } from "../format";
import { useApi } from "../hooks";
import { haptic, openTgLink } from "../tg";

const rub = (n: number) => `${Math.round(n).toLocaleString("ru-RU")} ₽`;
const num = (n: number) => Math.round(n).toLocaleString("ru-RU");
const PRODUCT_TITLE: Record<Product, string> = { run: "одна книга", month: "месяц", year: "год" };
const KIND_TITLE: Record<string, string> = {
  check: "проверка пересказов",
  trial: "пробные пересказы",
  summary: "краткие содержания",
  voice: "распознавание голоса",
};

type Tab = "stats" | "promos" | "access";

export default function Admin() {
  const { data, error, loading, reload } = useApi<AdminOverview>("/admin/overview");
  const [tab, setTab] = useState<Tab>("stats");

  if (error && !data) return <ErrorState message={error} onRetry={reload} />;
  if (loading || !data) return <ScreenSkeleton />;

  return (
    <div className="screen no-tabs admin">
      <div className="row between">
        <h1 className="h-title">Админка</h1>
        <button className="icon-btn" aria-label="Обновить" onClick={() => { haptic("light"); reload(); }}>
          <IRefresh size={18} />
        </button>
      </div>
      <div className="tiny muted">Данные на {new Date(data.generated_at).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" })}</div>

      <div className="seg-switch admin-tabs">
        <button className={tab === "stats" ? "on" : ""} onClick={() => setTab("stats")}>Аналитика</button>
        <button className={tab === "promos" ? "on" : ""} onClick={() => setTab("promos")}>Промокоды</button>
        <button className={tab === "access" ? "on" : ""} onClick={() => setTab("access")}>Доступ</button>
      </div>

      {tab === "stats" && <Stats d={data} />}
      {tab === "promos" && <Promos d={data} reload={reload} />}
      {tab === "access" && <Access />}
    </div>
  );
}

// ------------------------------------------------------------------ аналитика

function Stats({ d }: { d: AdminOverview }) {
  const first = d.funnel[0]?.n || 0;
  const s = d.sales;
  return (
    <>
      <div className="kpi-grid mt-16">
        <Kpi label="Пользователей" value={num(d.users.total)} note={`+${d.users.new_7d} за 7 дней`} />
        <Kpi label="Сдавали дни за 7 дней" value={num(d.users.active_7d)} note={`за сутки: ${d.users.active_today}`} />
        <Kpi label="Выручка за 30 дней" value={rub(s.month.rub)} note={`всего: ${rub(s.total.rub)}`} />
        <Kpi label="ИИ за 7 дней" value={rub(d.ai.week.rub)}
          note={d.ai.per_reader_week != null ? `на читателя: ${rub(d.ai.per_reader_week)}` : "читателей пока нет"} />
      </div>

      <div className="section-title">По дням, последние 2 недели</div>
      <div className="chart-grid">
        <MiniBars title="Новые люди" data={d.daily} pick={(x) => x.new_users} fmt={num} />
        <MiniBars title="Сдали день" data={d.daily} pick={(x) => x.active} fmt={num} />
        <MiniBars title="Выручка" data={d.daily} pick={(x) => x.revenue} fmt={rub} />
        <MiniBars title="Расходы на ИИ" data={d.daily} pick={(x) => x.ai_rub} fmt={rub} />
      </div>

      <div className="section-title">Воронка</div>
      <div className="card">
        {d.funnel.map((f) => <HBar key={f.title} title={f.title} n={f.n} of={first} />)}
        <p className="tiny muted mt-8">Уникальные люди за всё время. Процент — от нажавших /start.</p>
      </div>

      <div className="section-title">Удержание</div>
      <div className="card">
        {d.retention.map((f) => <HBar key={f.title} title={f.title} n={f.n} of={d.retention[0]?.n || 0} />)}
        <p className="tiny muted mt-8">Сколько дней сдал человек в лучшем забеге. Процент — от сдавших хотя бы день.</p>
      </div>

      <div className="section-title">Продажи</div>
      <div className="card">
        <table className="admin-table">
          <thead>
            <tr><th>Период</th><th>Оплат</th><th>Выручка</th><th>Платили</th></tr>
          </thead>
          <tbody>
            {([["Сутки", s.today], ["7 дней", s.week], ["30 дней", s.month], ["Всего", s.total]] as const).map(([t, x]) => (
              <tr key={t}><td>{t}</td><td>{x.count}</td><td>{rub(x.rub)}</td><td>{x.payers}</td></tr>
            ))}
          </tbody>
        </table>
        <div className="tiny muted mt-8">
          По тарифам (всего): {Object.entries(s.total.by_product).map(([k, v]) => `${PRODUCT_TITLE[k as Product]} — ${v}`).join(", ") || "—"}.
          {" "}Средний чек на платящего: {rub(d.arppu)}. Возвратов: {s.total.refunds}.
        </div>
        <div className="tiny muted mt-8">
          Сейчас с абонементом: {d.access.subscriptions}. Неиспользованных оплат книги: {d.access.credits}.
          Получали доступ (оплата, промокод, вручную): {d.access.with_access}.
        </div>
      </div>

      <div className="section-title">Пересказы</div>
      <div className="card">
        <div className="row between small"><span>За 7 дней</span><b>{num(d.users.retellings_7d)}</b></div>
        <div className="row between small mt-8"><span>Голосом</span><b>{Math.round(d.users.voice_share_7d * 100)}%</b></div>
      </div>

      <div className="section-title">Источники</div>
      <div className="card">
        <table className="admin-table">
          <thead>
            <tr><th>Откуда</th><th>Пришли</th><th>Заплатили</th></tr>
          </thead>
          <tbody>
            {d.sources.map((x) => (
              <tr key={x.source}><td className="ellipsis">{x.source}</td><td>{x.users}</td><td>{x.payers}</td></tr>
            ))}
          </tbody>
        </table>
        <p className="tiny muted mt-8">Метка ставится по ссылке: промокод (promo_КОД), реклама (src_метка) или друг.</p>
      </div>

      <div className="section-title">Расходы на ИИ</div>
      <div className="card">
        <table className="admin-table">
          <thead>
            <tr><th>Период</th><th>Сумма</th><th>Голос, мин</th></tr>
          </thead>
          <tbody>
            {([["Сутки", d.ai.today], ["7 дней", d.ai.week], ["30 дней", d.ai.month], ["Всего", d.ai.total]] as const).map(([t, x]) => (
              <tr key={t}><td>{t}</td><td>{rub(x.rub)}</td><td>{num(x.voice_min)}</td></tr>
            ))}
          </tbody>
        </table>
        <div className="tiny muted mt-8">
          За 30 дней: {Object.entries(d.ai.month.by_kind).map(([k, v]) => `${KIND_TITLE[k] ?? k} — ${rub(v)}`).join(", ") || "расходов пока нет"}.
        </div>
        <p className="tiny muted mt-8">Оценка по ценам из настроек (YANDEXGPT_RUB_PER_1K и др.). Точная сумма — в биллинге Yandex Cloud.</p>
      </div>

      <div className="section-title">Настройки продаж</div>
      <div className="card small">
        <div className="row between"><span>Одна книга</span><b>{rub(d.prices.run)}</b></div>
        <div className="row between mt-8"><span>Месяц</span><b>{rub(d.prices.month)}</b></div>
        <div className="row between mt-8"><span>Год</span><b>{rub(d.prices.year)}</b></div>
        <div className="row between mt-8"><span>Оплата через ЮKassa</span><b>{d.payments_enabled ? "включена" : "не подключена"}</b></div>
        <div className="row between mt-8"><span>Бесплатный спринт на 7 дней</span><b>{d.sprint_for_everyone ? "всем новичкам" : "по ссылке друга"}</b></div>
        <p className="tiny muted mt-8">Цены и эти настройки меняются в файле .env на сервере.</p>
      </div>
    </>
  );
}

function Kpi({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="kpi">
      <div className="kpi-l">{label}</div>
      <div className="kpi-v">{value}</div>
      {note && <div className="kpi-n">{note}</div>}
    </div>
  );
}

type Day = AdminOverview["daily"][number];

function MiniBars({ title, data, pick, fmt }: { title: string; data: Day[]; pick: (d: Day) => number; fmt: (n: number) => string }) {
  const [sel, setSel] = useState<number | null>(null);
  const values = data.map(pick);
  const max = Math.max(...values, 0);
  const total = values.reduce((a, b) => a + b, 0);
  const i = sel ?? values.length - 1;
  return (
    <div className="mini-chart">
      <div className="mc-title">{title}</div>
      <div className="mc-value">{fmt(values[i] ?? 0)}</div>
      <div className="mc-sub">{sel === null ? "сегодня" : dayMonth(data[i].date)}</div>
      <div className="mc-sub">за 2 недели: {fmt(total)}</div>
      <div className="mc-bars" role="img" aria-label={`${title}: ${values.map((v, k) => `${dayMonth(data[k].date)} — ${fmt(v)}`).join("; ")}`}>
        {values.map((v, k) => (
          <button
            key={data[k].date}
            className={`mc-bar${k === i ? " on" : ""}`}
            onClick={() => setSel(k === sel ? null : k)}
            aria-label={`${dayMonth(data[k].date)}: ${fmt(v)}`}
          >
            <i style={{ height: max > 0 ? `${Math.max(v > 0 ? 6 : 0, (v / max) * 100)}%` : 0 }} />
          </button>
        ))}
      </div>
      <div className="mc-axis"><span>{dayMonth(data[0]?.date)}</span><span>{dayMonth(data[data.length - 1]?.date)}</span></div>
    </div>
  );
}

function HBar({ title, n, of }: { title: string; n: number; of: number }) {
  const pct = of > 0 ? Math.round((n / of) * 100) : 0;
  return (
    <div className="hbar">
      <div className="row between small">
        <span>{title}</span>
        <span className="hbar-n"><b>{num(n)}</b>{of > 0 && <span className="muted"> · {pct}%</span>}</span>
      </div>
      <div className="hbar-track"><i style={{ width: `${Math.min(100, pct)}%` }} /></div>
    </div>
  );
}

// ------------------------------------------------------------------ промокоды

type Kind = "discount" | "free" | "trial";

function Promos({ d, reload }: { d: AdminOverview; reload: () => void }) {
  const [kind, setKind] = useState<Kind>("trial");
  const [code, setCode] = useState("");
  const [discount, setDiscount] = useState("20");
  const [days, setDays] = useState("14");
  const [limit, setLimit] = useState("");
  const [owner, setOwner] = useState("");
  const [products, setProducts] = useState<Product[]>(["month"]);
  const [busy, setBusy] = useState(false);

  const toggleProduct = (p: Product) =>
    setProducts((cur) => (cur.includes(p) ? cur.filter((x) => x !== p) : [...cur, p]));

  const create = async () => {
    setBusy(true);
    try {
      const row = await api.post<PromoRow>("/admin/promos", {
        code: code.trim(),
        discount: kind === "discount" ? Number(discount) : 100,
        trial_days: kind === "trial" ? Number(days) : null,
        products: kind === "trial" ? ["month"] : products,
        max_uses: limit.trim() ? Number(limit) : null,
        owner: owner.trim() || null,
      });
      haptic("success");
      toast(`Промокод ${row.code} готов`);
      setCode("");
      reload();
    } catch (e) {
      haptic("error");
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="section-title">Новый промокод</div>
      <div className="card col" style={{ gap: 12 }}>
        <div className="seg-switch" style={{ margin: 0 }}>
          <button className={kind === "trial" ? "on" : ""} onClick={() => setKind("trial")}>Пробный</button>
          <button className={kind === "free" ? "on" : ""} onClick={() => setKind("free")}>Бесплатно</button>
          <button className={kind === "discount" ? "on" : ""} onClick={() => setKind("discount")}>Скидка</button>
        </div>
        <p className="tiny muted" style={{ margin: 0 }}>
          {kind === "trial"
            ? "Бесплатный абонемент на выбранное число дней — для тестеров и пробного периода."
            : kind === "free"
              ? "Тариф целиком бесплатно (100%): одна книга, месяц или год."
              : "Скидка в процентах на выбранные тарифы — для блогеров и акций."}
        </p>
        <div className="field">
          <label>Код — латиница и цифры</label>
          <input className="input" value={code} onChange={(e) => setCode(e.target.value.toUpperCase())} placeholder="PILOT" autoCapitalize="characters" />
        </div>
        {kind === "trial" && (
          <div className="field">
            <label>Сколько дней доступа</label>
            <input className="input" inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value.replace(/\D/g, ""))} />
          </div>
        )}
        {kind === "discount" && (
          <div className="field">
            <label>Скидка, %</label>
            <input className="input" inputMode="numeric" value={discount} onChange={(e) => setDiscount(e.target.value.replace(/\D/g, ""))} />
          </div>
        )}
        {kind !== "trial" && (
          <div className="field">
            <label>Тарифы</label>
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              {(["run", "month", "year"] as Product[]).map((p) => (
                <button key={p} className={`chip-toggle${products.includes(p) ? " on" : ""}`} onClick={() => toggleProduct(p)}>
                  {PRODUCT_TITLE[p]}
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="field">
          <label>Сколько раз можно использовать (пусто — без лимита)</label>
          <input className="input" inputMode="numeric" value={limit} onChange={(e) => setLimit(e.target.value.replace(/\D/g, ""))} placeholder="30" />
        </div>
        <div className="field">
          <label>Чей код (необязательно) — для учёта партнёров</label>
          <input className="input" value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="@blogger" />
        </div>
        <button className="btn primary block" disabled={busy || code.trim().length < 2} onClick={create}>Создать</button>
      </div>

      <div className="section-title">Все промокоды</div>
      {d.promos.length === 0 && <p className="small muted">Пока нет ни одного.</p>}
      {d.promos.map((p) => <PromoCard key={p.code} p={p} reload={reload} />)}
    </>
  );
}

function PromoCard({ p, reload }: { p: PromoRow; reload: () => void }) {
  const what = p.days
    ? `пробный доступ на ${p.days} ${plural(p.days, "день", "дня", "дней")}`
    : p.discount >= 100
      ? `бесплатно: ${p.products.map((x) => PRODUCT_TITLE[x]).join(", ")}`
      : `−${p.discount}%: ${p.products.map((x) => PRODUCT_TITLE[x]).join(", ")}`;
  return (
    <div className={`card promo-card${p.active ? "" : " off"}`}>
      <div className="row between">
        <b className="promo-code">{p.code}</b>
        <span className={`badge ${p.active ? "ok" : ""}`}>{p.active ? "действует" : "выключен"}</span>
      </div>
      <div className="small muted mt-8">{what}{p.owner ? ` · ${p.owner}` : ""}</div>
      <div className="small mt-8">
        Использован {p.used}{p.max_uses ? ` из ${p.max_uses}` : ""} · пришли по ссылке: {p.came}
        {p.discount < 100 && ` · оплат: ${p.purchases}${p.revenue ? ` на ${rub(p.revenue)}` : ""}`}
      </div>
      <div className="tiny muted mt-8 ellipsis">{p.link}</div>
      <div className="btn-row mt-12">
        <button className="btn secondary" onClick={() => openTgLink(`https://t.me/share/url?url=${encodeURIComponent(p.link)}`)}>Поделиться</button>
        <button
          className="btn ghost"
          onClick={async () => {
            try {
              await api.post(`/admin/promos/${p.code}/toggle`);
              reload();
            } catch (e) {
              toast((e as Error).message);
            }
          }}
        >
          {p.active ? "Выключить" : "Включить"}
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ выдать доступ

function Access() {
  const [who, setWho] = useState("");
  const [what, setWhat] = useState<"run" | "month" | "year" | "days">("days");
  const [days, setDays] = useState("14");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ name: string; until: string | null; credits: number }>("/admin/grant", {
        user: who.trim(),
        product: what,
        days: what === "days" ? Number(days) : null,
      });
      haptic("success");
      toast(r.until ? `${r.name}: доступ до ${dayMonth(r.until)}` : `${r.name}: одна книга открыта`);
      setWho("");
    } catch (e) {
      haptic("error");
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <div className="section-title">Выдать доступ вручную</div>
      <div className="card col" style={{ gap: 12 }}>
        <div className="field">
          <label>Кому — @username или Telegram ID</label>
          <input className="input" value={who} onChange={(e) => setWho(e.target.value)} placeholder="@username" autoCapitalize="none" />
        </div>
        <div className="field">
          <label>Что выдать</label>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            {([["days", "N дней"], ["run", "одна книга"], ["month", "месяц"], ["year", "год"]] as const).map(([k, t]) => (
              <button key={k} className={`chip-toggle${what === k ? " on" : ""}`} onClick={() => setWhat(k)}>{t}</button>
            ))}
          </div>
        </div>
        {what === "days" && (
          <div className="field">
            <label>Сколько дней</label>
            <input className="input" inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value.replace(/\D/g, ""))} />
          </div>
        )}
        <button className="btn primary block" disabled={busy || !who.trim()} onClick={submit}>Выдать</button>
        <p className="tiny muted" style={{ margin: 0 }}>
          Человек должен хотя бы раз нажать /start в боте. Срок абонемента прибавляется к оставшемуся; бот сам напишет, что доступ открыт.
        </p>
      </div>
    </>
  );
}
