const MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
const MONTHS_SHORT = ["янв.", "февр.", "мар.", "апр.", "мая", "июня", "июля", "авг.", "сент.", "окт.", "нояб.", "дек."];
const WEEKDAYS = ["Воскресенье", "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"];

export function parseDate(iso: string): Date {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d);
}

export function dayMonth(iso: string | null | undefined, short = false): string {
  if (!iso) return "";
  const d = parseDate(iso);
  return `${d.getDate()} ${(short ? MONTHS_SHORT : MONTHS)[d.getMonth()]}`;
}

export function longDate(iso: string): string {
  const d = parseDate(iso);
  return `${WEEKDAYS[d.getDay()]}, ${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

export function plural(n: number, one: string, few: string, many: string): string {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

export const days = (n: number) => `${n} ${plural(n, "день", "дня", "дней")}`;
export const pages = (n: number) => `${n} ${plural(n, "страница", "страницы", "страниц")}`;

export function greeting(hour: number): string {
  if (hour < 5) return "Доброй ночи";
  if (hour < 12) return "Доброе утро";
  if (hour < 18) return "Привет";
  return "Добрый вечер";
}

export function daysUntil(iso: string, fromIso: string): number {
  return Math.round((parseDate(iso).getTime() - parseDate(fromIso).getTime()) / 86400000);
}

export function decimal(n: number): string {
  return String(Math.round(n * 10) / 10).replace(".", ",");
}

export function hm(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `${h} ч ${m} мин` : `${m} мин`;
}

export function firstName(name: string): string {
  return (name || "").split(" ")[0];
}

// Родительный падеж для «у Ани» — без склонения имён (надёжнее), поэтому формулировки нейтральные.
export const STATUS_TEXT: Record<string, string> = {
  done: "сдано",
  reading: "ещё читает",
  burned: "стрик сгорел",
  idle: "не в забеге",
  finished: "книга дочитана",
  waiting: "ждёт старта",
};

export function nudgeToast(result: string, name: string, partner = false): string {
  if (result === "ok") return partner ? "Напоминание отправлено" : `${name} получит толчок`;
  if (result === "already") return partner ? "Сегодня уже напоминали" : "Сегодня уже толкали";
  if (result === "done") return "Там день уже сдан";
  if (result === "idle") return "Сейчас читать нечего — толчок не нужен";
  return "Не получилось, попробуй позже";
}
