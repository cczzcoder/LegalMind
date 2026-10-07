/**
 * 会话（《前端界面说明》§5.8；设计 §9.6 多轮追问）。
 *
 * **会话 id 不进 Cookie、不进 URL**（设计 §9.6 边界、§5.2 验收标准 4 同口径）——问题文本与
 * 会话 id 都不该出现在 URL 里。所以放 `sessionStorage`：**刷新不丢、关掉标签页就散**。
 *
 * ⚠️ **轮次记录只存在浏览器本地**，不上传、不落库。代价是**换标签页或换设备看不到历史**；
 * 换来的是**不必为它新增接口与权限**——列运行列表要 `review.decide`，普通提问者没有这个权限，
 * 而会话列表本身在设计里就是「不做」（§9.6）。**本地记录只是给用户一个回到前几轮的入口。**
 *
 * ⚠️ **存储不可用时一律降级成单轮**，不抛错：多轮是增强，不能因为它挡住主链路。
 */

const KEY = "legalmind.conversation";

/** 本地最多记多少轮——只是入口列表，不影响后端上下文（后端只取最近 1 轮）。 */
const MAX_TURNS = 20;

export type Turn = {
  runId: string;
  question: string;
};

export type Conversation = {
  sessionId: string;
  turns: Turn[];
};

function randomId(): string {
  // `crypto.randomUUID` 需要安全上下文（https 或 localhost）。取不到就退回一个够用的 v4 形状。
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (char) => {
    const value = Math.floor(Math.random() * 16);
    return (char === "x" ? value : (value & 0x3) | 0x8).toString(16);
  });
}

function read(): Conversation | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<Conversation>;
    if (!parsed.sessionId) return null;
    return {
      sessionId: parsed.sessionId,
      turns: Array.isArray(parsed.turns) ? parsed.turns : [],
    };
  } catch {
    // 隐私模式下 `sessionStorage` 可能直接抛异常；内容坏掉也走这里。**降级成单轮，不报错。**
    return null;
  }
}

function write(value: Conversation | null): void {
  try {
    if (value) sessionStorage.setItem(KEY, JSON.stringify(value));
    else sessionStorage.removeItem(KEY);
  } catch {
    // 同上：存不了就当没有会话
  }
}

export function currentConversation(): Conversation | null {
  return read();
}

/** 开一个新会话（**清空轮次**）——用户问的是不相干的问题时该用它，否则上文会把检索带偏。 */
export function startConversation(): Conversation {
  const fresh: Conversation = { sessionId: randomId(), turns: [] };
  write(fresh);
  return fresh;
}

/** 有会话就沿用，没有就开一个。 */
export function ensureConversation(): Conversation {
  return read() ?? startConversation();
}

export function forgetConversation(): void {
  write(null);
}

/**
 * 记下这一轮。
 *
 * ⚠️ **会话 id 对不上就丢弃旧的轮次**——用户中途点了「开始新会话」时，旧轮次不该混进来。
 */
export function rememberTurn(sessionId: string, runId: string, question: string): void {
  const existing = read();
  const turns = existing && existing.sessionId === sessionId ? existing.turns : [];
  write({ sessionId, turns: [...turns, { runId, question }].slice(-MAX_TURNS) });
}
