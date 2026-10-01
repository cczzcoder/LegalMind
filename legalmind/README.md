# LegalMind

法律知识库与辅助研究系统。

## 当前版本

v0.1.0：本地开发基础版，不可直接用于生产。

## 已实现

- FastAPI + PostgreSQL + SQLAlchemy
- Alembic 迁移（0001 Wiki/审计/Outbox，0002 身份与会话）
- React 登录页与 Wiki 草稿界面
- Wiki 修订历史与并发冲突检测
- 用户名密码登录（Argon2id）、PostgreSQL 服务端会话、CSRF 防护
- 登录失败限制、退出及禁用用户立即吊销会话
- 角色权限（RBAC）与组织范围过滤；系统管理员不自动拥有业务内容权限
- 同事务审计及 Outbox 写入；登录、退出、用户与角色变更写审计

## 未实现

- 管理员 MFA、页面级授权（AccessGrant）、反向代理下的可信客户端 IP
- Wiki 审核及发布
- 法律版本业务
- 文档解析、检索、AI 问答
- Outbox 消费、审计防篡改、备份恢复

## 启动

确认 .env 中已经设置随机数据库密码，然后执行：

    docker compose up --build -d

系统不开放注册。首次启动后创建管理员和业务用户（密码交互输入，至少 12 位）：

    docker compose exec api python -m app.cli create-user --org 示例机构 --username admin --role system_admin
    docker compose exec api python -m app.cli create-user --org 示例机构 --username editor1 --role editor

之后也可由管理员通过 `/api/v1/users` 接口管理同组织用户。

前端：

    http://127.0.0.1:5173

API 文档：

    http://127.0.0.1:8000/docs

## 测试

    docker compose run --rm --no-deps api pytest

容器内未设置 TEST_DATABASE_URL，只运行冒烟测试，集成测试会跳过。
集成测试的本机运行方式见仓库根目录 CLAUDE.md。
测试通过不代表完整业务和安全测试已经通过。

## 停止

    docker compose down

不要随意使用 down -v；它会移除项目数据库卷。

## 开发限制

- API 与前端仅绑定本机。
- 会话 Cookie 默认不带 Secure（本机 HTTP）；经 HTTPS 访问时设置 SESSION_COOKIE_SECURE=true。
- APP_ENV=production 会拒绝启动，直到管理员 MFA 和页面级授权完成。
- Wiki 只能保存草稿。
- Outbox 暂不消费。
- 审计表暂未部署数据库级防修改权限。
- 数据库迁移与应用暂共用开发账号。
- 当前 Compose 不构成生产安全基线。
- 前端使用开发服务器，正式部署必须替换。
- 接入真实资料之前，先实现权限、来源管理及备份。
- 不应将包含敏感内容的 .env、数据库或原始文件提交仓库。

## 依赖

当前依赖使用范围约束，尚未生成经过验收的锁文件。
首次安装测试后，应锁定依赖、检查许可证和已知漏洞，
并在正式构建中使用锁文件和固定镜像摘要。
