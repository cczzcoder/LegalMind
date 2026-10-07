/**
 * 极简 hash 路由（约 60 行，零依赖）。
 *
 * **为什么自己写**（详见《前端界面说明》§2）：需要深链（运行 id 可分享、刷新不丢）与前进后退，
 * 但只有 6 个页面。本项目对依赖的口径是「没有测量支撑就不装」——拒绝 Elasticsearch、拒绝迁
 * Neo4j、拒绝引入 LangGraph 都是同一条口径。
 *
 * ⚠️ **换成 react-router 的触发点**：出现**嵌套路由**或**路由级数据加载**时。路由表集中在
 * `nav.ts` 一个文件里，所以那次替换是受控改动。
 */

import { useEffect, useState } from "react";

export type RouteParams = Record<string, string>;
export type RouteMatch = { pattern: string; params: RouteParams };

function readPath(): string {
  const raw = window.location.hash.replace(/^#/, "");
  if (!raw) return "/";
  return raw.startsWith("/") ? raw : `/${raw}`;
}

function matchPattern(path: string, pattern: string): RouteParams | null {
  const actual = path.split("/").filter(Boolean);
  const expected = pattern.split("/").filter(Boolean);
  if (actual.length !== expected.length) return null;

  const params: RouteParams = {};
  for (let index = 0; index < expected.length; index += 1) {
    const segment = expected[index];
    if (segment.startsWith(":")) {
      params[segment.slice(1)] = decodeURIComponent(actual[index]);
    } else if (segment !== actual[index]) {
      return null;
    }
  }
  return params;
}

/** 按顺序匹配；都不中时返回 `{ pattern: "*" }`。 */
export function useRoute(patterns: string[]): RouteMatch {
  const [path, setPath] = useState(readPath);

  useEffect(() => {
    const onHashChange = () => setPath(readPath());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  for (const pattern of patterns) {
    const params = matchPattern(path, pattern);
    if (params) return { pattern, params };
  }
  return { pattern: "*", params: {} };
}

export function navigate(to: string): void {
  const target = to.startsWith("/") ? to : `/${to}`;
  if (readPath() === target) return;
  window.location.hash = target;
}

/** 把当前 hash 换成目标（不新增历史记录）——用于登录后跳回原目标。 */
export function replaceRoute(to: string): void {
  const target = to.startsWith("/") ? to : `/${to}`;
  window.location.replace(`#${target}`);
}
