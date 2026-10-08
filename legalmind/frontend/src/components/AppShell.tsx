import {
  AuditOutlined,
  BookOutlined,
  FileSearchOutlined,
  FolderOpenOutlined,
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
  audit: <AuditOutlined />,
  search: <FileSearchOutlined />,
  book: <BookOutlined />,
  folder: <FolderOpenOutlined />,
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
          {/*
            ⚠️ 上下 `padding` 是**触控目标**（§4），不是留白：不加的话这个链接只有 85×20，
            手指点不准。字号与文字位置都没变——`inline-block` 只是让内边距真的撑开盒子。
          */}
          <Link to="/ask" style={{ color: "#fff", display: "inline-block", padding: "12px 0" }}>
            LegalMind
          </Link>
        </Typography.Text>
        <div style={{ flex: 1 }} />
        {/*
          ⚠️ `lineHeight: 1` 是**必须的**，不是风格偏好：antd 的 `Layout.Header` 自带
          `line-height: 56px`，而 `Typography.Text` 一旦加 `ellipsis`，antd 会给它
          `vertical-align: bottom` —— 于是这个 inline-block 被贴到 56px 行盒的**底部**，
          用户名比角色标签和登出按钮低 15px（实测 top 33 vs 18 / 10）。
          把行高归位后，行盒高度贴着内容，由外层 header 的 `align-items: center` 居中。
        */}
        {/*
          ⚠️ `header-user` 这个类**不是样式偏好**：`Space` 是 flex 容器，而 flex item 默认
          `min-width: auto`——不放开就压不下去，**长用户名会把整个顶栏撑出横向滚动条**
          （实测 375px 下溢出 92px）。放开之后用户名按 `ellipsis` 截断，完整值走 tooltip。
        */}
        <Space className="header-user" size={8} style={{ lineHeight: 1 }}>
          <Typography.Text
            style={{ color: "rgba(255,255,255,0.85)" }}
            ellipsis={{ tooltip: user?.username }}
          >
            {user?.username}
          </Typography.Text>
          {/*
            ⚠️ **窄屏不渲染角色标签**（`< md`）：一个 `legal_reviewer` 标签就有 97px 宽，
            加上长用户名与登出按钮，375px 下**顶栏必然被撑破**（实测横向溢出 92px）。
            角色不是导航必需信息，让位给用户名与登出。
          */}
          {screens.md &&
            user?.roles.slice(0, 1).map((role) => (
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
