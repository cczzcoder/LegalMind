import { ReloadOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Col,
  Input,
  Modal,
  Radio,
  Row,
  Segmented,
  Space,
  Typography,
} from "antd";
import dayjs from "dayjs";
import { useState } from "react";

import { listRuns } from "../api/answers";
import { describeError } from "../api/client";
import type { AnswerRun, PendingWikiRevision } from "../api/types";
import * as wiki from "../api/wiki";
import { useSession } from "../app/session-context";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { Link } from "../components/Link";
import { RunCard } from "../components/RunCard";
import { useAsync } from "../hooks/useAsync";

/**
 * 待审队列（《前端界面说明》§5.4、§5.6）。
 *
 * 两类**等人判读**的东西都收在这里，用页签分开：
 *
 * - **问答运行**（设计 §9.3 第三层）：被门禁拦下的运行。**队列只收 `NEEDS_REVIEW`**——
 *   「依据不足」「限定范围作答」不进队列（前者没料、后者按 §9.4 属「限制回答范围」）。
 * - **Wiki 修订**（设计 §10.2）：`submitted` 状态、等着审核发布的修订。
 *   ⚠️ **这一块此前只有接口没有界面**（`GET /wiki/revisions/pending` 一直没人调）——
 *   审核人只能一页页翻知识库去找「哪条等着审」，队列也就形同虚设。
 *
 * 入口本身由 `App` 按 `review.decide` 权限控制；**强制校验在后端**。
 */
export default function ReviewQueuePage() {
  const [kind, setKind] = useState<"runs" | "wiki">("runs");
  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Segmented
        value={kind}
        onChange={(value) => setKind(value as "runs" | "wiki")}
        options={[
          { value: "runs", label: "问答运行" },
          { value: "wiki", label: "Wiki 修订" },
        ]}
      />
      {kind === "runs" ? <RunQueue /> : <WikiQueue />}
    </Space>
  );
}

function RunQueue() {
  const [scope, setScope] = useState<"pending" | "all">("pending");
  const runs = useAsync<AnswerRun[]>(
    () => listRuns({ pending: scope === "pending", limit: 100 }),
    [scope],
  );
  const rows = runs.data ?? [];

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card
        title={
          <Radio.Group
            value={scope}
            onChange={(event) => setScope(event.target.value)}
            optionType="button"
            buttonStyle="solid"
          >
            <Radio.Button value="pending">只看待复核</Radio.Button>
            <Radio.Button value="all">全部运行</Radio.Button>
          </Radio.Group>
        }
        extra={
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={runs.status === "loading"}
            onClick={() => void runs.reload()}
          >
            刷新
          </Button>
        }
      >
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          复核<strong>不改变运行状态</strong>——状态机记的是「当时怎么走的」，复核是之后发生的
          另一件事，结论写在复核备注里并写审计。
        </Typography.Paragraph>
      </Card>

      <AsyncBoundary
        status={runs.status}
        error={runs.error}
        isEmpty={rows.length === 0}
        emptyText={scope === "pending" ? "没有待复核的运行。" : "还没有任何运行记录。"}
        onRetry={() => void runs.reload()}
      >
        <Row gutter={[12, 12]}>
          {rows.map((run) => (
            <Col key={run.id} xs={24} lg={12}>
              <RunCard run={run} />
            </Col>
          ))}
        </Row>
      </AsyncBoundary>
    </Space>
  );
}

function WikiQueue() {
  const { user } = useSession();
  const [target, setTarget] = useState<{
    revision: PendingWikiRevision;
    decision: "publish" | "reject";
  } | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const queue = useAsync<PendingWikiRevision[]>(() => wiki.listPending(100), []);
  const rows = queue.data ?? [];

  /** 执行审核结论。**失败时不关弹窗**——备注不能因为一次网络抖动就丢掉。 */
  async function submit() {
    if (!target) return;
    setBusy(true);
    setError("");
    try {
      const text = note.trim() || null;
      if (target.decision === "publish") {
        await wiki.publishRevision(target.revision.page_id, target.revision.number, text);
      } else {
        await wiki.rejectRevision(target.revision.page_id, target.revision.number, text);
      }
      setTarget(null);
      setNote("");
      await queue.reload();
    } catch (reason) {
      // 最常见的是「作者不能审自己的修订」与「发布前至少要有一条引用」，两者都由后端判定
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card
        title="等着审核发布的修订"
        extra={
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={queue.status === "loading"}
            onClick={() => void queue.reload()}
          >
            刷新
          </Button>
        }
      >
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          发布后读者看到的就是这一版；<strong>作者不能审自己的修订</strong>（设计 §10.3 的独立审核
          要求），发布前还至少要有一条引用。驳回理由会显示给作者，也进审计。
        </Typography.Paragraph>
      </Card>

      <AsyncBoundary
        status={queue.status}
        error={queue.error}
        isEmpty={rows.length === 0}
        emptyText="没有等着审核的修订。"
        onRetry={() => void queue.reload()}
      >
        <Row gutter={[12, 12]}>
          {rows.map((revision) => {
            const isAuthor = user !== null && revision.author_id === user.id;
            return (
              <Col key={revision.id} xs={24} lg={12}>
                <Card
                  size="small"
                  title={<Link to={`/wiki/${revision.page_id}`}>{revision.page_title}</Link>}
                  extra={
                    <Typography.Text type="secondary">
                      修订 {revision.number} · {dayjs(revision.created_at).format("MM-DD HH:mm")}
                    </Typography.Text>
                  }
                >
                  <Space direction="vertical" size={8} style={{ width: "100%" }}>
                    <Typography.Paragraph style={{ marginBottom: 0 }} ellipsis={{ rows: 4 }}>
                      {revision.body}
                    </Typography.Paragraph>
                    {isAuthor ? (
                      <Alert
                        type="info"
                        showIcon
                        message="这是你自己的修订"
                        description="作者不能审自己的修订，请让其他审核人处理。"
                      />
                    ) : (
                      <Space wrap>
                        <Button
                          type="primary"
                          onClick={() => {
                            setError("");
                            setNote("");
                            setTarget({ revision, decision: "publish" });
                          }}
                        >
                          发布
                        </Button>
                        <Button
                          danger
                          onClick={() => {
                            setError("");
                            setNote("");
                            setTarget({ revision, decision: "reject" });
                          }}
                        >
                          驳回
                        </Button>
                      </Space>
                    )}
                  </Space>
                </Card>
              </Col>
            );
          })}
        </Row>
      </AsyncBoundary>

      <Modal
        open={target !== null}
        title={target?.decision === "publish" ? "审核通过并发布" : "驳回这条修订"}
        okText={target?.decision === "publish" ? "发布" : "驳回"}
        okButtonProps={{ danger: target?.decision === "reject", loading: busy }}
        cancelText="取消"
        onOk={() => void submit()}
        onCancel={() => setTarget(null)}
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Typography.Text>
            {target?.revision.page_title} · 修订 {target?.revision.number}
          </Typography.Text>
          <Typography.Text type="secondary">
            {target?.decision === "publish"
              ? "发布后读者看到的就是这一版（设计 §10.2）。备注可留空。"
              : "驳回理由会显示给作者，也会进审计——建议写清「哪里不行、怎么改」。"}
          </Typography.Text>
          <Input.TextArea
            rows={3}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder={
              target?.decision === "publish"
                ? "例如：与原文核对无误"
                : "例如：引用的条款版本已失效，请换用现行版本"
            }
            maxLength={2000}
          />
          {error && <Alert type="error" showIcon message={error} />}
        </Space>
      </Modal>
    </Space>
  );
}
