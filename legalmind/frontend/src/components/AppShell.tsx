import {
  BookOutlined,
  FileSearchOutlined,
  LogoutOutlined,
  MenuOutlined,
  MessageOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { Button, Drawer, Grid, Layout, Menu, Space, Tag, Typography } from "antd";
import type { ReactNode } from "react";
import { useState } from "react";

import { MENU_ITEMS, navItemFor } from "../app/nav";
import { navigate } from "../app/router";
import { Link } from "../components/Link";
import { useSession } from "../app/session-context";

/**
 * 响应式外壳（《前端界面说明》§4）。
 *
 * | 断点 | 布局 |
 * | --- | --- |
 * | ≥ lg（≥992） | 固定侧栏 208px + 内容区 |
 * | md（768–991） | 顶栏 + 抽屉导航（侧栏收起） |
 * | < md | 顶栏 + 抽屉导航；单列；表格由各页降级为卡片 |
 *
 * 抽屉打开时锁定滚动、关闭后焦点回到触发按钮——这两件事 antd 的 Drawer 已经做了。
 */

const ICONS: Record<string, ReactNode> = {
  question: <MessageOutlined />,
  review: <SafetyCertificateOutlined />,
  search: <FileSearchOutlined />,
  book: <BookOutlined />,
};

export function AppShell({ pattern, children }: { pattern: string; children: ReactNode }) {
  const { user, signOut, can } = useSession();
  const screens = Grid.useBreakpoint();
  const [drawerOpen, setDrawerOpen] = useState(false);

  // lg 以上用固定侧栏；以下用抽屉
  const useDrawer = !screens.lg;

  const current = navItemFor(pattern);
  const visible = MENU_ITEMS.filter((item) => !item.permission || can(item.permission));

  const menu = (
    <Menu
      mode="inline"
      selectedKeys={current ? [current.path] : []}
      style={{ borderInlineEnd: "none", background: "transparent" }}
      items={visible.map((item) => ({
        key: item.path,
        icon: ICONS[item.icon],
        // 点菜单时收抽屉：**不用 effect 监听路由变化**（那是「渲染后再同步」，会多渲染一次）
        label: (
          <Link to={item.path} onClick={() => setDrawerOpen(false)}>
            {item.label}
          </Link>
        ),
      }))}
    />
  );

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout.Header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          background: "#174b70",
          position: "sticky",
          top: 0,
          zIndex: 10,
        }}
      >
        {useDrawer && (
          <Button
            type="text"
            aria-label="打开导航"
            icon={<MenuOutlined style={{ color: "#fff" }} />}
            onClick={() => setDrawerOpen(true)}
          />
        )}
        <Typography.Text strong style={{ color: "#fff", fontSize: 16, whiteSpace: "nowrap" }}>
          <Link to="/ask" style={{ color: "#fff" }}>
            LegalMind
          </Link>
        </Typography.Text>
        <div style={{ flex: 1 }} />
        <Space size={8}>
          <Typography.Text style={{ color: "rgba(255,255,255,0.85)" }} ellipsis>
            {user?.username}
          </Typography.Text>
          {user?.roles.slice(0, 1).map((role) => (
            <Tag key={role} color="blue" style={{ marginInlineEnd: 0 }}>
              {role}
            </Tag>
          ))}
          <Button
            type="text"
            aria-label="退出登录"
            icon={<LogoutOutlined style={{ color: "#fff" }} />}
            onClick={() => void signOut().then(() => navigate("/login"))}
          />
        </Space>
      </Layout.Header>

      <Layout>
        {!useDrawer && (
          <Layout.Sider
            width={208}
            theme="light"
            style={{ borderInlineEnd: "1px solid #e6e9ef", background: "#fff" }}
            breakpoint="lg"
          >
            {menu}
          </Layout.Sider>
        )}

        <Drawer
          title="导航"
          placement="left"
          width={248}
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          styles={{ body: { padding: 0 } }}
        >
          {menu}
        </Drawer>

        <Layout.Content>
          <div
            style={{
              maxWidth: 1280,
              margin: "0 auto",
              // 窄屏留 12px，宽屏 24px（与既有 style.css 的口径一致）
              padding: screens.md ? 24 : 12,
            }}
          >
            {children}
          </div>
        </Layout.Content>
      </Layout>
    </Layout>
  );
}
