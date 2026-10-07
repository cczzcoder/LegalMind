import { LockOutlined, UserOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Form, Input, Typography } from "antd";
import { useState } from "react";

import { describeError } from "../api/client";
import { DisclaimerBar } from "../components/NoticeBar";
import { useSession } from "../app/session-context";

/**
 * 登录页（《前端界面说明》§5.1）。
 *
 * 只负责**账号密码**这一步；TOTP 绑定/验证由 `MfaGate` 接管（`App` 按 `mfa_status` 分流）。
 * 窄屏单列、控件满宽；宽屏限宽 400 居中，避免输入框被拉成一条。
 */
export default function LoginPage() {
  const { signIn } = useSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function onSubmit(values: { username: string; password: string }) {
    setBusy(true);
    setError("");
    try {
      await signIn(values.username, values.password);
      // 成功后不在这里跳转：`App` 会按 mfa_status 决定去 MFA 还是业务页
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={{ maxWidth: 420, margin: "0 auto", padding: "48px 16px" }}>
      <Typography.Title level={3} style={{ marginBottom: 4 }}>
        LegalMind
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        法律知识库与辅助研究系统 · 机构内部使用
      </Typography.Paragraph>

      <Card style={{ marginTop: 16 }}>
        <Form layout="vertical" requiredMark={false} onFinish={onSubmit} disabled={busy}>
          <Form.Item
            name="username"
            label="用户名"
            rules={[{ required: true, message: "请输入用户名" }]}
          >
            <Input
              prefix={<UserOutlined />}
              autoComplete="username"
              placeholder="用户名"
              size="large"
            />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            rules={[{ required: true, message: "请输入密码" }]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              autoComplete="current-password"
              placeholder="密码"
              size="large"
            />
          </Form.Item>
          <Button type="primary" htmlType="submit" size="large" block loading={busy}>
            登录
          </Button>
        </Form>
      </Card>

      {error && <Alert style={{ marginTop: 16 }} type="error" showIcon message={error} />}

      <div style={{ marginTop: 16 }}>
        <DisclaimerBar />
      </div>
    </main>
  );
}
