import { CheckOutlined, CloseOutlined, ReloadOutlined } from "@ant-design/icons";
import { Button, Card, Col, Input, Modal, Radio, Row, Space, Tag, Typography } from "antd";
import { useState } from "react";

import { listLegalVersions, reviewLegalVersion } from "../api/legalVersions";
import type { LegalVersion, ReviewVersionDecision } from "../api/types";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { LegalStatusTag, ReviewStatusTag } from "../components/StatusTag";
import { useAsync } from "../hooks/useAsync";

/**
 * 版本复核（《前端界面说明》§5.9；需求第 3 节「法律审核员：审核资料版本」）。
 *
 * 解析流水线对低置信度的落库会置 `review_status='pending'`（设计 §5.1、§7）——此前**没有任何
 * 路径能把它改掉**，只能连库改 SQL；V1.42 先补了 CLI，这里补上界面。
 *
 * ⚠️ **两个状态必须分开显示，别让复核看起来像是在改效力状态**：
 * - `legal_status`（效力状态）是**法律事实**，由正文前言的公布信息推出，**复核改不了它**；
 * - `review_status`（审核状态）是**数据质量标记**，复核改的就是它。
 *
 * 入口由 `App` 按 `review.decide` 权限控制；**强制校验在后端**。
 */
export default function LegalVersionsPage() {
  const [scope, setScope] = useState<"pending" | "all">("pending");
  const [target, setTarget] = useState<{
    version: LegalVersion;
    decision: ReviewVersionDecision;
  } | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const versions = useAsync<LegalVersion[]>(
    () => listLegalVersions({ pending: scope === "pending", limit: 100 }),
    [scope],
  );
  const rows = versions.data ?? [];

  async function submit() {
    if (!target) return;
    setBusy(true);
    try {
      await reviewLegalVersion(target.version.id, target.decision, note.trim() || null);
      setTarget(null);
      setNote("");
      await versions.reload();
    } finally {
      setBusy(false);
    }
  }

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
            <Radio.Button value="all">全部版本</Radio.Button>
          </Radio.Group>
        }
        extra={
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={versions.status === "loading"}
            onClick={() => void versions.reload()}
          >
            刷新
          </Button>
        }
      >
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          复核改的是「审核状态」（这条记录的元数据可不可信），
          <Typography.Text strong>不是「效力状态」</Typography.Text>
          ——后者是法律事实，由正文前言的公布信息推出，复核改不了它。审错了可以再复核一次，
          每次都会写审计。
        </Typography.Paragraph>
      </Card>

      <AsyncBoundary
        status={versions.status}
        error={versions.error}
        isEmpty={rows.length === 0}
        emptyText={scope === "pending" ? "没有待复核的法律版本。" : "还没有任何法律版本。"}
        onRetry={() => void versions.reload()}
      >
        <Row gutter={[12, 12]}>
          {rows.map((version) => (
            <Col key={version.id} xs={24} lg={12}>
              <Card
                size="small"
                title={version.instrument_title}
                extra={<Typography.Text type="secondary">{version.version_label}</Typography.Text>}
                actions={[
                  <Button
                    key="approve"
                    type="link"
                    icon={<CheckOutlined />}
                    onClick={() => {
                      setNote("");
                      setTarget({ version, decision: "approved" });
                    }}
                  >
                    确认可用
                  </Button>,
                  <Button
                    key="reject"
                    type="link"
                    danger
                    icon={<CloseOutlined />}
                    onClick={() => {
                      setNote("");
                      setTarget({ version, decision: "rejected" });
                    }}
                  >
                    驳回
                  </Button>,
                ]}
              >
                <Space direction="vertical" size={4} style={{ width: "100%" }}>
                  <Space size={8} wrap>
                    <Typography.Text type="secondary">效力状态</Typography.Text>
                    <LegalStatusTag status={version.legal_status} />
                    <Typography.Text type="secondary">审核状态</Typography.Text>
                    <ReviewStatusTag status={version.review_status} />
                  </Space>
                  <Typography.Text type="secondary" className="long-text">
                    原件：{version.artifact_filename ?? "（未挂原件）"}
                  </Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {version.id}
                  </Typography.Text>
                </Space>
              </Card>
            </Col>
          ))}
        </Row>
      </AsyncBoundary>

      <Modal
        open={target !== null}
        title={target?.decision === "approved" ? "确认这条版本可用" : "驳回这条版本"}
        okText={target?.decision === "approved" ? "确认可用" : "驳回"}
        okButtonProps={{ danger: target?.decision === "rejected", loading: busy }}
        cancelText="取消"
        onOk={() => void submit()}
        onCancel={() => setTarget(null)}
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Typography.Text>
            {target?.version.instrument_title} · {target?.version.version_label}
          </Typography.Text>
          <Typography.Text type="secondary">
            备注会写进审计（可选，但建议写清依据——半年后回头看，只有 id 是认不出来的）。
          </Typography.Text>
          <Input.TextArea
            rows={3}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="例如：公布信息完整，与原文一致"
            maxLength={1000}
          />
          <Tag color="default" style={{ marginInlineEnd: 0 }}>
            效力状态不会被改动
          </Tag>
        </Space>
      </Modal>
    </Space>
  );
}
