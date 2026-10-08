import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Billing, type PriceInfo, type Product } from "../api";
import { IArrow, ICheck } from "../components/Icons";
import { OpenBookIll } from "../components/Illustrations";
import { Cover, ErrorState, ScreenSkeleton, toast } from "../components/ui";
import { dayMonth, days, plural } from "../format";
import { invalidate, useApi } from "../hooks";
import { useNav } from "../nav";
import { haptic, openExternal } from "../tg";

export const rub = (n: number) => `${Math.round(n).toLocaleString("ru-RU")} ₽`;

const INCLUDED = [
  "План под твою книгу: около 15 минут чтения в день",
  "Проверка каждого пересказа — голосом или текстом",
  "Стрик, заморозки и три дня отсрочки в конце",
  "Напарник и друзья — вместе дочитывают чаще",
  "Дочитанные книги собираются на полке",
];

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

type Order = { id: string; startedAt: number };
type OrderState = Billing & { status: "pending" | "paid" | "canceled" | "refunded" };

export default function Pay() {
  const nav = useNav();
  const { data, error, loading, reload } = useApi<Billing>("/billing?view=pay");
  const [chosen, setProduct] = useState<Product>("run");
  const [busy, setBusy] = useState(false);
  const [order, setOrder] = useState<Order | null>(null);
  const [email, setEmail] = useState("");
  const [promoOpen, setPromoOpen] = useState(false);
  const [promo, setPromo] = useState("");
  const alive = useRef(true);
  const inFlight = useRef(false);

  useEffect(() => () => { alive.current = false; }, []);

  const done = useCallback(() => {
    invalidate();
    nav.refreshMe();
    nav.tab("today");
  }, [nav]);

  // После перехода на страницу ЮKassa ждём подтверждения: проверяем заказ сами и при возвращении в приложение
  const check = useCallback(async (o: Order, quiet = true) => {
    if (inFlight.current) return; // прошлый запрос ещё идёт — не копим их
    inFlight.current = true;
    try {
      const r = await api.get<OrderState>(`/billing/order/${o.id}`);
      if (!alive.current) return;
      if (r.status === "paid") {
        haptic("success");
        toast("Оплата прошла — спасибо!");
        setOrder(null);
        done();
      } else if (r.status === "canceled") {
        haptic("error");
        toast("Оплата не прошла — деньги не списаны");
        setOrder(null);
      } else if (!quiet) {
        toast("Пока не видим оплату. Если уже оплачено — подождём ещё немного");
      }
    } catch (e) {
      if (!quiet) toast((e as Error).message);
    } finally {
      inFlight.current = false;
    }
  }, [done]);

  useEffect(() => {
    if (!order) return;
    const timer = window.setInterval(() => {
      if (Date.now() - order.startedAt > 15 * 60 * 1000) window.clearInterval(timer);
      else void check(order);
    }, 3000);
    const onVisible = () => document.visibilityState === "visible" && void check(order);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [order, check]);

  if (error && !data) return <ErrorState message={error} onRetry={reload} />;
  if (loading || !data) return <ScreenSkeleton />;

  const sub = data.subscription;
  const renewing = sub.active && !data.needs_access;
  // при продлении «одной книги» нет: выбираем между месяцем и годом
  const product: Product = renewing && chosen === "run" ? "month" : chosen;
  const p = data.prices;
  const sel = p[product];
  const emailOk = !data.needs_email || EMAIL_RE.test(email.trim());

  const pay = async () => {
    if (!emailOk) {
      toast("Укажи e-mail — пришлём на него чек");
      return;
    }
    setBusy(true);
    haptic("medium");
    try {
      const r = await api.post<{ url: string; order: string }>("/billing/pay", {
        product,
        email: data.needs_email ? email.trim() : undefined,
      });
      setOrder({ id: r.order, startedAt: Date.now() });
      openExternal(r.url);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const applyPromo = async () => {
    if (!promo.trim()) return;
    setBusy(true);
    try {
      await api.post<Billing>("/billing/promo", { code: promo.trim() });
      haptic("success");
      toast("Промокод применён");
      setPromoOpen(false);
      setPromo("");
      reload();
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
      toast("Доступ открыт");
      done();
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };


  if (order) {
    return (
      <div className="screen no-tabs">
        <div className="card book-card center mt-24">
          <div className="ill-wrap"><OpenBookIll size={150} /></div>
          <div className="seg-title">Ждём подтверждения оплаты</div>
          <p className="meta">
            Страница оплаты открылась отдельно. Как только платёж пройдёт, доступ откроется сам — а бот напишет в чат.
          </p>
          <div className="wait-dots" aria-hidden="true"><i /><i /><i /></div>
          <button className="btn primary block mt-12" onClick={() => check(order, false)}>Проверить оплату</button>
          <button className="btn ghost block mt-8" onClick={() => setOrder(null)}>Выбрать другой тариф</button>
        </div>
      </div>
    );
  }

  const perDay = product === "run" && data.plan_days ? sel.rub / data.plan_days : null;
  const yearMonthly = p.year.rub / 12;

  return (
    <div className="screen no-tabs pay">
      <h1 className="h-title">{renewing ? "Продлить абонемент" : data.needs_access && data.plan_days ? "Последний шаг" : "Тарифы"}</h1>

      {renewing && (
        <div className="card book-card center">
          <div className="ill-wrap"><OpenBookIll size={130} /></div>
          <div className="seg-title">Всё открыто до {dayMonth(sub.until)}</div>
          <p className="meta">Книга за книгой без доплат. Новый срок прибавится к оставшемуся.</p>
          <button className="btn secondary block mt-12" onClick={done}>К чтению <IArrow size={18} /></button>
        </div>
      )}

      {data.book && data.plan_days && data.needs_access && (
        <div className="card row" style={{ gap: 14 }}>
          <Cover title={data.book.title} author={data.book.author} color={data.book.spine_color} small />
          <div className="grow">
            <div className="eyebrow">План готов</div>
            <b>{data.book.title}</b>
            <div className="small muted">{days(data.plan_days)} · старт сразу после оплаты</div>
          </div>
        </div>
      )}

      <div className="section-title">Выбери формат</div>
      {!renewing && (
        <PlanOption
          on={product === "run"} onPick={() => setProduct("run")}
          title="Одна книга" price={p.run} sub="Забег до финиша: план, проверка пересказов, напарник"
          extra={perDay ? `≈ ${rub(perDay)} в день` : undefined}
        />
      )}
      <PlanOption
        on={product === "month"} onPick={() => setProduct("month")}
        title="Месяц" price={p.month} suffix="за 30 дней"
        sub={`Книга за книгой без доплат и ${data.freezes_sub} ${plural(data.freezes_sub, "заморозка", "заморозки", "заморозок")} в неделю`}
      />
      <PlanOption
        on={product === "year"} onPick={() => setProduct("year")}
        title="Год" price={p.year} suffix="за 365 дней" sub={`≈ ${rub(yearMonthly)} в месяц — дешевле всего`}
        badge={yearMonthly < p.month.rub ? "выгодно" : undefined}
      />

      {sel.free ? (
        <button className="btn primary block mt-16" disabled={busy} onClick={activateFree}>
          Подключить по промокоду
        </button>
      ) : !data.enabled ? (
        <div className="card mt-16 soon">
          <p className="small" style={{ whiteSpace: "pre-line", margin: 0 }}>{data.manual_info}</p>
        </div>
      ) : (
        <div className="mt-16">
          {data.needs_email && (
            <div className="field" style={{ marginBottom: 12 }}>
              <label htmlFor="pay-email">E-mail для чека</label>
              <input
                id="pay-email"
                className="input"
                type="email"
                inputMode="email"
                autoComplete="email"
                placeholder="name@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
          )}
          <button className="btn primary block" disabled={busy || !emailOk} onClick={pay}>
            {busy ? "Открываем оплату…" : `Оплатить ${rub(sel.rub)}`}
          </button>
          <p className="tiny muted center mt-8">
            Оплата разовая, без автопродления. Карта, СБП и другие способы — на защищённой странице ЮKassa.
          </p>
        </div>
      )}

      <div className="section-title">Что внутри</div>
      <div className="card" style={{ padding: "8px 16px" }}>
        {INCLUDED.map((t) => (
          <div key={t} className="row" style={{ gap: 10, padding: "8px 0" }}>
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
        {promoOpen ? (
          <div className="row" style={{ gap: 8 }}>
            <input
              className="input grow"
              placeholder="Промокод"
              value={promo}
              autoFocus
              autoCapitalize="characters"
              onChange={(e) => setPromo(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && applyPromo()}
            />
            <button className="btn secondary" disabled={busy || !promo.trim()} onClick={applyPromo}>
              Применить
            </button>
          </div>
        ) : (
          <button className="link block" onClick={() => setPromoOpen(true)}>
            {data.promo ? `Промокод ${data.promo} · сменить` : "У меня есть промокод"}
          </button>
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

function PlanOption({ on, onPick, title, price, suffix = "", sub, extra, badge }: {
  on: boolean;
  onPick: () => void;
  title: string;
  price: PriceInfo;
  suffix?: string;
  sub: string;
  extra?: string;
  badge?: string;
}) {
  const discounted = price.rub < price.list_rub;
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
        {extra && <div className="tiny" style={{ color: "var(--accent-text)", marginTop: 2 }}>{extra}</div>}
      </div>
      <div className="price">
        {discounted && <s className="tiny muted">{rub(price.list_rub)}</s>}
        <b className="num">{price.free ? "0 ₽" : rub(price.rub)}</b>
        {suffix && <span className="tiny muted">{suffix}</span>}
      </div>
    </button>
  );
}
