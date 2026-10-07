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
import {
  currentConversation,
  ensureConversation,
  rememberTurn,
  startConversation,
  type Conversation,
} from "../app/conversation";
import { navigate } from "../app/router";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { DisclaimerBar } from "../components/NoticeBar";
import { RunCard } from "../components/RunCard";
import { useAsync } from "../hooks/useAsync";
import { listRuns } from "../api/answers";

/**
 * 问答页（《前端界面说明》§5.2、§5.8）。
 *
 * **提交后立刻跳转**：问答是异步的（生成要几十秒），提问页不该挂在那儿等。
 * 跳转后由运行详情页订阅 SSE 展示进度。
 *
 * ⚠️ **本页默认在会话里提问**（设计 §9.6）。这不是可有可无的：**第一轮如果不带 `session_id`，
 * 后端就不知道上文，用户在运行页追问时补不出自足的问题**。所以第一次提交即开会话，
 * 页面上给一个显式的「开始新会话」——**问不相干的问题时该先点它**，否则上文会把检索带偏。
 */
export default function AskPage() {
  const screens = Grid.useBreakpoint();
  const [question, setQuestion] = useState("");
  const [limit, setLimit] = useState(5);
  const [maxTokens, setMaxTokens] = useState(512);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [cacheMissing, setCacheMissing] = useState(false);
  // 会话状态是**浏览器本地**的（§9.6：会话 id 不进 Cookie、不进 URL）
  const [conversation, setConversation] = useState<Conversation | null>(() =>
    currentConversation(),
  );

  const runs = useAsync<AnswerRun[]>(() => listRuns({ limit: 30 }), []);
  const rows = runs.data ?? [];

  async function submit() {
    const text = question.trim();
    if (!text) return;
    setSubmitting(true);
    setError("");
    setCacheMissing(false);
    // 没有会话就开一个——第一轮必须带上 session_id，否则后续追问接不上上文
    const active = ensureConversation();
    try {
      const run = await submitQuestion({
        question: text,
        limit,
        max_new_tokens: maxTokens,
        session_id: active.sessionId,
      });
      rememberTurn(active.sessionId, run.id, text);
      setConversation(currentConversation());
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

  function newSession() {
    setConversation(startConversation());
    setError("");
    setCacheMissing(false);
  }

  const turnCount = conversation?.turns.length ?? 0;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card size="small">
        <Space wrap size={12}>
          <Typography.Text>
            {turnCount > 0 ? `当前会话：已问 ${turnCount} 轮，追问会带上文` : "当前是新会话"}
          </Typography.Text>
          <Button size="small" onClick={newSession} disabled={submitting}>
            开始新会话
          </Button>
          <Typography.Text type="secondary">
            问不相干的问题前请先开新会话——否则上一轮的上下文会把检索带偏。
          </Typography.Text>
        </Space>
      </Card>

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
