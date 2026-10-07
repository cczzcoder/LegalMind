import { DeleteOutlined, PlusOutlined, ReloadOutlined, SaveOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Divider,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Space,
  Typography,
} from "antd";
import dayjs from "dayjs";
import { useEffect, useState } from "react";

import { ApiError, describeError } from "../api/client";
import type { WikiCitation, WikiPage as WikiPageType, WikiRevision } from "../api/types";
import * as wiki from "../api/wiki";
import { PERMISSIONS } from "../app/nav";
import { navigate } from "../app/router";
import { useSession } from "../app/session-context";
import { AsyncBoundary } from "../components/AsyncBoundary";
import { CopyableId } from "../components/CopyableId";
import { LongText } from "../components/NoticeBar";
import { RevisionStatusTag } from "../components/StatusTag";
import { useAsync } from "../hooks/useAsync";

/**
 * 知识库（《前端界面说明》§5.6）。
 *
 * 三条与后端语义对齐的界面规则：
 * 1. **保存带 `expected_revision`**：409 表示别人先改了，**不自动合并**，提示用户自行保留文字；
 * 2. **作者不能审自己的修订**：后端强制，前端据此**不渲染**发布/驳回按钮并说明原因；
 * 3. **发布前至少一条引用**：后端强制，前端提前提示。
 */
export default function WikiPage({ pageId }: { pageId?: string }) {
  return pageId ? <WikiDetail pageId={pageId} /> : <WikiList />;
}

/* ------------------------------------------------------------------ 列表 */

function WikiList() {
  const { can } = useSession();
  const [creating, setCreating] = useState(false);
  const pages = useAsync<WikiPageType[]>(() => wiki.listPages(), []);
  const rows = pages.data ?? [];

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card
        title="Wiki 页面"
        extra={
          <Space>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={pages.status === "loading"}
              onClick={() => void pages.reload()}
            >
              刷新
            </Button>
            {/* 显示层判断；写入权限由后端强制 */}
            {can(PERMISSIONS.wikiWrite) && (
              <Button
                type="primary"
                size="small"
                icon={<PlusOutlined />}
                onClick={() => setCreating(true)}
              >
                新建
              </Button>
            )}
          </Space>
        }
      >
        <AsyncBoundary
          status={pages.status}
          error={pages.error}
          isEmpty={rows.length === 0}
          emptyText="还没有 Wiki 页面。"
          onRetry={() => void pages.reload()}
        >
          <List
            dataSource={rows}
            renderItem={(page) => (
              <List.Item>
                <Space direction="vertical" size={2} style={{ width: "100%" }}>
                  <Space size={8} wrap>
                    <a href={`#/wiki/${page.id}`}>
                      <Typography.Text strong>{page.title}</Typography.Text>
                    </a>
                    {page.published_revision === null ? (
                      <Typography.Text type="secondary">（尚未发布）</Typography.Text>
                    ) : (
                      <Typography.Text type="secondary">
                        已发布至修订 {page.published_revision}
                      </Typography.Text>
                    )}
                    {page.access_scope === "restricted" && (
                      <Typography.Text type="secondary">受限</Typography.Text>
                    )}
                    {page.review_due_at && <Typography.Text type="danger">待复核</Typography.Text>}
                  </Space>
                  <CopyableId value={page.id} />
                </Space>
              </List.Item>
            )}
          />
        </AsyncBoundary>
      </Card>

      <CreatePageModal
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(revision) => {
          setCreating(false);
          void pages.reload();
          navigate(`/wiki/${revision.page_id}`);
        }}
      />
    </Space>
  );
}

function CreatePageModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (revision: WikiRevision) => void;
}) {
  const [form] = Form.useForm<{ title: string; body: string; restricted: boolean }>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  return (
    <Modal
      open={open}
      title="新建 Wiki 页面"
      okText="创建草稿"
      cancelText="取消"
      confirmLoading={busy}
      onCancel={onClose}
      onOk={() => form.submit()}
      width={640}
    >
      <Form
        form={form}
        layout="vertical"
        disabled={busy}
        initialValues={{ restricted: false }}
        onFinish={(values) =>
          void (async () => {
            setBusy(true);
            setError("");
            try {
              onCreated(
                await wiki.createPage({
                  title: values.title.trim(),
                  body: values.body,
                  access_scope: values.restricted ? "restricted" : "organization",
                }),
              );
            } catch (reason) {
              setError(describeError(reason));
            } finally {
              setBusy(false);
            }
          })()
        }
      >
        <Form.Item name="title" label="标题" rules={[{ required: true, message: "请输入标题" }]}>
          <Input maxLength={200} placeholder="页面标题" />
        </Form.Item>
        <Form.Item name="body" label="正文" rules={[{ required: true, message: "请输入正文" }]}>
          <Input.TextArea rows={10} maxLength={200000} showCount placeholder="纯文本正文" />
        </Form.Item>
        <Form.Item name="restricted" valuePropName="checked">
          <Checkbox>受限页面（仅自己和获授权的用户可见）</Checkbox>
        </Form.Item>
        {error && <Alert type="error" showIcon message={error} />}
      </Form>
    </Modal>
  );
}

/* ------------------------------------------------------------------ 详情 */

function WikiDetail({ pageId }: { pageId: string }) {
  const { user, can } = useSession();
  const page = useAsync<WikiPageType>(() => wiki.getPage(pageId), [pageId]);
  const revisions = useAsync<WikiRevision[]>(() => wiki.listRevisions(pageId), [pageId]);

  /**
   * 编辑中的正文。`null` = **跟随当前修订**（还没有本地改动）。
   *
   * 用「草稿优先」而不是 `useEffect` 同步：后者是「渲染后再 setState」，会多渲染一次，
   * 而且保存冲突时用户已输入的文字会被下一次同步冲掉。这里 `draft` 一旦写入就不再被覆盖。
   */
  const [draft, setDraft] = useState<string | null>(null);
  const [citations, setCitations] = useState<WikiCitation[]>([]);
  const [newCitation, setNewCitation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);

  const head = revisions.status === "ready" ? (revisions.data[0] ?? null) : null;
  const headNumber = head?.number ?? null;
  const body = draft ?? head?.body ?? "";

  useEffect(() => {
    if (headNumber === null) return;
    let cancelled = false;
    wiki
      .listCitations(pageId, headNumber)
      .then((rows) => {
        if (!cancelled) setCitations(rows);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [pageId, headNumber]);

  async function run(task: () => Promise<void>, success?: string) {
    setBusy(true);
    setError("");
    setNotice("");
    setConflict(false);
    try {
      await task();
      if (success) setNotice(success);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) setConflict(true);
      else setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  const reloadAll = async () => {
    await page.reload();
    await revisions.reload();
  };

  const canWrite = can(PERMISSIONS.wikiWrite);
  const canDecide = can(PERMISSIONS.reviewDecide);
  // **作者不能审自己的修订**（后端强制）。前端据此不渲染按钮并说明原因。
  const isAuthor = head !== null && user !== null && head.author_id === user.id;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <AsyncBoundary status={page.status} error={page.error} onRetry={() => void page.reload()}>
        <Card
          title={page.data?.title ?? "Wiki 页面"}
          extra={
            <Space>
              {head && <RevisionStatusTag status={head.status} />}
              <Button size="small" icon={<ReloadOutlined />} onClick={() => void reloadAll()}>
                刷新
              </Button>
            </Space>
          }
        >
          <Descriptions
            size="small"
            column={1}
            items={[
              {
                key: "published",
                label: "已发布修订",
                children: page.data?.published_revision ?? "尚未发布（读者看不到内容）",
              },
              { key: "head", label: "最新修订", children: headNumber ?? "—" },
              {
                key: "scope",
                label: "可见范围",
                children: page.data?.access_scope === "restricted" ? "受限" : "本组织",
              },
              {
                key: "id",
                label: "页面标识",
                children: <CopyableId value={pageId} />,
              },
            ]}
          />

          {page.data?.review_due_at && (
            <Alert
              style={{ marginTop: 12 }}
              type="warning"
              showIcon
              message="该页面的依据已变化，需要复核"
              description={
                <>
                  <div>{page.data.review_due_reason ?? "引用的依据被取代，或条款正文变了。"}</div>
                  <Typography.Text type="secondary">
                    标记于 {dayjs(page.data.review_due_at).format("YYYY-MM-DD HH:mm")}。
                    历史说明按设计保留，由人决定怎么改。
                  </Typography.Text>
                </>
              }
            />
          )}
        </Card>
      </AsyncBoundary>

      <AsyncBoundary
        status={revisions.status}
        error={revisions.error}
        isEmpty={(revisions.data ?? []).length === 0}
        emptyText="该页面没有可读取的修订。"
        onRetry={() => void revisions.reload()}
      >
        <Card title={head ? `编辑 · 修订 ${head.number}` : "编辑"}>
          <Form layout="vertical" disabled={busy}>
            <Form.Item
              label="正文"
              extra="当前按纯文本保存与展示。保存会产生一条**新修订**，不覆盖历史。"
            >
              <Input.TextArea
                rows={14}
                maxLength={200000}
                showCount
                value={body}
                onChange={(event) => setDraft(event.target.value)}
              />
            </Form.Item>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              loading={busy}
              disabled={!canWrite || !body.trim() || headNumber === null}
              onClick={() =>
                void run(async () => {
                  if (headNumber === null) return;
                  await wiki.createRevision(pageId, {
                    expected_revision: headNumber,
                    body,
                  });
                  setDraft(null); // 保存成功：回到「跟随当前修订」
                  await reloadAll();
                }, "已保存为新修订。")
              }
            >
              保存新修订
            </Button>
            {!canWrite && (
              <Typography.Text type="secondary" style={{ marginLeft: 12 }}>
                当前角色没有 wiki.write 权限，不能保存修订。
              </Typography.Text>
            )}
          </Form>

          {conflict && (
            <Alert
              style={{ marginTop: 12 }}
              type="error"
              showIcon
              message="保存冲突：这条修订已经被别人改过了"
              description="当前版本不会自动合并。请**先把上面的文字复制出去**，再点「刷新」重新打开页面，然后把你的修改重新粘进去。"
            />
          )}
        </Card>
      </AsyncBoundary>

      <Card title="引用条款">
        <Typography.Paragraph type="secondary">
          引用的是**具体条款版本**（不是「最新条款」），发布前至少要有一条引用。
        </Typography.Paragraph>

        {citations.length === 0 ? (
          <Empty description="还没有引用" />
        ) : (
          <List
            dataSource={citations}
            renderItem={(citation) => (
              <List.Item
                actions={
                  canWrite
                    ? [
                        <Button
                          key="remove"
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                          disabled={busy || headNumber === null}
                          onClick={() =>
                            void run(async () => {
                              if (headNumber === null) return;
                              const next = citations
                                .filter(
                                  (item) =>
                                    item.provision_version_id !== citation.provision_version_id,
                                )
                                .map((item) => item.provision_version_id);
                              await wiki.setCitations(pageId, headNumber, next);
                              await wiki.listCitations(pageId, headNumber).then(setCitations);
                            }, "已更新引用。")
                          }
                        />,
                      ]
                    : []
                }
              >
                <CopyableId value={citation.provision_version_id} label="条款版本" />
              </List.Item>
            )}
          />
        )}

        {canWrite && (
          <>
            <Divider />
            <Space.Compact style={{ width: "100%" }}>
              <Input
                value={newCitation}
                disabled={busy}
                placeholder="粘贴条款版本 id（在「条款检索」结果里可复制）"
                onChange={(event) => setNewCitation(event.target.value.trim())}
              />
              <Button
                type="primary"
                disabled={!newCitation || headNumber === null}
                onClick={() =>
                  void run(async () => {
                    if (headNumber === null) return;
                    const ids = [
                      ...citations.map((item) => item.provision_version_id),
                      newCitation,
                    ];
                    await wiki.setCitations(pageId, headNumber, ids);
                    setNewCitation("");
                    await wiki.listCitations(pageId, headNumber).then(setCitations);
                  }, "已更新引用。")
                }
              >
                添加引用
              </Button>
            </Space.Compact>
            <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
              ⚠️ 目前只能粘贴条款版本 id，**还没有「从检索结果直接引用」的选择器**。
            </Typography.Paragraph>
          </>
        )}
      </Card>

      {/* 审核动作：按状态与权限渲染；作者不能审自己的修订 */}
      <Card title="审核与发布">
        {!canDecide ? (
          <Typography.Text type="secondary">
            当前角色没有 review.decide 权限，不能审核发布。
          </Typography.Text>
        ) : isAuthor ? (
          <Alert
            type="info"
            showIcon
            message="作者不能审核自己的修订"
            description="这是设计 §10.3 的独立审核要求：撰写与审核分开，否则「独立审核」无从谈起。请让其他审核人处理。"
          />
        ) : (
          <Space wrap>
            {head?.status === "draft" && (
              <Button
                loading={busy}
                disabled={citations.length === 0}
                onClick={() =>
                  void run(async () => {
                    if (headNumber === null) return;
                    await wiki.submitRevision(pageId, headNumber);
                    await reloadAll();
                  }, "已提交审核。")
                }
              >
                提交审核
              </Button>
            )}
            {head?.status === "submitted" && (
              <>
                <Button
                  type="primary"
                  loading={busy}
                  onClick={() =>
                    void run(async () => {
                      if (headNumber === null) return;
                      await wiki.publishRevision(pageId, headNumber);
                      await reloadAll();
                    }, "已发布。")
                  }
                >
                  发布
                </Button>
                <Button
                  danger
                  loading={busy}
                  onClick={() =>
                    void run(async () => {
                      if (headNumber === null) return;
                      await wiki.rejectRevision(pageId, headNumber, "经审核未通过");
                      await reloadAll();
                    }, "已驳回。")
                  }
                >
                  驳回
                </Button>
              </>
            )}
            {head && !["draft", "submitted"].includes(head.status) && (
              <Typography.Text type="secondary">
                当前修订状态为「{head.status}」，没有可执行的审核动作。
              </Typography.Text>
            )}
          </Space>
        )}
        {head?.status === "draft" && citations.length === 0 && (
          <Typography.Paragraph type="danger" style={{ marginTop: 12, marginBottom: 0 }}>
            发布前至少要有一条引用，请先在上面的「引用条款」里添加。
          </Typography.Paragraph>
        )}
      </Card>

      <AsyncBoundary status={revisions.status} error={revisions.error}>
        <Card title="历史修订">
          <List
            dataSource={revisions.data ?? []}
            renderItem={(revision) => (
              <List.Item>
                <Space direction="vertical" size={4} style={{ width: "100%" }}>
                  <Space size={8} wrap>
                    <Typography.Text strong>修订 {revision.number}</Typography.Text>
                    <RevisionStatusTag status={revision.status} />
                    <Typography.Text type="secondary">
                      {dayjs(revision.created_at).format("YYYY-MM-DD HH:mm")}
                    </Typography.Text>
                  </Space>
                  {revision.review_note && (
                    <Typography.Text type="secondary">
                      审核意见：{revision.review_note}
                    </Typography.Text>
                  )}
                  <LongText>{revision.body}</LongText>
                </Space>
              </List.Item>
            )}
          />
        </Card>
      </AsyncBoundary>

      {notice && <Alert type="success" showIcon message={notice} />}
      {error && <Alert type="error" showIcon message={error} />}
    </Space>
  );
}
