import {
  DownloadOutlined,
  EditOutlined,
  InboxOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Form,
  Grid,
  Input,
  Modal,
  Popconfirm,
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

import {
  createSource,
  downloadDocument,
  getJob,
  grantAccess,
  importDocument,
  listDirectory,
  listDocuments,
  listGrants,
  listSources,
  revokeAccess,
  setAccessScope,
  updateSource,
} from "../api/documents";
import { describeError } from "../api/client";
import type {
  AccessScope,
  DirectoryEntry,
  DocumentRecord,
  GrantRecord,
  JobRecord,
  Sensitivity,
  SourceInput,
  SourceRecord,
  SourceType,
  TrustLevel,
} from "../api/types";
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

const SOURCE_TYPE_LABELS: Record<SourceType, string> = {
  official: "官方",
  republished: "转载",
  internal: "内部",
};

const TRUST_LEVEL_LABELS: Record<TrustLevel, string> = {
  high: "高",
  medium: "中",
  low: "低",
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
  /** 正在管理授权的原件；`null` = 没开弹窗。 */
  const [grantTarget, setGrantTarget] = useState<DocumentRecord | null>(null);
  /** 列表上的动作（下载 / 授权）出错时提示——与导入卡片的错误分开，免得串台。 */
  const [actionError, setActionError] = useState("");

  const rows = documents.data ?? [];
  const sourceRows = sources.data ?? [];
  const canWrite = can(PERMISSIONS.documentWrite);
  const canManageSources = can(PERMISSIONS.sourceManage);
  const canDownload = can(PERMISSIONS.documentDownload);
  const canGrant = can(PERMISSIONS.documentGrant);
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

  async function handleDownload(doc: DocumentRecord) {
    setActionError("");
    try {
      await downloadDocument(doc.id, doc.original_filename);
    } catch (reason) {
      setActionError(describeError(reason));
    }
  }

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

      <AsyncBoundary
        status={sources.status}
        error={sources.error}
        onRetry={() => void sources.reload()}
      >
        <SourcesCard
          sources={sourceRows}
          canManage={canManageSources}
          onChanged={() => {
            void sources.reload();
            // 来源改了名要反映到下面原件的「来源」一栏，所以两份一起刷
            void reloadDocuments();
          }}
        />
      </AsyncBoundary>

      {actionError && <Alert type="error" showIcon message={actionError} />}

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
                <Card
                  size="small"
                  title={doc.original_filename}
                  actions={[
                    ...(canDownload
                      ? [
                          <Button
                            key="download"
                            type="link"
                            icon={<DownloadOutlined />}
                            onClick={() => void handleDownload(doc)}
                          >
                            下载
                          </Button>,
                        ]
                      : []),
                    ...(canGrant
                      ? [
                          <Button
                            key="grant"
                            type="link"
                            icon={<SafetyOutlined />}
                            onClick={() => {
                              setActionError("");
                              setGrantTarget(doc);
                            }}
                          >
                            可见范围与授权
                          </Button>,
                        ]
                      : []),
                  ]}
                >
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

      {grantTarget && (
        <GrantsModal
          document={grantTarget}
          onClose={() => setGrantTarget(null)}
          onChanged={() => void reloadDocuments()}
        />
      )}
    </Space>
  );
}

/**
 * 来源登记与编辑（FR-01；设计 §20.3）。
 *
 * ⚠️ **授权说明（`license_note`）是必填的**，不是可选的备注——没有授权说明的来源不能用来入库。
 * 所以列表上**必须把它显示出来**，编辑时也不能悄悄留空。
 */
function SourcesCard({
  sources,
  canManage,
  onChanged,
}: {
  sources: SourceRecord[];
  canManage: boolean;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState<SourceRecord | "new" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(values: SourceInput) {
    setBusy(true);
    setError("");
    try {
      // 空字符串要落成 null，别把「没填」存成「填了个空」
      const payload: SourceInput = {
        ...values,
        url: values.url?.trim() || null,
        publisher: values.publisher?.trim() || null,
      };
      if (editing === "new") await createSource(payload);
      else if (editing) await updateSource(editing.id, payload);
      setEditing(null);
      onChanged();
    } catch (reason) {
      // **不关弹窗**：填了一半的内容不能因为一次报错就丢掉
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title={`来源（${sources.length}）`}
      extra={
        canManage && (
          <Button size="small" icon={<PlusOutlined />} onClick={() => setEditing("new")}>
            登记来源
          </Button>
        )
      }
    >
      {sources.length === 0 ? (
        <Typography.Text type="secondary">
          还没有登记任何来源。<strong>来源必须先登记授权说明，否则不能用来入库。</strong>
        </Typography.Text>
      ) : (
        <Row gutter={[12, 12]}>
          {sources.map((source) => (
            <Col key={source.id} xs={24} lg={12}>
              <Card
                size="small"
                title={source.name}
                extra={
                  canManage && (
                    <Button
                      size="small"
                      type="text"
                      icon={<EditOutlined />}
                      onClick={() => setEditing(source)}
                    >
                      编辑
                    </Button>
                  )
                }
              >
                <Descriptions
                  size="small"
                  column={1}
                  items={[
                    {
                      key: "type",
                      label: "类型 / 可信度",
                      children: (
                        <Space size={4}>
                          <Tag>{SOURCE_TYPE_LABELS[source.source_type] ?? source.source_type}</Tag>
                          <Tag color={source.trust_level === "high" ? "green" : "default"}>
                            可信度 {TRUST_LEVEL_LABELS[source.trust_level] ?? source.trust_level}
                          </Tag>
                        </Space>
                      ),
                    },
                    {
                      key: "publisher",
                      label: "发布方",
                      children: source.publisher ?? "—",
                    },
                    {
                      key: "license",
                      label: "授权说明",
                      children: source.license_note,
                    },
                  ]}
                />
              </Card>
            </Col>
          ))}
        </Row>
      )}

      {editing !== null && (
        <Modal
          open
          title={editing === "new" ? "登记来源" : "编辑来源"}
          okText="保存"
          cancelText="取消"
          confirmLoading={busy}
          onCancel={() => setEditing(null)}
          footer={null}
        >
          <Form<SourceInput>
            layout="vertical"
            initialValues={
              editing === "new"
                ? {
                    name: "",
                    source_type: "official",
                    trust_level: "high",
                    url: "",
                    publisher: "",
                    license_note: "",
                  }
                : {
                    name: editing.name,
                    source_type: editing.source_type,
                    trust_level: editing.trust_level,
                    url: editing.url ?? "",
                    publisher: editing.publisher ?? "",
                    license_note: editing.license_note,
                  }
            }
            onFinish={(values) => void submit(values)}
          >
            <Form.Item name="name" label="名称" rules={[{ required: true, message: "请输入名称" }]}>
              <Input placeholder="例如：国家法律法规数据库" />
            </Form.Item>
            <Row gutter={16}>
              <Col span={12}>
                <Form.Item name="source_type" label="类型" rules={[{ required: true }]}>
                  <Select
                    options={(Object.keys(SOURCE_TYPE_LABELS) as SourceType[]).map((value) => ({
                      value,
                      label: SOURCE_TYPE_LABELS[value],
                    }))}
                  />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="trust_level" label="可信度" rules={[{ required: true }]}>
                  <Select
                    options={(Object.keys(TRUST_LEVEL_LABELS) as TrustLevel[]).map((value) => ({
                      value,
                      label: TRUST_LEVEL_LABELS[value],
                    }))}
                  />
                </Form.Item>
              </Col>
            </Row>
            <Form.Item name="publisher" label="发布方">
              <Input placeholder="例如：全国人民代表大会常务委员会" />
            </Form.Item>
            <Form.Item name="url" label="地址">
              <Input placeholder="https://…" />
            </Form.Item>
            <Form.Item
              name="license_note"
              label="授权说明"
              rules={[{ required: true, message: "授权说明必填——没有它这个来源不能用来入库" }]}
              extra={
                <>
                  写清「依据什么可以使用这批资料」。
                  <strong>版权声明与 ICP 备案不算授权说明。</strong>
                </>
              }
            >
              <Input.TextArea rows={3} maxLength={2000} />
            </Form.Item>
            {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
            <Space>
              <Button type="primary" htmlType="submit" loading={busy}>
                保存
              </Button>
              <Button onClick={() => setEditing(null)}>取消</Button>
            </Space>
          </Form>
        </Modal>
      )}
    </Card>
  );
}

/**
 * 可见范围与授权名单（设计 §11.2、§20.3）。
 *
 * ⚠️ **`restricted` 只对名单里的人可见**——所以改范围和发授权放在同一个弹窗里：
 * 先把范围调成「受限」，再往里加人，这两步是一件事的两半。
 *
 * ⚠️ 名单来自 `GET /users/directory`（需 `document.grant`），**不是 `GET /users`**
 * ——后者要 `user.manage`，只有 `system_admin` 有，而授权权在 `knowledge_admin` 手里。
 */
function GrantsModal({
  document,
  onClose,
  onChanged,
}: {
  document: DocumentRecord;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [userId, setUserId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // 范围存本地：改完要立刻反映在弹窗里，而外面传进来的 `document` 是打开时的快照
  const [scope, setScope] = useState<AccessScope>(document.access_scope);

  const directory = useAsync<DirectoryEntry[]>(() => listDirectory(), []);
  const grants = useAsync<GrantRecord[]>(() => listGrants(document.id), [document.id]);

  const names = new Map((directory.data ?? []).map((item) => [item.id, item.username]));
  const granted = new Set((grants.data ?? []).map((item) => item.user_id));
  const candidates = (directory.data ?? []).filter((item) => !granted.has(item.id));

  async function run(task: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await task();
      await grants.reload();
      onChanged();
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      title={`可见范围与授权 · ${document.original_filename}`}
      footer={<Button onClick={onClose}>关闭</Button>}
      onCancel={onClose}
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Space direction="vertical" size={4} style={{ width: "100%" }}>
          <Typography.Text strong>可见范围</Typography.Text>
          <Select
            value={scope}
            style={{ width: "100%" }}
            disabled={busy}
            onChange={(value) =>
              void run(async () => {
                await setAccessScope(document.id, value as AccessScope);
                setScope(value as AccessScope);
              })
            }
            options={(Object.keys(ACCESS_SCOPE_LABELS) as AccessScope[]).map((value) => ({
              value,
              label: ACCESS_SCOPE_LABELS[value],
            }))}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            「本组织可见」时名单不起作用；「受限」时<strong>只有下面名单里的人</strong>
            能访问这个原件。
          </Typography.Text>
        </Space>

        <Space.Compact style={{ width: "100%" }}>
          <Select
            value={userId}
            onChange={setUserId}
            style={{ flex: 1 }}
            placeholder={candidates.length ? "选择要授权的人" : "没有可授权的人"}
            loading={directory.status === "loading"}
            options={candidates.map((item) => ({
              value: item.id,
              // 停用的人也能授权，但要说清楚——不然等于白发
              label: item.is_active ? item.username : `${item.username}（已停用）`,
            }))}
          />
          <Button
            type="primary"
            disabled={!userId || busy}
            onClick={() => void run(() => grantAccess(document.id, userId as string))}
          >
            授权
          </Button>
        </Space.Compact>

        <AsyncBoundary
          status={grants.status}
          error={grants.error}
          isEmpty={(grants.data ?? []).length === 0}
          emptyText="还没有任何人被单独授权。"
          onRetry={() => void grants.reload()}
        >
          <Space direction="vertical" size={4} style={{ width: "100%" }}>
            {(grants.data ?? []).map((grant) => (
              <Space key={grant.user_id} size={8} wrap>
                <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                  {names.get(grant.user_id) ?? "（名单里没有这个人）"}
                </Tag>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {dayjs(grant.created_at).format("YYYY-MM-DD HH:mm")}
                </Typography.Text>
                <Popconfirm
                  title="撤销这条授权？"
                  description="撤销后对方立刻看不到这个原件（鉴权每请求重读）。"
                  okText="撤销"
                  cancelText="取消"
                  onConfirm={() => void run(() => revokeAccess(document.id, grant.user_id))}
                >
                  <Button size="small" type="link" danger>
                    撤销
                  </Button>
                </Popconfirm>
              </Space>
            ))}
          </Space>
        </AsyncBoundary>

        {error && <Alert type="error" showIcon message={error} />}
      </Space>
    </Modal>
  );
}
