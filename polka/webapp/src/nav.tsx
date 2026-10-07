import { createContext, useContext } from "react";
import type { Me } from "./api";

export type RouteName =
  | "today"
  | "run"
  | "friends"
  | "shelf"
  | "read"
  | "retell"
  | "profile"
  | "book"
  | "friend"
  | "conspect"
  | "finish"
  | "pay";

export interface Route {
  name: RouteName;
  params?: Record<string, string | number | boolean | undefined>;
}

export interface Nav {
  route: Route;
  push: (r: Route) => void;
  replace: (r: Route) => void;
  back: () => void;
  tab: (name: "today" | "run" | "friends" | "shelf") => void;
  me: Me;
  refreshMe: () => void;
}

export const NavContext = createContext<Nav | null>(null);

export function useNav(): Nav {
  const n = useContext(NavContext);
  if (!n) throw new Error("NavContext");
  return n;
}

export const TABS = new Set<RouteName>(["today", "run", "friends", "shelf"]);
