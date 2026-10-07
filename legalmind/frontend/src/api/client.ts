/**
 * 后端调用封装。
 *
 * 三条约定（与《前端界面说明》§6 一致）：
 * - 非 GET 请求带 `X-CSRF-Token`（令牌**只存内存**，会话本身在 HttpOnly Cookie 里）；
 * - 服务端统一错误体是 `{ code, message, trace_id }`，**不是 FastAPI 的 `detail`**；
 * - 401 时清空会话并跳登录——由 `session.tsx` 注册的回调负责，这里不直接依赖 React。
 */

/** 会话与调用层之间的桥：避免 `client → session → client` 的循环依赖。 */
type AuthBridge = {
  csrfToken: () => string | null;
  onUnauthorized: () => void;
};

let bridge: AuthBridge = { csrfToken: () => null, onUnauthorized: () => undefined };

export function setAuthBridge(next: AuthBridge): void {
  bridge = next;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly code?: string,
    readonly traceId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** 给用户看的一句话；已知状态码给可操作的解释，而不是把错误码甩出去。 */
  get hint(): string {
    if (this.status === 401) return "登录已过期，请重新登录。";
    if (this.status === 403) return "当前角色没有这个权限。";
    if (this.status === 404) return "对象不存在，或你没有访问权限。";
    if (this.status === 409) return "状态冲突：对象已被改动，请刷新后重试。";
    if (this.status === 503) return "服务暂时不可用（通常是部署配置缺失）。";
    return this.message;
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();

  if (method !== "GET" && method !== "HEAD") {
    const token = bridge.csrfToken();
    if (token) headers.set("X-CSRF-Token", token);
  }
  if (init.body !== undefined && !headers.has("Content-Type")) {
    // ⚠️ **只在调用方没指定时才默认 JSON**：上传原件要发 `application/octet-stream`
    // （后端读的是**原始字节**，不是 multipart），硬盖成 JSON 会让请求体被当成 JSON 解析。
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, { ...init, headers, credentials: "same-origin" });
  } catch {
    // 网络层失败（断网、后端没起）也要变成 ApiError，调用方只需处理一种错误
    throw new ApiError(0, "无法连接到服务端，请检查后端是否已启动。");
  }

  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      code?: string;
      message?: string;
      trace_id?: string;
      detail?: unknown;
    } | null;
    const message =
      payload?.message ?? (typeof payload?.detail === "string" ? payload.detail : undefined);
    if (response.status === 401 && path !== "/auth/login") bridge.onUnauthorized();
    throw new ApiError(
      response.status,
      message ?? response.statusText,
      payload?.code,
      payload?.trace_id,
    );
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** 把任意异常转成一句给用户看的话。 */
export function describeError(reason: unknown): string {
  if (reason instanceof ApiError) return reason.hint;
  if (reason instanceof Error) return reason.message;
  return String(reason);
}
