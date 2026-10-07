import { createContext, useContext } from "react";

import type { CurrentUser } from "../api/types";

/**
 * 会话上下文与读取 hook。
 *
 * **与 Provider 分文件**：`react-refresh` 要求「一个文件只导出组件」，否则热更新会整页刷新。
 * 这里只放类型、context 与 hook（非组件），`session.tsx` 只放 `SessionProvider`。
 */

export type SessionValue = {
  user: CurrentUser | null;
  /** 首次恢复会话中（刷新页面时）；期间不要闪登录页 */
  restoring: boolean;
  /**
   * 是否具备某项权限。
   *
   * ⚠️ **只用于显示层**（决定菜单与按钮要不要渲染）。**强制校验永远在后端**
   * （`require_permission` 逐请求重读角色）。权限集合由后端 `CurrentUser.permissions` 给出，
   * 前端**不复制**「角色→权限」映射——那份复制会静默漂移（CODE_REVIEW m6）。
   */
  can: (permission: string) => boolean;
  /** MFA 已就绪（未绑定/未验证的管理员不能使用业务功能） */
  ready: boolean;
  signIn: (username: string, password: string) => Promise<CurrentUser>;
  signOut: () => Promise<void>;
  /** 登录后 / MFA 状态变化后更新用户对象 */
  update: (patch: Partial<CurrentUser>) => void;
};

export const SessionContext = createContext<SessionValue | null>(null);

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession 必须在 SessionProvider 内使用");
  return value;
}
