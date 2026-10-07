import { ReloadOutlined } from "@ant-design/icons";
import { Button, Card, Col, Radio, Row, Space, Typography } from "antd";
import { useState } from "react";

import { listRuns } from "../api/answers";
import type { AnswerRun } from "../api/types";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { RunCard } from "../components/RunCard";
import { useAsync } from "../hooks/useAsync";

/**
 * 待审队列（《前端界面说明》§5.4）。
 *
 * 设计 §9.3 第三层：被门禁拦下的运行在这里等人判读。**队列只收 `NEEDS_REVIEW`**——
 * 「依据不足」「限定范围作答」不进队列（前者没料、后者按 §9.4 属「限制回答范围」）。
 *
 * 入口本身由 `App` 按 `review.decide` 权限控制；**强制校验在后端**。
 */
export default function ReviewQueuePage() {
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
          复核**不改变运行状态**——状态机记的是「当时怎么走的」，复核是之后发生的另一件事，
          结论写在复核备注里并写审计。
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
