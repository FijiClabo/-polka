import { useState } from "react";
import { api, type ClubState, type FriendStatus, type PairData } from "../api";
import { IChevron, IFlameSolid, IHand, IPeople, IPlus } from "../components/Icons";
import { BookStackIll } from "../components/Illustrations";
import { Avatar, Empty, ErrorState, ScreenSkeleton, Skeleton, toast } from "../components/ui";
import { firstName, nudgeToast, STATUS_TEXT } from "../format";
import { invalidate, useApi } from "../hooks";
import { useNav } from "../nav";
import { confirmDialog, haptic, openTgLink } from "../tg";

interface FriendsData {
  items: FriendStatus[];
  count: number;
}

export default function Friends() {
  const nav = useNav();
  const friends = useApi<FriendsData>("/friends");
  const pair = useApi<PairData>("/pair");
  const club = useApi<ClubState>("/club");

  const invite = async () => {
    haptic("light");
    try {
      const r = await api.get<{ share_url: string }>("/friends/invite");
      openTgLink(r.share_url);
    } catch (e) {
      toast((e as Error).message);
    }
  };

  if (friends.error && !friends.data) return <ErrorState message={friends.error} onRetry={friends.reload} />;
  if (!friends.data) return <ScreenSkeleton />;

  const items = friends.data.items;
  return (
    <div className="screen">
      <div className="row between">
        <h1 className="h-display">Друзья</h1>
        <button className="btn secondary small" onClick={invite}>
          <IPlus size={16} /> Позвать
        </button>
      </div>

      <div className="mt-16">{pair.data ? <PairBlock data={pair.data} reload={pair.reload} /> : <Skeleton h={190} />}</div>

      {club.data && <ClubBlock state={club.data} />}

      <div className="section-title">
        <span>Сегодня</span>
        <span className="tiny muted" style={{ fontWeight: 500 }}>стрик</span>
      </div>
      {friends.data.count === 0 ? (
        <Empty icon={<BookStackIll size={150} />} title="Пока никого" text="Отправь личную ссылку — друг сразу появится здесь, без подтверждений." action={<button className="btn primary" onClick={invite}>Позвать друга</button>} />
      ) : (
        <div className="list">
          {items.map((f) => (
            <FriendRow key={f.user_id} f={f} onOpen={() => !f.is_me && nav.push({ name: "friend", params: { id: f.user_id } })} onNudged={friends.reload} />
          ))}
        </div>
      )}
    </div>
  );
}

function FriendRow({ f, onOpen, onNudged }: { f: FriendStatus; onOpen: () => void; onNudged: () => void }) {
  const [sent, setSent] = useState(false);
  const status = f.is_me ? (f.today === "done" ? "сдано" : f.today === "reading" ? "ещё читаешь" : STATUS_TEXT[f.today]) : STATUS_TEXT[f.today];
  const sub = [f.book_title, status].filter(Boolean).join(" · ");
  return (
    <div className={`list-item${f.is_me ? " me" : ""}`} role="button" onClick={onOpen}>
      <Avatar name={f.name} url={f.photo_url} size={44} seed={f.user_id} />
      <div className="grow" style={{ minWidth: 0 }}>
        <b>{f.is_me ? "Ты" : f.name}</b>
        <div className={`sub ellipsis${f.is_me && f.today !== "done" ? " accent" : ""}`}>{sub}</div>
      </div>
      {f.can_nudge && !sent ? (
        <button
          className="icon-btn"
          aria-label="Толкнуть"
          onClick={async (e) => {
            e.stopPropagation();
            haptic("light");
            const r = await api.post<{ result: string }>(`/friends/${f.user_id}/nudge`).catch(() => ({ result: "error" }));
            setSent(true);
            toast(nudgeToast(r.result, firstName(f.name)));
            invalidate("/friends");
            onNudged();
          }}
        >
          <IHand size={18} />
        </button>
      ) : null}
      <span className={`streak-mini${f.streak ? "" : " zero"}`}>
        {f.streak ? <IFlameSolid size={15} /> : null}
        {f.streak || 0}
      </span>
    </div>
  );
}

function PairBlock({ data, reload }: { data: PairData; reload: () => void }) {
  const nav = useNav();
  const me = nav.me.user;
  const [busy, setBusy] = useState(false);

  if (!data.has_pair || !data.partner) {
    if (!data.invite_link) return null;
    return (
      <div className="partner-card">
        <div className="label">НАПАРНИК</div>
        <div className="row mt-12" style={{ alignItems: "flex-start" }}>
          <div className="grow">
            {data.invite_kind === "together" ? (
              <>
                <b style={{ fontSize: 18 }}>Читать «{data.book_title}» вместе</b>
                <p className="small" style={{ color: "var(--text-2)", margin: "6px 0 0" }}>
                  Друг получит тот же план: те же главы в те же дни и общий стрик. Книга у каждого своя — твой файл другу не передаётся.
                </p>
              </>
            ) : (
              <>
                <b style={{ fontSize: 18 }}>Читать вдвоём проще</b>
                <p className="small" style={{ color: "var(--text-2)", margin: "6px 0 0" }}>
                  Общий стрик растёт, только если день сдан у вас двоих. Выбери книгу и срок — и друга можно позвать читать её вместе.
                </p>
              </>
            )}
          </div>
        </div>
        <button className="btn primary block mt-16" onClick={() => openTgLink(`https://t.me/share/url?url=${encodeURIComponent(data.invite_link!)}&text=${encodeURIComponent(data.invite_text || "")}`)}>
          {data.invite_kind === "together" ? "Позвать читать вместе" : "Позвать напарника"}
        </button>
      </div>
    );
  }

  const p = data.partner;
  const pDone = p.today === "done";
  return (
    <div className="partner-card">
      <div className="label">НАПАРНИК</div>
      <div className="duo">
        <div className="person">
          <Avatar name={me.name} url={me.photo_url} size={64} seed={me.id} ring="grey" />
          <div className="who">Ты</div>
        </div>
        <div className="center">
          <IFlameSolid size={26} />
          <div className="big">{data.pair_streak}</div>
          <div className="tiny muted">общий стрик</div>
        </div>
        <div className="person">
          <Avatar name={p.name} url={p.photo_url} size={64} seed={p.id} ring={pDone ? "green" : "grey"} />
          <div className="who">{firstName(p.name)}</div>
          <div className={`st${pDone ? " ok" : ""}`}>{pDone ? (p.done_at ? `сдано в ${p.done_at}` : "сдано") : STATUS_TEXT[p.today] || ""}</div>
        </div>
      </div>
      {data.together ? (
        <div className="together-progress">
          <div className="small">Читаете вместе «{data.together.title}»</div>
          <ProgressRow who="Ты" pct={data.together.me} />
          <ProgressRow who={firstName(p.name)} pct={data.together.partner} />
        </div>
      ) : (
        <div className="foot">
          {p.plan_day && p.plan_days
            ? `${firstName(p.name)}: день ${Math.min(p.plan_day, p.plan_days)} из ${p.plan_days}. `
            : `${firstName(p.name)} ещё настраивает план. `}
          Каждый читает свою книгу — напарнику виден только прогресс.
        </div>
      )}
      {data.can_nudge && (
        <button
          className="btn secondary block mt-12"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            haptic("light");
            const r = await api.post<{ result: string }>("/pair/nudge").catch(() => ({ result: "error" }));
            toast(nudgeToast(r.result, firstName(p.name), true));
            reload();
            setBusy(false);
          }}
        >
          <IHand size={18} /> Напомнить напарнику
        </button>
      )}
      {data.partner_is_friend === false && (
        <button
          className="btn ghost block mt-8"
          disabled={busy}
          onClick={async () => {
            try {
              const r = await api.post<{ result: string }>("/friends/request", { user_id: p.id });
              toast(r.result === "already" ? "Вы уже друзья" : `${firstName(p.name)} получит приглашение в друзья в боте`);
            } catch (e) {
              toast((e as Error).message);
            }
          }}
        >
          Позвать напарника в друзья
        </button>
      )}
      <button
        className="link block tiny mt-12"
        disabled={busy}
        onClick={async () => {
          if (!(await confirmDialog("Выйти из пары? Общий стрик закончится, каждый будет читать дальше сам."))) return;
          setBusy(true);
          try {
            await api.post("/pair/leave");
            invalidate("/pair");
            invalidate("/today");
            reload();
          } catch (e) {
            toast((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        Выйти из пары
      </button>
    </div>
  );
}

function ProgressRow({ who, pct }: { who: string; pct: number }) {
  return (
    <div className="tp-row">
      <span className="tp-who">{who}</span>
      <div className="hbar-track grow"><i style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} /></div>
      <b className="tp-pct">{pct}%</b>
    </div>
  );
}

function ClubBlock({ state }: { state: ClubState }) {
  const nav = useNav();
  const c = state.club;
  if (c) {
    const top = c.members.slice(0, 4);
    return (
      <button className="card club-card mt-12" onClick={() => nav.push({ name: "club" })}>
        <div className="row between">
          <div className="eyebrow">Клуб</div>
          <IChevron size={16} />
        </div>
        <b style={{ fontSize: 18 }}>{c.title}</b>
        <div className="small muted">«{c.book_title}» · {c.count} {c.count % 10 === 1 && c.count % 100 !== 11 ? "участник" : "участников"} · сегодня сдали {c.done_today}</div>
        <div className="avatars-stack mt-8">
          {top.map((m) => <Avatar key={m.id} name={m.name} url={m.photo_url} size={30} seed={m.id} />)}
        </div>
      </button>
    );
  }
  return (
    <button className="card row club-card mt-12" style={{ textAlign: "left" }} onClick={() => nav.push({ name: "club" })}>
      <IPeople size={24} />
      <div className="grow">
        <b>Ведёшь книжный клуб?</b>
        <div className="small muted">Вся группа читает одну книгу к встрече, а ты видишь, кто идёт по плану.</div>
      </div>
      <IChevron size={16} />
    </button>
  );
}
