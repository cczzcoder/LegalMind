import { InboxOutlined, ReloadOutlined, UploadOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Form,
  Grid,
  Row,
  Select,
  Space,
  Tag,
  Typography,
  Upload,
} from "antd";
import type { UploadFile } from "antd";
import dayjs from "dayjs";
import { useEffect, useState } from "react";

import { getJob, importDocument, listDocuments, listSources } from "../api/documents";
import { describeError } from "../api/client";
import type { AccessScope, DocumentRecord, JobRecord, Sensitivity } from "../api/types";
import { PERMISSIONS } from "../app/nav";
import { useSession } from "../app/session-context";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { CopyableId } from "../components/CopyableId";
import { useAsync } from "../hooks/useAsync";

/**
 * 文献管理（《前端界面说明》§5.8；设计 §7 入库流水线）。
 *
 * **只做「登记原件 + 看解析进度」两件事**，因为这两条是导入的必经链路：
 *
 * 1. **上传**：`POST /documents` 的请求体是**文件原始字节**（不是 multipart），返回 **202** ——
 *    原件登记了，但**解析任务只是登记、还没跑**；
 * 2. **进度**：`GET /jobs/{id}` 轮询——解析要几十秒到几分钟，**不轮询就等于让用户干等**。
 *
 * ⚠️ **导入 ≠ 能检索**。解析产物、条款身份与版本树由后台流水线生成（`run-worker` 在跑才有）；
 * 任务失败时这里如实显示 `error_code`，**不把它包装成「导入成功」**——尤其 `resource_exhausted`
 * 是资源不足（FR-13），不是业务结果。
 *
 * ⚠️ **权限只做显示层**：上传按钮按 `document.write` 决定渲不渲染，**强制校验永远在后端**。
 */

/** 终态：到了就不再轮询。 */
const TERMINAL_JOB_STATES = ["succeeded", "failed", "cancelled"];

const JOB_LABELS: Record<string, string> = {
  pending: "排队中",
  running: "解析中",
  retry_wait: "等待重试",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const JOB_COLORS: Record<string, string> = {
  pending: "default",
  running: "processing",
  retry_wait: "warning",
  succeeded: "success",
  failed: "error",
  cancelled: "default",
};

const SENSITIVITY_LABELS: Record<Sensitivity, string> = {
  public: "公开",
  internal: "内部",
  confidential: "机密",
};

const ACCESS_SCOPE_LABELS: Record<AccessScope, string> = {
  organization: "本组织可见",
  restricted: "受限（仅显式授权者）",
};

/** 解析失败的原因说人话——**不把错误码直接甩给用户**。 */
const JOB_ERROR_HINTS: Record<string, string> = {
  document_too_large: "原件超过单文件上限，请拆分或压缩后重传。",
  resource_exhausted: "解析时资源不足（内存或磁盘），系统会重试；持续失败请缩小批次。",
  parse_rejected: "原件被解析器拒绝——可能是扫描件（取不到文本层）或格式不支持。",
  document_missing: "原件已不存在，请重新上传。",
  document_unavailable: "原件暂时读不到，稍后会自动重试。",
  internal_error: "解析过程出错，稍后会自动重试。",
};

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

export default function DocumentsPage() {
  const { can } = useSession();
  const screens = Grid.useBreakpoint();

  const documents = useAsync<DocumentRecord[]>(() => listDocuments(), []);
  const sources = useAsync(() => listSources(), []);
  const reloadDocuments = documents.reload;

  const [sourceId, setSourceId] = useState<string | null>(null);
  const [sensitivity, setSensitivity] = useState<Sensitivity>("public");
  const [accessScope, setAccessScope] = useState<AccessScope>("organization");
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  // ⚠️ **单独留一份原始 File**：`fileList[i].originFileObj` 在不同 antd 版本里不保证有，
  // 而上传要的正是裸字节（后端读 `application/octet-stream`）。实测踩过：`originFileObj`
  // 取不到时 `submit()` 会在守卫处静默返回——**按钮点了没反应，也没有任何错误提示**。
  const [rawFile, setRawFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<JobRecord | null>(null);

  const rows = documents.data ?? [];
  const sourceRows = sources.data ?? [];
  const canWrite = can(PERMISSIONS.documentWrite);
  const sourceNames = new Map(sourceRows.map((item) => [item.id, item.name]));

  // 解析进度：每 2 秒问一次，到终态就停，并刷新原件列表
  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let timer = 0;

    async function tick() {
      try {
        const next = await getJob(jobId as string);
        if (cancelled) return;
        setJob(next);
        if (TERMINAL_JOB_STATES.includes(next.status)) {
          window.clearInterval(timer);
          void reloadDocuments();
        }
      } catch (reason) {
        if (!cancelled) setError(describeError(reason));
        window.clearInterval(timer);
      }
    }

    timer = window.setInterval(() => void tick(), 2000);
    void tick();
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [jobId, reloadDocuments]);

  async function submit() {
    if (!sourceId || !rawFile) return;
    setBusy(true);
    setError("");
    setJob(null);
    try {
      const result = await importDocument({
        sourceId,
        file: rawFile,
        sensitivity,
        accessScope,
      });
      setJobId(result.job_id);
      setFileList([]);
      setRawFile(null);
      // 原件已经登记了，列表立刻刷新——**解析还没跑完，所以状态是「排队中」**
      void reloadDocuments();
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {canWrite && (
        <Card
          title={
            <Space>
              <InboxOutlined />
              导入原件
            </Space>
          }
        >
          <Form layout="vertical">
            <Row gutter={16}>
              <Col xs={24} md={8}>
                <Form.Item label="来源" required extra="来源必须先登记授权说明，否则不能入库。">
                  <Select
                    value={sourceId}
                    onChange={setSourceId}
                    disabled={busy}
                    placeholder={sourceRows.length ? "选择来源" : "还没有登记任何来源"}
                    options={sourceRows.map((item) => ({
                      value: item.id,
                      label: `${item.name}（${item.trust_level === "high" ? "高可信" : item.trust_level}）`,
                    }))}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} md={8}>
                <Form.Item label="敏感级别" required>
                  <Select
                    value={sensitivity}
                    onChange={setSensitivity}
                    disabled={busy}
                    options={(Object.keys(SENSITIVITY_LABELS) as Sensitivity[]).map((key) => ({
                      value: key,
                      label: SENSITIVITY_LABELS[key],
                    }))}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} md={8}>
                <Form.Item
                  label="访问范围"
                  extra="含个人信息的原件必须选「受限」——入库前会先脱敏。"
                >
                  <Select
                    value={accessScope}
                    onChange={setAccessScope}
                    disabled={busy}
                    options={(Object.keys(ACCESS_SCOPE_LABELS) as AccessScope[]).map((key) => ({
                      value: key,
                      label: ACCESS_SCOPE_LABELS[key],
                    }))}
                  />
                </Form.Item>
              </Col>
            </Row>

            <Form.Item label="原件文件" required>
              <Upload
                maxCount={1}
                fileList={fileList}
                // 返回 false 阻止 antd 自己上传——**这里必须走我们的接口**（要带来源与级别）
                beforeUpload={(file) => {
                  setRawFile(file as unknown as File);
                  return false;
                }}
                onChange={({ fileList: next }) => setFileList(next.slice(-1))}
                onRemove={() => setRawFile(null)}
                disabled={busy}
              >
                <Button icon={<UploadOutlined />} disabled={busy}>
                  选择文件
                </Button>
              </Upload>
            </Form.Item>

            <Button
              type="primary"
              loading={busy}
              disabled={!sourceId || rawFile === null}
              onClick={() => void submit()}
              block={!screens.sm}
            >
              导入并解析
            </Button>
          </Form>

          <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
            导入的是<strong>原件</strong>；解析产物、条款身份与版本树由后台流水线生成， 需要 worker
            在运行。扫描件（没有文本层）会解析失败并如实报错。
          </Typography.Paragraph>

          {error && <Alert style={{ marginTop: 12 }} type="error" showIcon message={error} />}

          {job && (
            <Alert
              style={{ marginTop: 12 }}
              type={
                job.status === "succeeded" ? "success" : job.status === "failed" ? "error" : "info"
              }
              showIcon
              message={
                <Space>
                  解析任务
                  <Tag color={JOB_COLORS[job.status] ?? "default"}>
                    {JOB_LABELS[job.status] ?? job.status}
                  </Tag>
                  <Typography.Text type="secondary">
                    第 {job.attempt_count} / {job.max_attempts} 次尝试
                  </Typography.Text>
                </Space>
              }
              description={
                job.status === "succeeded" ? (
                  "解析完成，条文已入库。"
                ) : job.status === "failed" ? (
                  <Space direction="vertical" size={2}>
                    <Typography.Text>
                      {job.error_code
                        ? (JOB_ERROR_HINTS[job.error_code] ?? `错误码：${job.error_code}`)
                        : "解析失败。"}
                    </Typography.Text>
                    {job.error_message && (
                      <Typography.Text type="secondary">{job.error_message}</Typography.Text>
                    )}
                  </Space>
                ) : (
                  "解析在后台进行，几十秒到几分钟不等，可以离开本页。"
                )
              }
            />
          )}
        </Card>
      )}

      <Card
        title={`原件（${rows.length}）`}
        extra={
          <Button
            size="small"
            icon={<ReloadOutlined />}
            onClick={() => void reloadDocuments()}
            loading={documents.status === "loading"}
          >
            刷新
          </Button>
        }
      >
        <AsyncBoundary
          status={documents.status}
          error={documents.error}
          isEmpty={rows.length === 0}
          emptyText="还没有导入任何原件。"
          onRetry={() => void reloadDocuments()}
        >
          <Row gutter={[12, 12]}>
            {rows.map((doc) => (
              <Col key={doc.id} xs={24} lg={12}>
                <Card size="small" title={doc.original_filename}>
                  <Descriptions
                    size="small"
                    column={1}
                    items={[
                      {
                        key: "source",
                        label: "来源",
                        children: sourceNames.get(doc.source_id) ?? (
                          <CopyableId value={doc.source_id} />
                        ),
                      },
                      {
                        key: "meta",
                        label: "格式与大小",
                        children: `${doc.media_type} · ${formatBytes(doc.size_bytes)}`,
                      },
                      {
                        key: "sensitivity",
                        label: "敏感级别 / 范围",
                        children: (
                          <Space size={4}>
                            <Tag>{SENSITIVITY_LABELS[doc.sensitivity] ?? doc.sensitivity}</Tag>
                            <Tag color={doc.access_scope === "restricted" ? "warning" : "default"}>
                              {ACCESS_SCOPE_LABELS[doc.access_scope] ?? doc.access_scope}
                            </Tag>
                          </Space>
                        ),
                      },
                      {
                        key: "sha",
                        label: "SHA-256",
                        children: <CopyableId value={doc.sha256} />,
                      },
                      {
                        key: "created",
                        label: "导入时间",
                        children: dayjs(doc.created_at).format("YYYY-MM-DD HH:mm:ss"),
                      },
                    ]}
                  />
                </Card>
              </Col>
            ))}
          </Row>
        </AsyncBoundary>
      </Card>
    </Space>
  );
}
