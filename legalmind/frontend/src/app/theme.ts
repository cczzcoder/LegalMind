import type { ThemeConfig } from "antd";

/**
 * 主题 token。集中在这里，页面里不写魔法色值。
 *
 * 字号与间距按「桌面舒适、移动不挤」取：正文 14（antd 默认），页面标题用 Typography.Title 的层级。
 */
export const theme: ThemeConfig = {
  token: {
    colorPrimary: "#174b70",
    colorInfo: "#174b70",
    borderRadius: 6,
    fontFamily:
      "system-ui, -apple-system, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif",
    // 中文正文行高放宽一点，长段落更好读
    lineHeight: 1.65,
  },
  components: {
    Layout: { headerHeight: 56, headerPadding: "0 16px" },
    Card: { paddingLG: 16 },
    // 触控目标：移动端按钮不低于 40（antd 默认 32 偏小，见《前端界面说明》§4）
    Button: { controlHeight: 36 },
  },
};

/** 断点常量，与 antd 的 `Grid.useBreakpoint()` 同源，避免两套断点打架。 */
export const BREAKPOINTS = { md: 768, lg: 992 } as const;
