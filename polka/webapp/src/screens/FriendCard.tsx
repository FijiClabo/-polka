import { useState } from "react";
import { api, type Achievement, type FriendStatus, type ShelfData } from "../api";
import { AchIcon, IFlameSolid, IHand } from "../components/Icons";
import { Avatar, ErrorState, ScreenSkeleton, Shelf, toast } from "../components/ui";
import { STATUS_TEXT, firstName, plural } from "../format";
import { useApi } from "../hooks";
import { haptic } from "../tg";

interface Card {
  status: FriendStatus;
  achievements: Achievement[];
  shelf: ShelfData;
}

export default function FriendCard({ id }: { id: number }) {
  const { data, error, loading, reload } = useApi<Card>(`/friends/${id}`);
  const [sent, setSent] = useState(false);
  if (error && !data) return <ErrorState message={error} onRetry={reload} />;
  if (loading || !data) return <ScreenSkeleton />;
  const s = data.status;
  const spines = data.shelf.finished.map((b) => ({ id: b.book_id, color: b.spine_color, pages: b.pages }));
  if (data.shelf.current) spines.push({ id: data.shelf.current.book_id, color: data.shelf.current.spine_color, pages: data.shelf.current.pages, reading: true, progress: data.shelf.current.progress } as never);
  return (
    <div className="screen no-tabs">
      <div className="col" style={{ alignItems: "center", textAlign: "center", marginTop: 10 }}>
        <Avatar name={s.name} url={s.photo_url} size={88} seed={s.user_id} ring={s.today === "done" ? "green" : undefined} />
        <h2 style={{ margin: "8px 0 0" }}>{s.name}</h2>
        <div className="small muted">{STATUS_TEXT[s.today]}{s.done_at ? ` в ${s.done_at}` : ""}</div>
      </div>
      <div className="stats3 mt-24">
        <div className="stat">
          <div className="v" style={{ color: "var(--accent-1)", display: "flex", alignItems: "center", gap: 4 }}>
            <IFlameSolid size={20} />
            {s.streak}
          </div>
          <div className="l">стрик</div>
        </div>
        <div className="stat">
          <div className="v">{data.shelf.finished_count}</div>
          <div className="l">{plural(data.shelf.finished_count, "книга", "книги", "книг")}</div>
        </div>
        <div className="stat">
          <div className="v">{data.achievements.filter((a) => a.earned).length}</div>
          <div className="l">значков</div>
        </div>
      </div>
      {s.book_title && (
        <div className="card mt-12">
          <div className="eyebrow">Читает</div>
          <div className="h-title" style={{ fontSize: 26, marginTop: 4 }}>{s.book_title}</div>
          <div className="small muted">
            {[s.book_author, s.plan_day && s.plan_days ? `день ${Math.min(s.plan_day, s.plan_days)} из ${s.plan_days}` : null].filter(Boolean).join(" · ")}
          </div>
        </div>
      )}
      {(s.today === "reading" || s.today === "burned") && !sent && (
        <button
          className="btn secondary block mt-12"
          onClick={async () => {
            haptic("light");
            const r = await api.post<{ result: string }>(`/friends/${id}/nudge`).catch(() => ({ result: "error" }));
            setSent(true);
            toast(r.result === "ok" ? `${firstName(s.name)} получит толчок` : r.result === "disabled" ? "Толчки отключены" : "Сегодня уже толкали");
          }}
        >
          <IHand size={18} /> Толкнуть
        </button>
      )}
      {spines.length > 0 && (
        <>
          <div className="section-title">Полка</div>
          <div className="card">
            <Shelf items={spines} slot={false} height={150} />
          </div>
        </>
      )}
      <div className="section-title">Значки</div>
      <div className="ach-grid">
        {data.achievements.map((a) => (
          <div key={a.code} className={`ach${a.earned ? " on" : ""}`}>
            <div className="e"><AchIcon code={a.code} /></div>
            <div className="t">{a.title}</div>
          </div>
        ))}
      </div>
      <p className="tiny muted center mt-16">Пересказы не хранятся и никому не видны. Друзьям видны книга, стрик, значки и полка.</p>
    </div>
  );
}
