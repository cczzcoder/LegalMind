# LegalMind

法律知识库与辅助研究系统。

## 当前版本

v0.1.0：本地开发基础版，不可直接用于生产。

当前实现覆盖设计实施阶段 P0–P2，P3 已开始：需求阶段 A 已完成；阶段 B 的 P2（认证授权、文件存储、备份恢复）已完成；P3 的数据模型已完整建立（原文定位 0007、法律版本树 0008、条款关系与适用性 0009），解析器与检索尚未实现；P4、P5 尚未开始；阶段 C 未开始。进度口径见《系统设计文档》17.2。

## 已实现

- FastAPI + PostgreSQL + SQLAlchemy
- Alembic 迁移（0001 Wiki/审计/Outbox，0002 身份与会话，0003 MFA 与页面授权，0004 来源/原件/任务，0005 审计索引，0006 业务表外键，0007 解析版本/分块/原文定位，0008 法律版本/条款，0009 条款关系/适用性）
- React 登录页、MFA 绑定/验证与 Wiki 草稿界面
- Wiki 修订历史与并发冲突检测
- 用户名密码登录（Argon2id）、PostgreSQL 服务端会话、CSRF 防护
- 登录失败限制、退出及禁用用户立即吊销会话
- 角色权限（RBAC）与组织范围过滤；系统管理员不自动拥有业务内容权限
- 管理员 TOTP 第二因素：system_admin、knowledge_admin 必须绑定，每个新会话须验证；
  密钥加密存储，验证码不可重放，10 个一次性恢复码，失败限流
- 页面级授权：受限页面只对 AccessGrant 中的用户可见，未授权与不存在同样返回 404；
  knowledge_admin 管理授权名单，但管理授权不等于可阅读内容
- 可信代理：仅信任 TRUSTED_PROXIES 中的代理传来的 X-Forwarded-For
- 同事务审计及 Outbox 写入；登录、退出、MFA、用户与角色、页面授权变更写审计
- 来源登记（FR-01）：来源类型、可信等级、授权说明、最后核查时间
- 原始文件导入与下载（FR-02）：流式上传限流、压缩炸弹/宏/PDF 主动内容检查、原子写盘加哈希校验；文档级授权与页面级授权共用 AccessGrant
- 备份与恢复：`app.cli backup` / `restore`，含原件清单与 SHA-256 校验、恢复前冲突检查与 dry-run
- 业务表统一外键（设计 5.3）：organization_id 与"人"引用列（created_by / author_id / granted_by / actor_id），ondelete 一律 RESTRICT
- 前端静态检查：eslint + prettier（`npm run lint` / `npm run format:check`）
- CI（GitHub Actions）：push 与 PR 自动跑后端 lint / 迁移漂移检查 / 测试，以及前端 lint / 构建

## 未实现

- 来源登记、文件导入下载与授权名单的前端管理界面（目前均通过 API）
- Wiki 审核及发布
- 法律版本业务
- 文档解析、检索、AI 问答
- Outbox 消费、审计防篡改

## 启动

确认 .env 中已经设置随机数据库密码，然后执行：

    docker compose up --build -d

系统不开放注册。首次启动后创建管理员和业务用户（密码交互输入，至少 12 位）：

    docker compose exec api python -m app.cli create-user --org 示例机构 --username admin --role system_admin
    docker compose exec api python -m app.cli create-user --org 示例机构 --username editor1 --role editor

之后也可由管理员通过 `/api/v1/users` 接口管理同组织用户。

管理员首次登录时须绑定 TOTP，前提是 .env 中已设置 MFA_ENCRYPTION_KEY（生成方法见 .env.example）。
认证器和恢复码都丢失时，由运维人员重置（会同时吊销该用户全部会话）：

    docker compose exec api python -m app.cli reset-mfa --username admin

页面授权接口（需 knowledge_admin）：

    PUT    /api/v1/wiki/pages/{page_id}/access            {"access_scope": "restricted"}
    GET    /api/v1/wiki/pages/{page_id}/grants
    PUT    /api/v1/wiki/pages/{page_id}/grants/{user_id}
    DELETE /api/v1/wiki/pages/{page_id}/grants/{user_id}

前端：

    http://127.0.0.1:5173

API 文档：

    http://127.0.0.1:8000/docs

## 测试

    docker compose run --rm --no-deps api pytest

容器内未设置 TEST_DATABASE_URL，只运行冒烟测试，集成测试会跳过。
集成测试的本机运行方式见仓库根目录 CLAUDE.md。
测试通过不代表完整业务和安全测试已经通过。
CI（`.github/workflows/ci.yml`）在 push 与 PR 上跑同一套门禁。

## 备份与恢复

备份与恢复依赖宿主机的 `pg_dump` / `pg_restore`，且版本需与数据库服务器一致：

    python -m app.cli backup --dest /path/to/backups
    python -m app.cli restore --src /path/to/backups/<label> --dry-run   # 先校验，不写入
    python -m app.cli restore --src /path/to/backups/<label>

数据库跑在 Docker、宿主机没有这两个命令时，`backup` 会直接报错并给出提示。此时若只需备份数据库：

    docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom > db.dump

注意这只含数据库，**不含原始文件**（原始文件在 `artifacts_data` 卷内，需一并备份）。

## 停止

    docker compose down

不要随意使用 down -v；它会移除项目数据库卷。

## 开发限制

- API 与前端仅绑定本机。
- 会话 Cookie 默认不带 Secure（本机 HTTP）；经 HTTPS 访问时设置 SESSION_COOKIE_SECURE=true。
- APP_ENV=production 要求设置 MFA_ENCRYPTION_KEY 且 SESSION_COOKIE_SECURE=true，否则拒绝启动；
  这只是最低配置检查，不代表满足下面列出的其余生产条件。
- 部署在反向代理后时，把代理地址写入 TRUSTED_PROXIES，否则登录限流会把所有用户算作代理 IP。
- 更换 MFA_ENCRYPTION_KEY 会使已绑定的 TOTP 无法解密，需逐个 reset-mfa。
- Wiki 只能保存草稿。
- Outbox 暂不消费。
- 审计表暂未部署数据库级防修改权限。
- 数据库迁移与应用暂共用开发账号。
- 当前 Compose 不构成生产安全基线。
- 前端使用开发服务器，正式部署必须替换。
- 接入真实资料前须先确认来源授权说明与数据分级（需求 2.2）。
- 不应将包含敏感内容的 .env、数据库或原始文件提交仓库。

## 依赖

当前依赖使用范围约束，尚未生成经过验收的锁文件。
首次安装测试后，应锁定依赖、检查许可证和已知漏洞，
并在正式构建中使用锁文件和固定镜像摘要。
