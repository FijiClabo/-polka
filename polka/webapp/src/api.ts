// Клиент API. Авторизация — подписанные данные запуска Telegram (initData) в каждом запросе.

import { tg } from "./tg";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function authHeader(): string {
  if (tg?.initData) return `tma ${tg.initData}`;
  const dev = import.meta.env.VITE_DEV_TG_ID as string | undefined;
  return dev ? `tma dev:${dev}` : "";
}

async function request<T>(method: string, path: string, body?: unknown, isForm = false): Promise<T> {
  const headers: Record<string, string> = { Authorization: authHeader() };
  let payload: BodyInit | undefined;
  if (body !== undefined) {
    if (isForm) payload = body as FormData;
    else {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
  }
  let res: Response;
  try {
    res = await fetch(`/api${path}`, { method, headers, body: payload });
  } catch {
    throw new ApiError(0, "Нет связи. Проверь интернет и попробуй ещё раз.");
  }
  if (!res.ok) {
    let msg = "Что-то пошло не так. Попробуй ещё раз.";
    try {
      const j = await res.json();
      if (typeof j.detail === "string") msg = j.detail;
    } catch {
      /* ignore */
    }
    if (res.status === 401) msg = "Открой приложение из Telegram — так мы узнаем, что это ты.";
    throw new ApiError(res.status, msg);
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return (await res.json()) as T;
  return (await res.blob()) as unknown as T;
}

export const api = {
  get: <T>(p: string) => request<T>("GET", p),
  post: <T>(p: string, b?: unknown) => request<T>("POST", p, b ?? {}),
  patch: <T>(p: string, b: unknown) => request<T>("PATCH", p, b),
  del: <T>(p: string) => request<T>("DELETE", p),
  form: <T>(p: string, f: FormData) => request<T>("POST", p, f, true),
  blob: (p: string) => request<Blob>("GET", p),
};

export function track(type: "webapp_open" | "share_clicked" | "onboarding_seen", screen?: string): void {
  api.post("/events", { type, screen }).catch(() => {});
}

// ------------------------------------------------------------------ типы ответов

export interface BookBrief {
  id: number;
  title: string;
  author: string;
  spine_color: string;
  source: "epub" | "fb2" | "paper";
  pages: number;
  has_text: boolean;
  parse_status: "pending" | "ok" | "failed";
  chapters: number;
  words: number | null;
  reading_minutes: number;
}

export interface SegBrief {
  day_number: number;
  title: string;
  page_from: number;
  page_to: number;
  pages: number;
  minutes: number;
  retell_prompt: string | null;
  can_read: boolean;
  pos_to: number;
}

export interface UserBrief {
  id: number;
  name: string;
  photo_url: string | null;
}

export interface Me {
  user: UserBrief & {
    first_name: string;
    timezone: string;
    morning_time: string;
    evening_time: string;
    retell_format: "voice" | "text";
    nudges_enabled: boolean;
    webapp_onboarded: boolean;
    is_admin: boolean;
  };
  access: { state: string; enrollment_status: string | null; run: RunBrief | null };
  project_name: string;
  bot_username: string;
  features: { ai: boolean; voice: boolean };
}

export interface RunBrief {
  id: number;
  title: string;
  kind: "main" | "sprint" | "solo";
  start_date: string | null;
  price_rub: number;
  grace_days: number;
}

export interface PartnerBlock extends UserBrief {
  today: string;
  done_at: string | null;
  pair_streak: number;
  best_pair_streak: number;
  book_title: string | null;
  plan_day: number | null;
  plan_days: number | null;
}

export interface WeekDay {
  date: string;
  weekday: string;
  day: number;
  state: "done" | "frozen" | "missed" | "today" | "future" | "none";
  is_today: boolean;
  plan_day: number | null;
}

export interface Today {
  state: string;
  date: string;
  hour: number;
  book: BookBrief | null;
  plan_day: number | null;
  plan_days: number | null;
  segment: SegBrief | null;
  next_segment: SegBrief | null;
  streak: number;
  best_streak: number;
  freezes_left: number;
  progress: number;
  done_today: boolean;
  accepted_today: number;
  limit: number;
  catching_up: boolean;
  open_retelling: { id: number; verdict: string; reply: string | null; question: string | null } | null;
  starts_on: string | null;
  deadline: string | null;
  run: RunBrief | null;
  partner: PartnerBlock | null;
  week: WeekDay[];
  payment_info: string | null;
  accepted_days: number[];
  sprint_available: boolean;
  has_access: boolean;
}

export interface RetellResult {
  status: string;
  reply: string;
  question: string | null;
  retelling_id: number | null;
  segment_title: string;
  day_number: number | null;
  streak: number;
  streak_grew: boolean;
  pair_streak: number | null;
  partner_name: string | null;
  partner_done: boolean | null;
  achievements: string[];
  finished: boolean;
  verified: boolean;
  can_submit_more: boolean;
  next_title: string | null;
  message: string | null;
  heard?: string;
}

export interface RunData {
  state: string;
  book: BookBrief | null;
  plan_days?: number;
  start?: string | null;
  finish?: string | null;
  deadline?: string | null;
  today_n?: number | null;
  days: { n: number; date: string | null; state: string; grace: boolean }[];
  stats?: { done: number; segments_done: number; streak: number; freezes_left: number };
}

export interface RunDay {
  n: number;
  date: string;
  state: string | null;
  segment: SegBrief | null;
  retelling: { text: string; verdict: string; reply: string | null; question: string | null; verified: boolean; source: string } | null;
}

export interface FriendStatus {
  user_id: number;
  name: string;
  photo_url: string | null;
  book_title: string | null;
  book_author: string | null;
  spine_color: string | null;
  streak: number;
  today: "done" | "reading" | "burned" | "idle" | "finished" | "waiting";
  plan_day: number | null;
  plan_days: number | null;
  done_at: string | null;
  is_me?: boolean;
  can_nudge?: boolean;
  nudged?: boolean;
}

export interface PairData {
  has_pair: boolean;
  invite_link?: string | null;
  invite_text?: string;
  partner?: UserBrief & {
    today: string;
    done_at: string | null;
    book_title: string | null;
    book_author: string | null;
    plan_day: number | null;
    plan_days: number | null;
    streak: number;
  };
  pair_streak?: number;
  best_pair_streak?: number;
  same_book?: boolean;
  feed?: { day_number: number; title: string; locked: boolean; text: string | null; source: string }[];
  can_nudge?: boolean;
  nudged?: boolean;
}

export interface ShelfItem {
  book_id: number;
  title: string;
  author: string;
  spine_color: string;
  pages: number;
  status: "finished" | "reading";
  finished_at?: string | null;
  started_at?: string | null;
  plan_days: number | null;
  no_skips?: boolean;
  partner: string | null;
  retellings?: number;
  progress: number;
}

export interface ShelfData {
  finished: ShelfItem[];
  current: ShelfItem | null;
  finished_count: number;
  retellings_count?: number;
}

export interface Achievement {
  code: string;
  title: string;
  description: string;
  emoji: string;
  earned: boolean;
  awarded_at: string | null;
}

export interface PlanOption {
  days: number;
  pages_per_day: number;
  minutes_per_day: number;
  recommended: boolean;
  warning: string | null;
}

export interface BookState {
  book: BookBrief | null;
  parse_status?: string;
  parse_error?: string | null;
  plan_days?: number | null;
  plan_confirmed?: boolean;
  start?: string | null;
  has_progress?: boolean;
  run?: RunBrief | null;
  max_mb: number;
  can_add?: boolean;
  awaiting_payment?: boolean;
}

export interface Conspect {
  book: { id: number; title: string; author: string; spine_color: string; pages: number; source: string };
  intro: string | null;
  items: { day_number: number; title: string; note: string; retelling: string; ai_reply: string | null; date: string }[];
  status?: string;
}

export interface FinishData {
  book: BookBrief | null;
  plan_days: number;
  best_streak: number;
  retellings: number;
  partner: UserBrief | null;
  start: string | null;
  finished_at: string | null;
  shelf: { color: string; pages: number }[];
}

export interface PriceInfo {
  rub: number;
  stars: number;
  list_rub: number;
  list_stars: number;
  promo: string | null;
  discount: number;
  free: boolean;
}

export type Product = "run" | "month" | "year";

export interface Billing {
  enabled: boolean;
  methods: { card: boolean; stars: boolean };
  prices: Record<Product, PriceInfo>;
  promo: string | null;
  subscription: { active: boolean; until: string | null; kind: string | null; recurring: boolean };
  credits: number;
  refund: { eligible: boolean; partial: boolean; reason: string; until: string | null };
  guarantee_days: number;
  offer_url: string | null;
  privacy_url: string | null;
  manual_info: string;
  sprint_available: boolean;
  book: { title: string; author: string; spine_color: string } | null;
  plan_days: number | null;
  needs_access: boolean;
  purchases: { id: number; product: Product; amount: string; date: string; status: string }[];
}
