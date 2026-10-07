import { Alert, Typography } from "antd";
import type { ReactNode } from "react";

/**
 * 合规文案与各类提示的统一呈现。
 *
 * **不各页各写一遍**：免责声明、知识范围说明、范围限制只在这里定义一次，
 * 避免某页漏写或措辞走样（设计 §20.2 要求每个正式输出都带声明）。
 */

export const DISCLAIMER = "本系统是法律信息辅助工具，输出不构成法律意见，也不替代执业律师的判断。";

export const SCOPE_NOTE =
  "仅用于机构内部的法律资料管理与研究，不提供法律服务；检索结果、Wiki 内容与问答结论均须经人工审核后方可使用。";

export function DisclaimerBar() {
  return (
    <Alert
      type="info"
      showIcon
      message={DISCLAIMER}
      description={<Typography.Text type="secondary">{SCOPE_NOTE}</Typography.Text>}
    />
  );
}

export function NoticeBar({
  type = "warning",
  title,
  children,
}: {
  type?: "warning" | "error" | "info" | "success";
  title: ReactNode;
  children?: ReactNode;
}) {
  return <Alert type={type} showIcon message={title} description={children} />;
}

/** 长文本展示：保留换行、不撑破容器（中文长句与长 id 都要能断）。 */
export function LongText({ children }: { children: ReactNode }) {
  return (
    <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", lineHeight: 1.75 }}>
      {children}
    </div>
  );
}
