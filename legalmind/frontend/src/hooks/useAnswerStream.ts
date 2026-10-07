import { useCallback, useEffect, useState } from "react";

import type { AnswerState, StreamEvent } from "../api/types";

/**
 * 订阅一次问答运行的事件流（设计 §9.4）。
 *
 * 用浏览器原生 `EventSource`：GET 不需要 CSRF 头、Cookie 同源自动带上、断线自动重连。
 *
 * ⚠️ **收到 `done` 必须手动 `close()`**：流正常结束后浏览器会**自动重连**，
 * 于是事件被从头重放一遍（状态时间线会重复、澄清会再弹一次）。这不是可选项。
 */

export type StreamStatus = "connecting" | "open" | "closed" | "error";

export type StreamView = {
  status: StreamStatus;
  /** 状态时间线（按到达顺序，去重由调用方按需处理） */
  timeline: { state: AnswerState; at: string }[];
  answer: Extract<StreamEvent, { name: "answer" }>["data"] | null;
  clarification: string | null;
  timeout: string | null;
  error: string | null;
  /** 手动重连（超时或出错后用） */
  reconnect: () => void;
};

export function useAnswerStream(runId: string | null): StreamView {
  const [nonce, setNonce] = useState(0);
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [timeline, setTimeline] = useState<StreamView["timeline"]>([]);
  const [answer, setAnswer] = useState<StreamView["answer"]>(null);
  const [clarification, setClarification] = useState<string | null>(null);
  const [timeout, setTimeout] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reconnect = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    if (!runId) return undefined;

    // 重连（或换了运行）时清空上一轮的事件：这是「订阅了一个新的事件源」这个外部事实的反映，
    // 不是从 props 派生的状态，所以豁免该规则。
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 见上
    setStatus("connecting");
    setTimeline([]);
    setAnswer(null);
    setClarification(null);
    setTimeout(null);
    setError(null);

    const source = new EventSource(`/api/v1/answers/${runId}/events`, { withCredentials: true });
    let finished = false;

    const onOpen = () => setStatus("open");

    const onState = (event: MessageEvent<string>) => {
      const payload = JSON.parse(event.data) as { state: AnswerState; at: string };
      setTimeline((previous) => [...previous, { state: payload.state, at: payload.at }]);
    };

    const onAnswer = (event: MessageEvent<string>) => {
      setAnswer(JSON.parse(event.data) as StreamView["answer"]);
    };

    const onClarify = (event: MessageEvent<string>) => {
      const payload = JSON.parse(event.data) as { question: string | null };
      setClarification(payload.question);
    };

    const onTimeout = (event: MessageEvent<string>) => {
      const payload = JSON.parse(event.data) as { detail: string };
      setTimeout(payload.detail);
      finished = true;
      source.close();
      setStatus("closed");
    };

    const onDone = () => {
      finished = true;
      source.close();
      setStatus("closed");
    };

    /**
     * `error` 这个名字被**两层**用着，必须区分：
     * - **服务端推的 `error` 事件**带 `data`（是我们自己的事件）；
     * - **连接层失败**没有 `data`（EventSource 的默认行为，不重连就会一直重试）。
     * 只挂一个监听、按 `data` 分流——挂两个的话连接失败会同时触发两者。
     */
    const onErrorEvent = (event: Event) => {
      const message = event as MessageEvent<string>;
      if (typeof message.data === "string" && message.data) {
        const payload = JSON.parse(message.data) as { detail: string };
        setError(payload.detail);
      } else if (!finished) {
        setError("事件流中断，可点「重新连接」继续。");
        setStatus("error");
      } else {
        return; // 服务端已经收尾过，这不是错误
      }
      finished = true;
      source.close();
      setStatus("closed");
    };

    source.addEventListener("open", onOpen);
    source.addEventListener("state", onState as EventListener);
    source.addEventListener("answer", onAnswer as EventListener);
    source.addEventListener("clarify", onClarify as EventListener);
    source.addEventListener("timeout", onTimeout as EventListener);
    // ⚠️ **`done` 必须挂**：不挂的话流正常结束后 `EventSource` 会自动重连，
    // 于是状态时间线重复、澄清再弹一次（实测过）。
    source.addEventListener("done", onDone);
    source.addEventListener("error", onErrorEvent);

    return () => {
      // 离开页面必须关连接，否则会留下悬挂的 SSE 连接
      source.close();
    };
  }, [runId, nonce]);

  return { status, timeline, answer, clarification, timeout, error, reconnect };
}
