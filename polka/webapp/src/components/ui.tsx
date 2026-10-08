import { useEffect, useRef, useState, type ReactNode } from "react";
import { IBooks, ICalendar, IFlame, IPeople } from "./Icons";
import { OpenBookIll } from "./Illustrations";

// ------------------------------------------------------------------ аватар

const AVATAR_COLORS = ["#7E9C7A", "#4F6F9F", "#C9644F", "#B98B6E", "#8A6A85", "#4E6B57", "#D49A8C", "#3D4B66"];

// старые яркие цвета корешков (книги, добавленные до смены дизайна) → спокойная палитра
const CALM: Record<string, string> = {
  "#e2553f": "#C9644F", "#3e8e6e": "#7E9C7A", "#e98a6b": "#D49A8C", "#5b63d6": "#4F6F9F", "#f2c14e": "#E2B84F",
  "#8c5bd6": "#8A6A85", "#2f7fb8": "#3D4B66", "#c2410c": "#B98B6E", "#4d7c0f": "#4E6B57",
};

export const calmColor = (c: string): string => CALM[(c || "").toLowerCase()] || c || "#B98B6E";

// светлый фон (горчица, шалфей, пыльно-розовый) — тёмный текст, иначе — светлый: так читается в обоих случаях
export function isLight(hex: string): boolean {
  const m = hex.replace("#", "");
  const full = m.length === 3 ? m.split("").map((c) => c + c).join("") : m;
  const n = parseInt(full, 16);
  if (Number.isNaN(n)) return false;
  const ch = (v: number) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * ch(n >> 16) + 0.7152 * ch((n >> 8) & 255) + 0.0722 * ch(n & 255) > 0.25;
}

export function Avatar({ name, url, size = 44, ring, seed }: { name: string; url?: string | null; size?: number; ring?: "accent" | "green" | "grey"; seed?: number }) {
  const [broken, setBroken] = useState(false);
  const letter = (name || "?").trim().charAt(0).toUpperCase();
  const idx = Math.abs(seed ?? [...(name || "")].reduce((a, c) => a + c.charCodeAt(0), 0)) % AVATAR_COLORS.length;
  const cls = ["avatar", ring === "accent" ? "ring" : ring === "green" ? "ring-green" : ring === "grey" ? "ring-grey" : ""].join(" ");
  return (
    <span className={cls} style={{ width: size, height: size, background: AVATAR_COLORS[idx], color: isLight(AVATAR_COLORS[idx]) ? "#2a2623" : "#fbf8f3", fontSize: size * 0.42 }}>
      {url && !broken ? <img src={url} alt="" onError={() => setBroken(true)} /> : letter}
    </span>
  );
}

// ------------------------------------------------------------------ обложка книги (как в макете)

function hash(s: string): number {
  let h = 0;
  for (const c of s) h = (h * 31 + c.charCodeAt(0)) | 0;
  return Math.abs(h);
}

export function Cover({ title, author, color, small }: { title: string; author: string; color: string; small?: boolean }) {
  const h = hash(title + author);
  const size = small ? 36 + (h % 12) : 64 + (h % 20);
  const top = (small ? 20 : 34) + (h % 22);
  const right = -(h % (small ? 14 : 22));
  const surname = (author || "").split(/[ ,]/).filter(Boolean).slice(-1)[0] || author;
  return (
    <div className={`cover${small ? " small" : ""}`} style={{ background: calmColor(color), color: isLight(calmColor(color)) ? "#2a2623" : "#fbf8f3" }}>
      <div className="c-author">{surname}</div>
      <div className="c-sun" style={{ width: size, height: size, top, right }} />
      <div className="c-title">{title}</div>
    </div>
  );
}

// ------------------------------------------------------------------ полка

export interface SpineItem {
  id: number | string;
  color: string;
  pages: number;
  reading?: boolean;
  progress?: number;
  isNew?: boolean;
}

export function Shelf({ items, active, onPick, slot = true, height = 200 }: { items: SpineItem[]; active?: number | string | null; onPick?: (id: number | string) => void; slot?: boolean; height?: number }) {
  const maxH = height - 16;
  return (
    <div className="shelf-viz" style={{ height }}>
      {items.map((it, i) => {
        const hgt = Math.round(maxH * Math.min(1, 0.55 + Math.min(it.pages, 800) / 1800 + ((hash(String(it.id)) % 10) / 100)));
        const cls = ["spine", it.reading ? "reading" : "", active === it.id ? "active" : ""].join(" ");
        return (
          <button key={it.id} className={cls} style={{ height: hgt, background: calmColor(it.color), animationDelay: `${i * 70}ms` }} onClick={() => onPick?.(it.id)} aria-label="книга">
            {it.reading && <span className="fill" style={{ height: `${Math.round((it.progress || 0) * 100)}%` }} />}
            {it.isNew && <span className="plus-one">+1</span>}
          </button>
        );
      })}
      {slot && (
        <span className="spine slot" style={{ height: Math.round(maxH * 0.62) }}>
          +
        </span>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ таббар

export type Tab = "today" | "run" | "friends" | "shelf";

export function TabBar({ tab, onTab }: { tab: Tab; onTab: (t: Tab) => void }) {
  const items: [Tab, string, ReactNode][] = [
    ["today", "Сегодня", <IFlame key="f" />],
    ["run", "Забег", <ICalendar key="c" />],
    ["friends", "Друзья", <IPeople key="p" />],
    ["shelf", "Полка", <IBooks key="b" />],
  ];
  return (
    <nav className="tabbar">
      <div className="inner">
        {items.map(([id, label, icon]) => (
          <button key={id} className={`tab${tab === id ? " active" : ""}`} onClick={() => onTab(id)}>
            {icon}
            {label}
          </button>
        ))}
      </div>
    </nav>
  );
}

// ------------------------------------------------------------------ состояния

export function Skeleton({ h = 120, mt = 12, r }: { h?: number; mt?: number; r?: number }) {
  return <div className="skeleton" style={{ height: h, marginTop: mt, borderRadius: r }} />;
}

export function ScreenSkeleton() {
  return (
    <div className="screen">
      <Skeleton h={48} mt={8} r={24} />
      <Skeleton h={260} />
      <Skeleton h={110} />
      <Skeleton h={80} />
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state">
      <div className="ill"><OpenBookIll size={140} /></div>
      <h3>Не получилось загрузить</h3>
      <p>{message}</p>
      {onRetry && (
        <button className="btn secondary" onClick={onRetry}>
          Попробовать ещё раз
        </button>
      )}
    </div>
  );
}

export function Empty({ icon, title, text, action }: { icon: ReactNode; title: string; text?: string; action?: ReactNode }) {
  return (
    <div className="state">
      <div className="ill">{icon}</div>
      <h3>{title}</h3>
      {text && <p>{text}</p>}
      {action}
    </div>
  );
}

// ------------------------------------------------------------------ тост и шторка

let toastFn: ((m: string) => void) | null = null;

export function toast(message: string): void {
  toastFn?.(message);
}

export function ToastHost() {
  const [msg, setMsg] = useState<string | null>(null);
  const timer = useRef<number>(0);
  useEffect(() => {
    toastFn = (m) => {
      setMsg(m);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setMsg(null), 2600);
    };
    return () => {
      toastFn = null;
    };
  }, []);
  return msg ? <div className="toast">{msg}</div> : null;
}

export function Sheet({ open, onClose, children }: { open: boolean; onClose: () => void; children: ReactNode }) {
  if (!open) return null;
  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} />
      <div className="sheet" role="dialog">
        <div className="grip" />
        {children}
      </div>
    </>
  );
}

// ------------------------------------------------------------------ конфетти

export function Confetti({ count = 46 }: { count?: number }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const c = ref.current;
    if (!c) return;
    const ctx = c.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const w = c.offsetWidth, h = c.offsetHeight;
    c.width = w * dpr;
    c.height = h * dpr;
    ctx.scale(dpr, dpr);
    const colors = ["#C9644F", "#E2B84F", "#7E9C7A", "#4F6F9F", "#D49A8C"];
    // по краям экрана, чтобы не закрывать заголовок
    const edgeX = () => (Math.random() < 0.5 ? Math.random() * w * 0.26 : w * 0.74 + Math.random() * w * 0.26);
    const parts = Array.from({ length: count }, () => ({
      x: Math.random() < 0.85 ? edgeX() : Math.random() * w,
      y: -20 - Math.random() * h * 0.6,
      vy: 1.2 + Math.random() * 2.2,
      vx: -0.6 + Math.random() * 1.2,
      r: Math.random() * Math.PI,
      vr: -0.08 + Math.random() * 0.16,
      w: 5 + Math.random() * 4,
      h: 9 + Math.random() * 6,
      c: colors[Math.floor(Math.random() * colors.length)],
    }));
    let raf = 0;
    let frames = 0;
    const tick = () => {
      ctx.clearRect(0, 0, w, h);
      for (const p of parts) {
        p.x += p.vx;
        p.y += p.vy;
        p.r += p.vr;
        if (p.y > h * 0.75 && frames < 240) p.y = -10;
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(p.r);
        ctx.fillStyle = p.c;
        ctx.globalAlpha = 0.75;
        ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
        ctx.restore();
      }
      frames++;
      if (frames < 420) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [count]);
  return <canvas ref={ref} className="confetti" style={{ width: "100%", height: "100%" }} />;
}

export function Switch({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return <button className={`switch${on ? " on" : ""}`} onClick={() => onChange(!on)} role="switch" aria-checked={on} />;
}
