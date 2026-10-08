import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, track, type Me } from "./api";
import { BookStackIll } from "./components/Illustrations";
import { ErrorState, ScreenSkeleton, TabBar, ToastHost, type Tab } from "./components/ui";
import { NavContext, TABS, type Nav, type Route } from "./nav";
import AddBook from "./screens/AddBook";
import Consent from "./screens/Consent";
import Finish from "./screens/Finish";
import FriendCard from "./screens/FriendCard";
import Friends from "./screens/Friends";
import Pay from "./screens/Pay";
import Profile from "./screens/Profile";
import Read from "./screens/Read";
import Retell from "./screens/Retell";
import Run from "./screens/Run";
import Shelf from "./screens/Shelf";
import Today from "./screens/Today";
import Welcome from "./screens/Welcome";
import { inTelegram, initTelegram, onThemeChange, paintChrome, setBackButton, setSettingsButton, startParams, themeName } from "./tg";

function initialRoute(): Route {
  const p = startParams();
  const s = p.get("s") || "today";
  const known = ["today", "run", "friends", "shelf", "read", "retell", "profile", "book", "finish", "pay"];
  if (!known.includes(s)) return { name: "today" };
  const params: Record<string, string> = {};
  p.forEach((v, k) => {
    if (k !== "s" && !k.startsWith("tgWebApp")) params[k] = v;
  });
  return { name: s as Route["name"], params };
}

function applyTheme(): void {
  const t = themeName();
  document.documentElement.dataset.theme = t;
  paintChrome(t === "dark" ? "#1d1a17" : "#f3eee6");
}

export default function App() {
  const [stack, setStack] = useState<Route[]>(() => {
    const r = initialRoute();
    return TABS.has(r.name) ? [r] : [{ name: "today" }, r];
  });
  const [me, setMe] = useState<Me | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [skipWelcome, setSkipWelcome] = useState(false);

  const loadMe = useCallback(() => {
    setErr(null);
    api
      .get<Me>("/me")
      .then(setMe)
      .catch((e: ApiError) => setErr(e.message));
  }, []);

  useEffect(() => {
    initTelegram();
    applyTheme();
    loadMe();
    return onThemeChange(applyTheme);
  }, [loadMe]);

  const route = stack[stack.length - 1];

  const nav: Nav | null = useMemo(() => {
    if (!me) return null;
    return {
      route,
      me,
      refreshMe: loadMe,
      push: (r) => setStack((s) => [...s, r]),
      replace: (r) => setStack((s) => [...s.slice(0, -1), r]),
      back: () => setStack((s) => (s.length > 1 ? s.slice(0, -1) : s)),
      tab: (name) => setStack([{ name }]),
    };
  }, [route, me, loadMe]);

  // Нативная кнопка «Назад» Telegram для вложенных экранов
  useEffect(() => {
    setBackButton(stack.length > 1 ? () => setStack((s) => s.slice(0, -1)) : null);
    window.scrollTo({ top: 0 });
  }, [stack.length, route]);

  // Кнопка «Настройки» в меню Telegram → профиль
  useEffect(() => {
    setSettingsButton(() => setStack((s) => (s[s.length - 1]?.name === "profile" ? s : [...s, { name: "profile" }])));
    return () => setSettingsButton(null);
  }, []);

  useEffect(() => {
    if (me && TABS.has(route.name)) track("webapp_open", route.name);
  }, [route.name, me]);

  if (!inTelegram && !import.meta.env.VITE_DEV_TG_ID) {
    return (
      <div className="app">
        <div className="state" style={{ paddingTop: 120 }}>
          <div className="ill"><BookStackIll /></div>
          <h3>Открой приложение в Telegram</h3>
          <p>Приложение работает внутри Telegram: открой бота и нажми кнопку внизу чата.</p>
        </div>
      </div>
    );
  }
  if (err) return <div className="app"><ErrorState message={err} onRetry={loadMe} /></div>;
  if (!me || !nav) return <div className="app"><ScreenSkeleton /></div>;

  if (!me.user.webapp_onboarded && !skipWelcome) {
    return (
      <div className="app">
        <Welcome
          onDone={() => {
            setSkipWelcome(true);
            api.patch<Me>("/me", { webapp_onboarded: true }).then((m) => setMe((prev) => ({ ...(prev as Me), ...m }))).catch(() => {});
            track("onboarding_seen");
          }}
        />
      </div>
    );
  }

  if (!me.consent) {
    return (
      <div className="app">
        <Consent me={me} onDone={loadMe} />
        <ToastHost />
      </div>
    );
  }

  const showTabs = TABS.has(route.name);
  let screen;
  switch (route.name) {
    case "today": screen = <Today />; break;
    case "run": screen = <Run />; break;
    case "friends": screen = <Friends />; break;
    case "shelf": screen = <Shelf />; break;
    case "read": screen = <Read day={Number(route.params?.d || route.params?.day || 0)} />; break;
    case "retell": screen = <Retell />; break;
    case "profile": screen = <Profile />; break;
    case "book": screen = <AddBook />; break;
    case "friend": screen = <FriendCard id={Number(route.params?.id)} />; break;
    case "finish": screen = <Finish />; break;
    case "pay": screen = <Pay />; break;
    default: screen = <Today />;
  }

  return (
    <NavContext.Provider value={nav}>
      <div className="app" key={stack.length + route.name}>
        {screen}
      </div>
      {showTabs && <TabBar tab={route.name as Tab} onTab={(t) => nav.tab(t)} />}
      <ToastHost />
    </NavContext.Provider>
  );
}
