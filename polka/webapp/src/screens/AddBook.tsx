import { useEffect, useRef, useState } from "react";
import { api, type BookState, type PlanOption } from "../api";
import { IBook, IUpload } from "../components/Icons";
import { Cover, ErrorState, ScreenSkeleton, toast } from "../components/ui";
import { dayMonth, decimal, hm, pages } from "../format";
import { invalidate, useApi, usePoll } from "../hooks";
import { useNav } from "../nav";
import { closeApp, confirmDialog, haptic } from "../tg";

const COLORS = ["#E2553F", "#3E8E6E", "#E98A6B", "#5B63D6", "#F2C14E", "#8C5BD6", "#2F7FB8", "#C2410C", "#4D7C0F"];

export default function AddBook() {
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
      toast(`Файл больше ${data.max_mb} МБ — Telegram такой не пропустит. Добавь книгу как бумажную.`);
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
          <div style={{ fontSize: 40 }}>📖</div>
          <div className="seg-title">Разбираю книгу…</div>
          <p className="meta">Делю на главы и отрезки. Обычно меньше минуты — результат продублирую в чат.</p>
          <div className="progress"><div className="bar"><div className="fill" style={{ width: "55%" }} /></div></div>
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
        <p className="muted">Файл или бумажная — как удобнее. Текст файла видишь только ты.</p>
        {book?.parse_status === "failed" && (
          <div className="card mt-12" style={{ border: "1px solid rgba(255,107,107,.35)" }}>
            <b>Не получилось разобрать файл</b>
            <p className="small muted" style={{ margin: "6px 0 0" }}>{data.parse_error || "Попробуй другой файл или добавь книгу как бумажную."}</p>
          </div>
        )}
        {mode === "choose" ? (
          <>
            <div className="drop mt-16" onClick={() => fileRef.current?.click()}>
              <IUpload size={30} />
              <div style={{ fontWeight: 700, marginTop: 8 }}>Загрузить epub или fb2</div>
              <div className="small muted">до {data.max_mb} МБ · или просто отправь файл боту в чат</div>
            </div>
            <button className="card row mt-12" style={{ width: "100%", textAlign: "left" }} onClick={() => setMode("paper")}>
              <IBook size={26} />
              <div className="grow">
                <b>У меня бумажная книга</b>
                <div className="small muted">Название, автор и число страниц — отрезки по страницам</div>
              </div>
            </button>
            <button className="btn ghost block mt-12" onClick={closeApp}>Отправить файл в чат</button>
          </>
        ) : (
          <PaperForm onDone={() => { invalidate(); st.reload(); setMode("choose"); }} onCancel={() => setMode("choose")} />
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
          onConfirmed={() => {
            invalidate();
            haptic("success");
            nav.tab("today");
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
          <span className="eyebrow">{b.source === "paper" ? "Бумажная книга" : b.source.toUpperCase()}</span>
          <span className="small muted">{pages(b.pages)}{b.chapters ? ` · ${b.chapters} глав` : ""}</span>
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

function PlanPicker({ onConfirmed }: { onConfirmed: () => void }) {
  const opts = useApi<{ options: PlanOption[] }>("/book/plan-options");
  const [days, setDays] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (opts.data && days === null) setDays(opts.data.options.find((o) => o.recommended)?.days ?? opts.data.options[0]?.days ?? null);
  }, [opts.data, days]);
  if (!opts.data) return <div className="mt-24"><ScreenSkeleton /></div>;
  return (
    <>
      <div className="section-title">Выбери срок</div>
      {opts.data.options.map((o) => (
        <button key={o.days} className={`option${days === o.days ? " on" : ""}`} onClick={() => { haptic("select"); setDays(o.days); }}>
          <span className="radio" />
          <div className="grow">
            <div className="row" style={{ gap: 8 }}>
              <b className="num" style={{ fontSize: 18 }}>{o.days} дней</b>
              {o.recommended && <span className="badge accent">рекомендуем</span>}
            </div>
            <div className="small muted">{decimal(o.pages_per_day)} стр. · ≈ {o.minutes_per_day} мин в день</div>
            {o.warning && <div className="tiny" style={{ color: "var(--accent)", marginTop: 4 }}>{o.warning}</div>}
          </div>
        </button>
      ))}
      <button
        className="btn primary block mt-16"
        disabled={!days || busy}
        onClick={async () => {
          setBusy(true);
          try {
            const r = await api.post<{ start: string | null; awaiting_payment: boolean }>("/book/plan", { days });
            toast(r.start ? `План готов. Старт — ${dayMonth(r.start)}` : r.awaiting_payment ? "План готов. Включится после оплаты" : "План готов");
            onConfirmed();
          } catch (e) {
            toast((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        Подтвердить план
      </button>
      <p className="tiny muted center mt-12">Нагрузка — до 20 страниц в день. Лучше всего держится 10–12.</p>
    </>
  );
}

function PlanSummary({ data, onReplan, onReplace }: { data: BookState; onReplan: () => void; onReplace: () => void }) {
  const nav = useNav();
  const [busy, setBusy] = useState(false);
  return (
    <div className="card mt-16">
      <div className="eyebrow">План</div>
      <div className="seg-title" style={{ fontSize: 20, margin: "6px 0" }}>{data.plan_days} дней</div>
      <div className="small muted">{data.start ? `Старт — ${dayMonth(data.start)}` : "Старт — в день начала забега, после оплаты"}</div>
      <div className="btn-row mt-16">
        <button className="btn secondary" onClick={onReplace}>Заменить книгу</button>
        <button
          className="btn ghost"
          disabled={busy}
          onClick={async () => {
            if (!(await confirmDialog(data.has_progress ? "Удалить текст книги? Пересказы и конспект останутся." : "Удалить книгу?"))) return;
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

function PaperForm({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [title, setTitle] = useState("");
  const [author, setAuthor] = useState("");
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
        <label>Сколько страниц</label>
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
