import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, setAuthBridge } from "../api/client";
import type { CurrentUser } from "../api/types";
import { SessionContext } from "./session-context";
import type { SessionValue } from "./session-context";

/**
 * 会话：当前用户、权限集合、CSRF、登出。
 *
 * **只有「会话」是跨页共享的状态**，所以用 Context 就够了——不引入 Redux / TanStack Query
 * （见《前端界面说明》§2）。列表数据各页自己用 `useAsync` 拉。
 *
 * 会话令牌在 HttpOnly Cookie 里，前端**只**持有 CSRF 令牌（内存，不落 localStorage）。
 */

export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [restoring, setRestoring] = useState(true);

  // 调用层（非 React）要读 CSRF、要在 401 时清会话，用一个 ref 桥接。
  // ⚠️ **ref 只在 effect 里写**：渲染期写 ref 会被 `react-hooks` 拦下（渲染必须是纯的）。
  const userRef = useRef<CurrentUser | null>(null);
  useEffect(() => {
    userRef.current = user;
  }, [user]);

  useEffect(() => {
    setAuthBridge({
      csrfToken: () => userRef.current?.csrf_token ?? null,
      // **只清会话，不跳转**：由 App 按「有没有用户」决定渲染登录页还是业务页。
      // 这样当前 hash 会被保留，登录后能回到原来想去的页面。
      onUnauthorized: () => setUser(null),
    });
  }, []);

  // 刷新页面后凭 Cookie 恢复会话；未登录时静默忽略（首次访问必然 401）
  useEffect(() => {
    let cancelled = false;
    api<CurrentUser>("/auth/me")
      .then((me) => {
        if (!cancelled) setUser(me);
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setRestoring(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const signIn = useCallback(async (username: string, password: string) => {
    const me = await api<CurrentUser>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    setUser(me);
    return me;
  }, []);

  const signOut = useCallback(async () => {
    try {
      await api<void>("/auth/logout", { method: "POST" });
    } finally {
      // **无论成败都清**：登出请求失败也不该把用户留在已登录界面
      setUser(null);
    }
  }, []);

  const update = useCallback((patch: Partial<CurrentUser>) => {
    setUser((previous) => (previous ? { ...previous, ...patch } : previous));
  }, []);

  const value = useMemo<SessionValue>(
    () => ({
      user,
      restoring,
      can: (permission: string) => Boolean(user?.permissions.includes(permission)),
      ready: user?.mfa_status === "ok",
      signIn,
      signOut,
      update,
    }),
    [user, restoring, signIn, signOut, update],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
