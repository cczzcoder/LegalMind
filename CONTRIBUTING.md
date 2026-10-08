# 贡献与开发约定

LegalMind：准确性优先、可追溯的法律知识库与辅助研究系统。

## 项目文档

- [doc/法律知识库系统需求分析文档.md](doc/法律知识库系统需求分析文档.md)：FR-01 至 FR-13、分阶段交付 A/B/C、非功能需求、验收标准
- [doc/法律知识库系统设计文档.md](doc/法律知识库系统设计文档.md)：架构、数据模型、检索与问答流水线、16 GB 单机资源档位、扩展触发条件、实施阶段 P0–P7
- [doc/开发与部署指南.md](doc/开发与部署指南.md)：v0.1.0 脚手架生成脚本、启动与验证步骤
- [doc/技术决策与踩坑记录.md](doc/技术决策与踩坑记录.md)：选型理由与踩坑记录（例如为什么没引入 Elasticsearch、PDF 折行空格、多原件择优顺序）；**不是规范**，要求以需求文档为准、设计与选型以设计文档为准

动手前先查对应的 FR 编号和设计章节。文档与代码冲突时，指出冲突，不要自行选择一方。

## 常用命令

脚手架由 `create_legalmind.py` 生成到 `legalmind/` 目录，以下命令在该目录下执行：

```bash
docker compose up --build -d                  # 启动全部服务
docker compose run --rm --no-deps api pytest  # 运行后端测试
docker compose logs -f api                    # 查看 API 日志
docker compose exec api python -m app.cli run-worker   # 按需启动后台 worker（解析等；--once 只处理一个）
docker compose down                           # 停止（不要随意加 -v，会删除数据库卷）
```

Lint 与格式化使用 ruff（配置见 `backend/pyproject.toml`）：`ruff check .` 与 `ruff format .`；提交前保证 `ruff format --check .` 通过。

前端 lint 与格式化使用 eslint + prettier（配置见 `frontend/eslint.config.js`、`frontend/.prettierrc.json`）：`npm run lint`、`npm run format`；提交前保证 `npm run format:check` 通过。

改动界面布局后另跑一次**前端验收**：`npm run acceptance`（`frontend/scripts/acceptance.mjs`，用 `playwright-core` + 本机 Chrome，不下载 Chromium）。它需要活的后端与 vite，所以**不在 CI 里**——先起服务（vite 记得带 `VITE_API_TARGET=http://127.0.0.1:8000`，否则代理连不上 `api` 服务名、登录页点了没反应），再用免 MFA 的账号跑。断言横向溢出、导航形态、触控目标 ≥ 44×44（窄屏）与失败请求，规格见 `doc/前端界面说明.md` §4、§8.1。

### 本机环境的一个必备项：`MFA_ENCRYPTION_KEY`

`.env` 里**必须有** `MFA_ENCRYPTION_KEY`，否则 `knowledge_admin` / `system_admin` **一律登不进界面**
（`/auth/mfa/enroll` 返回 503，登录卡在第二步）。生成方式见 `.env.example`。

⚠️ 界面验收默认用的 `legal_reviewer` 等角色**免 MFA**，所以这个缺失平时看不出来——但
**「来源登记」「可见范围与授权」这类只有 `knowledge_admin` 看得到的界面就没法验**。
⚠️ **换密钥会让已绑定的 TOTP 失效**，要逐个 `python -m app.cli reset-mfa`。

### 提交前门禁（pre-commit）

配置在仓库根的 `.pre-commit-config.yaml`。**全部是 local hook**——本机直连 GitHub 不通，远程 hook 会卡在 clone 上。装一次即可：

```bash
cd legalmind/backend
.venv/Scripts/python.exe -m pip install pre-commit
.venv/Scripts/pre-commit install
```

它跑的就是 CI 的那几条命令（后端 `ruff check` / `ruff format --check`，前端 `npm run lint` / `format:check`），只对**改动到的文件**生效。手动全跑：`pre-commit run --all-files`。⚠️ 不要用 `git commit --no-verify` 绕过——CI 会红，只是红得晚一点。

### 依赖锁定

`backend/pyproject.toml` 里是**范围约束**（`fastapi>=0.115,<1` 这种），同一个提交在不同日子装出来可能不是同一套。所以依赖**从锁文件装**：

```bash
cd legalmind/backend
.venv/Scripts/python.exe -m pip install -r requirements.lock   # 装依赖
.venv/Scripts/python.exe -m pip install --no-deps -e .         # 再装项目本体
```

CI 走的也是这两条。**改了 `pyproject.toml` 的依赖之后**要重新生成锁文件：

```bash
cd legalmind/backend
.venv/Scripts/python.exe -m piptools compile pyproject.toml --extra dev --strip-extras --output-file requirements.lock
```

三点要知道的：

- 锁文件**只覆盖默认依赖 + `dev`**。可选的 `embeddings`（torch / sentence-transformers，体积以 GB 计）**故意不在锁里**——它只在跑向量通路时才装，理由见设计 §9.5。
- 重新生成会**升到当前允许的最新版本**（`pip-compile` 在输出文件已存在时会保留原有 pin；删掉重生成才是一次全面升级）。升完**先跑一遍测试再提交**。
- 换 ruff 版本要**同时改** `.pre-commit-config.yaml` 里 pin 的那个版本，否则会出现「本地 pre-commit 过、CI 红」。

### 容器镜像固定摘要

`compose.yaml` 里的 `postgres` / `redis` 都写了 `@sha256:…`：`pg17`、`7-alpine` 这类 tag 是可变的，上游一重建镜像，本地与 CI 就跑在不同的字节上。升级时在本机 pull 后取新摘要替换：

```bash
docker image inspect <image> --format '{{index .RepoDigests 0}}'
```

⚠️ 摘要是**按平台**取的，当前取的是 `linux/amd64`（第一阶段开发基线，见设计 §14）。换到 arm64 机器要重新取一次。

CI 见 `.github/workflows/ci.yml`：push 与 PR 自动跑后端 lint / 迁移漂移检查 / 测试，以及前端 lint / 构建。

### 本机开发（数据库在 Docker，API 在本机）

统一使用虚拟环境里的 Python（3.12）：`legalmind/backend/.venv/Scripts/python.exe`。不要用 `py -3.12`，它在本机因中文用户名路径无法启动。

```bash
cd legalmind
docker compose -f compose.yaml -f compose.localdb.yaml up -d postgres   # 只启动数据库，绑定 127.0.0.1:5432

cd backend
set -a && . ../.env && set +a
export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}"
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m app.cli create-user --org 示例机构 --username admin --role system_admin   # 首次；密码交互输入
.venv/Scripts/python.exe -m app.cli reset-mfa --username admin   # 管理员丢失认证器和恢复码时
.venv/Scripts/python.exe -m app.cli backup --dest /path/to/backups              # 备份（标签默认为 UTC 时间戳）
.venv/Scripts/python.exe -m app.cli restore --src /path/to/backups/<label> --dry-run   # 先 dry-run 校验
.venv/Scripts/python.exe -m app.cli restore --src /path/to/backups/<label>             # 正式恢复
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
.venv/Scripts/python.exe -m app.cli run-worker   # 后台 worker（另开一个终端；导入只登记任务，不启动则不解析）
.venv/Scripts/python.exe -m pytest
```

`backup` / `restore` 依赖宿主机的 `pg_dump` / `pg_restore`，且版本需与数据库服务器一致。本机开发时数据库跑在 Docker 容器里、宿主机通常没有这两个命令，`app.cli backup` 会直接报错并给出提示。此时若只需备份数据库：

```bash
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom > /path/to/db.dump
```

注意这只含数据库，**不含原始文件**（原始文件在 `artifacts_data` 卷内，需一并备份）。

系统不开放注册，用户只能由 CLI 或管理员通过 `/api/v1/users` 创建。system_admin、knowledge_admin 登录后须先完成 TOTP（需在 `.env` 设置 `MFA_ENCRYPTION_KEY`）。

集成测试（`tests/test_*_integration.py`、`tests/test_permission_matrix.py`）连接独立的 `legalmind_test` 库，未设置 `TEST_DATABASE_URL` 时自动跳过。测试库首次需手动创建，测试会自动迁移：

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE legalmind_test"   # 在 legalmind/ 下执行，仅首次
export TEST_DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/legalmind_test"
.venv/Scripts/python.exe -m pytest
```

## 行为准则

改编自 [andrej-karpathy-skills](https://github.com/multica-ai/andrej-karpathy-skills)（MIT）。偏向谨慎而非速度；琐碎任务自行判断。

### 1. 编码前思考

不要假设，不要隐藏困惑，呈现权衡。

- 明确说出假设；不确定就问
- 存在多种理解时列出来，不要默默选一个
- 有更简单的做法就说出来，该反对时反对
- 不清楚就停下，指出困惑点

### 2. 简洁优先

用最少的代码解决问题，不做推测性设计。

- 不加要求之外的功能
- 不为一次性代码建抽象
- 不加没人要求的"灵活性"或"可配置性"
- 不为不可能发生的场景写错误处理
- 200 行能写成 50 行，就重写

### 3. 精准修改

只碰必须碰的，只清理自己造成的混乱。

- 不"改进"相邻代码、注释或格式，不重构没坏的东西
- 匹配现有风格
- 发现无关死代码，提一句，不删除
- 删除因自己改动而失效的导入、变量和函数

检验标准：每一行改动都能追溯到用户的请求。

### 4. 目标驱动执行

定义成功标准，循环验证直到达成。

- "加校验" → 先写无效输入的测试，再让它通过
- "修 bug" → 先写复现测试，再让它通过
- "重构" → 确保重构前后测试都通过

多步任务先列计划：`步骤 → 验证方式`。

## 项目特定规则

- **不伪装完成**：未实现的功能不用假接口、桩数据或"TODO 但返回成功"包装成已完成。报告时区分已实现、已验证、未验证
- **权限不可跳过**：检索前、证据装配时、输出和下载前都要做授权检查；授权失败默认拒绝，不使用旧权限放行；AI 不决定访问权限（FR-10、设计 11.2）
- **正式记录不可原地覆盖**：Wiki 修订、法律版本、审计记录只追加新记录（FR-08、设计 5.3）
- **同事务写入**：关键业务变更与 AuditEvent、OutboxEvent 在同一事务提交；模型调用、文件处理、队列发送不放进长事务（设计 12.1）
- **引用绑定不可变版本**：不绑定"最新页面"；日期未知存为未知，不虚构页码、日期或字符位置（设计 5.3、6）
- **模型输出受约束**：模型只输出结构化主张和本次授权证据集中的证据 ID；引用标题和链接由服务端生成（设计 9.2）
- **数据库变更走 Alembic 迁移**，不手工改表
- **轻量部署优先**：开发基线为 16 GB 笔记本；不擅自引入 Elasticsearch、Qdrant、Celery/Redis、对象存储服务等组件，需满足设计第 18 节触发条件并经用户确认。资源不足时排队或降批次，不跳过核验（FR-13）
- **敏感信息**：`.env`、密钥、数据库文件和原始资料不提交仓库，不写进日志、提示词或审计正文
