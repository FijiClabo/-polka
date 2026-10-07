import { useState } from "react";
import type { Conspect as ConspectData } from "../api";
import { Cover, Empty, ErrorState, ScreenSkeleton } from "../components/ui";
import { dayMonth } from "../format";
import { useApi } from "../hooks";

export default function Conspect({ bookId }: { bookId: number }) {
  const { data, error, loading, reload } = useApi<ConspectData>(bookId ? `/shelf/${bookId}` : null);
  const [open, setOpen] = useState<number | null>(null);
  if (error && !data) return <ErrorState message={error} onRetry={reload} />;
  if (loading || !data) return <ScreenSkeleton />;
  const b = data.book;
  return (
    <div className="screen no-tabs">
      <div className="row" style={{ gap: 16, alignItems: "flex-end" }}>
        <Cover title={b.title} author={b.author} color={b.spine_color} small />
        <div className="grow">
          <div className="eyebrow">Конспект</div>
          <div className="h-title" style={{ fontSize: 28, marginTop: 4 }}>{b.title}</div>
          {b.author && <div className="small muted">{b.author}</div>}
        </div>
      </div>
      <p className="small muted mt-16">Собран из твоих пересказов — твоими словами. Виден только тебе.</p>
      {data.intro && (
        <div className="card mt-12">
          <div className="eyebrow">Итог</div>
          <p style={{ margin: "8px 0 0", lineHeight: 1.55 }}>{data.intro}</p>
        </div>
      )}
      {data.items.length === 0 ? (
        <Empty icon="📝" title="Пока пусто" text="Каждый засчитанный пересказ добавит сюда пару строк." />
      ) : (
        <div className="col mt-16" style={{ gap: 10 }}>
          {data.items.map((it) => (
            <button key={it.day_number} className="card" style={{ textAlign: "left", width: "100%", margin: 0 }} onClick={() => setOpen(open === it.day_number ? null : it.day_number)}>
              <div className="row between">
                <b className="ellipsis">День {it.day_number} · {it.title}</b>
                <span className="tiny muted">{dayMonth(it.date, true)}</span>
              </div>
              <p style={{ margin: "8px 0 0", lineHeight: 1.5, color: "var(--text-2)" }}>{it.note}</p>
              {open === it.day_number && (
                <p className="small muted" style={{ marginTop: 10, whiteSpace: "pre-wrap" }}>Пересказ: {it.retelling}</p>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
