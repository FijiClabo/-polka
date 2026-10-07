import { useEffect, useRef, useState } from "react";
import { api, type Billing, type Product } from "../api";
import { IArrow, ICheck } from "../components/Icons";
import { Cover, ErrorState, ScreenSkeleton, toast } from "../components/ui";
import { dayMonth, days } from "../format";
import { invalidate, useApi } from "../hooks";
import { useNav } from "../nav";
import { haptic, openExternal, openInvoice } from "../tg";

export const rub = (n: number) => `${Math.round(n).toLocaleString("ru-RU")} ₽`;
const stars = (n: number) => `${Math.round(n).toLocaleString("ru-RU")} ⭐`;
type Unit = "rub" | "stars";

const INCLUDED = [
  "План под твою книгу: 15 минут чтения в день",
  "ИИ проверяет каждый пересказ — голосом или текстом",
  "Стрик, заморозки и 3 дня отсрочки в конце",
  "Напарник и друзья — вместе дочитывают чаще",
  "Конспект из твоих пересказов и книга на полке",
];

export default function Pay() {
  const nav = useNav();
  const { data, error, loading, reload } = useApi<Billing>("/billing");
  const [product, setProduct] = useState<Product>("run");
  const [busy, setBusy] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [entry, setEntry] = useState<null | "promo" | "code">(null);
  const [code, setCode] = useState("");
  const alive = useRef(true);

  useEffect(() => () => { alive.current = false; }, []);

  if (error && !data) return <ErrorState message={error} onRetry={reload} />;
  if (loading || !data) return <ScreenSkeleton />;

  const done = () => {
    invalidate();
    nav.refreshMe();
    nav.tab("today");
  };

  // Telegram сообщает «paid» раньше, чем до сервера доходит подтверждение оплаты: ждём его до ~30 секунд
  const waitForAccess = async (before: Billing) => {
    setWaiting(true);
    for (let i = 0; i < 20 && alive.current; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      try {
        const b = await api.get<Billing>("/billing");
        const changed = b.credits !== before.credits || b.subscription.until !== before.subscription.until || b.needs_access !== before.needs_access;
        if (changed) {
          haptic("success");
          toast("Оплата прошла — спасибо! 🔥");
          done();
          return;
        }
      } catch {
        /* сеть мигнула — пробуем ещё */
      }
    }
    if (alive.current) {
      setWaiting(false);
      toast("Оплата обрабатывается — подтверждение придёт в чат");
      done();
    }
  };

  const pay = async (method: "card" | "stars") => {
    setBusy(true);
    haptic("medium");
    try {
      const { url } = await api.post<{ url: string }>("/billing/invoice", { product, method });
      const status = await openInvoice(url);
      if (status === "paid") await waitForAccess(data);
      else if (status === "failed") toast("Платёж не прошёл. Попробуй другой способ.");
      else if (status === "pending") toast("Платёж в обработке — напишем в чат, как только он пройдёт");
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const applyPromo = async () => {
    if (!code.trim()) return;
    setBusy(true);
    try {
      await api.post<Billing>("/billing/promo", { code: code.trim() });
      haptic("success");
      toast("Промокод применён");
      setEntry(null);
      setCode("");
      reload();
    } catch (e) {
      haptic("error");
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const redeem = async () => {
    if (!code.trim()) return;
    setBusy(true);
    try {
      await api.post("/billing/redeem", { code: code.trim() });
      haptic("success");
      toast("Код активирован 🎉");
      done();
    } catch (e) {
      haptic("error");
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const activateFree = async () => {
    setBusy(true);
    try {
      await api.post("/billing/free", { product });
      haptic("success");
      toast("Доступ открыт 🎉");
      done();
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const sub = data.subscription;
  if (sub.active && !data.needs_access) {
    return (
      <div className="screen no-tabs">
        <h1 className="h-title">Абонемент</h1>
        <div className="card book-card center">
          <div style={{ fontSize: 44 }}>🔥</div>
          <div className="seg-title">Всё открыто</div>
          <p className="meta">
            Абонемент действует до {dayMonth(sub.until)}{sub.recurring ? " и продлевается сам — отменить можно в настройках Telegram" : ""}.
            Книга за книгой без доплат.
          </p>
          <button className="btn primary block mt-12" onClick={done}>К чтению <IArrow size={18} /></button>
        </div>
      </div>
    );
  }

  const p = data.prices;
  const sel = p[product];
  // только звёзды — показываем цены в звёздах, иначе в рублях
  const unit: Unit = !data.methods.card && data.methods.stars ? "stars" : "rub";
  const val = (x: Billing["prices"]["run"]) => (unit === "rub" ? x.rub : x.stars);
  const fmt = unit === "rub" ? rub : stars;
  const perDay = product === "run" && data.plan_days ? val(sel) / data.plan_days : null;
  const yearMonthly = val(p.year) / 12;

  return (
    <div className="screen no-tabs pay">
      <h1 className="h-title">{data.needs_access && data.plan_days ? "Последний шаг" : "Тарифы"}</h1>

      {data.book && data.plan_days && (
        <div className="card row" style={{ gap: 14 }}>
          <Cover title={data.book.title} author={data.book.author} color={data.book.spine_color} small />
          <div className="grow">
            <div className="eyebrow">План готов</div>
            <b>{data.book.title}</b>
            <div className="small muted">{days(data.plan_days)} · старт сразу после оплаты</div>
          </div>
        </div>
      )}

      {!data.enabled && !sel.free ? (
        <div className="card mt-16">
          <b>Онлайн-оплата скоро появится</b>
          <p className="small muted" style={{ whiteSpace: "pre-line" }}>{data.manual_info}</p>
        </div>
      ) : (
        <>
          <div className="section-title">Выбери формат</div>
          <PlanOption
            on={product === "run"} onPick={() => setProduct("run")}
            title="Одна книга" price={p.run} sub="Забег до финиша: план, проверка, напарник, конспект"
            unit={unit} extra={perDay ? `≈ ${fmt(perDay)} в день` : undefined}
          />
          <PlanOption
            on={product === "month"} onPick={() => setProduct("month")}
            title="Месяц" price={p.month} unit={unit} suffix="/мес" sub="Книга за книгой без доплат + 2 заморозки в неделю"
          />
          <PlanOption
            on={product === "year"} onPick={() => setProduct("year")}
            title="Год" price={p.year} unit={unit} suffix="/год" sub={`≈ ${fmt(yearMonthly)} в месяц — дешевле всего`}
            badge={yearMonthly < val(p.month) ? "выгодно" : undefined}
          />

          {sel.free ? (
            <button className="btn primary block mt-16" disabled={busy} onClick={activateFree}>
              Активировать по промокоду
            </button>
          ) : (
            <div className="col mt-16" style={{ gap: 10 }}>
              {data.methods.stars && (
                <button className="btn primary block" disabled={busy || waiting} onClick={() => pay("stars")}>
                  {waiting ? "Проверяем оплату…" : `Оплатить ${sel.stars} ⭐`}
                </button>
              )}
              {data.methods.card && (
                <button className="btn secondary block" disabled={busy || waiting} onClick={() => pay("card")}>
                  {`Оплатить ${rub(sel.rub)}`}
                </button>
              )}
              <div className="tiny muted center">
                {data.methods.stars && "Оплата звёздами Telegram — купить их можно прямо в окне оплаты."}
                {product === "month" && data.methods.stars && " Месяц продлевается сам; отключить — в профиле в один клик."}
              </div>
            </div>
          )}
        </>
      )}

      <div className="card mt-16 guarantee">
        <div className="row" style={{ gap: 10 }}>
          <span style={{ fontSize: 26 }}>🛡</span>
          <div>
            <b>Гарантия {days(data.guarantee_days)}</b>
            <div className="small muted">Не зашло — вернём деньги без вопросов, кнопкой в профиле.</div>
          </div>
        </div>
      </div>

      <div className="section-title">Что внутри</div>
      <div className="card" style={{ padding: "8px 16px" }}>
        {INCLUDED.map((t) => (
          <div key={t} className="row included" style={{ gap: 10, padding: "8px 0" }}>
            <span className="check-mini"><ICheck size={14} /></span>
            <span className="small">{t}</span>
          </div>
        ))}
      </div>

      {data.sprint_available && data.needs_access && (
        <button
          className="btn secondary block mt-16"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await api.post("/sprint");
              invalidate();
              nav.replace({ name: "book" });
            } catch (e) {
              toast((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          Сначала попробовать: 7 дней бесплатно
        </button>
      )}

      <div className="mt-16">
        {entry ? (
          <>
            <div className="row" style={{ gap: 8 }}>
              <input
                className="input grow"
                placeholder={entry === "code" ? "Код активации" : "Промокод"}
                value={code}
                autoFocus
                autoCapitalize="characters"
                onChange={(e) => setCode(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && (entry === "code" ? redeem() : applyPromo())}
              />
              <button className="btn secondary" disabled={busy || !code.trim()} onClick={entry === "code" ? redeem : applyPromo}>
                {entry === "code" ? "Активировать" : "Применить"}
              </button>
            </div>
            {entry === "code" && <p className="tiny muted mt-8">Например, подарочный код или код из письма после покупки.</p>}
          </>
        ) : (
          <div className="row center" style={{ gap: 18, justifyContent: "center" }}>
            <button className="link" onClick={() => setEntry("code")}>🔑 У меня есть код</button>
            <button className="link" onClick={() => setEntry("promo")}>
              {data.promo ? `Промокод ${data.promo} · сменить` : "Промокод"}
            </button>
          </div>
        )}
      </div>

      <p className="tiny muted center mt-16">
        Оплачивая, ты принимаешь{" "}
        {data.offer_url ? <button className="link tiny" onClick={() => openExternal(data.offer_url!)}>условия оферты</button> : "условия оферты (/terms в боте)"}
        {data.privacy_url && (
          <>
            {" "}и{" "}
            <button className="link tiny" onClick={() => openExternal(data.privacy_url!)}>политику данных</button>
          </>
        )}
        .
      </p>
    </div>
  );
}

function PlanOption({ on, onPick, title, price, unit, suffix = "", sub, extra, badge }: {
  on: boolean;
  unit: Unit;
  onPick: () => void;
  title: string;
  price: Billing["prices"]["run"];
  suffix?: string;
  sub: string;
  extra?: string;
  badge?: string;
}) {
  const discounted = unit === "rub" ? price.rub < price.list_rub : price.stars < price.list_stars;
  return (
    <button className={`option${on ? " on" : ""}`} onClick={() => { haptic("select"); onPick(); }}>
      <span className="radio" />
      <div className="grow">
        <div className="row" style={{ gap: 8 }}>
          <b style={{ fontSize: 17 }}>{title}</b>
          {badge && <span className="badge accent">{badge}</span>}
          {discounted && <span className="badge ok">−{price.discount}%</span>}
        </div>
        <div className="small muted">{sub}</div>
        {extra && <div className="tiny" style={{ color: "var(--accent)", marginTop: 2 }}>{extra}</div>}
      </div>
      <div className="price">
        {discounted && <s className="tiny muted">{unit === "rub" ? rub(price.list_rub) : stars(price.list_stars)}</s>}
        <b className="num">{price.free ? "0 ₽" : unit === "rub" ? rub(price.rub) : stars(price.stars)}</b>
        {suffix && <span className="tiny muted">{suffix}</span>}
      </div>
    </button>
  );
}
