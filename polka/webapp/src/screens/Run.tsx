import { useEffect, useState } from "react";
import type { RunData, RunDay } from "../api";
import { ICheck, IFlameSolid, ISnow } from "../components/Icons";
import { Empty, ErrorState, ScreenSkeleton, Skeleton } from "../components/ui";
import { dayMonth, plural } from "../format";
import { useApi } from "../hooks";
import { useNav } from "../nav";
import { haptic } from "../tg";

const STATE_BADGE: Record<string, [string, string]> = {
  done: ["ok", "засчитан"],
  frozen: ["frozen", "заморозка"],
  missed: ["missed", "пропущен"],
  today: ["accent", "сегодня"],
};

export default function Run() {
  const nav = useNav();
  const { data, error, loading, reload } = useApi<RunData>("/run");
  const [sel, setSel] = useState<number | null>(null);

  useEffect(() => {
    if (!data?.days?.length || sel !== null) return;
    const done = data.days.filter((d) => d.state === "done");
    setSel(done.length ? done[done.length - 1].n : data.today_n && data.today_n > 0 ? Math.min(data.today_n, data.days.length) : 1);
  }, [data, sel]);

  if (error && !data) return <ErrorState message={error} onRetry={reload} />;
  if (loading || !data) return <ScreenSkeleton />;

  if (!data.days.length) {
    return (
      <div className="screen">
        <h1 className="h-display">Забег</h1>
        <Empty
          icon="🗓"
          title="План ещё не построен"
          text="Добавь книгу и выбери срок — здесь появится календарь на каждый день."
          action={<button className="btn primary" onClick={() => nav.push({ name: "book" })}>Книга и план</button>}
        />
      </div>
    );
  }

  const st = data.stats!;
  return (
    <div className="screen">
      <div className="small muted">
        {data.book?.title}
        {data.finish ? ` · до ${dayMonth(data.finish, true)}` : ""}
      </div>
      <div className="row between" style={{ marginTop: 4 }}>
        <h1 className="h-display">Забег</h1>
        {data.book && (
          <button className="btn secondary small" onClick={() => nav.push({ name: "conspect", params: { book: data.book!.id } })}>
            Конспект
          </button>
        )}
      </div>

      <div className="stats3 mt-16">
        <div className="stat">
          <div className="v">
            {st.done}
            <small>/{data.plan_days}</small>
          </div>
          <div className="l">{plural(st.done, "день сдан", "дня сдано", "дней сдано")}</div>
        </div>
        <div className="stat">
          <div className="v" style={{ color: "var(--accent-1)" }}>{st.streak}</div>
          <div className="l">стрик</div>
        </div>
        <div className="stat">
          <div className="v" style={{ color: "var(--blue)" }}>{st.freezes_left}</div>
          <div className="l">{plural(st.freezes_left, "заморозка", "заморозки", "заморозок")}</div>
        </div>
      </div>

      <div className="card mt-12">
        <div className="grid-days">
          {data.days.map((d) => (
            <button
              key={d.n}
              className={`day-cell ${d.state}${sel === d.n ? " selected" : ""}${d.grace ? " grace" : ""}`}
              onClick={() => {
                haptic("select");
                setSel(d.n);
              }}
            >
              {d.state === "frozen" ? <ISnow size={16} /> : d.n}
            </button>
          ))}
        </div>
        {data.days.some((d) => d.grace) && <p className="tiny muted mt-12">Точки — дни отсрочки: можно догнать и сдавать по два отрезка в день.</p>}
      </div>

      {sel !== null && <DayDetail n={sel} />}
    </div>
  );
}

function DayDetail({ n }: { n: number }) {
  const { data, loading, error } = useApi<RunDay>(`/run/day/${n}`);
  if (loading) return <Skeleton h={150} />;
  if (error || !data) return null;
  const badge = data.state ? STATE_BADGE[data.state] : null;
  const r = data.retelling;
  return (
    <div className="card">
      <div className="row between">
        <b>
          День {n}
          {data.segment ? ` · ${data.segment.title}` : ""}
        </b>
        {badge && <span className={`badge ${badge[0]}`}>{badge[1]}</span>}
      </div>
      <div className="tiny muted" style={{ marginTop: 4 }}>{dayMonth(data.date)}</div>
      {r ? (
        <>
          <p className="quote">«{r.text.split("\n— ")[0]}»</p>
          {r.reply && (
            <div className="ai-bubble">
              <span className="flame-avatar">
                <IFlameSolid size={16} />
              </span>
              <div className="bubble">
                {r.reply}
                {r.question && <> {r.question}</>}
              </div>
            </div>
          )}
          {!r.verified && r.verdict === "accepted" && <p className="tiny muted mt-8">засчитано без сверки с текстом</p>}
        </>
      ) : (
        <p className="small muted mt-8">
          {data.state === "frozen"
            ? "Этот день прикрыла заморозка — стрик не сгорел."
            : data.state === "missed"
              ? "Пересказа не было. Отрезок можно догнать — он остаётся первым в очереди."
              : data.segment
                ? `стр. ${data.segment.page_from}–${data.segment.page_to} · ≈ ${data.segment.minutes} мин`
                : "Этот день ещё впереди."}
        </p>
      )}
      {data.state === "done" && !r && (
        <div className="row mt-8 small muted">
          <ICheck size={16} /> сдано
        </div>
      )}
    </div>
  );
}
