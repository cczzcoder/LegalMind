import { api } from "./client";
import type { LegalVersion, ReviewVersionDecision } from "./types";

/** 列出法律版本；`pending=true` 即**待审队列**（需求第 3 节）。 */
export function listLegalVersions({ pending = false, limit = 50 } = {}) {
  const query = new URLSearchParams({ pending: String(pending), limit: String(limit) });
  return api<LegalVersion[]>(`/legal-versions?${query.toString()}`);
}

/**
 * 人工复核一个法律版本（需求第 3 节）。需 `review.decide`，写审计。
 *
 * ⚠️ **只改审核状态，不动效力状态**——后者是法律事实，不是审核意见。
 * ⚠️ **可以改已复核的版本**（与问答运行的复核不同）：审错了必须能纠正。
 */
export function reviewLegalVersion(
  versionId: string,
  decision: ReviewVersionDecision,
  note: string | null,
) {
  return api<LegalVersion>(`/legal-versions/${versionId}/review`, {
    method: "POST",
    body: JSON.stringify({ decision, note }),
  });
}
