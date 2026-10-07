import { Alert, Button, Card, Skeleton, Space, Typography } from "antd";
import type { ReactNode } from "react";

/**
 * loading / error / empty 三态的统一呈现。
 *
 * 每个页面都手写这三态会写出三种不同的样子，所以收在这里。**空态与错误态要能区分**：
 * 「没有数据」不是「出错了」。
 */
export function AsyncBoundary({
  status,
  error,
  isEmpty = false,
  emptyText = "暂无数据",
  emptyExtra,
  onRetry,
  children,
}: {
  status: "loading" | "ready" | "error";
  error?: string | null;
  isEmpty?: boolean;
  emptyText?: ReactNode;
  emptyExtra?: ReactNode;
  onRetry?: () => void;
  children: ReactNode;
}) {
  if (status === "loading") {
    return (
      <Card>
        <Skeleton active paragraph={{ rows: 4 }} />
      </Card>
    );
  }

  if (status === "error") {
    return (
      <Alert
        type="error"
        showIcon
        message="加载失败"
        description={error}
        action={
          onRetry && (
            <Button size="small" onClick={onRetry}>
              重试
            </Button>
          )
        }
      />
    );
  }

  if (isEmpty) {
    return (
      <Card>
        <Space direction="vertical" style={{ width: "100%" }} align="center">
          <Typography.Text type="secondary">{emptyText}</Typography.Text>
          {emptyExtra}
        </Space>
      </Card>
    );
  }

  return <>{children}</>;
}
