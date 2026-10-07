import { ReloadOutlined, SendOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Col,
  Form,
  Grid,
  Input,
  InputNumber,
  Row,
  Space,
  Typography,
} from "antd";
import { useState } from "react";

import { submitQuestion } from "../api/answers";
import { ApiError, describeError } from "../api/client";
import type { AnswerRun } from "../api/types";
import { navigate } from "../app/router";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { DisclaimerBar } from "../components/NoticeBar";
import { RunCard } from "../components/RunCard";
import { useAsync } from "../hooks/useAsync";
import { listRuns } from "../api/answers";

/**
 * 问答页（《前端界面说明》§5.2）。
 *
 * **提交后立刻跳转**：问答是异步的（生成要几十秒），提问页不该挂在那儿等。
 * 跳转后由运行详情页订阅 SSE 展示进度。
 */
export default function AskPage() {
  const screens = Grid.useBreakpoint();
  const [question, setQuestion] = useState("");
  const [limit, setLimit] = useState(5);
  const [maxTokens, setMaxTokens] = useState(512);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [cacheMissing, setCacheMissing] = useState(false);

  const runs = useAsync<AnswerRun[]>(() => listRuns({ limit: 30 }), []);
  const rows = runs.data ?? [];

  async function submit() {
    const text = question.trim();
    if (!text) return;
    setSubmitting(true);
    setError("");
    setCacheMissing(false);
    try {
      const run = await submitQuestion({ question: text, limit, max_new_tokens: maxTokens });
      setQuestion("");
      navigate(`/answers/${run.id}`);
    } catch (reason) {
      // 503 = 未配置短期缓存。这是**部署配置问题**，要如实说明并给替代路径，
      // 而不是把 503 甩给用户（《前端界面说明》§5.2 验收标准 3）。
      if (reason instanceof ApiError && reason.status === 503) {
        setCacheMissing(true);
      } else {
        setError(describeError(reason));
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card>
        <Form layout="vertical">
          <Form.Item
            label="问题"
            extra="Enter 提交，Shift + Enter 换行。问题越具体，检索到的条文越准。"
          >
            <Input.TextArea
              value={question}
              autoSize={{ minRows: 3, maxRows: 8 }}
              maxLength={2000}
              showCount
              disabled={submitting}
              placeholder="例如：用人单位无故不缴纳社会保险费，会被怎么处理？"
              onChange={(event) => setQuestion(event.target.value)}
              onPressEnter={(event) => {
                if (!event.shiftKey) {
                  event.preventDefault();
                  void submit();
                }
              }}
            />
          </Form.Item>

          <Collapse
            ghost
            items={[
              {
                key: "advanced",
                label: "高级参数",
                children: (
                  <Row gutter={16}>
                    <Col xs={24} sm={12} md={8}>
                      <Form.Item label="取多少条依据">
                        <InputNumber
                          min={1}
                          max={50}
                          value={limit}
                          disabled={submitting}
                          onChange={(value) => setLimit(value ?? 5)}
                          style={{ width: "100%" }}
                        />
                      </Form.Item>
                    </Col>
                    <Col xs={24} sm={12} md={8}>
                      <Form.Item label="最多生成 token">
                        <InputNumber
                          min={64}
                          max={4096}
                          step={64}
                          value={maxTokens}
                          disabled={submitting}
                          onChange={(value) => setMaxTokens(value ?? 512)}
                          style={{ width: "100%" }}
                        />
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
            icon={<SendOutlined />}
            loading={submitting}
            disabled={!question.trim()}
            onClick={() => void submit()}
            block={!screens.sm}
          >
            提交问题
          </Button>
        </Form>

        {error && <Alert style={{ marginTop: 16 }} type="error" showIcon message={error} />}
        {cacheMissing && (
          <Alert
            style={{ marginTop: 16 }}
            type="warning"
            showIcon
            message="当前部署没有配置短期缓存，无法提交异步问答"
            description="问答的正文经短期缓存交付（不落库）。请让管理员配置 CACHE_URL 后重试；在配置好之前，可以先用「条款检索」查条文。"
          />
        )}
      </Card>

      <DisclaimerBar />

      <Card
        title="我提交的运行"
        extra={
          <Button
            size="small"
            icon={<ReloadOutlined />}
            onClick={() => void runs.reload()}
            loading={runs.status === "loading"}
          >
            刷新
          </Button>
        }
      >
        <AsyncBoundary
          status={runs.status}
          error={runs.error}
          isEmpty={rows.length === 0}
          emptyText="还没有提交过问题。上面输入一个问题试试。"
          onRetry={() => void runs.reload()}
        >
          <Row gutter={[12, 12]}>
            {rows.map((run) => (
              <Col key={run.id} xs={24} lg={12}>
                <RunCard run={run} />
              </Col>
            ))}
          </Row>
          {rows.length > 0 && (
            <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
              最多显示最近 30 条。
            </Typography.Paragraph>
          )}
        </AsyncBoundary>
      </Card>
    </Space>
  );
}
