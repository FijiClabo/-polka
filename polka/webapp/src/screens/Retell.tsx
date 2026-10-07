import { useEffect, useState } from "react";
import { api, type RetellResult, type Today } from "../api";
import { ICheck, IClose, IFlameSolid, IRefresh } from "../components/Icons";
import { BARS, fmtTime, useRecorder } from "../components/recorder";
import { ErrorState, ScreenSkeleton } from "../components/ui";
import { invalidate, useApi } from "../hooks";
import { useNav } from "../nav";
import { closeApp, haptic } from "../tg";

const MAX_SEC = 180;
const MIN_CHARS = 51; // «длиннее 50 знаков», как в правилах бота

const ACH: Record<string, [string, string]> = {
  first_page: ["📖", "Первая страница"],
  week: ["🔥", "Неделя"],
  two_weeks: ["⚡", "Две недели"],
  iron: ["🛡", "Железный"],
  duet: ["🤝", "Дуэт"],
  kept_word: ["💎", "Месяц вдвоём"],
  comeback: ["🌱", "Возвращение"],
  brought_friend: ["💌", "Друг в деле"],
  finish: ["🏁", "Финиш"],
};

type Phase = "compose" | "sending" | "result";

export default function Retell() {
  const nav = useNav();
  const today = useApi<Today>("/today");
  const [mode, setMode] = useState<"voice" | "text">(nav.me.user.retell_format === "text" || !nav.me.features.voice ? "text" : "voice");
  const [text, setText] = useState("");
  const [phase, setPhase] = useState<Phase>("compose");
  const [result, setResult] = useState<RetellResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [question, setQuestion] = useState<string | null>(null);
  const rec = useRecorder(MAX_SEC);

  const t = today.data;
  useEffect(() => {
    if (t?.state === "clarify" && t.open_retelling?.question) setQuestion(t.open_retelling.question);
  }, [t]);
  useEffect(() => {
    if (rec.state === "denied" || rec.state === "unsupported") setMode("text");
  }, [rec.state]);

  if (today.error && !t) return <ErrorState message={today.error} onRetry={today.reload} />;
  if (!t) return <ScreenSkeleton />;

  const canSubmit = ["to_read", "clarify"].includes(t.state) || phase !== "compose";
  const seg = t.segment;
  const answering = !!question;
  const minChars = answering ? 2 : MIN_CHARS;

  const finish = (r: RetellResult) => {
    setResult(r);
    setPhase("result");
    invalidate("/today");
    invalidate("/run");
    invalidate("/friends");
    invalidate("/pair");
    if (r.status === "accepted") haptic("success");
    else if (r.status === "rejected") haptic("error");
    else haptic("warning");
    if (r.status === "clarify") setQuestion(r.question);
  };

  const sendText = async () => {
    setError(null);
    setPhase("sending");
    try {
      finish(await api.post<RetellResult>("/retell", { text }));
      setText("");
    } catch (e) {
      setError((e as Error).message);
      setPhase("compose");
    }
  };

  const sendVoice = async () => {
    if (!rec.blob) return;
    setError(null);
    setPhase("sending");
    const fd = new FormData();
    const ext = rec.blob.type.includes("mp4") || rec.blob.type.includes("aac") ? "m4a" : rec.blob.type.includes("ogg") ? "ogg" : "webm";
    fd.append("audio", rec.blob, `voice.${ext}`);
    fd.append("duration", String(Math.round(rec.seconds)));
    try {
      finish(await api.form<RetellResult>("/retell/voice", fd));
      rec.reset();
    } catch (e) {
      setError((e as Error).message);
      setPhase("compose");
    }
  };

  // ------------------------------------------------------------------ результат
  if (phase === "result" && result) {
    return <ResultView r={result} onAgain={() => { setResult(null); setPhase("compose"); }} />;
  }

  // ------------------------------------------------------------------ недоступно
  if (!canSubmit) {
    return (
      <div className="retell">
        <TopBar />
        <div className="state">
          <div className="ill">{t.state === "done_today" ? "✅" : "📚"}</div>
          <h3>{t.state === "done_today" ? "На сегодня всё сдано" : "Сейчас пересказывать нечего"}</h3>
          <p>{t.state === "done_today" ? (t.next_segment ? `Завтра: «${t.next_segment.title}».` : "Отличная работа.") : "Загляни на главный экран — там видно, что дальше."}</p>
          <button className="btn secondary" onClick={nav.back}>Назад</button>
        </div>
      </div>
    );
  }

  const prompt = answering
    ? question!
    : seg?.retell_prompt || "Что было в этом отрезке? Расскажи своими словами.";
  const recording = rec.state === "recording";

  return (
    <div className="retell">
      <TopBar />
      <div className="row" style={{ marginTop: 18 }}>
        <span className="flame-avatar">
          <IFlameSolid size={16} />
        </span>
        <span className="small muted">{answering ? "Уточнение" : `Вопрос по отрезку · ${seg?.title ?? ""}`}</span>
      </div>
      <div className="prompt">{prompt}</div>

      {phase === "sending" ? (
        <div className="state" style={{ paddingTop: 30 }}>
          <div className="rec-btn on" style={{ margin: "0 auto 26px", width: 84, height: 84 }}>
            <IFlameSolid size={34} />
          </div>
          <h3>{mode === "voice" ? "Слушаю…" : "Читаю…"}</h3>
          <p>Пара секунд — сверяюсь с отрезком.</p>
        </div>
      ) : mode === "voice" ? (
        <>
          <div className="live">
            {rec.state === "recorded"
              ? "Запись готова. Отправить?"
              : recording
                ? "Говори свободно — 30–60 секунд обычно хватает."
                : answering
                  ? "Ответь одним-двумя предложениями."
                  : "Нажми на кнопку и расскажи, что запомнилось: события, мысли, детали."}
          </div>
          <div style={{ flex: 1 }} />
          <div className="wave" aria-hidden>
            {rec.levels.map((l, i) => (
              <i key={i} style={{ height: `${Math.round(8 + l * 60)}px` }} className={!recording && rec.state !== "recorded" ? "rest" : ""} />
            ))}
            {Array.from({ length: Math.max(0, 46 - BARS) }, (_, i) => (
              <i key={`r${i}`} className="rest" style={{ height: 6 }} />
            ))}
          </div>
          <div className="timer">
            {fmtTime(rec.seconds)}
            <small>из {fmtTime(MAX_SEC)}</small>
          </div>
          <div className="rec-controls">
            <button className="rec-side" aria-label="Заново" onClick={rec.reset} disabled={rec.state === "idle"} style={{ opacity: rec.state === "idle" ? 0.4 : 1 }}>
              <IRefresh size={22} />
            </button>
            <button
              className={`rec-btn${recording ? " on" : ""}`}
              aria-label={recording ? "Остановить" : "Записать"}
              onClick={() => {
                haptic("medium");
                if (recording) rec.stop();
                else rec.start();
              }}
            >
              {recording ? <span className="sq" /> : <MicGlyph />}
            </button>
            <button
              className="rec-side ok"
              aria-label="Отправить"
              disabled={rec.state !== "recorded"}
              style={{ opacity: rec.state === "recorded" ? 1 : 0.35 }}
              onClick={sendVoice}
            >
              <ICheck size={22} />
            </button>
          </div>
        </>
      ) : (
        <>
          {(rec.state === "denied" || rec.state === "unsupported") && (
            <p className="small muted">
              Микрофон здесь недоступен. Напиши текстом или{" "}
              <button className="link" onClick={closeApp}>отправь голосовое боту в чат</button>.
            </p>
          )}
          <textarea
            className="textarea mt-8"
            placeholder={answering ? "Твой ответ…" : "Например: герой приехал в город и узнал, что…"}
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={4000}
            autoFocus
          />
          <div className={`counter${text.trim().length >= minChars ? " ok" : ""}`}>
            {text.trim().length < minChars ? `ещё ${minChars - text.trim().length} знаков` : "можно отправлять"}
          </div>
          <div style={{ flex: 1 }} />
          <button className="btn primary block mt-16" disabled={text.trim().length < minChars} onClick={sendText}>
            Отправить
          </button>
        </>
      )}

      {error && <p className="small center" style={{ color: "var(--danger)", marginTop: 12 }}>{error}</p>}

      {phase === "compose" && nav.me.features.voice && rec.state !== "unsupported" && (
        <div className="seg-switch">
          <button className={mode === "voice" ? "on" : ""} onClick={() => setMode("voice")} disabled={rec.state === "denied"}>
            Голос
          </button>
          <button className={mode === "text" ? "on" : ""} onClick={() => { rec.reset(); setMode("text"); }}>
            Текст
          </button>
        </div>
      )}
    </div>
  );
}

function TopBar() {
  const nav = useNav();
  return (
    <div className="row between">
      <button className="icon-btn" onClick={nav.back} aria-label="Закрыть">
        <IClose size={18} />
      </button>
      <b>Пересказ</b>
      <span style={{ width: 40 }} />
    </div>
  );
}

function MicGlyph() {
  return (
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="#1b0f08" strokeWidth="2" strokeLinecap="round">
      <rect x="9" y="3" width="6" height="11" rx="3" fill="#1b0f08" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" />
    </svg>
  );
}

// ------------------------------------------------------------------ результат

function ResultView({ r, onAgain }: { r: RetellResult; onAgain: () => void }) {
  const nav = useNav();
  useEffect(() => {
    if (r.finished) {
      const id = setTimeout(() => nav.replace({ name: "finish" }), 1600);
      return () => clearTimeout(id);
    }
  }, [r.finished, nav]);

  if (r.status === "accepted") {
    return (
      <div className="retell">
        <div className="verdict">
          <div className="mark">
            <svg className="check-draw" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="m5 12.5 4.5 4.5L19 7.5" />
            </svg>
          </div>
          <h2 className="h-display" style={{ marginTop: 22 }}>Засчитано</h2>
          {r.streak_grew && (
            <div className="streak-pill bump" style={{ margin: "14px auto 0", display: "inline-flex" }}>
              <IFlameSolid size={18} /> {r.streak}
            </div>
          )}
        </div>
        {r.heard && <p className="small muted mt-24">🎙 {r.heard}</p>}
        <div className="ai-bubble">
          <span className="flame-avatar"><IFlameSolid size={16} /></span>
          <div className="bubble">
            {r.reply}
            {r.question && <><br /><br /><i>Подумать:</i> {r.question}</>}
          </div>
        </div>
        {!r.verified && <p className="tiny muted mt-12">Засчитано без сверки с текстом.</p>}
        {r.achievements.length > 0 && (
          <div className="card soft mt-16 row">
            <span style={{ fontSize: 30 }}>{ACH[r.achievements[0]]?.[0] ?? "🏅"}</span>
            <div>
              <b>Новый значок{r.achievements.length > 1 ? "и" : ""}</b>
              <div className="small muted">{r.achievements.map((c) => ACH[c]?.[1] ?? c).join(", ")}</div>
            </div>
          </div>
        )}
        {r.partner_name && (
          <p className="small muted mt-16 center">
            {r.partner_done ? `${r.partner_name}: день тоже сдан — общий стрик растёт 🤝` : `${r.partner_name} получит весточку: теперь очередь напарника.`}
          </p>
        )}
        <div style={{ flex: 1 }} />
        {r.finished ? (
          <p className="center muted">Это был последний отрезок… 🎉</p>
        ) : (
          <div className="col mt-16">
            {r.can_submit_more && (
              <button className="btn secondary block" onClick={onAgain}>Сдать ещё один отрезок</button>
            )}
            <button className="btn primary block" onClick={() => nav.tab("today")}>Готово</button>
            {r.next_title && !r.can_submit_more && <p className="tiny muted center">Завтра: «{r.next_title}»</p>}
          </div>
        )}
      </div>
    );
  }

  if (r.status === "clarify") {
    return (
      <div className="retell">
        <div className="verdict">
          <div className="mark q"><span style={{ fontSize: 44, fontWeight: 700 }}>?</span></div>
          <h2 className="h-display" style={{ marginTop: 22, fontSize: 28 }}>Уточню</h2>
        </div>
        {r.heard && <p className="small muted mt-24">🎙 {r.heard}</p>}
        <div className="ai-bubble">
          <span className="flame-avatar"><IFlameSolid size={16} /></span>
          <div className="bubble">{r.reply}</div>
        </div>
        <div className="prompt" style={{ fontSize: 28 }}>{r.question}</div>
        <div style={{ flex: 1 }} />
        <button className="btn primary block" onClick={onAgain}>Ответить</button>
      </div>
    );
  }

  if (r.status === "rejected") {
    return (
      <div className="retell">
        <div className="verdict">
          <div className="mark q"><span style={{ fontSize: 40 }}>🤔</span></div>
          <h2 className="h-display" style={{ marginTop: 22, fontSize: 28 }}>Не похоже</h2>
        </div>
        <div className="ai-bubble">
          <span className="flame-avatar"><IFlameSolid size={16} /></span>
          <div className="bubble">{r.reply}</div>
        </div>
        <p className="small muted mt-16">Расскажи пару конкретных моментов из отрезка — события, героев или мысль автора.</p>
        <div style={{ flex: 1 }} />
        <button className="btn primary block" onClick={onAgain}>Попробовать ещё раз</button>
      </div>
    );
  }

  return (
    <div className="retell">
      <div className="state" style={{ paddingTop: 80 }}>
        <div className="ill">{r.status === "queued" ? "⏳" : "📚"}</div>
        <h3>{r.status === "queued" ? "Принял" : r.status === "too_short" ? "Чуть подробнее" : "Готово"}</h3>
        <p>{r.message}</p>
        <button className="btn primary" onClick={r.status === "too_short" ? onAgain : () => nav.tab("today")}>
          {r.status === "too_short" ? "Дописать" : "Хорошо"}
        </button>
      </div>
    </div>
  );
}
