import { useCallback, useEffect, useRef, useState } from "react";

import { describeError } from "../api/client";

/**
 * 三态数据获取（loading / ready / error）+ 重试。
 *
 * 两个刻意的设计：
 * - **`task` 不进依赖数组**（它每次渲染都是新函数，放进去会无限循环）。依赖由调用方用 `deps`
 *   显式给出——这样调用方写的是「什么时候该重新拉」，而不是「函数引用变没变」。
 * - **丢弃过期响应**：用自增请求号，慢请求回来时不能覆盖新请求的结果（切页面/连点搜索时会出现）。
 */
export type AsyncState<T> =
  | { status: "loading"; data: null; error: null }
  | { status: "ready"; data: T; error: null }
  | { status: "error"; data: null; error: string };

export function useAsync<T>(task: () => Promise<T>, deps: unknown[]) {
  // ⚠️ ref 只在 effect 里写（渲染期写 ref 会被 `react-hooks` 拦下）。
  // 声明顺序保证它**先于**下面的取数 effect 执行，所以取数拿到的总是本次渲染的任务。
  const taskRef = useRef(task);
  useEffect(() => {
    taskRef.current = task;
  }, [task]);

  const [state, setState] = useState<AsyncState<T>>({ status: "loading", data: null, error: null });
  const requestId = useRef(0);

  const reload = useCallback(async () => {
    const id = (requestId.current += 1);
    setState({ status: "loading", data: null, error: null });
    try {
      const data = await taskRef.current();
      if (id === requestId.current) setState({ status: "ready", data, error: null });
    } catch (reason) {
      if (id === requestId.current) {
        setState({ status: "error", data: null, error: describeError(reason) });
      }
    }
  }, []);

  // 这个 hook 的职责就是**与外部系统（网络）同步**：进入 loading 是「请求已发出」这个外部事实
  // 的反映，不是从 props 派生的状态。所以这里豁免该规则（`reload` 会同步 setState）。
  // ⚠️ 规则会**追进被调用的函数**，所以豁免必须写在调用点。
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 见上
    void reload();
    // `deps` 由调用方显式给出，不是数组字面量——这是本 hook 的 API 约定，无法静态校验
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { ...state, reload };
}
