import { Alert, Button, Card, Form, Input, Space, Typography } from "antd";
import { useState } from "react";

import { confirmMfaEnrollment, startMfaEnrollment, verifyMfa } from "../api/auth";
import { describeError } from "../api/client";
import type { MfaEnrollment } from "../api/types";
import { useSession } from "../app/session-context";
import { LongText } from "./NoticeBar";

/**
 * 第二因素门禁（管理员必须绑定 TOTP 才能使用业务功能）。
 *
 * 这是**门禁而不是路由**：未通过时 `App` 直接渲染它，不进入任何业务页面。
 *
 * ⚠️ **恢复码只在绑定成功时显示一次**，不持久化、不可再取——界面必须把这件事说清楚。
 */
export function MfaGate() {
  const { user, update, signOut } = useSession();
  const [enrollment, setEnrollment] = useState<MfaEnrollment | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const enrolling = user?.mfa_status === "enroll";

  async function run(task: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await task();
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  if (recoveryCodes.length > 0) {
    return (
      <main style={{ maxWidth: 520, margin: "0 auto", padding: "48px 16px" }}>
        <Alert
          type="warning"
          showIcon
          message="恢复码只显示这一次，请立即离线保存"
          description="每个恢复码只能使用一次；丢失认证器时可代替验证码使用。离开本页后无法再次查看。"
        />
        <Card style={{ marginTop: 16 }}>
          <LongText>{recoveryCodes.join("\n")}</LongText>
        </Card>
        <Button
          type="primary"
          block
          size="large"
          style={{ marginTop: 16 }}
          onClick={() => {
            setRecoveryCodes([]);
            update({ mfa_status: "ok" });
          }}
        >
          我已保存，进入系统
        </Button>
      </main>
    );
  }

  return (
    <main style={{ maxWidth: 520, margin: "0 auto", padding: "48px 16px" }}>
      <Typography.Title level={4}>
        {enrolling ? "绑定第二因素（TOTP）" : "第二因素验证"}
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        {enrolling
          ? "管理员账号须绑定 TOTP 认证器（如 Microsoft Authenticator、Google Authenticator）后才能使用业务功能。"
          : "请输入认证器中的 6 位验证码；丢失认证器时可输入一个恢复码。"}
      </Typography.Paragraph>

      <Card>
        {enrolling && !enrollment && (
          <Button
            type="primary"
            size="large"
            block
            loading={busy}
            onClick={() => void run(async () => setEnrollment(await startMfaEnrollment()))}
          >
            开始绑定
          </Button>
        )}

        {(enrollment || !enrolling) && (
          <Form
            layout="vertical"
            onFinish={() =>
              void run(async () => {
                if (enrollment) {
                  const result = await confirmMfaEnrollment(code);
                  setEnrollment(null);
                  setCode("");
                  setRecoveryCodes(result.recovery_codes);
                } else {
                  await verifyMfa(code);
                  setCode("");
                  update({ mfa_status: "ok" });
                }
              })
            }
            disabled={busy}
          >
            {enrollment && (
              <Space direction="vertical" style={{ width: "100%", marginBottom: 16 }}>
                <Typography.Text type="secondary">
                  在认证器中手动添加以下密钥，或复制 otpauth 链接导入：
                </Typography.Text>
                <Typography.Text code copyable style={{ overflowWrap: "anywhere" }}>
                  {enrollment.secret}
                </Typography.Text>
                <Typography.Text type="secondary" copyable={{ text: enrollment.otpauth_uri }}>
                  复制 otpauth 链接
                </Typography.Text>
              </Space>
            )}
            <Form.Item label={enrollment ? "6 位验证码" : "验证码或恢复码"} required>
              <Input
                value={code}
                size="large"
                autoFocus
                // 认证器填码：移动端弹数字键盘、支持系统自动填充
                inputMode={enrolling ? "numeric" : "text"}
                autoComplete="one-time-code"
                placeholder={enrollment ? "6 位验证码" : "验证码或恢复码"}
                maxLength={20}
                onChange={(event) => setCode(event.target.value.trim())}
              />
            </Form.Item>
            <Button
              type="primary"
              htmlType="submit"
              size="large"
              block
              loading={busy}
              disabled={code.length < 6}
            >
              {enrollment ? "确认绑定" : "验证"}
            </Button>
          </Form>
        )}
      </Card>

      {error && <Alert style={{ marginTop: 16 }} type="error" showIcon message={error} />}

      <Button type="link" style={{ marginTop: 8, paddingInline: 0 }} onClick={() => void signOut()}>
        退出登录
      </Button>
    </main>
  );
}
