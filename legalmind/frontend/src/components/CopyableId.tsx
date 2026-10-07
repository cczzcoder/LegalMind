import { CheckOutlined, CopyOutlined } from "@ant-design/icons";
import { Button, Tooltip, Typography } from "antd";
import { useState } from "react";

/**
 * 长 id 的显示：**截断显示 + 一键复制**。
 *
 * 运行 id / 版本 id 要能复制出去排查问题，但整串 UUID 摆在界面上既占地方又难读。
 * `navigator.clipboard` 在非 HTTPS 的 localhost 之外可能不可用，所以失败要**如实提示**。
 */
export function CopyableId({ value, label }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // 剪贴板不可用时把完整值选中，让用户自己复制
      window.prompt("剪贴板不可用，请手动复制：", value);
    }
  }

  return (
    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
      {label ? `${label} ` : ""}
      <Tooltip title={value}>
        <span style={{ fontFamily: "ui-monospace, monospace" }}>{value.slice(0, 8)}…</span>
      </Tooltip>
      <Button
        type="text"
        size="small"
        aria-label="复制完整标识"
        icon={copied ? <CheckOutlined /> : <CopyOutlined />}
        onClick={() => void copy()}
      />
    </Typography.Text>
  );
}
