import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Input,
  List,
  Space,
  Typography,
} from "antd";

type Page = {
  id: string;
  title: string;
  head_revision: number;
  created_at: string;
};

type Revision = {
  id: string;
  page_id: string;
  number: number;
  body: string;
  status: string;
  author_id: string;
  created_at: string;
};

export default function App() {
  const [key, setKey] = useState("");
  const [pages, setPages] = useState<Page[]>([]);
  const [selected, setSelected] = useState<Page | null>(null);
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  async function api<T>(
    path: string,
    init: RequestInit = {},
  ): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("X-Dev-Key", key);

    if (init.body !== undefined) {
      headers.set("Content-Type", "application/json");
    }

    const response = await fetch(`/api/v1${path}`, {
      ...init,
      headers,
    });

    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new Error(
        `${response.status}: ${
          payload?.detail
            ? JSON.stringify(payload.detail)
            : response.statusText
        }`,
      );
    }

    return response.json() as Promise<T>;
  }

  async function execute(task: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");

    try {
      await task();
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : String(reason),
      );
    } finally {
      setBusy(false);
    }
  }

  async function loadPages() {
    const result = await api<Page[]>("/wiki/pages?limit=100");
    setPages(result);
  }

  async function openPage(page: Page) {
    const result = await api<Revision[]>(
      `/wiki/pages/${page.id}/revisions?limit=100`,
    );

    if (!result.length) {
      throw new Error("该页面没有可读取修订。");
    }

    setSelected({
      ...page,
      head_revision: result[0].number,
    });
    setTitle(page.title);
    setBody(result[0].body);
    setRevisions(result);
  }

  function newPage() {
    setSelected(null);
    setRevisions([]);
    setTitle("");
    setBody("");
    setError("");
    setNotice("");
  }

  async function saveDraft() {
    if (selected) {
      const revision = await api<Revision>(
        `/wiki/pages/${selected.id}/revisions`,
        {
          method: "POST",
          body: JSON.stringify({
            expected_revision: selected.head_revision,
            body,
          }),
        },
      );

      setSelected({
        ...selected,
        head_revision: revision.number,
      });
      setRevisions((previous) => [revision, ...previous]);
      setNotice(`已保存为修订 ${revision.number}。`);
    } else {
      const revision = await api<Revision>("/wiki/pages", {
        method: "POST",
        body: JSON.stringify({ title, body }),
      });

      setSelected({
        id: revision.page_id,
        title,
        head_revision: revision.number,
        created_at: revision.created_at,
      });
      setRevisions([revision]);
      setNotice("页面已创建，当前状态为草稿。");
    }

    await loadPages();
  }

  return (
    <main className="container">
      <Typography.Title>LegalMind</Typography.Title>
      <Typography.Paragraph type="secondary">
        法律知识库与辅助研究系统 · v0.1.0 开发基础版
      </Typography.Paragraph>

      <Alert
        type="warning"
        showIcon
        message="仅供本地开发"
        description={
          "当前只有 Wiki 草稿与修订能力。未实现正式登录、审核发布、法律检索或 AI 问答。"
        }
      />

      <Card title="开发身份" className="section">
        <Space wrap>
          <Input.Password
            value={key}
            onChange={(event) => {
              setKey(event.target.value);
              setPages([]);
              setSelected(null);
              setRevisions([]);
              setTitle("");
              setBody("");
              setError("");
              setNotice("");
            }}
            disabled={busy}
            placeholder="输入 .env 中的 DEV_API_KEY"
            style={{ width: 360 }}
          />
          <Button
            disabled={!key || busy}
            onClick={() => void execute(loadPages)}
          >
            加载页面
          </Button>
          <Button
            disabled={busy}
            onClick={() => {
              setKey("");
              setPages([]);
              newPage();
            }}
          >
            清除身份与页面
          </Button>
        </Space>
        <Typography.Paragraph type="secondary">
          Key 只保存在当前页面内存中，不写入浏览器存储。
        </Typography.Paragraph>
      </Card>

      {error && (
        <Alert
          className="section"
          type="error"
          showIcon
          message={error}
        />
      )}

      {notice && (
        <Alert
          className="section"
          type="success"
          showIcon
          message={notice}
        />
      )}

      <div className="columns">
        <Card
          title="Wiki 页面"
          extra={
            <Button disabled={busy} onClick={newPage}>
              新建
            </Button>
          }
        >
          <Typography.Paragraph type="secondary">
            开发界面最多显示 100 页。
          </Typography.Paragraph>

          <List
            dataSource={pages}
            locale={{ emptyText: "请加载页面或新建草稿" }}
            renderItem={(page) => (
              <List.Item>
                <Button
                  type="link"
                  disabled={busy}
                  onClick={() => void execute(() => openPage(page))}
                >
                  {page.title}
                </Button>
              </List.Item>
            )}
          />
        </Card>

        <Card
          title={
            selected
              ? `编辑草稿 · 修订 ${selected.head_revision}`
              : "新建 Wiki 草稿"
          }
        >
          <Space direction="vertical" style={{ width: "100%" }}>
            <Input
              placeholder="页面标题"
              value={title}
              disabled={busy || selected !== null}
              onChange={(event) => setTitle(event.target.value)}
              maxLength={200}
            />

            <Input.TextArea
              rows={14}
              placeholder="正文；当前按纯文本保存和展示"
              value={body}
              disabled={busy}
              onChange={(event) => setBody(event.target.value)}
              maxLength={200000}
              showCount
            />

            <Button
              type="primary"
              loading={busy}
              disabled={!key || !title.trim() || !body.trim()}
              onClick={() => void execute(saveDraft)}
            >
              保存新修订
            </Button>

            <Typography.Paragraph type="secondary">
              遇到 409 冲突时，先自行保留未保存文字，再重新打开页面。
              当前版本不会自动合并编辑。
            </Typography.Paragraph>
          </Space>
        </Card>
      </div>

      <Card title="历史修订" className="section">
        <List
          dataSource={revisions}
          locale={{ emptyText: "请选择页面" }}
          renderItem={(revision) => (
            <List.Item>
              <div style={{ width: "100%" }}>
                <Typography.Text strong>
                  修订 {revision.number} · {revision.status}
                </Typography.Text>
                <Typography.Paragraph type="secondary">
                  {new Date(revision.created_at).toLocaleString()}
                </Typography.Paragraph>
                <pre className="revision-body">{revision.body}</pre>
              </div>
            </List.Item>
          )}
        />
      </Card>
    </main>
  );
}
