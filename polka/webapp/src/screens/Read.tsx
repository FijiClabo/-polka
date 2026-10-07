import { useEffect, useRef, useState } from "react";
import { IArrow, IBack } from "../components/Icons";
import { ErrorState, Sheet, Skeleton } from "../components/ui";
import { useApi, useLocal } from "../hooks";
import { useNav } from "../nav";

interface ReadData {
  day_number: number;
  title: string;
  book_title: string;
  author: string;
  minutes: number;
  page_from: number;
  page_to: number;
  paragraphs: { t: "p" | "h"; text: string }[];
  accepted: boolean;
  plan_days: number;
}

const SIZES = [16, 18, 20, 23];

export default function Read({ day }: { day: number }) {
  const nav = useNav();
  const { data, error, reload } = useApi<ReadData>(day ? `/read/${day}` : null);
  const [size, setSize] = useLocal<number>("reader.size", 1);
  const [lh, setLh] = useLocal<number>("reader.lh", 1.6);
  const [progress, setProgress] = useState(0);
  const [aa, setAa] = useState(false);
  const restored = useRef(false);
  const posKey = `reader.pos.${day}`;

  useEffect(() => {
    const onScroll = () => {
      const max = document.documentElement.scrollHeight - window.innerHeight;
      const p = max > 0 ? Math.min(1, window.scrollY / max) : 1;
      setProgress(p);
      try {
        localStorage.setItem(posKey, String(window.scrollY));
      } catch {
        /* ignore */
      }
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [posKey]);

  useEffect(() => {
    if (!data || restored.current) return;
    restored.current = true;
    const y = Number(localStorage.getItem(posKey) || 0);
    if (y > 0) requestAnimationFrame(() => window.scrollTo({ top: y }));
  }, [data, posKey]);

  if (error) return <div className="reader"><ErrorState message={error} onRetry={reload} /></div>;

  const minutesLeft = data ? Math.max(1, Math.round(data.minutes * (1 - progress))) : 0;

  return (
    <div className="reader">
      <div className="reader-top">
        <div className="row between">
          <button className="icon-btn" onClick={nav.back} aria-label="Назад">
            <IBack size={20} />
          </button>
          <span className="small muted">{data ? `${shortTitle(data.title)} · ${minutesLeft} мин` : "…"}</span>
          <button className="icon-btn" onClick={() => setAa(true)} aria-label="Шрифт" style={{ fontFamily: "var(--font-read)", fontSize: 16 }}>
            Aa
          </button>
        </div>
        <div className="bar">
          <i style={{ width: `${progress * 100}%` }} />
        </div>
      </div>

      <article className="reader-body" lang="ru">
        {!data ? (
          <>
            <Skeleton h={14} r={6} />
            <Skeleton h={80} />
            {Array.from({ length: 6 }, (_, i) => <Skeleton key={i} h={90} />)}
          </>
        ) : (
          <>
            <div className="book-name">{data.book_title}</div>
            <h1>{data.title}</h1>
            <div className="reader-text" style={{ fontSize: SIZES[size] ?? 18, lineHeight: lh }}>
              {data.paragraphs.map((p, i) => (p.t === "h" ? <h3 key={i}>{p.text}</h3> : <p key={i}>{p.text}</p>))}
            </div>
            <p className="small muted center mt-24">стр. {data.page_from}–{data.page_to} · конец отрезка</p>
          </>
        )}
      </article>

      {data && (
        <div className="reader-cta">
          {data.accepted ? (
            <button className="btn secondary block" onClick={nav.back}>
              Этот отрезок уже сдан ✓
            </button>
          ) : (
            <button className="btn primary block" onClick={() => nav.replace({ name: "retell" })}>
              Прочитано — рассказать <IArrow size={18} />
            </button>
          )}
        </div>
      )}

      <Sheet open={aa} onClose={() => setAa(false)}>
        <b>Размер текста</b>
        <div className="seg-switch" style={{ display: "flex", margin: "12px 0 18px" }}>
          {SIZES.map((s, i) => (
            <button key={s} className={size === i ? "on" : ""} style={{ flex: 1, fontSize: 12 + i * 2 }} onClick={() => setSize(i)}>
              А
            </button>
          ))}
        </div>
        <b>Интервал</b>
        <div className="seg-switch" style={{ display: "flex", margin: "12px 0 6px" }}>
          {[1.45, 1.6, 1.8].map((v, i) => (
            <button key={v} className={lh === v ? "on" : ""} style={{ flex: 1 }} onClick={() => setLh(v)}>
              {["Плотно", "Обычно", "Свободно"][i]}
            </button>
          ))}
        </div>
      </Sheet>
    </div>
  );
}

function shortTitle(t: string): string {
  return t.length > 26 ? t.slice(0, 24).trimEnd() + "…" : t;
}
