import { useState } from "react";
import { api, type FriendStatus, type PairData } from "../api";
import { IFlameSolid, IHand, ILock, IPlus } from "../components/Icons";
import { Avatar, Empty, ErrorState, ScreenSkeleton, Skeleton, toast } from "../components/ui";
import { firstName, STATUS_TEXT } from "../format";
import { invalidate, useApi } from "../hooks";
import { useNav } from "../nav";
import { haptic, openTgLink } from "../tg";

interface FriendsData {
  items: FriendStatus[];
  count: number;
}

export default function Friends() {
  const nav = useNav();
  const friends = useApi<FriendsData>("/friends");
  const pair = useApi<PairData>("/pair");

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

      <div className="section-title">
        <span>Сегодня</span>
        <span className="tiny muted" style={{ fontWeight: 500 }}>стрик</span>
      </div>
      {friends.data.count === 0 ? (
        <Empty icon="👋" title="Пока никого" text="Отправь личную ссылку — друг сразу появится здесь, без подтверждений." action={<button className="btn primary" onClick={invite}>Позвать друга</button>} />
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
            toast(r.result === "ok" ? `${firstName(f.name)} получит толчок 👋` : r.result === "disabled" ? "Толчки отключены" : "Сегодня уже толкали");
            invalidate("/friends");
            onNudged();
          }}
        >
          <IHand size={18} />
        </button>
      ) : null}
      <span className={`streak-mini${f.streak ? "" : " zero"}`}>
        {f.streak ? <IFlameSolid size={15} /> : <span style={{ fontSize: 14 }}>○</span>}
        {f.streak || ""}
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
            <b style={{ fontSize: 18 }}>Читать вдвоём проще</b>
            <p className="small" style={{ color: "var(--text-2)", margin: "6px 0 0" }}>
              Общий стрик растёт, только если сдали оба. Каждый читает свою книгу.
            </p>
          </div>
        </div>
        <button className="btn primary block mt-16" onClick={() => openTgLink(`https://t.me/share/url?url=${encodeURIComponent(data.invite_link!)}&text=${encodeURIComponent(data.invite_text || "")}`)}>
          Позвать напарника
        </button>
      </div>
    );
  }

  const p = data.partner;
  const pDone = p.today === "done";
  const feed = data.feed || [];
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
      <div className="foot">
        {p.book_title ? (
          <>
            {firstName(p.name)} читает «{p.book_title}»{p.plan_day && p.plan_days ? `, день ${Math.min(p.plan_day, p.plan_days)} из ${p.plan_days}` : ""}.{" "}
            {data.same_book ? "Книга та же — пересказы открываются по мере твоего продвижения." : "Пересказы скрыты."}
          </>
        ) : (
          "Напарник ещё выбирает книгу."
        )}
      </div>
      {data.can_nudge && (
        <button
          className="btn secondary block mt-12"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            haptic("light");
            const r = await api.post<{ result: string }>("/pair/nudge").catch(() => ({ result: "error" }));
            toast(r.result === "ok" ? "Напоминание отправлено 👋" : "Сегодня уже напоминали");
            reload();
            setBusy(false);
          }}
        >
          <IHand size={18} /> Напомнить напарнику
        </button>
      )}
      {data.same_book && feed.length > 0 && (
        <div className="col mt-16">
          {feed.map((f) => (
            <div key={f.day_number} className="card soft" style={{ padding: 12, background: "rgba(0,0,0,0.18)" }}>
              <div className="row between small">
                <b>День {f.day_number} · {f.title}</b>
                {f.locked && <ILock size={16} />}
              </div>
              <div className="small" style={{ color: "var(--text-2)", marginTop: 4 }}>
                {f.locked ? "Откроется, когда ты дочитаешь до этого места." : `«${(f.text || "").split("\n— ")[0]}»`}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
