import {
  CheckCircleOutlined,
  CommentOutlined,
  LoadingOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SendOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Divider,
  Form,
  Grid,
  Input,
  Progress,
  Space,
  Steps,
  Tag,
  Typography,
} from "antd";
import dayjs from "dayjs";
import { useEffect, useMemo, useState } from "react";

import { getRun, reviewRun, submitClarification, submitQuestion } from "../api/answers";
import { describeError } from "../api/client";
import type { AnswerState, AnswerRun } from "../api/types";
import { currentConversation, rememberTurn, startConversation } from "../app/conversation";
import { PERMISSIONS } from "../app/nav";
import { navigate } from "../app/router";
import { useSession } from "../app/session-context";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { CopyableId } from "../components/CopyableId";
import { Link } from "../components/Link";
import { DisclaimerBar, LongText } from "../components/NoticeBar";
import { RunStateTag } from "../components/StatusTag";
import { runStateLabel } from "../app/statusLabels";
import { useAnswerStream } from "../hooks/useAnswerStream";
import { useAsync } from "../hooks/useAsync";

/**
 * 单次问答运行（《前端界面说明》§5.3，**核心页面**）。
 *
 * 设计 §9.4 的两条口径决定这个页面长什么样：
 * 1. **进度随状态转移推送**，正式答案**只在终态**推 —— 所以先出现时间线，结论最后才出现；
 * 2. **门禁没过的结论不能长得像结论** —— `published=false` 时用警示色边框 + 明确字样，
 *    与正式结论在视觉上区分开。
 *
 * 另外：**内容取不到就如实说取不到**（正文只在短期缓存里，§21），不显示空白或假内容。
 */

const TERMINAL: AnswerState[] = [
  "ANSWERED",
  "PARTIAL",
  "NEEDS_REVIEW",
  "INSUFFICIENT_EVIDENCE",
  "FAILED",
];

const BLOCKED_REASON: Record<string, string> = {
  scope: "问题问的是提问者本人的情形，不给个人结论",
  verification: "引用核验未通过：结论里的引用或数值没落在本次证据里",
  format: "模型没有按约定输出结构化结论",
  clarifying: "问题太笼统，需要补充信息",
};

/**
 * 各阶段的**经验总耗时**（秒），用来给用户一个「还要多久」的量级。
 *
 * ⚠️ **这是粗估，不是预测**：取自本机实测（检索 ~6 s、装配 <1 s、生成 20–60 s、核验 <1 s，
 * 一次问答通常 55–90 s；**首次加载 7B 权重会更久**）。换机器、换模型都要重核这些数。
 * 界面上必须写明它是经验值——**把估算说成事实，比不给估算更糟**。
 */
const STAGE_BUDGET_SECONDS: Record<string, number> = {
  CREATED: 90,
  RETRIEVING: 85,
  RERANKING: 80,
  ASSEMBLING_EVIDENCE: 75,
  GENERATING: 70,
  VERIFYING: 15,
};

const DEFAULT_BUDGET_SECONDS = 90;

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest === 0 ? `${minutes} 分` : `${minutes} 分 ${rest} 秒`;
}

function remainingLabel(state: string | null, elapsed: number): string {
  const budget = (state && STAGE_BUDGET_SECONDS[state]) || DEFAULT_BUDGET_SECONDS;
  const remaining = budget - elapsed;
  // 超了预算就**如实说慢**，不要显示「还需 0 秒」或者干脆不显示——那是在骗人
  if (remaining <= 15) return "比预计慢一些，仍在处理中";
  if (remaining < 60) return "预计还需不到 1 分钟";
  return `预计还需约 ${Math.ceil(remaining / 60)} 分钟`;
}

/** 已用时间（秒）。起点取运行记录的 `created_at`——**刷新页面后仍然算得准**。 */
function useElapsedSeconds(startedAt: string | null | undefined): number {
  const [mountedAt] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const start = startedAt ? dayjs(startedAt).valueOf() : mountedAt;
  return Math.max(0, Math.round((now - start) / 1000));
}

/**
 * 把后端渲染好的澄清文本拆成「问题 + 候选项」。
 *
 * ⚠️ **这是在解析我们自己渲染的格式**（`clarify.Clarification.render()` 产出
 * `问题\n\n1. 甲\n2. 乙`）。接口目前只回传渲染后的文本，所以前端只能这样拆。
 * 拆不出来就整段显示（不影响提交补充）。**要根治得让接口回传结构化的 options。**
 */
function splitClarification(text: string): { question: string; options: string[] } {
  const lines = text.split("\n");
  const options: string[] = [];
  const questionLines: string[] = [];

  for (const line of lines) {
    const match = /^\s*\d+[.、]\s*(.+)$/.exec(line);
    if (match) options.push(match[1].trim());
    else if (!(options.length > 0 && line.trim() === "")) questionLines.push(line);
  }

  if (options.length < 2) return { question: text, options: [] };
  return { question: questionLines.join("\n").trim(), options };
}

export default function AnswerRunPage({ runId }: { runId: string }) {
  const { can } = useSession();
  const screens = Grid.useBreakpoint();
  const stream = useAnswerStream(runId);
  const detail = useAsync<AnswerRun>(() => getRun(runId), [runId]);

  const [supplement, setSupplement] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");

  const reloadDetail = detail.reload;

  // 流结束后再拉一次详情：`reviewed_at` / `review_note` 只在 REST 里，
  // 而且终态之后运行记录里的字段才齐（证据条数、是否发布）。
  useEffect(() => {
    if (stream.status === "closed") void reloadDetail();
  }, [stream.status, reloadDetail]);

  const run = detail.status === "ready" ? detail.data : null;
  const state: AnswerState | null = run?.state ?? stream.answer?.state ?? null;
  const published = stream.answer?.published ?? run?.published ?? false;
  const blockedBy = stream.answer?.blocked_by ?? run?.blocked_by ?? null;
  const evidenceCount = stream.answer?.evidence_count ?? run?.evidence_count ?? 0;
  const contentAvailable = stream.answer?.content_available ?? run?.content_available ?? false;
  const answerText = stream.answer?.answer ?? run?.answer ?? null;
  const contentNote = stream.answer?.note ?? null;

  const isTerminal = state !== null && TERMINAL.includes(state);
  const isClarifying = state === "CLARIFYING";
  const clarificationText = stream.clarification ?? (isClarifying ? answerText : null);
  const clarification = useMemo(
    () => (clarificationText ? splitClarification(clarificationText) : null),
    [clarificationText],
  );

  async function runAction(task: () => Promise<void>) {
    setBusy(true);
    setActionError("");
    setNotice("");
    try {
      await task();
    } catch (reason) {
      setActionError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function continueAfterClarification() {
    await runAction(async () => {
      await submitClarification(runId, supplement.trim());
      setSupplement("");
      setNotice("已提交补充，正在继续这次运行。");
      stream.reconnect();
      await reloadDetail();
    });
  }

  async function submitReview(note: string) {
    await runAction(async () => {
      await reviewRun(runId, note.trim() || null);
      setNotice("复核已记录。");
      await reloadDetail();
    });
  }

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <AsyncBoundary
        status={detail.status}
        error={detail.error}
        onRetry={() => void reloadDetail()}
      >
        <Card
          title={
            <Space size={8} wrap>
              {state && <RunStateTag state={state} />}
              <CopyableId value={runId} label="运行" />
            </Space>
          }
          extra={
            <Space>
              {stream.status === "open" && <Tag color="processing">实时更新中</Tag>}
              {(stream.status === "error" || stream.timeout) && (
                <Button size="small" icon={<ReloadOutlined />} onClick={stream.reconnect}>
                  重新连接
                </Button>
              )}
            </Space>
          }
        >
          <Descriptions
            size="small"
            column={screens.md ? 2 : 1}
            items={[
              {
                key: "question",
                label: "问题",
                children: run?.question ?? "（内容不可用）",
                span: 2,
              },
              {
                key: "evidence",
                label: "依据条数",
                children: `${evidenceCount} 条`,
              },
              {
                key: "created",
                label: "提交时间",
                children: dayjs(run?.created_at).format("YYYY-MM-DD HH:mm:ss"),
              },
              ...(run?.seconds !== null && run?.seconds !== undefined
                ? [{ key: "seconds", label: "耗时", children: `${run.seconds.toFixed(1)} 秒` }]
                : []),
              ...(blockedBy
                ? [
                    {
                      key: "blocked",
                      label: "门禁",
                      children: BLOCKED_REASON[blockedBy] ?? blockedBy,
                      span: 2,
                    },
                  ]
                : []),
            ]}
          />

          {run?.question && (
            <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
              运行记录里只存问题与回答的哈希，正文在短期缓存中；缓存过期后正文不可再取。
            </Typography.Paragraph>
          )}
        </Card>
      </AsyncBoundary>

      {/* 进度时间线：状态**逐个**出现，能看出当前进行到哪一步 */}
      {stream.timeline.length > 0 && (
        <Card title="进度">
          <Steps
            direction={screens.md ? "horizontal" : "vertical"}
            size="small"
            current={stream.timeline.length - 1}
            items={stream.timeline.map((entry, index) => ({
              title: runStateLabel(entry.state),
              description: dayjs(entry.at).format("HH:mm:ss"),
              status: index === stream.timeline.length - 1 && !isTerminal ? "process" : "finish",
            }))}
          />
          {stream.timeout && (
            <Alert
              style={{ marginTop: 12 }}
              type="warning"
              showIcon
              message="事件流等待超时"
              description={`${stream.timeout}——可以点右上角「重新连接」，或稍后刷新本页查看结果。`}
            />
          )}
          {stream.error && (
            <Alert
              style={{ marginTop: 12 }}
              type="error"
              showIcon
              message="事件流中断"
              description={stream.error}
            />
          )}
        </Card>
      )}

      {/* 澄清：这是**反问**，不是结论 */}
      {isClarifying && clarification && (
        <Card
          title={
            <Space>
              <WarningOutlined style={{ color: "#d46b08" }} />
              需要补充信息
            </Space>
          }
          style={{ borderColor: "#ffd591" }}
        >
          <LongText>{clarification.question}</LongText>
          {clarification.options.length > 0 && (
            <>
              <Divider style={{ margin: "12px 0" }} orientation="left" plain>
                可能的理解（点一下填入补充框）
              </Divider>
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                {clarification.options.map((option) => (
                  <Button
                    key={option}
                    block
                    style={{ textAlign: "left", height: "auto", paddingBlock: 8 }}
                    disabled={busy}
                    onClick={() => setSupplement(option)}
                  >
                    {option}
                  </Button>
                ))}
              </Space>
            </>
          )}

          <Divider style={{ margin: "16px 0 12px" }} />
          <Form layout="vertical">
            <Form.Item label="补充说明" style={{ marginBottom: 8 }}>
              <Input.TextArea
                value={supplement}
                rows={3}
                maxLength={2000}
                disabled={busy}
                placeholder="补充后会用「原问题 + 补充」继续这次运行"
                onChange={(event) => setSupplement(event.target.value)}
              />
            </Form.Item>
            <Button
              type="primary"
              loading={busy}
              disabled={!supplement.trim()}
              onClick={() => void continueAfterClarification()}
            >
              补充并继续
            </Button>
          </Form>
        </Card>
      )}

      {/* 终态：正式结论 / 拒答说明，**两者视觉上必须不同** */}
      {isTerminal && (
        <Card
          title={
            <Space>
              {published ? (
                <>
                  <CheckCircleOutlined style={{ color: "#389e0d" }} />
                  结论
                </>
              ) : (
                <>
                  <WarningOutlined style={{ color: "#cf1322" }} />
                  这不是正式结论
                </>
              )}
            </Space>
          }
          style={published ? { borderColor: "#b7eb8f" } : { borderColor: "#ffa39e" }}
        >
          {!published && (
            <Alert
              style={{ marginBottom: 12 }}
              type="error"
              showIcon
              message="未通过门禁，不作为正式答案发布"
              description={blockedBy ? (BLOCKED_REASON[blockedBy] ?? blockedBy) : undefined}
            />
          )}

          {contentAvailable && answerText ? (
            <LongText>{answerText}</LongText>
          ) : (
            <Alert
              type="warning"
              showIcon
              message="内容不可用"
              description={
                contentNote ??
                "这次运行没有产出内容，或短期缓存已过期（运行记录里不存正文，只留哈希）。"
              }
            />
          )}

          <Divider style={{ margin: "16px 0 12px" }} />
          <Space direction="vertical" size={4} style={{ width: "100%" }}>
            <Typography.Text type="secondary">
              依据 {evidenceCount} 条 · 状态 {state ? runStateLabel(state) : "—"}
            </Typography.Text>
            <Typography.Text type="secondary">
              结论须经人工审核后方可对外使用；对外出具法律意见请由执业律师判断。
            </Typography.Text>
          </Space>
        </Card>
      )}

      {!isTerminal && !isClarifying && (
        <ProcessingCard state={state} startedAt={run?.created_at ?? null} />
      )}

      <DisclaimerBar />

      {/* 继续追问（§5.8 / 设计 §9.6）：**只在终态渲染**——运行还没跑完就追问，用户会同时看到
          两条在飞的运行，而单机是单并发（§14.2），新的那条还得等前面那条让出资源。 */}
      {isTerminal && <FollowUpPanel run={run} />}

      {/* 人工复核（§9.3 第三层）：有 review.decide 才渲染；强制校验在后端 */}
      {can(PERMISSIONS.reviewDecide) && (
        <ReviewPanel run={run} busy={busy} onSubmit={(note) => void submitReview(note)} />
      )}

      {notice && <Alert type="success" showIcon message={notice} />}
      {actionError && <Alert type="error" showIcon message={actionError} />}
    </Space>
  );
}

function ReviewPanel({
  run,
  busy,
  onSubmit,
}: {
  run: AnswerRun | null;
  busy: boolean;
  onSubmit: (note: string) => void;
}) {
  const [note, setNote] = useState("");
  const reviewed = run?.reviewed_at !== null && run?.reviewed_at !== undefined;

  if (reviewed && run) {
    return (
      <Card
        title={
          <Space>
            <SafetyCertificateOutlined />
            已复核
          </Space>
        }
      >
        <Descriptions
          size="small"
          column={1}
          items={[
            {
              key: "at",
              label: "复核时间",
              children: dayjs(run.reviewed_at).format("YYYY-MM-DD HH:mm:ss"),
            },
            { key: "by", label: "复核人", children: <CopyableId value={run.reviewed_by ?? ""} /> },
            { key: "note", label: "结论", children: run.review_note || "（未填备注）" },
          ]}
        />
        <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
          复核不改变运行状态——状态机记的是「当时怎么走的」，复核是之后发生的另一件事。
        </Typography.Paragraph>
      </Card>
    );
  }

  return (
    <Card
      title={
        <Space>
          <SafetyCertificateOutlined />
          人工复核
        </Space>
      }
    >
      <Form layout="vertical" onFinish={() => onSubmit(note)} disabled={busy}>
        <Form.Item label="复核结论（可选，但驳回时应当写明理由）">
          <Input.TextArea
            rows={3}
            maxLength={2000}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="例如：已核对条文，属个人情形，转执业律师。"
          />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={busy}>
          记录复核
        </Button>
      </Form>
    </Card>
  );
}

/**
 * 生成中的进度卡（《前端界面说明》§5.3）。
 *
 * **为什么要给时间量级**：一次问答要 1–2 分钟，而页面在这期间除了状态文字没有任何变化——
 * 用户不知道是在跑还是卡住了。给一个**已用时间（真实）**加**预计量级（粗估）**，是让人放心的
 * 最低成本做法。
 *
 * ⚠️ **必须写明是粗估**：估算说成事实比不给估算更糟（§20.2 的「标注不确定性」同一口径）。
 * 进度条**刻意不到 100%**——到了 100% 却还在转，比停在 80% 更让人怀疑。
 */
function ProcessingCard({
  state,
  startedAt,
}: {
  state: AnswerState | null;
  startedAt: string | null;
}) {
  const elapsed = useElapsedSeconds(startedAt);
  const budget = (state && STAGE_BUDGET_SECONDS[state]) || DEFAULT_BUDGET_SECONDS;
  const percent = Math.min(95, Math.max(5, Math.round((elapsed / budget) * 100)));

  return (
    <Card
      title={
        <Space>
          <LoadingOutlined />
          正在处理
        </Space>
      }
    >
      <Space direction="vertical" size={10} style={{ width: "100%" }}>
        <Space wrap size={12}>
          <Tag color="processing">{state ? runStateLabel(state) : "等待中"}</Tag>
          <Typography.Text type="secondary">已用 {formatDuration(elapsed)}</Typography.Text>
          <Typography.Text strong>{remainingLabel(state, elapsed)}</Typography.Text>
        </Space>

        <Progress percent={percent} status="active" showInfo={false} />

        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          时间按本机实测的经验值粗估，不是精确预测：生成阶段最慢，一次问答通常 1–2 分钟，
          首次加载模型会更久。可以离开本页，运行会在后台继续；超过 5 分钟没有变化可刷新重进。
        </Typography.Text>
      </Space>
    </Card>
  );
}

/**
 * 继续追问（《前端界面说明》§5.8；设计 §9.6 多轮追问）。
 *
 * 后端会把**最近 1 轮**的问题与这次追问拼成自足的问题再检索；**历史答案不进提示词**（§9.6 边界一）。
 *
 * ⚠️ **会话 id 不进 URL**（§9.6）：后端认的那个在运行记录里（`run.session_id`），本地只留一份
 * 轮次列表，用来给用户一个「回到前几轮」的入口。**本地列表只在与运行记录的会话一致时才有意义**
 * ——否则它是别的会话遗留的，拼进来只会让人误解。
 */
function FollowUpPanel({ run }: { run: AnswerRun | null }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conversation, setConversation] = useState(() => currentConversation());

  const thread =
    run?.session_id && conversation?.sessionId === run.session_id ? conversation.turns : [];

  async function ask() {
    const question = text.trim();
    if (!question) return;
    setBusy(true);
    setError("");
    // ⚠️ 运行没有会话（CLI 建的、或本功能上线前的数据）就**新开一个**——宁可少一点上下文，
    // 也不要把浏览器里遗留的上文拼进来。
    const sessionId = run?.session_id ?? startConversation().sessionId;
    try {
      const next = await submitQuestion({ question, session_id: sessionId });
      rememberTurn(sessionId, next.id, question);
      setConversation(currentConversation());
      setText("");
      navigate(`/answers/${next.id}`);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title={
        <Space>
          <CommentOutlined />
          继续追问
        </Space>
      }
      extra={
        <Button size="small" disabled={busy} onClick={() => setConversation(startConversation())}>
          开始新会话
        </Button>
      }
    >
      {thread.length > 0 && (
        <Space direction="vertical" size={4} style={{ width: "100%", marginBottom: 12 }}>
          {thread.map((turn, index) => (
            <Typography.Text
              key={turn.runId}
              type={turn.runId === run?.id ? undefined : "secondary"}
              strong={turn.runId === run?.id}
            >
              第 {index + 1} 轮：
              {turn.runId === run?.id ? (
                turn.question
              ) : (
                <Link to={`/answers/${turn.runId}`}>{turn.question}</Link>
              )}
            </Typography.Text>
          ))}
        </Space>
      )}

      <Form layout="vertical">
        <Form.Item
          label="追问"
          extra="同一会话内的追问会带上文（后端只取最近 1 轮）；问题文本与会话 id 都不进 URL。"
        >
          <Input.TextArea
            value={text}
            rows={2}
            maxLength={2000}
            showCount
            disabled={busy}
            placeholder="例如：那如果是这样呢？"
            onChange={(event) => setText(event.target.value)}
            onPressEnter={(event) => {
              if (!event.shiftKey) {
                event.preventDefault();
                void ask();
              }
            }}
          />
        </Form.Item>
        <Button
          type="primary"
          icon={<SendOutlined />}
          loading={busy}
          disabled={!text.trim()}
          onClick={() => void ask()}
        >
          提交追问
        </Button>
      </Form>

      {error && <Alert style={{ marginTop: 12 }} type="error" showIcon message={error} />}
    </Card>
  );
}
