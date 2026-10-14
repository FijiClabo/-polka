import { useEffect, useState } from "react";
import type { RunData, RunDay } from "../api";
import { ICheck, IFlameSolid, ISnow } from "../components/Icons";
import { OpenBookIll } from "../components/Illustrations";
import { Empty, ErrorState, ScreenSkeleton, Skeleton } from "../components/ui";
import { dayMonth, plural } from "../format";
import { useApi } from "../hooks";
import { useNav } from "../nav";
import { haptic } from "../tg";

const STATE_BADGE: Record<string, [string, string]> = {
  done: ["ok", "засчитан"],
  frozen: ["frozen", "заморозка"],
  missed: ["missed", "пропущен"],
  ahead: ["ok", "сдан заранее"],
  paused: ["frozen", "пауза"],
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
          icon={<OpenBookIll />}
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
      <h1 className="h-display" style={{ marginTop: 4 }}>Забег</h1>

      <div className="stats3 mt-16">
        <div className="stat">
          <div className="v">
            {st.segments_done}
            <small>/{data.plan_days}</small>
          </div>
          <div className="l">{plural(st.segments_done, "отрезок сдан", "отрезка сдано", "отрезков сдано")}</div>
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
              {d.state === "frozen" ? <ISnow size={16} /> : d.plan_day ?? "–"}
            </button>
          ))}
        </div>
        {data.days.some((d) => d.grace) && <p className="tiny muted mt-12">Точки — дни отсрочки: можно догнать, сдавая по несколько отрезков в день.</p>}
        {data.days.some((d) => d.state === "ahead") && <p className="tiny muted mt-8">Светлая галочка — отрезок дня был сдан заранее: день не сгорел.</p>}
        {data.days.some((d) => d.state === "paused") && <p className="tiny muted mt-8">Голубая рамка — день паузы: не сгорел, план сдвинут.</p>}
      </div>

      {data.state !== "finished" && data.state !== "expired" && (
        <button className="card row" style={{ width: "100%", textAlign: "left" }} onClick={() => nav.push({ name: "pace" })}>
          <div className="grow">
            <b>Свой темп</b>
            <div className="small muted">
              {st.behind > 1
                ? `Позади ${st.behind - 1} ${plural(st.behind - 1, "отрезок", "отрезка", "отрезков")} — пересобери план или поставь паузу`
                : st.ahead > 0
                  ? `Впереди плана на ${st.ahead} ${plural(st.ahead, "отрезок", "отрезка", "отрезков")}`
                  : "Минуты в день, глава в день, к дате или пауза"}
            </div>
          </div>
        </button>
      )}

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
          {data.plan_day ? `День ${data.plan_day}` : "Вне плана"}
          {data.segment ? ` · ${data.segment.title}` : ""}
        </b>
        {badge && <span className={`badge ${badge[0]}`}>{badge[1]}</span>}
      </div>
      <div className="tiny muted" style={{ marginTop: 4 }}>{dayMonth(data.date)}</div>
      {r ? (
        <>
          <div className="row mt-12 small" style={{ gap: 8 }}>
            {r.verdict === "accepted" ? <span className="check-mini"><ICheck size={14} /></span> : <span className="flame-avatar" style={{ width: 22, height: 22 }}><IFlameSolid size={12} /></span>}
            <span>
              {r.verdict === "accepted"
                ? `Пересказ засчитан${r.source === "voice" ? " · голосом" : " · текстом"}${data.segments_today > 1 ? ` · за день ${data.segments_today} ${plural(data.segments_today, "отрезок", "отрезка", "отрезков")}` : ""}`
                : r.verdict === "clarify"
                  ? "Ждём ответ на уточняющий вопрос"
                  : r.verdict === "rejected"
                    ? "Пересказ не засчитан — можно сдать заново"
                    : "Пересказ на проверке"}
            </span>
          </div>
          {r.verdict === "clarify" && r.question && (
            <div className="ai-bubble">
              <span className="flame-avatar">
                <IFlameSolid size={16} />
              </span>
              <div className="bubble">{r.question}</div>
            </div>
          )}
          {!r.verified && r.verdict === "accepted" && <p className="tiny muted mt-8">засчитано без сверки с текстом</p>}
          <p className="tiny muted mt-8">Тексты пересказов не храним — только отметку о сдаче.</p>
        </>
      ) : (
        <p className="small muted mt-8">
          {data.state === "frozen"
            ? "Этот день прикрыла заморозка — стрик не сгорел."
            : data.state === "ahead"
              ? "Отрезок этого дня был сдан заранее — день не сгорел, стрик цел."
              : !data.plan_day
                ? "Пауза или сдвиг плана — этот день не считается."
            : data.state === "missed"
              ? "Пересказа не было. Отрезок можно догнать — он остаётся первым в очереди, или пересобрать план в «Свой темп»."
              : data.segment
                ? `${data.segment.place} · ≈ ${data.segment.minutes} мин`
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
