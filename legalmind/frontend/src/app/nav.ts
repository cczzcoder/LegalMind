/**
 * 导航表——**菜单、路由、页面标题的唯一来源**。
 *
 * 加一个页面只改这里 + `pages/` 里加一个组件，不需要动三处。
 *
 * `permission` 只用于**决定要不要渲染入口**；**强制校验永远在后端**（CODE_REVIEW m6 的结论）。
 * 权限常量与后端 `app/modules/authorization/service.py` 对齐。
 */

export type NavItem = {
  /** 路由模式，`:name` 为路径参数 */
  path: string;
  label: string;
  /** Ant Design 图标组件名（见 `AppShell` 的映射表） */
  icon: string;
  /** 显示层权限；留空表示所有登录用户可见 */
  permission?: string;
  /** 不进主菜单（详情页） */
  detail?: boolean;
  /** 一句话说明这个页面能做什么（用于页头副标题） */
  hint?: string;
};

export const PERMISSIONS = {
  documentRead: "document.read",
  documentWrite: "document.write",
  wikiRead: "wiki.read",
  wikiWrite: "wiki.write",
  reviewDecide: "review.decide",
} as const;

export const NAV: NavItem[] = [
  {
    path: "/ask",
    label: "问答",
    icon: "question",
    permission: PERMISSIONS.documentRead,
    hint: "基于检索到的条款给出带引用的结论；结论未经人工审核前不构成法律意见。",
  },
  {
    path: "/answers/:id",
    label: "问答运行",
    icon: "question",
    permission: PERMISSIONS.documentRead,
    detail: true,
  },
  {
    path: "/review",
    label: "待审队列",
    icon: "review",
    permission: PERMISSIONS.reviewDecide,
    hint: "被门禁拦下的运行在这里等人判读；复核不改变运行状态。",
  },
  {
    path: "/versions",
    label: "版本复核",
    icon: "audit",
    permission: PERMISSIONS.reviewDecide,
    hint: "低置信度落库的法律版本在这里等人确认；复核只改审核状态，不改效力状态。",
  },
  {
    path: "/search",
    label: "条款检索",
    icon: "search",
    permission: PERMISSIONS.documentRead,
    hint: "按名称、文号、条号精确查，或按自然语言检索；默认屏蔽尚未生效的版本。",
  },
  {
    path: "/wiki",
    label: "知识库",
    icon: "book",
    permission: PERMISSIONS.wikiRead,
    hint: "机构内部的法律说明；发布前须经独立审核。",
  },
  {
    path: "/wiki/:pageId",
    label: "Wiki 页面",
    icon: "book",
    permission: PERMISSIONS.wikiRead,
    detail: true,
  },
  {
    path: "/documents",
    label: "文献管理",
    icon: "folder",
    // 看得见列表只要 `document.read`；**上传按钮**另按 `document.write` 判（见页面内注释）
    permission: PERMISSIONS.documentRead,
    hint: "登记原件并查看解析进度。导入的是**原件**，解析产物与版本树由后台流水线生成。",
  },
];

export const LOGIN_PATH = "/login";

/** 路由匹配顺序：登录页优先，其余按导航表。 */
export const ROUTE_PATTERNS: string[] = [LOGIN_PATH, ...NAV.map((item) => item.path)];

/** 主菜单项（不含详情页）。 */
export const MENU_ITEMS = NAV.filter((item) => !item.detail);

export function navItemFor(pattern: string): NavItem | undefined {
  return NAV.find((item) => item.path === pattern);
}
