import { api } from "./client";
import type { AnswerRun, SubmitQuestion } from "./types";

/** 提交异步问答（设计 §9.4）：立刻拿到 run id，worker 在后台跑。 */
export function submitQuestion(body: SubmitQuestion) {
  return api<AnswerRun>("/answers", { method: "POST", body: JSON.stringify(body) });
}

/** `pending=true` 即**待审队列**（设计 §9.3 第三层）。 */
export function listRuns({ pending = false, limit = 50 } = {}) {
  const query = new URLSearchParams({ pending: String(pending), limit: String(limit) });
  return api<AnswerRun[]>(`/answers?${query.toString()}`);
}

export function getRun(runId: string) {
  return api<AnswerRun>(`/answers/${runId}`);
}

/** 回答澄清问题、**继续同一个运行**（§9.1 的 `CLARIFYING → RETRIEVING`）。 */
export function submitClarification(runId: string, supplement: string) {
  return api<AnswerRun>(`/answers/${runId}/clarify`, {
    method: "POST",
    body: JSON.stringify({ supplement }),
  });
}

/** 人工复核（§9.3 第三层）。**复核不改变运行状态**，结论写在 note 里。 */
export function reviewRun(runId: string, note: string | null) {
  return api<AnswerRun>(`/answers/${runId}/review`, {
    method: "POST",
    body: JSON.stringify({ note }),
  });
}
