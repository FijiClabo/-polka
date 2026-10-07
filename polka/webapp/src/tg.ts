// Обёртка над официальным SDK Telegram Mini Apps (telegram-web-app.js).
// Все вызовы защищены проверкой версии — старые клиенты Telegram не падают.

type Haptic = "light" | "medium" | "heavy" | "rigid" | "soft";

interface TgButton {
  show(): void;
  hide(): void;
  onClick(cb: () => void): void;
  offClick(cb: () => void): void;
  isVisible?: boolean;
}

interface TgWebApp {
  initData: string;
  initDataUnsafe: { user?: { id: number; first_name?: string; photo_url?: string }; start_param?: string };
  version: string;
  platform: string;
  colorScheme: "light" | "dark";
  themeParams: Record<string, string>;
  isExpanded: boolean;
  ready(): void;
  expand(): void;
  close(): void;
  isVersionAtLeast(v: string): boolean;
  setHeaderColor(c: string): void;
  setBackgroundColor(c: string): void;
  setBottomBarColor?(c: string): void;
  disableVerticalSwipes?(): void;
  enableClosingConfirmation?(): void;
  disableClosingConfirmation?(): void;
  onEvent(e: string, cb: () => void): void;
  offEvent(e: string, cb: () => void): void;
  openTelegramLink(url: string): void;
  openLink(url: string): void;
  shareToStory?(url: string, params?: { text?: string; widget_link?: { url: string; name?: string } }): void;
  showConfirm?(msg: string, cb: (ok: boolean) => void): void;
  BackButton: TgButton;
  SettingsButton?: TgButton;
  HapticFeedback?: {
    impactOccurred(s: Haptic): void;
    notificationOccurred(t: "error" | "success" | "warning"): void;
    selectionChanged(): void;
  };
}

declare global {
  interface Window {
    Telegram?: { WebApp: TgWebApp };
  }
}

export const tg: TgWebApp | null = window.Telegram?.WebApp ?? null;

export const inTelegram = !!tg && !!tg.initData;

function atLeast(v: string): boolean {
  try {
    return !!tg && tg.isVersionAtLeast(v);
  } catch {
    return false;
  }
}

export function initTelegram(): void {
  if (!tg) return;
  try {
    tg.ready();
    tg.expand();
    if (atLeast("7.7")) tg.disableVerticalSwipes?.();
  } catch {
    /* старый клиент */
  }
}

export function themeName(): "dark" | "light" {
  if (tg?.colorScheme) return tg.colorScheme;
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function paintChrome(bg: string): void {
  if (!tg) return;
  try {
    if (atLeast("6.1")) {
      tg.setHeaderColor(bg);
      tg.setBackgroundColor(bg);
    }
    if (atLeast("7.10")) tg.setBottomBarColor?.(bg);
  } catch {
    /* ignore */
  }
}

export function onThemeChange(cb: () => void): () => void {
  if (!tg) return () => {};
  tg.onEvent("themeChanged", cb);
  return () => tg.offEvent("themeChanged", cb);
}

export function haptic(kind: Haptic | "success" | "error" | "warning" | "select" = "light"): void {
  const h = tg?.HapticFeedback;
  if (!h || !atLeast("6.1")) return;
  try {
    if (kind === "success" || kind === "error" || kind === "warning") h.notificationOccurred(kind);
    else if (kind === "select") h.selectionChanged();
    else h.impactOccurred(kind);
  } catch {
    /* ignore */
  }
}

let backHandler: (() => void) | null = null;

export function setBackButton(handler: (() => void) | null): void {
  if (!tg || !atLeast("6.1")) return;
  if (backHandler) tg.BackButton.offClick(backHandler);
  backHandler = handler;
  if (handler) {
    tg.BackButton.onClick(handler);
    tg.BackButton.show();
  } else {
    tg.BackButton.hide();
  }
}

let settingsHandler: (() => void) | null = null;

export function setSettingsButton(handler: (() => void) | null): void {
  const sb = tg?.SettingsButton;
  if (!sb || !atLeast("7.0")) return;
  if (settingsHandler) sb.offClick(settingsHandler);
  settingsHandler = handler;
  if (handler) {
    sb.onClick(handler);
    sb.show();
  } else sb.hide();
}

export function openTgLink(url: string): void {
  if (tg) tg.openTelegramLink(url);
  else window.open(url, "_blank");
}

export function canShareStory(): boolean {
  return !!tg?.shareToStory && atLeast("7.8");
}

export function shareStory(url: string, text?: string): void {
  tg?.shareToStory?.(url, { text });
}

export function closeApp(): void {
  if (tg) tg.close();
}

export function startParams(): URLSearchParams {
  return new URLSearchParams(window.location.search);
}

export function confirmDialog(message: string): Promise<boolean> {
  // В WebView Telegram window.confirm работает не везде — используем нативный диалог
  if (tg?.showConfirm && atLeast("6.2")) {
    return new Promise((resolve) => {
      try {
        tg.showConfirm!(message, (ok) => resolve(ok));
      } catch {
        resolve(window.confirm(message));
      }
    });
  }
  return Promise.resolve(window.confirm(message));
}

export function openExternal(url: string): void {
  if (tg) tg.openLink(url);
  else window.open(url, "_blank");
}
