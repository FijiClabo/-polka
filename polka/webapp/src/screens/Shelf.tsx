import { useEffect, useState } from "react";
import { api, type Achievement, type ShelfData } from "../api";
import { IChevron } from "../components/Icons";
import { ErrorState, ScreenSkeleton, Shelf as ShelfViz, toast, type SpineItem } from "../components/ui";
import { dayMonth, plural } from "../format";
import { useApi } from "../hooks";
import { useNav } from "../nav";
import { haptic } from "../tg";

export default function Shelf() {
  const nav = useNav();
  const { data, error, loading, reload } = useApi<ShelfData>("/shelf");
  const ach = useApi<{ items: Achievement[] }>("/achievements");
  const [active, setActive] = useState<number | string | null>(null);

  useEffect(() => {
    if (!data || active !== null) return;
    const last = data.finished[data.finished.length - 1];
    setActive(last ? last.book_id : data.current ? data.current.book_id : null);
  }, [data, active]);

  if (error && !data) return <ErrorState message={error} onRetry={reload} />;
  if (loading || !data) return <ScreenSkeleton />;

  const spines: SpineItem[] = data.finished.map((b) => ({ id: b.book_id, color: b.spine_color, pages: b.pages }));
  if (data.current) spines.push({ id: data.current.book_id, color: data.current.spine_color, pages: data.current.pages, reading: true, progress: data.current.progress });
  const sel = [...data.finished, ...(data.current ? [data.current] : [])].find((b) => b.book_id === active) || null;
  const earned = ach.data?.items.filter((a) => a.earned).length ?? 0;

  return (
    <div className="screen">
      <h1 className="h-display">Полка</h1>
      <div className="small muted" style={{ marginTop: 4 }}>
        {data.finished_count} {plural(data.finished_count, "книга дочитана", "книги дочитано", "книг дочитано")}
        {data.retellings_count ? ` · ${data.retellings_count} ${plural(data.retellings_count, "пересказ", "пересказа", "пересказов")}` : ""}
      </div>

      <div style={{ margin: "26px 4px 0" }}>
        <ShelfViz items={spines} active={active} onPick={(id) => { haptic("select"); setActive(id); }} />
      </div>
      <div className="row between small mt-16">
        <span className="muted">{spines.length ? "Новая книга встанет на свободное место" : "Здесь появится первая дочитанная книга"}</span>
        {data.current && (
          <span style={{ color: "var(--accent)", fontWeight: 600 }}>Читаю · {Math.round(data.current.progress * 100)}%</span>
        )}
      </div>

      {sel && (
        <div className="card mt-16">
          <div className="eyebrow">
            {sel.status === "finished" ? `Дочитана ${dayMonth(sel.finished_at?.slice(0, 10))}` : `Читаю · ${Math.round(sel.progress * 100)}%`}
          </div>
          <div className="h-title" style={{ fontSize: 30, marginTop: 6 }}>{sel.title}</div>
          <div className="small muted" style={{ marginTop: 6 }}>
            {[sel.author, sel.plan_days ? `${sel.plan_days} ${plural(sel.plan_days, "день", "дня", "дней")}${sel.no_skips ? " без пропусков" : ""}` : null, sel.partner ? `с ${sel.partner}` : null]
              .filter(Boolean)
              .join(" · ")}
          </div>
          <div className="btn-row mt-16">
            <button className="btn primary" onClick={() => nav.push({ name: "conspect", params: { book: sel.book_id } })}>
              Конспект
            </button>
            {sel.status === "finished" ? (
              <button
                className="btn secondary"
                onClick={async () => {
                  try {
                    await api.post("/share/finish/send");
                    toast("Карточка в чате — перешли её друзьям");
                  } catch (e) {
                    toast((e as Error).message);
                  }
                }}
              >
                Поделиться
              </button>
            ) : (
              <button className="btn secondary" onClick={() => nav.tab("run")}>
                Забег
              </button>
            )}
          </div>
        </div>
      )}

      <button className="card row mt-12" style={{ width: "100%", textAlign: "left" }} onClick={() => nav.push({ name: "profile", params: { focus: "ach" } })}>
        <span style={{ fontSize: 26 }}>🏅</span>
        <div className="grow">
          <b>Значки</b>
          <div className="small muted">{earned} из 9 · за реальные действия, а не за вход в приложение</div>
        </div>
        <IChevron size={18} />
      </button>
    </div>
  );
}
