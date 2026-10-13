import { useState } from "react";
import { api, type PaceInfo } from "../api";
import { ErrorState, ScreenSkeleton, toast } from "../components/ui";
import { dayMonth, days as fmtDays, parseDate, plural } from "../format";
import { invalidate, useApi } from "../hooks";
import { useNav } from "../nav";
import { confirmDialog, haptic } from "../tg";

type Choice = { kind: "minutes"; minutes: number } | { kind: "chapter" } | { kind: "date"; date: string };

const segWord = (n: number) => `${n} ${plural(n, "отрезок", "отрезка", "отрезков")}`;

function addDays(iso: string, n: number): string {
  const d = parseDate(iso);
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function daysBetween(fromIso: string, toIso: string): number {
  return Math.round((parseDate(toIso).getTime() - parseDate(fromIso).getTime()) / 86400000);
}

export default function Pace({ focus }: { focus?: string }) {
  const { data, error, loading, reload } = useApi<PaceInfo>("/pace");
  if (error && !data) return <ErrorState message={error} onRetry={reload} />;
  if (loading || !data) return <ScreenSkeleton />;
  return <PaceView info={data} focus={focus} reload={reload} />;
}

function PaceView({ info, focus, reload }: { info: PaceInfo; focus?: string; reload: () => void }) {
  const nav = useNav();
  const [choice, setChoice] = useState<Choice | null>(null);
  const [busy, setBusy] = useState(false);
  const showPartial = info.today.segment && info.today.partial.length > 0;

  if (!info.left) {
    return (
      <div className="screen no-tabs">
        <h1 className="h-display">Темп чтения</h1>
        <p className="muted">Все отрезки уже сданы — книга дочитана.</p>
      </div>
    );
  }

  const preview = (() => {
    if (!choice) return null;
    if (choice.kind === "minutes") {
      const o = info.minute_options.find((x) => x.minutes === choice.minutes)!;
      return { days: o.days, finish: o.finish, minutes: choice.minutes };
    }
    if (choice.kind === "chapter" && info.chapters) {
      return { days: info.chapters.days, finish: info.chapters.finish, minutes: null };
    }
    if (choice.kind === "date" && choice.date) {
      const n = daysBetween(info.first_day, choice.date) + 1;
      if (n < 1) return { days: 0, finish: choice.date, minutes: null, error: "Эта дата уже прошла" };
      if (n > 365) return { days: n, finish: choice.date, minutes: null, error: "Больше года — слишком долго" };
      return { days: n, finish: choice.date, minutes: Math.max(1, Math.round(info.minutes_left / n)) };
    }
    return null;
  })();

  const meet = info.club?.meet_date;
  const lateForClub = preview && meet && preview.finish > meet;

  const apply = async () => {
    if (!choice) return;
    setBusy(true);
    try {
      const body = choice.kind === "minutes" ? { minutes: choice.minutes } : choice.kind === "chapter" ? { by_chapter: true } : { finish: choice.date };
      const r = await api.post<PaceInfo>("/pace", body);
      haptic("success");
      toast(r.finish ? `Готово: финиш ${dayMonth(r.finish)}. Сданное и стрик на месте` : "Готово");
      invalidate("/today");
      invalidate("/run");
      setChoice(null);
      reload();
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="screen no-tabs pace">
      <div className="eyebrow mt-8">Свой темп</div>
      <h1 className="h-title" style={{ marginTop: 4 }}>Темп чтения</h1>

      <div className="card mt-12">
        <div className="row between">
          <div>
            <div className="pace-big">≈ {info.minutes_per_day} мин</div>
            <div className="small muted">в день сейчас</div>
          </div>
          <div style={{ textAlign: "right" }}>
            <div className="pace-big">{info.finish ? dayMonth(info.finish) : "—"}</div>
            <div className="small muted">финиш</div>
          </div>
        </div>
        <div className="small mt-12">
          Сдано {segWord(info.done)} из {info.plan_days}, впереди {segWord(info.left)}.
        </div>
        {info.behind > 1 && (
          <p className="small mt-8" style={{ color: "var(--accent-text)", marginBottom: 0 }}>
            Позади {segWord(info.behind - 1)}. Новый темп начнётся с первого несданного — без долгов.
          </p>
        )}
        {info.ahead > 0 && (
          <p className="small mt-8" style={{ color: "var(--green-text)", marginBottom: 0 }}>
            Ты впереди плана на {segWord(info.ahead)}.
          </p>
        )}
      </div>

      {showPartial && <PartialCard info={info} open={focus === "partial"} />}

      <div className="section-title">Сколько читать в день</div>
      <div className="pace-grid">
        {info.minute_options.map((o) => {
          const on = choice?.kind === "minutes" && choice.minutes === o.minutes;
          return (
            <button key={o.minutes} className={`pace-chip${on ? " on" : ""}`} onClick={() => { haptic("select"); setChoice({ kind: "minutes", minutes: o.minutes }); }}>
              <b>{o.minutes} мин</b>
              <span>до {dayMonth(o.finish, true)}</span>
            </button>
          );
        })}
      </div>

      {info.chapters && (
        <button
          className={`option mt-12${choice?.kind === "chapter" ? " on" : ""}`}
          onClick={() => { haptic("select"); setChoice({ kind: "chapter" }); }}
        >
          <span className="radio" />
          <div className="grow">
            <b>Глава в день</b>
            <div className="small muted">
              {fmtDays(info.chapters.days)}, от {info.chapters.min_minutes} до {info.chapters.max_minutes} мин · до {dayMonth(info.chapters.finish, true)}
            </div>
          </div>
        </button>
      )}

      <button
        className={`option mt-8${choice?.kind === "date" ? " on" : ""}`}
        onClick={() => { haptic("select"); setChoice({ kind: "date", date: choice?.kind === "date" ? choice.date : meet || addDays(info.first_day, info.left - 1) }); }}
      >
        <span className="radio" />
        <div className="grow">
          <b>Дочитать к дате</b>
          <div className="small muted">{meet ? `Например, к встрече клуба — ${dayMonth(meet)}` : "Например, к отпуску или к встрече клуба"}</div>
          {choice?.kind === "date" && (
            <input
              className="input mt-8"
              type="date"
              value={choice.date}
              min={info.first_day}
              onClick={(e) => e.stopPropagation()}
              onChange={(e) => setChoice({ kind: "date", date: e.target.value })}
            />
          )}
        </div>
      </button>

      {preview && (
        <div className="card soft mt-12">
          {"error" in preview && preview.error ? (
            <b style={{ color: "var(--accent-text)" }}>{preview.error}</b>
          ) : (
            <>
              <b>
                {fmtDays(preview.days)}
                {preview.minutes ? ` · ≈ ${preview.minutes} мин в день` : ""} · финиш {dayMonth(preview.finish)}
              </b>
              <div className="small muted mt-8">
                Остаток книги перестроится. Сданные отрезки, стрик и прошлые дни останутся как были.
              </div>
              {info.pair && <div className="small mt-8">{info.pair.name} останется на своём плане: дни у вас разойдутся, но прогресс друг друга будет виден.</div>}
              {lateForClub && <div className="small mt-8" style={{ color: "var(--accent-text)" }}>Финиш позже встречи клуба ({dayMonth(meet!)}).</div>}
            </>
          )}
          <button className="btn primary block mt-12" disabled={busy || ("error" in preview && !!preview.error)} onClick={apply}>
            Пересобрать план
          </button>
        </div>
      )}

      {info.pause_options.length > 0 && (
        <>
          <div className="section-title">Пауза</div>
          <p className="small muted" style={{ marginTop: -4 }}>
            Уезжаешь или болеешь — план сдвинется целиком. Дни паузы не сгорят, стрик и заморозки целы.
          </p>
          <div className="pace-grid">
            {info.pause_options.map((o) => (
              <button
                key={o.days}
                className="pace-chip"
                disabled={busy}
                onClick={async () => {
                  if (!(await confirmDialog(`Пауза на ${fmtDays(o.days)}? Финиш сдвинется на ${dayMonth(o.finish)}.`))) return;
                  setBusy(true);
                  try {
                    await api.post("/pace/pause", { days: o.days });
                    haptic("success");
                    toast(`Пауза на ${fmtDays(o.days)}. Финиш — ${dayMonth(o.finish)}`);
                    invalidate("/today");
                    invalidate("/run");
                    reload();
                  } catch (e) {
                    toast((e as Error).message);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <b>{o.days === 7 ? "Неделя" : fmtDays(o.days)}</b>
                <span>до {dayMonth(o.finish, true)}</span>
              </button>
            ))}
          </div>
        </>
      )}

      <div className="card mt-16">
        <b>Прочитано больше, чем нужно?</b>
        <p className="small muted" style={{ margin: "6px 0 0" }}>
          Перескажи и следующий отрезок — он засчитается заранее. В дни, когда всё сдано заранее, можно отдыхать: стрик не сгорит.
          За день — до пяти отрезков.
        </p>
        <button className="btn secondary block mt-12" onClick={() => nav.tab("today")}>На главный</button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ прочитано не всё

export function PartialCard({ info, open }: { info: PaceInfo; open: boolean }) {
  const nav = useNav();
  const seg = info.today.segment!;
  const [q, setQ] = useState<number | null>(null);
  const [rest, setRest] = useState<"tomorrow" | "day">(info.today.has_next ? "tomorrow" : "day");
  const [busy, setBusy] = useState(false);
  const [shown, setShown] = useState(open);

  if (!shown) {
    return (
      <button className="card row mt-12" style={{ width: "100%", textAlign: "left" }} onClick={() => setShown(true)}>
        <div className="grow">
          <b>Прочитано не всё?</b>
          <div className="small muted">Перескажи только прочитанное — остаток перенесём</div>
        </div>
      </button>
    );
  }

  return (
    <div className="card mt-12 partial">
      <div className="eyebrow">Сегодня · {seg.title}</div>
      <b className="mt-8" style={{ display: "block" }}>Сколько прочитано?</b>
      <div className="pace-grid mt-8">
        {info.today.partial.map((o) => (
          <button key={o.quarter} className={`pace-chip${q === o.quarter ? " on" : ""}`} onClick={() => { haptic("select"); setQ(o.quarter); }}>
            <b>{o.page ? `до ${o.page}` : o.label}</b>
            <span>{o.page ? "страницы" : `${o.quarter * 25}% отрезка`}</span>
          </button>
        ))}
      </div>
      {q && (
        <>
          <b className="mt-16" style={{ display: "block" }}>Куда деть остаток</b>
          {info.today.has_next && (
            <button className={`option mt-8${rest === "tomorrow" ? " on" : ""}`} onClick={() => setRest("tomorrow")}>
              <span className="radio" />
              <div className="grow">
                <b>К следующему отрезку</b>
                <div className="small muted">Финиш не сдвинется, следующий отрезок станет чуть длиннее</div>
              </div>
            </button>
          )}
          <button className={`option mt-8${rest === "day" ? " on" : ""}`} onClick={() => setRest("day")}>
            <span className="radio" />
            <div className="grow">
              <b>Отдельным днём</b>
              <div className="small muted">План станет на день длиннее, финиш — на день позже</div>
            </div>
          </button>
          <button
            className="btn primary block mt-12"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await api.post("/pace/partial", { quarter: q, rest });
                haptic("success");
                toast("Готово: сегодня перескажи только прочитанное");
                invalidate("/today");
                invalidate("/run");
                nav.tab("today");
              } catch (e) {
                toast((e as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            Перескажу прочитанное
          </button>
        </>
      )}
    </div>
  );
}
