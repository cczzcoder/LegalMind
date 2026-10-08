import { Card, Space, Tag, Typography } from "antd";
import dayjs from "dayjs";

import type { AnswerRun } from "../api/types";
import { Link } from "../components/Link";
import { CopyableId } from "./CopyableId";
import { RunStateTag } from "./StatusTag";

/**
 * 一次问答运行的摘要卡片（问答页与待审队列共用）。
 *
 * **用卡片而不是表格**：记录列表在窄屏会退化成横向滚动的表格，而卡片天然是纵向的。
 * 这样就不需要「宽屏表格 / 窄屏卡片」两套渲染（见《前端界面说明》§4）。
 */

const BLOCKED_LABEL: Record<string, string> = {
  scope: "涉及本人情形",
  verification: "引用核验未通过",
  format: "输出不合约定",
  clarifying: "在等用户补充",
};

export function RunCard({ run, extra }: { run: AnswerRun; extra?: React.ReactNode }) {
  return (
    <Card
      size="small"
      style={{ height: "100%" }}
      title={
        <Space size={8} wrap>
          <RunStateTag state={run.state} />
          {run.published ? (
            <Tag color="success" style={{ marginInlineEnd: 0 }}>
              已发布
            </Tag>
          ) : (
            <Tag style={{ marginInlineEnd: 0 }}>未发布</Tag>
          )}
          {run.review_required && run.reviewed_at === null && (
            <Tag color="error" style={{ marginInlineEnd: 0 }}>
              待人工判读
            </Tag>
          )}
          {run.reviewed_at !== null && (
            <Tag color="blue" style={{ marginInlineEnd: 0 }}>
              已复核
            </Tag>
          )}
        </Space>
      }
      extra={
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {dayjs(run.created_at).format("MM-DD HH:mm")}
        </Typography.Text>
      }
    >
      <Space direction="vertical" size={4} style={{ width: "100%" }}>
        <Typography.Paragraph style={{ marginBottom: 0 }} ellipsis={{ rows: 2 }}>
          {run.question ?? <Typography.Text type="secondary">（问题内容不可用）</Typography.Text>}
        </Typography.Paragraph>

        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {run.evidence_count} 条依据
          {run.blocked_by && ` · 门禁：${BLOCKED_LABEL[run.blocked_by] ?? run.blocked_by}`}
          {run.seconds !== null && ` · 耗时 ${run.seconds.toFixed(1)}s`}
        </Typography.Text>

        <Space size={8} wrap>
          {/* `tap-target`：窄屏下把只有一行文字高的链接撑到 ≥44px（《前端界面说明》§4） */}
          <Link className="tap-target" to={`/answers/${run.id}`}>
            查看详情
          </Link>
          <CopyableId value={run.id} />
        </Space>

        {extra}
      </Space>
    </Card>
  );
}
