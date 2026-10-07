import { useEffect, useState } from "react";
import { api, type Today as TodayData } from "../api";
import { IArrow, ICheck, IFlameSolid, IHand, ISnow } from "../components/Icons";
import { OpenBookIll } from "../components/Illustrations";
import { Avatar, Cover, ErrorState, ScreenSkeleton, toast } from "../components/ui";
import { dayMonth, days, daysUntil, firstName, greeting, longDate, plural } from "../format";
import { invalidate, useApi, usePoll } from "../hooks";
import { useNav } from "../nav";
import { haptic } from "../tg";

export default function Today() {
  const nav = useNav();
  const { data, error, loading, reload } = useApi<TodayData>("/today");
  const [bump, setBump] = useState(false);

  usePoll(reload, 4000, data?.state === "parsing" || data?.state === "checking");

  useEffect(() => {
    if (data?.done_today) {
      setBump(true);
      const t = setTimeout(() => setBump(false), 700);
      return () => clearTimeout(t);
    }
  }, [data?.done_today]);

  if (error && !data) return <ErrorState message={error} onRetry={reload} />;
  if (loading || !data) return <ScreenSkeleton />;

  const me = nav.me.user;
  return (
    <div className="screen">
      <header className="hello">
        <button onClick={() => nav.push({ name: "profile" })} aria-label="Профиль">
          <Avatar name={me.name} url={me.photo_url} size={46} ring="accent" seed={me.id} />
        </button>
        <div className="grow">
          <div className="date">{longDate(data.date)}</div>
          <div className="name">
            {greeting(data.hour)}, {firstName(me.first_name || me.name)}
          </div>
        </div>
        <div className={`streak-pill${bump ? " bump" : ""}`} title="Стрик">
          <IFlameSolid size={18} />
          {data.streak}
        </div>
      </header>

      <MainCard data={data} />

      {data.week.some((d) => d.plan_day) && <WeekCard data={data} />}

      <PartnerCard data={data} reload={reload} />
    </div>
  );
}

// ------------------------------------------------------------------ главная карточка

function MainCard({ data }: { data: TodayData }) {
  const nav = useNav();
  const s = data.state;
  const book = data.book;

  if (s === "no_run" || s === "refunded") return <NoRun data={data} />;
  if (s === "awaiting_payment") return <AwaitingPayment data={data} />;
  if (s === "no_book" || s === "parse_failed") {
    return (
      <div className="card book-card">
        <div className="eyebrow">{s === "parse_failed" ? "Файл не подошёл" : "Шаг 1 из 2"}</div>
        <div className="seg-title" style={{ fontSize: 22 }}>
          {s === "parse_failed" ? "Попробуем другую книгу?" : "Добавь свою книгу"}
        </div>
        <p className="meta">
          Файл epub или fb2 — или бумажная книга по названию и числу страниц. Мы разобьём её на отрезки по 15 минут в день.
        </p>
        <button className="btn primary block mt-12" onClick={() => nav.push({ name: "book" })}>
          Добавить книгу <IArrow size={18} />
        </button>
      </div>
    );
  }
  if (s === "parsing") {
    return (
      <div className="card book-card">
        <div className="eyebrow">Разбираю книгу</div>
        <div className="seg-title">Делю на главы и отрезки…</div>
        <p className="meta">Обычно это меньше минуты. Можно закрыть приложение — я напишу в чат.</p>
        <div className="progress">
          <div className="bar">
            <div className="fill" style={{ width: "60%", animation: "shimmer 1.3s infinite linear" }} />
          </div>
        </div>
      </div>
    );
  }
  if (s === "plan_needed") {
    return (
      <div className="card book-card">
        {book && (
          <div className="top">
            <Cover title={book.title} author={book.author} color={book.spine_color} />
            <div className="grow">
              <div className="eyebrow">Шаг 2 из 2</div>
              <div className="seg-title">Выбери срок</div>
              <p className="meta">{book.title}</p>
            </div>
          </div>
        )}
        <button className="btn primary block mt-16" onClick={() => nav.push({ name: "book" })}>
          Настроить план <IArrow size={18} />
        </button>
      </div>
    );
  }
  if (s === "finished") {
    return (
      <div className="card book-card center">
        <div className="ill-wrap"><OpenBookIll size={150} /></div>
        <div className="seg-title">Книга дочитана</div>
        <p className="meta">Она уже на полке. Можно подвести итоги или взять следующую.</p>
        <div className="btn-row mt-12">
          <button className="btn primary" onClick={() => nav.push({ name: "finish" })}>Итоги</button>
          <button className="btn secondary" onClick={() => nav.tab("shelf")}>Полка</button>
        </div>
      </div>
    );
  }
  if (s === "expired") {
    return (
      <div className="card book-card">
        <div className="eyebrow">Срок вышел</div>
        <div className="seg-title">Забег закончился</div>
        <p className="meta">Прогресс и полка сохранились. Можно начать новый забег — с этой же книгой или с другой.</p>
        <NewRunButton label="Новый забег" />
      </div>
    );
  }

  // waiting_start / not_started / to_read / clarify / checking / done_today
  if (!book) return null;
  const seg = s === "done_today" ? data.next_segment : data.segment;
  const notStarted = s === "waiting_start" || s === "not_started";
  const paper = book.source === "paper";
  const pct = Math.round(data.progress * 100);

  return (
    <div className="card book-card">
      <div className="top">
        <Cover title={book.title} author={book.author} color={book.spine_color} />
        <div className="grow" style={{ minWidth: 0 }}>
          {notStarted ? (
            <>
              <div className="eyebrow">{data.starts_on ? `Старт ${dayMonth(data.starts_on)}` : "Скоро старт"}</div>
              <div className="seg-title">{book.title}</div>
              <div className="meta">
                {data.starts_on ? startsIn(data.starts_on, data.date) : "Дату старта пришлём в чат"}
              </div>
            </>
          ) : s === "done_today" ? (
            <>
              <div className="eyebrow" style={{ color: "var(--green)" }}>Сегодня сдано</div>
              <div className="seg-title">{seg ? `Завтра: ${seg.title}` : "Все отрезки на сегодня сданы"}</div>
              {seg && <div className="meta">стр. {seg.page_from}–{seg.page_to}</div>}
            </>
          ) : (
            <>
              <div className="eyebrow">
                День {data.plan_day! > (data.plan_days || 0) ? `${data.plan_day} · отсрочка` : `${data.plan_day} из ${data.plan_days}`}
              </div>
              <div className="seg-title">{seg?.title}</div>
              {seg && <div className="meta">стр. {seg.page_from}–{seg.page_to}</div>}
              {seg && (
                <div className="chips">
                  <span className="chip">≈ {seg.minutes} мин</span>
                  <span className="chip">{seg.pages} стр.</span>
                  {data.catching_up && <span className="chip" style={{ color: "var(--accent)" }}>догоняем</span>}
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {!notStarted && (
        <div className="progress">
          <div className="row between small">
            <span className="muted">Прочитано</span>
            <b>{pct}%</b>
          </div>
          <div className="bar">
            <div className="fill" style={{ width: `${Math.max(pct, 2)}%` }} />
          </div>
        </div>
      )}

      {s === "clarify" && data.open_retelling?.question && (
        <div className="ai-bubble">
          <span className="flame-avatar">
            <IFlameSolid size={16} />
          </span>
          <div className="bubble">
            <b>Уточню:</b> {data.open_retelling.question}
          </div>
        </div>
      )}
      {s === "checking" && <p className="meta mt-12">Проверяю пересказ — это займёт пару секунд…</p>}

      <div className="btn-row mt-16">
        {s === "to_read" || s === "clarify" ? (
          <>
            {seg?.can_read && !paper && (
              <button className="btn primary" onClick={() => nav.push({ name: "read", params: { d: seg.day_number } })}>
                Читать главу
              </button>
            )}
            <button
              className={`btn ${seg?.can_read && !paper ? "secondary" : "primary"}`}
              onClick={() => {
                haptic("light");
                nav.push({ name: "retell" });
              }}
            >
              {s === "clarify" ? "Ответить" : "Рассказать"}
            </button>
          </>
        ) : s === "done_today" ? (
          <>
            <button className="btn secondary" onClick={() => nav.tab("run")}>Мой забег</button>
            <ShareStreak streak={data.streak} />
          </>
        ) : notStarted ? (
          <button className="btn secondary" onClick={() => nav.push({ name: "book" })}>Книга и план</button>
        ) : null}
      </div>
      {paper && (s === "to_read" || s === "clarify") && seg && (
        <p className="tiny muted mt-12 center">Читай по своей книге: стр. {seg.page_from}–{seg.page_to}</p>
      )}
      {s === "to_read" && data.limit > 1 && (
        <p className="tiny muted mt-12 center">Финишная прямая: сегодня можно сдать до {data.limit} отрезков.</p>
      )}
    </div>
  );
}

function startsIn(start: string, today: string): string {
  const n = daysUntil(start, today);
  if (n <= 0) return "Первый отрезок — сегодня утром";
  if (n === 1) return "Старт завтра. Первый отрезок придёт утром";
  return `Через ${days(n)}. Первый отрезок придёт утром`;
}

function ShareStreak({ streak }: { streak: number }) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      className="btn primary"
      disabled={busy || streak < 1}
      onClick={async () => {
        setBusy(true);
        try {
          await api.post("/share/streak/send");
          toast("Карточка в чате — перешли её друзьям");
        } catch (e) {
          toast((e as Error).message);
        } finally {
          setBusy(false);
        }
      }}
    >
      Поделиться
    </button>
  );
}

function NewRunButton({ label, primary = true }: { label: string; primary?: boolean }) {
  const nav = useNav();
  const [busy, setBusy] = useState(false);
  return (
    <button
      className={`btn ${primary ? "primary" : "secondary"} block mt-12`}
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          await api.post("/runs/new");
          invalidate();
          nav.push({ name: "book" });
        } catch (e) {
          toast((e as Error).message);
        } finally {
          setBusy(false);
        }
      }}
    >
      {label} <IArrow size={18} />
    </button>
  );
}

function SprintButton() {
  const nav = useNav();
  const [busy, setBusy] = useState(false);
  return (
    <button
      className="btn secondary block mt-12"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          await api.post("/sprint");
          invalidate();
          nav.push({ name: "book" });
        } catch (e) {
          toast((e as Error).message);
        } finally {
          setBusy(false);
        }
      }}
    >
      Бесплатный спринт на 7 дней
    </button>
  );
}

function NoRun({ data }: { data: TodayData }) {
  return (
    <div className="card book-card">
      <div className="eyebrow">{data.has_access ? "Доступ открыт" : "С чего начать"}</div>
      <div className="seg-title" style={{ fontSize: 22 }}>Какую книгу дочитаем?</div>
      <p className="meta">
        Загрузи epub или fb2 — или выбери бумажную книгу. Разобьём её на отрезки по 15 минут в день, а ИИ будет проверять
        короткие пересказы.
      </p>
      <NewRunButton label="Выбрать книгу" />
      {data.sprint_available && (
        <>
          <SprintButton />
          <p className="tiny muted center mt-8">Спринт — рассказ или короткая книга за неделю, бесплатно.</p>
        </>
      )}
    </div>
  );
}

function AwaitingPayment({ data }: { data: TodayData }) {
  const nav = useNav();
  const book = data.book;
  return (
    <div className="card book-card">
      {book && (
        <div className="top">
          <Cover title={book.title} author={book.author} color={book.spine_color} />
          <div className="grow">
            <div className="eyebrow">План готов</div>
            <div className="seg-title">{book.title}</div>
            {data.plan_days && <p className="meta">{days(data.plan_days)} · по 15 минут в день</p>}
          </div>
        </div>
      )}
      {!book && <div className="seg-title" style={{ fontSize: 22 }}>{data.run?.title || "Забег"}</div>}
      <p className="meta">
        {data.run?.kind === "main" && data.run.start_date ? `Старт группы — ${dayMonth(data.run.start_date)}. ` : ""}
        Осталось открыть доступ — первый отрезок придёт сразу, а если уже вечер — завтра утром.
      </p>
      <button className="btn primary block mt-12" onClick={() => nav.push({ name: "pay" })}>
        Открыть доступ <IArrow size={18} />
      </button>
      {data.sprint_available && <SprintButton />}
    </div>
  );
}

// ------------------------------------------------------------------ неделя

function WeekCard({ data }: { data: TodayData }) {
  return (
    <div className="card">
      <div className="row between">
        <b>Эта неделя</b>
        <span className="link row" style={{ gap: 4 }}>
          <ISnow size={14} />
          {data.freezes_left} {plural(data.freezes_left, "заморозка", "заморозки", "заморозок")} в запасе
        </span>
      </div>
      <div className="week">
        {data.week.map((d) => (
          <div key={d.date}>
            <div className="wd">{d.weekday}</div>
            <div className={`dot ${d.state}`}>{d.state === "done" ? <ICheck size={18} /> : d.state === "frozen" ? <ISnow size={16} /> : d.day}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ напарник

function PartnerCard({ data, reload }: { data: TodayData; reload: () => void }) {
  const nav = useNav();
  const me = nav.me.user;
  const p = data.partner;
  const [nudged, setNudged] = useState(false);
  if (!p) {
    if (!data.run || data.state === "no_run") return null;
    return (
      <button className="card row" style={{ width: "100%", textAlign: "left" }} onClick={() => nav.tab("friends")}>
        <div className="avatars-stack">
          <Avatar name={me.name} url={me.photo_url} size={40} seed={me.id} />
          <span className="avatar" style={{ width: 40, height: 40, background: "var(--surface-3)", color: "var(--muted)" }}>+</span>
        </div>
        <div className="grow">
          <b>Позови напарника</b>
          <div className="small muted">С общим стриком дочитывают чаще</div>
        </div>
      </button>
    );
  }
  const done = p.today === "done";
  const myDone = data.done_today;
  const status = done ? (myDone ? "оба сдали сегодня" : "твоя очередь") : myDone ? "ждём напарника" : "ещё читаете оба";
  return (
    <div className="card partner-row">
      <div className="avatars-stack">
        <Avatar name={me.name} url={me.photo_url} size={42} seed={me.id} />
        <Avatar name={p.name} url={p.photo_url} size={42} seed={p.id} />
      </div>
      <div className="grow">
        <b>{done ? `${firstName(p.name)}: день сдан` : `${firstName(p.name)} ещё читает`}</b>
        <div className="small muted">
          Общий стрик {p.pair_streak} · {status}
        </div>
      </div>
      {done ? (
        <span className="check-circle">
          <ICheck size={18} />
        </span>
      ) : myDone && !nudged ? (
        <button
          className="icon-btn"
          aria-label="Напомнить"
          onClick={async () => {
            haptic("light");
            const r = await api.post<{ result: string }>("/pair/nudge").catch(() => ({ result: "error" }));
            setNudged(true);
            toast(r.result === "ok" ? "Напоминание отправлено" : r.result === "already" ? "Сегодня уже напоминали" : "Не получилось");
            reload();
          }}
        >
          <IHand size={20} />
        </button>
      ) : null}
    </div>
  );
}

