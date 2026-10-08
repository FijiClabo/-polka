import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";

export interface Loadable<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
  setData: (d: T) => void;
}

// Загрузка с кешем в памяти: повторный заход на экран показывает прошлые данные сразу, без белого экрана.
const cache = new Map<string, unknown>();

export function useApi<T>(path: string | null, deps: unknown[] = []): Loadable<T> {
  const [data, setData] = useState<T | null>(path ? ((cache.get(path) as T) ?? null) : null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(!!path && !cache.has(path));
  const [tick, setTick] = useState(0);
  const shownPath = useRef(path);

  useEffect(() => {
    // свой флаг на каждый запрос: ответ на старый путь (другой день в календаре) не перезапишет новый
    let cancelled = false;
    if (!path) return;
    if (shownPath.current !== path) {
      shownPath.current = path;
      setData((cache.get(path) as T) ?? null);
    }
    setLoading(!cache.has(path));
    setError(null);
    api
      .get<T>(path)
      .then((d) => {
        if (cancelled) return;
        cache.set(path, d);
        setData(d);
      })
      .catch((e: ApiError) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, tick, ...deps]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  const set = useCallback(
    (d: T) => {
      if (path) cache.set(path, d);
      setData(d);
    },
    [path],
  );
  return { data, error, loading, reload, setData: set };
}

export function invalidate(prefix?: string): void {
  if (!prefix) cache.clear();
  else for (const k of [...cache.keys()]) if (k.startsWith(prefix)) cache.delete(k);
}

export function usePoll(cb: () => void, ms: number, enabled: boolean): void {
  const ref = useRef(cb);
  ref.current = cb;
  useEffect(() => {
    if (!enabled) return;
    const id = window.setInterval(() => ref.current(), ms);
    return () => window.clearInterval(id);
  }, [ms, enabled]);
}

export function useLocal<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (nv: T) => {
      setV(nv);
      try {
        localStorage.setItem(key, JSON.stringify(nv));
      } catch {
        /* приватный режим */
      }
    },
    [key],
  );
  return [v, set];
}
