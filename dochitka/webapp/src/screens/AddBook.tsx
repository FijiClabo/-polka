import { useEffect, useRef, useState } from "react";
import { api, type BookState, type PlanChoices } from "../api";
import { IBook, IUpload } from "../components/Icons";
import { OpenBookIll } from "../components/Illustrations";
import { Cover, ErrorState, ScreenSkeleton, toast } from "../components/ui";
import { dayMonth, days as fmtDays, decimal, hm, pages, parseDate, plural } from "../format";
import { invalidate, useApi, usePoll } from "../hooks";
import { useNav } from "../nav";
import { closeApp, confirmDialog, haptic } from "../tg";

const COLORS = ["#C9644F", "#7E9C7A", "#4F6F9F", "#E2B84F", "#8A6A85", "#4E6B57", "#B98B6E", "#3D4B66", "#D49A8C"];

export default function AddBook({ prefill }: { prefill?: { title: string; author: string } }) {
  const nav = useNav();
  const st = useApi<BookState>("/book");
  const [mode, setMode] = useState<"choose" | "paper">("choose");
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const parsing = st.data?.book?.parse_status === "pending" || uploading;
  usePoll(st.reload, 2500, !!st.data?.book && st.data.book.parse_status === "pending");

  if (st.error && !st.data) return <ErrorState message={st.error} onRetry={st.reload} />;
  if (!st.data) return <ScreenSkeleton />;

  const data = st.data;
  const book = data.book;

  const upload = async (f: File) => {
    if (f.size > data.max_mb * 1024 * 1024) {
      toast(`Файл больше ${data.max_mb} МБ — Telegram такой не пропустит. Можно читать без файла.`);
      return;
    }
    setUploading(true);
    const fd = new FormData();
    fd.append("file", f, f.name);
    try {
      await api.form("/book/upload", fd);
      invalidate();
      setTimeout(st.reload, 800);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setUploading(false);
    }
  };

  const input = (
    <input
      ref={fileRef}
      type="file"
      accept=".epub,.fb2,.zip,application/epub+zip"
      style={{ display: "none" }}
      onChange={(e) => {
        const f = e.target.files?.[0];
        if (f) upload(f);
        e.target.value = "";
      }}
    />
  );

  // ------------------------------------------------------------------ разбор
  if (parsing) {
    return (
      <div className="screen no-tabs">
        <h1 className="h-display">Книга</h1>
        <div className="card book-card mt-16 center">
          <div className="ill-wrap"><OpenBookIll size={140} /></div>
          <div className="seg-title">Разбираю книгу…</div>
          <p className="meta">Делю на главы и отрезки. Обычно меньше минуты — результат продублирую в чат.</p>
          <div className="progress"><div className="bar"><div className="fill indeterminate" /></div></div>
        </div>
      </div>
    );
  }

  // ------------------------------------------------------------------ нет книги / ошибка
  if (!book || book.parse_status === "failed") {
    return (
      <div className="screen no-tabs">
        {input}
        <h1 className="h-display">Своя книга</h1>
        <p className="muted">Два способа — выбери, как удобнее. Текст файла видишь только ты.</p>
        {book?.parse_status === "failed" && (
          <div className="card mt-12" style={{ background: "var(--missed-bg)", borderColor: "transparent" }}>
            <b>Не получилось разобрать файл</b>
            <p className="small muted" style={{ margin: "6px 0 0" }}>{data.parse_error || "Попробуй другой файл или читай без файла."}</p>
          </div>
        )}
        {mode === "choose" ? (
          <>
            {data.together && <TogetherCard offer={data.together} onDone={() => { invalidate(); st.reload(); }} />}
            {prefill && !data.together && (
              <div className="card mt-16">
                <div className="eyebrow">С полки друга</div>
                <div className="seg-title" style={{ fontSize: 20, margin: "6px 0" }}>«{prefill.title}»</div>
                <p className="small muted" style={{ margin: 0 }}>
                  Загрузи файл этой книги, если он у тебя есть, — проверка будет полной. Или читай без файла: название уже подставим.
                </p>
              </div>
            )}
            <div className="drop mt-16" onClick={() => fileRef.current?.click()}>
              <IUpload size={30} />
              <div style={{ fontWeight: 700, marginTop: 8 }}>Есть файл epub или fb2</div>
              <div className="small">Полная проверка: ИИ сверяет пересказ с текстом</div>
              <div className="small muted mt-8">
                Читать можно где удобно — здесь, в другой читалке или в бумажной книге: каждый день покажем первые и последние слова отрезка.
              </div>
              <div className="tiny muted mt-8">до {data.max_mb} МБ · или отправь файл боту в чат</div>
            </div>
            <button className="card row mt-12" style={{ width: "100%", textAlign: "left" }} onClick={() => setMode("paper")}>
              <IBook size={26} />
              <div className="grow">
                <b>Файла нет</b>
                <div className="small muted">
                  Бумажная книга или другое приложение. Отрезки — по страницам твоего издания, проверка — разговор о прочитанном, без сверки с текстом.
                </div>
              </div>
            </button>
            <button className="btn ghost block mt-12" onClick={closeApp}>Отправить файл в чат</button>
          </>
        ) : (
          <PaperForm prefill={prefill} onDone={() => { invalidate(); st.reload(); setMode("choose"); }} onCancel={() => setMode("choose")} />
        )}
      </div>
    );
  }

  // ------------------------------------------------------------------ книга есть
  return (
    <div className="screen no-tabs">
      {input}
      <BookMeta data={data} onChange={st.reload} />
      {data.plan_confirmed ? (
        <PlanSummary data={data} onReplan={st.reload} onReplace={() => fileRef.current?.click()} />
      ) : (
        <PlanPicker
          onConfirmed={(awaitingPayment) => {
            invalidate();
            haptic("success");
            if (awaitingPayment) nav.replace({ name: "pay" });
            else nav.tab("today");
          }}
        />
      )}
    </div>
  );
}

function BookMeta({ data, onChange }: { data: BookState; onChange: () => void }) {
  const b = data.book!;
  const [title, setTitle] = useState(b.title);
  const [author, setAuthor] = useState(b.author);
  const [color, setColor] = useState(b.spine_color);
  useEffect(() => {
    setTitle(b.title);
    setAuthor(b.author);
    setColor(b.spine_color);
  }, [b.title, b.author, b.spine_color]);
  const patch = async (p: Record<string, string>) => {
    try {
      await api.patch("/book", p);
      invalidate("/today");
      invalidate("/shelf");
      onChange();
    } catch (e) {
      toast((e as Error).message);
    }
  };
  return (
    <>
      <div className="row" style={{ gap: 18, alignItems: "flex-end", marginTop: 14 }}>
        <Cover title={title || "Без названия"} author={author} color={color} />
        <div className="grow col" style={{ gap: 6 }}>
          <span className="eyebrow">{b.source === "paper" ? "Без файла" : b.source === "shared" ? "План напарника" : b.source.toUpperCase()}</span>
          <span className="small muted">{pages(b.pages)}{b.chapters ? ` · ${b.chapters} ${plural(b.chapters, "глава", "главы", "глав")}` : ""}</span>
          <span className="small muted">≈ {hm(b.reading_minutes)} чтения</span>
        </div>
      </div>
      <div className="col mt-16">
        <div className="field">
          <label>Название</label>
          <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} onBlur={() => title !== b.title && patch({ title })} />
        </div>
        <div className="field">
          <label>Автор</label>
          <input className="input" value={author} onChange={(e) => setAuthor(e.target.value)} onBlur={() => author !== b.author && patch({ author })} />
        </div>
        <div className="field">
          <label>Цвет корешка на полке</label>
          <div className="swatches">
            {COLORS.map((c) => (
              <button key={c} className={`swatch${c.toLowerCase() === color.toLowerCase() ? " on" : ""}`} style={{ background: c }} onClick={() => { setColor(c); patch({ spine_color: c }); }} aria-label={c} />
            ))}
          </div>
        </div>
      </div>
    </>
  );
}

type PlanChoice = { kind: "days"; days: number } | { kind: "minutes"; minutes: number } | { kind: "chapter" } | { kind: "date"; date: string };

function PlanPicker({ onConfirmed }: { onConfirmed: (awaitingPayment: boolean) => void }) {
  const opts = useApi<PlanChoices>("/book/plan-options");
  const [choice, setChoice] = useState<PlanChoice | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (opts.data && choice === null) {
      const rec = opts.data.options.find((o) => o.recommended)?.days ?? opts.data.options[0]?.days;
      if (rec) setChoice({ kind: "days", days: rec });
    }
  }, [opts.data, choice]);
  if (!opts.data) return <div className="mt-24"><ScreenSkeleton /></div>;
  const c = opts.data;
  const start = c.expected_start;
  const dateDays = choice?.kind === "date" && start ? Math.round((parseDate(choice.date).getTime() - parseDate(start).getTime()) / 86400000) + 1 : null;
  const dateBad = dateDays !== null && (dateDays < c.min_days || dateDays > c.max_days);
  const pick = (ch: PlanChoice) => { haptic("select"); setChoice(ch); };
  return (
    <>
      <div className="section-title">Выбери срок</div>
      {c.options.map((o) => (
        <button key={o.days} className={`option${choice?.kind === "days" && choice.days === o.days ? " on" : ""}`} onClick={() => pick({ kind: "days", days: o.days })}>
          <span className="radio" />
          <div className="grow">
            <div className="row" style={{ gap: 8 }}>
              <b className="num" style={{ fontSize: 18 }}>{fmtDays(o.days)}</b>
              {o.recommended && <span className="badge accent">рекомендуем</span>}
            </div>
            <div className="small muted">{decimal(o.pages_per_day)} стр. · ≈ {o.minutes_per_day} мин в день</div>
            {o.warning && <div className="tiny" style={{ color: "var(--accent-text)", marginTop: 4 }}>{o.warning}</div>}
          </div>
        </button>
      ))}

      {c.custom && c.minute_options.length > 0 && (
        <>
          <div className="section-title">Или своё время в день</div>
          <div className="pace-grid">
            {c.minute_options.map((o) => (
              <button
                key={o.minutes}
                className={`pace-chip${choice?.kind === "minutes" && choice.minutes === o.minutes ? " on" : ""}`}
                onClick={() => pick({ kind: "minutes", minutes: o.minutes })}
              >
                <b>{o.minutes} мин</b>
                <span>{fmtDays(o.days)}</span>
              </button>
            ))}
          </div>
        </>
      )}
      {c.custom && c.chapters && (
        <button className={`option mt-12${choice?.kind === "chapter" ? " on" : ""}`} onClick={() => pick({ kind: "chapter" })}>
          <span className="radio" />
          <div className="grow">
            <b>Глава в день</b>
            <div className="small muted">{fmtDays(c.chapters.days)}, от {c.chapters.min_minutes} до {c.chapters.max_minutes} мин — каждый день целая глава</div>
          </div>
        </button>
      )}
      {c.custom && start && (
        <button
          className={`option mt-8${choice?.kind === "date" ? " on" : ""}`}
          onClick={() => choice?.kind !== "date" && pick({ kind: "date", date: addDaysIso(start, 29) })}
        >
          <span className="radio" />
          <div className="grow">
            <b>Дочитать к дате</b>
            <div className="small muted">К встрече клуба, экзамену или отпуску</div>
            {choice?.kind === "date" && (
              <>
                <input className="input mt-8" type="date" value={choice.date} min={start} onClick={(e) => e.stopPropagation()}
                  onChange={(e) => setChoice({ kind: "date", date: e.target.value })} />
                {dateDays !== null && (
                  <div className="small mt-8" style={dateBad ? { color: "var(--accent-text)" } : undefined}>
                    {dateBad
                      ? `Нужно от ${c.min_days} до ${c.max_days} дней`
                      : `${fmtDays(dateDays)} · ≈ ${Math.max(1, Math.round(c.total_minutes / dateDays))} мин в день`}
                  </div>
                )}
              </>
            )}
          </div>
        </button>
      )}

      <button
        className="btn primary block mt-16"
        disabled={!choice || busy || dateBad}
        onClick={async () => {
          if (!choice) return;
          setBusy(true);
          try {
            const body = choice.kind === "days" ? { days: choice.days } : choice.kind === "minutes" ? { minutes: choice.minutes }
              : choice.kind === "chapter" ? { by_chapter: true } : { finish: choice.date };
            const r = await api.post<{ start: string | null; awaiting_payment: boolean }>("/book/plan", body);
            toast(r.start ? `План готов. Старт — ${dayMonth(r.start)}` : "План готов");
            onConfirmed(r.awaiting_payment);
          } catch (e) {
            toast((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        Подтвердить план
      </button>
      <p className="tiny muted center mt-12">Лучше всего держится 15–20 минут в день. Темп можно поменять в любой момент.</p>
    </>
  );
}

function addDaysIso(iso: string, n: number): string {
  const d = parseDate(iso);
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function PlanSummary({ data, onReplan, onReplace }: { data: BookState; onReplan: () => void; onReplace: () => void }) {
  const nav = useNav();
  const [busy, setBusy] = useState(false);
  return (
    <div className="card mt-16">
      <div className="eyebrow">План</div>
      <div className="seg-title" style={{ fontSize: 20, margin: "6px 0" }}>{fmtDays(data.plan_days || 0)}</div>
      <div className="small muted">{data.start ? `Старт — ${dayMonth(data.start)}` : "Старт — сразу после оплаты"}</div>
      {data.awaiting_payment && (
        <button className="btn primary block mt-16" onClick={() => nav.push({ name: "pay" })}>
          Открыть забег
        </button>
      )}
      {data.book?.source === "shared" && (
        <p className="small muted mt-12" style={{ marginBottom: 0 }}>
          Тот же план, что у напарника: те же главы в те же дни. Читаешь по своему экземпляру. Есть свой файл этой книги —
          загрузи его, и проверка станет полной, со сверкой с текстом.
        </p>
      )}
      <button className="btn secondary block mt-16" onClick={() => nav.push({ name: "pace" })}>Свой темп: минуты в день, пауза</button>
      <div className="btn-row mt-8">
        <button className="btn ghost" onClick={onReplace}>{data.book?.source === "shared" ? "Загрузить свой файл" : "Заменить книгу"}</button>
        <button
          className="btn ghost"
          disabled={busy}
          onClick={async () => {
            if (!(await confirmDialog(data.has_progress ? "Удалить текст книги? Прогресс и стрик останутся." : "Удалить книгу?"))) return;
            setBusy(true);
            try {
              await api.del("/book");
              invalidate();
              onReplan();
              nav.tab("today");
            } catch (e) {
              toast((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          Удалить
        </button>
      </div>
      {data.has_progress && <p className="tiny muted mt-12">При замене план начнётся заново со следующего дня, стрик сохранится.</p>}
    </div>
  );
}

function TogetherCard({ offer, onDone }: { offer: NonNullable<BookState["together"]>; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  return (
    <div className="card mt-16 together-card">
      <div className="eyebrow">Читать вместе</div>
      <div className="seg-title" style={{ fontSize: 20, margin: "6px 0" }}>«{offer.title}»</div>
      <p className="small muted" style={{ margin: 0 }}>
        {offer.partner_name} зовёт читать эту книгу вместе: тот же план на {fmtDays(offer.plan_days)}, одна и та же глава в один день,
        общий стрик. Читаешь по своему экземпляру — бумажному, в любом приложении или своим файлом.
      </p>
      <button
        className="btn primary block mt-16"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await api.post("/book/together");
            haptic("success");
            onDone();
          } catch (e) {
            toast((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        Читаем вместе
      </button>
      <p className="tiny muted mt-8 center" style={{ marginBottom: 0 }}>Или выбери свою книгу ниже</p>
    </div>
  );
}

function PaperForm({ prefill, onDone, onCancel }: { prefill?: { title: string; author: string }; onDone: () => void; onCancel: () => void }) {
  const [title, setTitle] = useState(prefill?.title ?? "");
  const [author, setAuthor] = useState(prefill?.author ?? "");
  const [pg, setPg] = useState("");
  const [busy, setBusy] = useState(false);
  const n = Number(pg);
  const ok = title.trim() && n >= 20 && n <= 3000;
  return (
    <div className="col mt-16">
      <div className="field">
        <label>Название</label>
        <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Мастер и Маргарита" autoFocus />
      </div>
      <div className="field">
        <label>Автор</label>
        <input className="input" value={author} onChange={(e) => setAuthor(e.target.value)} placeholder="Михаил Булгаков" />
      </div>
      <div className="field">
        <label>Сколько страниц в твоём издании</label>
        <input className="input" inputMode="numeric" value={pg} onChange={(e) => setPg(e.target.value.replace(/\D/g, ""))} placeholder="480" />
        {pg && (n < 20 || n > 3000) && <span className="tiny" style={{ color: "var(--danger)" }}>от 20 до 3000</span>}
      </div>
      <button
        className="btn primary block mt-8"
        disabled={!ok || busy}
        onClick={async () => {
          setBusy(true);
          try {
            await api.post("/book/paper", { title: title.trim(), author: author.trim(), pages: n });
            onDone();
          } catch (e) {
            toast((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        Дальше — выбрать срок
      </button>
      <button className="btn ghost block" onClick={onCancel}>Назад</button>
    </div>
  );
}
