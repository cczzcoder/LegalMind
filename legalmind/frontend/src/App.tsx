import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Input,
  List,
  Space,
  Tag,
  Typography,
} from "antd";

type AccessScope = "organization" | "restricted";

type Page = {
  id: string;
  title: string;
  head_revision: number;
  access_scope: AccessScope;
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

type CurrentUser = {
  id: string;
  username: string;
  roles: string[];
  csrf_token: string;
  // ok：可使用业务接口；enroll：需先绑定 TOTP；verify：需输入验证码
  mfa_status: "ok" | "enroll" | "verify";
};

type Enrollment = {
  secret: string;
  otpauth_uri: string;
};

export default function App() {
  // 会话令牌在 HttpOnly Cookie 中，前端只持有 CSRF 令牌（仅内存）
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pages, setPages] = useState<Page[]>([]);
  const [selected, setSelected] = useState<Page | null>(null);
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [restricted, setRestricted] = useState(false);
  const [mfaCode, setMfaCode] = useState("");
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  // 恢复码只在绑定成功时显示一次，不持久化
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  async function api<T>(
    path: string,
    init: RequestInit = {},
  ): Promise<T> {
    const headers = new Headers(init.headers);
    const method = init.method ?? "GET";

    if (method !== "GET" && user) {
      headers.set("X-CSRF-Token", user.csrf_token);
    }

    if (init.body !== undefined) {
      headers.set("Content-Type", "application/json");
    }

    const response = await fetch(`/api/v1${path}`, {
      ...init,
      headers,
      credentials: "same-origin",
    });

    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      // 服务端统一错误体为 { code, message, trace_id }；detail 是旧格式的兜底
      const message = payload?.message ?? payload?.detail;

      if (response.status === 401 && path !== "/auth/login") {
        clearWorkspace();
      }

      throw new Error(
        `${response.status}: ${
          message ? String(message) : response.statusText
        }`,
      );
    }

    if (response.status === 204) {
      return undefined as T;
    }

    return response.json() as Promise<T>;
  }

  function clearWorkspace() {
    setUser(null);
    setPages([]);
    setSelected(null);
    setRevisions([]);
    setTitle("");
    setBody("");
    setRestricted(false);
    setMfaCode("");
    setEnrollment(null);
    setRecoveryCodes([]);
  }

  // 刷新页面后用已有会话恢复登录状态；未登录时静默忽略 401
  useEffect(() => {
    fetch("/api/v1/auth/me", { credentials: "same-origin" })
      .then((response) => (response.ok ? response.json() : null))
      .then((result: CurrentUser | null) => {
        if (result) {
          setUser(result);
        }
      })
      .catch(() => undefined);
  }, []);

  async function login() {
    const result = await api<CurrentUser>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    setPassword("");
    setUser(result);
  }

  async function logout() {
    try {
      await api<void>("/auth/logout", { method: "POST" });
    } finally {
      clearWorkspace();
    }
  }

  async function startEnrollment() {
    setEnrollment(await api<Enrollment>("/auth/mfa/enroll", { method: "POST" }));
  }

  async function confirmEnrollment() {
    const result = await api<{ recovery_codes: string[] }>("/auth/mfa/confirm", {
      method: "POST",
      body: JSON.stringify({ code: mfaCode }),
    });
    setMfaCode("");
    setEnrollment(null);
    setRecoveryCodes(result.recovery_codes);
    setUser((previous) => previous && { ...previous, mfa_status: "ok" });
  }

  async function verifyMfa() {
    await api<void>("/auth/mfa/verify", {
      method: "POST",
      body: JSON.stringify({ code: mfaCode }),
    });
    setMfaCode("");
    setUser((previous) => previous && { ...previous, mfa_status: "ok" });
  }

  const mfaReady = user?.mfa_status === "ok";

  // 仅作界面提示，不得作为权限依据；强制校验在后端 ROLE_PERMISSIONS
  const canWrite = mfaReady && Boolean(
    user?.roles.some((role) => ["editor", "knowledge_admin"].includes(role)),
  );

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
    setRestricted(false);
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
      const access_scope: AccessScope = restricted ? "restricted" : "organization";
      const revision = await api<Revision>("/wiki/pages", {
        method: "POST",
        body: JSON.stringify({ title, body, access_scope }),
      });

      setSelected({
        id: revision.page_id,
        title,
        head_revision: revision.number,
        access_scope,
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
          "当前有登录、管理员 TOTP 第二因素、角色权限、受限页面和 Wiki 草稿与修订能力。页面授权名单通过 API 管理，界面尚未提供；未实现审核发布、法律检索或 AI 问答。"
        }
      />

      <Alert
        className="section"
        type="info"
        showIcon
        message="本系统是法律信息辅助工具，输出不构成法律意见"
        description="仅用于机构内部的法律资料管理与研究，不提供法律服务，也不替代执业律师的判断。检索结果、Wiki 内容与问答结论均须经人工审核后方可使用。"
      />

      {user ? (
        <Card title="当前用户" className="section">
          <Space wrap>
            <Typography.Text>
              {user.username}（{user.roles.join("、")}）
            </Typography.Text>
            <Button
              disabled={busy || !mfaReady}
              onClick={() => void execute(loadPages)}
            >
              加载页面
            </Button>
            <Button disabled={busy} onClick={() => void execute(logout)}>
              退出登录
            </Button>
          </Space>
          {mfaReady && !canWrite && (
            <Typography.Paragraph type="secondary">
              当前角色没有写入权限，不能保存修订。
            </Typography.Paragraph>
          )}
        </Card>
      ) : null}

      {user && !mfaReady && (
        <Card
          title={user.mfa_status === "enroll" ? "绑定第二因素" : "第二因素验证"}
          className="section"
        >
          {user.mfa_status === "enroll" && !enrollment && (
            <Space direction="vertical">
              <Typography.Paragraph>
                管理员账号须绑定 TOTP 认证器（如 Microsoft Authenticator、Google
                Authenticator）后才能使用业务功能。
              </Typography.Paragraph>
              <Button
                type="primary"
                loading={busy}
                onClick={() => void execute(startEnrollment)}
              >
                开始绑定
              </Button>
            </Space>
          )}
          {(user.mfa_status === "verify" || enrollment) && (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void execute(enrollment ? confirmEnrollment : verifyMfa);
              }}
            >
              <Space direction="vertical" style={{ width: "100%" }}>
                {enrollment && (
                  <>
                    <Typography.Paragraph>
                      在认证器中手动添加以下密钥，或复制链接导入，然后输入显示的 6 位验证码。
                    </Typography.Paragraph>
                    <Typography.Text code copyable>
                      {enrollment.secret}
                    </Typography.Text>
                    <Typography.Text type="secondary" copyable={{ text: enrollment.otpauth_uri }}>
                      复制 otpauth 链接
                    </Typography.Text>
                  </>
                )}
                {!enrollment && (
                  <Typography.Paragraph>
                    输入认证器中的 6 位验证码；丢失认证器时可输入一个恢复码。
                  </Typography.Paragraph>
                )}
                <Space wrap>
                  <Input
                    aria-label="验证码"
                    autoComplete="one-time-code"
                    placeholder={enrollment ? "6 位验证码" : "验证码或恢复码"}
                    value={mfaCode}
                    disabled={busy}
                    maxLength={20}
                    onChange={(event) => setMfaCode(event.target.value.trim())}
                    style={{ width: 200 }}
                  />
                  <Button
                    type="primary"
                    htmlType="submit"
                    loading={busy}
                    disabled={mfaCode.length < 6}
                  >
                    {enrollment ? "确认绑定" : "验证"}
                  </Button>
                </Space>
              </Space>
            </form>
          )}
        </Card>
      )}

      {recoveryCodes.length > 0 && (
        <Alert
          className="section"
          type="info"
          showIcon
          closable
          onClose={() => setRecoveryCodes([])}
          message="恢复码只显示这一次，请离线妥善保存。每个恢复码只能使用一次。"
          description={<pre className="revision-body">{recoveryCodes.join("\n")}</pre>}
        />
      )}

      {!user && (
        <Card title="登录" className="section">
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void execute(login);
            }}
          >
            <Space wrap>
              <Input
                aria-label="用户名"
                autoComplete="username"
                placeholder="用户名"
                value={username}
                disabled={busy}
                onChange={(event) => setUsername(event.target.value)}
                style={{ width: 200 }}
              />
              <Input.Password
                aria-label="密码"
                autoComplete="current-password"
                placeholder="密码"
                value={password}
                disabled={busy}
                onChange={(event) => setPassword(event.target.value)}
                style={{ width: 240 }}
              />
              <Button
                type="primary"
                htmlType="submit"
                loading={busy}
                disabled={!username || !password}
              >
                登录
              </Button>
            </Space>
          </form>
        </Card>
      )}

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
                {page.access_scope === "restricted" && <Tag>受限</Tag>}
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

            {selected ? (
              selected.access_scope === "restricted" && (
                <Typography.Text type="secondary">
                  受限页面：仅获授权的用户可见。
                </Typography.Text>
              )
            ) : (
              <Checkbox
                checked={restricted}
                disabled={busy}
                onChange={(event) => setRestricted(event.target.checked)}
              >
                受限页面（仅自己和获授权的用户可见）
              </Checkbox>
            )}

            <Button
              type="primary"
              loading={busy}
              disabled={!canWrite || !title.trim() || !body.trim()}
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
