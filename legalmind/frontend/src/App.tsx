import { Layout, Result, Skeleton, Typography } from "antd";
import type { ReactNode } from "react";
import { Suspense, lazy, useEffect } from "react";

import { LOGIN_PATH, MENU_ITEMS, NAV, ROUTE_PATTERNS, navItemFor } from "./app/nav";
import type { RouteParams } from "./app/router";
import { replaceRoute, useRoute } from "./app/router";
import { useSession } from "./app/session-context";
import { AppShell } from "./components/AppShell";
import { MfaGate } from "./components/MfaGate";

/**
 * 应用装配：会话门禁 → 布局 → 路由出口。
 *
 * **页面按路由懒加载**（`React.lazy`）：首屏只加载登录/问答，Wiki 与检索的代码等用到再下。
 * 这是《前端界面说明》§6 的性能措施之一。
 *
 * 页面组件用**默认导出**（`React.lazy` 的惯例），组件与 hooks 用命名导出。
 */

const LoginPage = lazy(() => import("./pages/LoginPage"));
const AskPage = lazy(() => import("./pages/AskPage"));
const AnswerRunPage = lazy(() => import("./pages/AnswerRunPage"));
const ReviewQueuePage = lazy(() => import("./pages/ReviewQueuePage"));
const SearchPage = lazy(() => import("./pages/SearchPage"));
const WikiPage = lazy(() => import("./pages/WikiPage"));
const NotFoundPage = lazy(() => import("./pages/NotFoundPage"));

/** 路由 → 页面。参数以 props 传入（详情页需要 id）。 */
const PAGES: Record<string, (params: RouteParams) => ReactNode> = {
  "/ask": () => <AskPage />,
  "/answers/:id": (params) => <AnswerRunPage runId={params.id} />,
  "/review": () => <ReviewQueuePage />,
  "/search": () => <SearchPage />,
  "/wiki": () => <WikiPage />,
  "/wiki/:pageId": (params) => <WikiPage pageId={params.pageId} />,
};

// 开发期自检：导航表里每一项都必须有渲染分支，否则新加页面会静默 404
if (import.meta.env.DEV) {
  const missing = NAV.map((item) => item.path).filter((path) => !(path in PAGES));
  if (missing.length > 0) {
    console.error("导航表里的路由没有渲染分支：", missing);
  }
}

function PageHeader({ title, hint }: { title: string; hint?: string }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <Typography.Title level={4} style={{ marginBottom: hint ? 4 : 0 }}>
        {title}
      </Typography.Title>
      {hint && (
        <Typography.Text type="secondary" style={{ display: "block" }}>
          {hint}
        </Typography.Text>
      )}
    </div>
  );
}

export default function App() {
  const { user, restoring, ready, can } = useSession();
  const route = useRoute(ROUTE_PATTERNS);

  // 首次恢复会话时不要闪登录页——宁可多显示 100ms 骨架
  useEffect(() => {
    if (restoring || !user || !ready) return;
    // 已登录还停在登录页 / 根路径：送到第一个有权限的页面
    const target = MENU_ITEMS.find((item) => !item.permission || can(item.permission));
    if ((route.pattern === LOGIN_PATH || route.pattern === "*") && target) {
      replaceRoute(target.path);
    }
  }, [restoring, user, ready, route.pattern, can]);

  if (restoring) {
    return (
      <Layout style={{ minHeight: "100vh", background: "#f5f7fa" }}>
        <div style={{ maxWidth: 420, margin: "0 auto", padding: "48px 16px", width: "100%" }}>
          <Skeleton active paragraph={{ rows: 6 }} />
        </div>
      </Layout>
    );
  }

  // 未登录：任何路由都先登录（hash 保留，登录后能回到原目标）
  if (!user) {
    return (
      <Suspense fallback={null}>
        <LoginPage />
      </Suspense>
    );
  }

  // 第二因素未就绪：**门禁**，不进业务页面
  if (!ready) return <MfaGate />;

  const item = navItemFor(route.pattern);
  const allowed = !item?.permission || can(item.permission);

  return (
    <AppShell pattern={route.pattern}>
      <Suspense fallback={<Skeleton active paragraph={{ rows: 6 }} />}>
        {!allowed ? (
          <Result
            status="403"
            title="没有访问权限"
            subTitle={`当前角色没有 ${item?.permission} 权限。如需使用请联系管理员调整角色。`}
          />
        ) : (
          <>
            {item && <PageHeader title={item.label} hint={item.hint} />}
            {(PAGES[route.pattern] ?? (() => <NotFoundPage />))(route.params)}
          </>
        )}
      </Suspense>
    </AppShell>
  );
}
