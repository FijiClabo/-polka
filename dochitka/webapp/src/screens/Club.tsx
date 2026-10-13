import { useState } from "react";
import { api, type ClubData, type ClubMember, type ClubState } from "../api";
import { IHand, IPeople, IShare } from "../components/Icons";
import { Avatar, Cover, ErrorState, ScreenSkeleton, toast } from "../components/ui";
import { dayMonth, days as fmtDays, daysUntil, firstName, plural, STATUS_TEXT } from "../format";
import { invalidate, useApi } from "../hooks";
import { useNav } from "../nav";
import { confirmDialog, haptic, openTgLink } from "../tg";

export default function Club() {
  const { data, error, loading, reload } = useApi<ClubState>("/club");
  if (error && !data) return <ErrorState message={error} onRetry={reload} />;
  if (loading || !data) return <ScreenSkeleton />;
  if (!data.club) return <CreateClub state={data} onCreated={reload} />;
  return <ClubView club={data.club} reload={reload} />;
}

// ------------------------------------------------------------------ создание

function CreateClub({ state, onCreated }: { state: ClubState; onCreated: () => void }) {
  const nav = useNav();
  const [title, setTitle] = useState("");
  const [meet, setMeet] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="screen no-tabs">
      <h1 className="h-display">Книжный клуб</h1>
      <p className="muted">
        Вся группа читает одну книгу к встрече: у каждого план по 15 минут в день и минута пересказа. Ты видишь, кто идёт по плану,
        и можешь напомнить отстающим одной кнопкой.
      </p>
      <ol className="steps mt-16">
        <li><b>Выбери книгу и срок</b> — план клуба копируется с твоего.</li>
        <li><b>Создай клуб и отправь ссылку</b> в чат группы.</li>
        <li><b>Участники читают по своим экземплярам</b> — бумажным, в любом приложении или файлом. Текст твоей книги никому не передаётся.</li>
      </ol>
      {!state.can_create ? (
        <div className="card mt-16">
          <b>Сначала нужна книга с выбранным сроком</b>
          <p className="small muted" style={{ margin: "6px 0 0" }}>Добавь книгу, которую будет читать клуб, и выбери срок — к дате встречи или чуть раньше.</p>
          <button className="btn primary block mt-16" onClick={() => nav.push({ name: "book" })}>Выбрать книгу</button>
        </div>
      ) : (
        <div className="card col mt-16" style={{ gap: 12 }}>
          <div className="eyebrow">Книга клуба</div>
          <div className="seg-title" style={{ fontSize: 20, margin: 0 }}>«{state.book_title}»</div>
          <div className="field">
            <label>Название клуба</label>
            <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={`Читаем «${state.book_title}»`} maxLength={120} />
          </div>
          <div className="field">
            <label>Дата встречи (необязательно)</label>
            <input className="input" type="date" value={meet} onChange={(e) => setMeet(e.target.value)} />
          </div>
          <button
            className="btn primary block"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await api.post("/club", { title: title.trim(), meet_date: meet || null });
                haptic("success");
                invalidate("/friends");
                onCreated();
              } catch (e) {
                toast((e as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            Создать клуб
          </button>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ клуб

function ClubView({ club, reload }: { club: ClubData; reload: () => void }) {
  const nav = useNav();
  const [busy, setBusy] = useState(false);
  const today = new Date().toISOString().slice(0, 10);
  const left = club.meet_date ? daysUntil(club.meet_date, today) : null;
  const behind = club.members.filter((m) => !m.is_me && (m.today === "reading" || m.today === "burned")).length;

  return (
    <div className="screen no-tabs club">
      <div className="eyebrow mt-8">Книжный клуб{club.host_name && !club.is_host ? ` · ведёт ${firstName(club.host_name)}` : ""}</div>
      <h1 className="h-title" style={{ marginTop: 4 }}>{club.title}</h1>

      <div className="card row mt-12" style={{ gap: 14 }}>
        <Cover title={club.book_title || ""} author={club.book_author || ""} color={club.spine_color || "#4F6F9F"} small />
        <div className="grow">
          <b>{club.book_title}</b>
          <div className="small muted">{club.plan_days ? `план на ${fmtDays(club.plan_days)}` : ""}</div>
          {club.meet_date && (
            <div className="small" style={{ color: "var(--accent-text)", marginTop: 4 }}>
              Встреча {dayMonth(club.meet_date)}
              {left != null && left >= 0 ? ` · ${left === 0 ? "сегодня" : `через ${left} ${plural(left, "день", "дня", "дней")}`}` : ""}
            </div>
          )}
        </div>
      </div>

      <div className="club-summary mt-12">
        <div><b>{club.count}</b> {plural(club.count, "участник", "участника", "участников")}</div>
        <div><b>{club.done_today}</b> сдали сегодня</div>
      </div>

      <div className="btn-row mt-12">
        <button className="btn primary" onClick={() => { haptic("light"); openTgLink(club.share_url); }}>
          <IShare size={18} /> Позвать в клуб
        </button>
        {club.is_host && (
          <button
            className="btn secondary"
            disabled={busy || behind === 0}
            onClick={async () => {
              setBusy(true);
              try {
                const r = await api.post<{ sent: number }>("/club/nudge");
                haptic("success");
                toast(r.sent ? `Напомнили: ${r.sent} ${plural(r.sent, "человек", "человека", "человек")}` : "Все уже сдали или напоминание сегодня уже было");
                reload();
              } catch (e) {
                toast((e as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            <IHand size={18} /> Напомнить
          </button>
        )}
      </div>
      {club.is_host && (
        <p className="tiny muted mt-8">«Напомнить» отправит сообщение тем, у кого сегодняшний день ещё не сдан. Каждому — не чаще раза в день.</p>
      )}

      <div className="section-title">
        <span>Кто как идёт</span>
        <span className="tiny muted" style={{ fontWeight: 500 }}>прочитано</span>
      </div>
      <div className="list">
        {club.members.map((m) => <MemberRow key={m.id} m={m} onChanged={reload} />)}
      </div>
      <p className="tiny muted mt-12">
        Участники клуба видят имена, фото и прогресс друг друга. Пересказы и тексты книг никому не видны.
      </p>

      <button
        className="link block tiny mt-12"
        onClick={async () => {
          const q = club.is_host
            ? "Закрыть клуб? Участники продолжат читать свои планы, но общего списка больше не будет."
            : "Выйти из клуба? Книга и план останутся, просто без общего списка.";
          if (!(await confirmDialog(q))) return;
          try {
            await api.post("/club/leave");
            invalidate("/club");
            invalidate("/friends");
            nav.back();
          } catch (e) {
            toast((e as Error).message);
          }
        }}
      >
        {club.is_host ? "Закрыть клуб" : "Выйти из клуба"}
      </button>
    </div>
  );
}

function MemberRow({ m, onChanged }: { m: ClubMember; onChanged: () => void }) {
  const [sent, setSent] = useState(false);
  const status = m.today === "done" ? "сдано сегодня" : STATUS_TEXT[m.today] || "";
  return (
    <div className={`list-item member${m.is_me ? " me" : ""}`}>
      <Avatar name={m.name} url={m.photo_url} size={40} seed={m.id} ring={m.today === "done" ? "green" : undefined} />
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="row" style={{ gap: 6 }}>
          <b className="ellipsis">{m.is_me ? "Ты" : m.name}</b>
          {m.is_host && <span className="badge accent">ведущий</span>}
        </div>
        <div className="sub ellipsis">
          {[m.plan_day && m.plan_days ? `день ${Math.min(m.plan_day, m.plan_days)} из ${m.plan_days}` : "план ещё не начат", status].filter(Boolean).join(" · ")}
        </div>
        <div className="hbar-track mt-8"><i style={{ width: `${Math.max(0, Math.min(100, m.progress))}%` }} /></div>
      </div>
      <div className="col" style={{ alignItems: "flex-end", gap: 6 }}>
        <b className="tp-pct">{m.progress}%</b>
        {!m.is_me && !m.is_friend && !sent && (
          <button
            className="chip-link"
            onClick={async () => {
              try {
                const r = await api.post<{ result: string }>("/friends/request", { user_id: m.id });
                setSent(true);
                toast(r.result === "already" ? "Вы уже друзья" : `${firstName(m.name)} получит приглашение в друзья`);
                onChanged();
              } catch (e) {
                toast((e as Error).message);
              }
            }}
          >
            <IPeople size={14} /> В друзья
          </button>
        )}
      </div>
    </div>
  );
}
