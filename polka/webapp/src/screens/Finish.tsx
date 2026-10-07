import { useState } from "react";
import { api, type FinishData } from "../api";
import { Confetti, ErrorState, ScreenSkeleton, Sheet, Shelf, toast, type SpineItem } from "../components/ui";
import { dayMonth, days, firstName } from "../format";
import { invalidate, useApi } from "../hooks";
import { useNav } from "../nav";
import { canShareStory, haptic, shareStory } from "../tg";

export default function Finish() {
  const nav = useNav();
  const { data, error, loading, reload } = useApi<FinishData>("/finish");
  const [share, setShare] = useState(false);
  const [busy, setBusy] = useState(false);

  if (error && !data) return <ErrorState message={error} onRetry={reload} />;
  if (loading || !data) return <ScreenSkeleton />;

  const spines: SpineItem[] = data.shelf.slice(-5).map((s, i, arr) => ({ id: i, color: s.color, pages: s.pages, isNew: i === arr.length - 1 }));
  if (!spines.length && data.book) spines.push({ id: 0, color: data.book.spine_color, pages: data.book.pages, isNew: true });
  const range = data.start && data.finished_at ? `${dayMonth(data.start)} — ${dayMonth(data.finished_at.slice(0, 10))}` : "";

  const sendToChat = async (size: "story" | "post") => {
    setBusy(true);
    try {
      await api.post(`/share/finish/send?size=${size}`);
      toast("Карточка в чате — перешли её или сохрани");
      setShare(false);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const toStory = async () => {
    setBusy(true);
    try {
      const r = await api.get<{ url: string }>("/share/finish/link?size=story");
      shareStory(r.url, `Дочитано: «${data.book?.title ?? ""}»`);
      setShare(false);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="finish">
      <Confetti />
      <div className="eyebrow">{range}</div>
      <h1>Дочитано!</h1>
      <div className="muted">«{data.book?.title}» за {days(data.plan_days)}</div>
      <div style={{ margin: "56px 20px 0" }}>
        <Shelf items={spines} slot={false} height={190} />
      </div>
      <div className="stats-row mt-24">
        <div className="stat">
          <div className="v" style={{ color: "var(--accent-1)" }}>{data.best_streak}</div>
          <div className="l">дней подряд</div>
        </div>
        <div className="stat">
          <div className="v">{data.retellings}</div>
          <div className="l">пересказов</div>
        </div>
        <div className="stat">
          <div className="v" style={{ color: "var(--blue)", fontSize: data.partner ? 22 : 26 }}>{data.partner ? firstName(data.partner.name) : "соло"}</div>
          <div className="l">{data.partner ? "напарник" : "без напарника"}</div>
        </div>
      </div>
      <div style={{ flex: 1, minHeight: 24 }} />
      <button className="btn primary block" onClick={() => { haptic("medium"); setShare(true); }}>Поделиться</button>
      <div className="btn-row mt-12">
        <button className="btn secondary" onClick={() => nav.tab("shelf")}>Полка</button>
        <button
          className="btn secondary"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await api.post("/runs/new");
              invalidate();
              nav.replace({ name: "book" });
            } catch (e) {
              toast((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          Следующая книга
        </button>
      </div>

      <Sheet open={share} onClose={() => setShare(false)}>
        <b style={{ fontSize: 18 }}>Поделиться финишем</b>
        <p className="small muted">Карточка с книгой, стриком и полкой.</p>
        <div className="col">
          {canShareStory() && <button className="btn primary block" disabled={busy} onClick={toStory}>В сторис Telegram</button>}
          <button className="btn secondary block" disabled={busy} onClick={() => sendToChat("story")}>Прислать картинку в чат (сторис)</button>
          <button className="btn secondary block" disabled={busy} onClick={() => sendToChat("post")}>Прислать картинку в чат (пост)</button>
        </div>
      </Sheet>
    </div>
  );
}
