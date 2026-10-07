import { FileSearchOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Empty,
  Form,
  Input,
  InputNumber,
  Row,
  Space,
  Switch,
  Tag,
  Typography,
} from "antd";
import { useState } from "react";

import { searchProvisions } from "../api/retrieval";
import type { ProvisionHit, SearchResponse } from "../api/types";
import { Link } from "../components/Link";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { CopyableId } from "../components/CopyableId";
import { LegalStatusTag, ReviewStatusTag } from "../components/StatusTag";
import { useAsync } from "../hooks/useAsync";

/**
 * 条款检索（《前端界面说明》§5.5）。
 *
 * 三条要在界面上说清楚的事：
 * 1. **效力状态**：非现行有效的版本要用警示色（设计 §8.3）——拿它当依据是要防的事；
 * 2. **走了哪条通路**：级联检索下 `path` 是 exact / keyword / vector，不说明的话用户没法解释
 *    「为什么结果看起来不相关」；
 * 3. **审核状态不过滤但回传**：`pending` 是数据质量标记，不是效力事实。
 */

const PATH_LABEL: Record<string, string> = {
  exact: "精确字段命中",
  keyword: "关键词命中",
  vector: "语义兜底（关键词无命中）",
  clarify: "未检索（转澄清）",
};

export default function SearchPage() {
  const [form] = Form.useForm<{
    semantic?: string;
    keyword?: string;
    instrument_title?: string;
    article_number?: string;
    limit: number;
    include_not_yet_effective: boolean;
  }>();

  // 只有「提交过查询」才请求——空查询不发请求（验收标准 3）
  const [submitted, setSubmitted] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");

  const result = useAsync<SearchResponse | null>(
    async () => (submitted ? searchProvisions(submitted) : null),
    [submitted],
  );

  function onSubmit(values: Record<string, unknown>) {
    setError("");
    const semantic = typeof values.semantic === "string" ? values.semantic.trim() : "";
    const keyword = typeof values.keyword === "string" ? values.keyword.trim() : "";
    // semantic 与 keyword **互斥**（同时给后端会 422）；有 semantic 时优先走它
    setSubmitted({
      semantic: semantic || undefined,
      keyword: semantic ? undefined : keyword || undefined,
      instrument_title: (values.instrument_title as string)?.trim() || undefined,
      article_number: (values.article_number as string)?.trim() || undefined,
      limit: values.limit ?? 20,
      include_not_yet_effective: Boolean(values.include_not_yet_effective),
    });
  }

  const hits = result.data?.hits ?? [];
  const nonCurrent = hits.filter((hit) => hit.legal_status !== "effective");

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card>
        <Form
          form={form}
          layout="vertical"
          initialValues={{ limit: 20, include_not_yet_effective: false }}
          onFinish={onSubmit}
        >
          <Form.Item
            name="semantic"
            label="自然语言或关键词"
            extra="留空则只按下面的精确字段与过滤条件浏览。"
          >
            <Input
              size="large"
              allowClear
              placeholder="例如：用人单位欠缴社会保险费"
              prefix={<FileSearchOutlined />}
              onPressEnter={() => form.submit()}
            />
          </Form.Item>

          <Collapse
            ghost
            items={[
              {
                key: "advanced",
                label: "精确字段与过滤",
                children: (
                  <Row gutter={16}>
                    <Col xs={24} md={8}>
                      <Form.Item name="instrument_title" label="法律名称">
                        <Input placeholder="如《中华人民共和国劳动法》" allowClear />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item name="article_number" label="条号">
                        <Input placeholder="第八十七条 或 87" allowClear />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item name="keyword" label="关键词（与自然语言互斥）">
                        <Input placeholder="仅在未填自然语言时生效" allowClear />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item name="limit" label="返回条数">
                        <InputNumber min={1} max={200} style={{ width: "100%" }} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={16}>
                      <Form.Item
                        name="include_not_yet_effective"
                        label="包含「已公布未生效」"
                        valuePropName="checked"
                        extra="默认屏蔽。核对未来生效的版本时才打开。"
                      >
                        <Switch />
                      </Form.Item>
                    </Col>
                  </Row>
                ),
              },
            ]}
          />

          <Button
            type="primary"
            size="large"
            htmlType="submit"
            loading={result.status === "loading"}
          >
            检索
          </Button>
        </Form>
        {error && <Alert style={{ marginTop: 12 }} type="error" showIcon message={error} />}
      </Card>

      {submitted && (
        <AsyncBoundary
          status={result.status}
          error={result.error}
          isEmpty={hits.length === 0}
          emptyText="没有命中。换个说法、补一个关键词，或者到「问答」页用自然语言问。"
          emptyExtra={
            <Link to="/ask">
              <Button type="primary">去问答页</Button>
            </Link>
          }
          onRetry={() => void result.reload()}
        >
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Card size="small">
              <Space size={12} wrap>
                <Typography.Text>
                  命中 <strong>{hits.length}</strong> 条
                  {result.data?.truncated && "（已截断，可提高返回条数）"}
                </Typography.Text>
                <Tag color="blue">{PATH_LABEL[result.data?.path ?? ""] ?? result.data?.path}</Tag>
                {nonCurrent.length > 0 && (
                  <Tag color="warning">{nonCurrent.length} 条非现行有效</Tag>
                )}
                <Button size="small" icon={<ReloadOutlined />} onClick={() => void result.reload()}>
                  重试
                </Button>
              </Space>
            </Card>

            {nonCurrent.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message="结果里有非现行有效的版本"
                description="已废止或已被取代的版本可以用于核对历史，但不能直接作为当前依据。"
              />
            )}

            {hits.map((hit) => (
              <HitCard key={hit.citation.provision_version_id} hit={hit} />
            ))}
          </Space>
        </AsyncBoundary>
      )}

      {!submitted && (
        <Card>
          <Empty description="输入查询条件后开始检索" />
        </Card>
      )}
    </Space>
  );
}

function HitCard({ hit }: { hit: ProvisionHit }) {
  const [expanded, setExpanded] = useState(false);
  const long = hit.text.length > 120;
  const shown = expanded || !long ? hit.text : `${hit.text.slice(0, 120)}…`;

  return (
    <Card
      size="small"
      title={
        <Space size={8} wrap>
          <Typography.Text strong>
            {hit.instrument_title} {hit.provision_display ?? hit.provision_number}
          </Typography.Text>
          <LegalStatusTag status={hit.legal_status} />
          <ReviewStatusTag status={hit.review_status} />
        </Space>
      }
      extra={
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {hit.version_label}
        </Typography.Text>
      }
    >
      <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", lineHeight: 1.75 }}>
        {shown}
      </div>

      {long && (
        <Button type="link" style={{ paddingInline: 0 }} onClick={() => setExpanded(!expanded)}>
          {expanded ? "收起" : "展开全文"}
        </Button>
      )}

      <div style={{ marginTop: 8 }}>
        <Space direction="vertical" size={2}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {hit.issuing_body}
            {hit.document_number ? ` · ${hit.document_number}` : ""}
            {hit.effective_from ? ` · 自 ${hit.effective_from} 起施行` : ""}
          </Typography.Text>
          <CopyableId
            value={hit.citation.provision_version_id}
            label="条款版本（可作为 Wiki 引用）"
          />
        </Space>
      </div>
    </Card>
  );
}
