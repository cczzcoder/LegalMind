import { api } from "./client";
import type { SearchQuery, SearchResponse } from "./types";

/**
 * 检索条款版本（设计 §8.3）。
 *
 * `semantic` 与 `keyword` **互斥**（同时给会 422）；`semantic` 走级联：先关键词、命中为空才向量
 * 兜底，结果里的 `path` 说明这次走的是哪条通路——否则没法解释结果为什么「看起来不相关」。
 */
export function searchProvisions(query: SearchQuery) {
  return api<SearchResponse>("/search", { method: "POST", body: JSON.stringify(query) });
}
