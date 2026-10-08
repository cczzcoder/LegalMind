# LegalMind

**面向律师、企业法务与法律研究人员的法律信息辅助系统。**

把「法律法规与裁判文书分散存储、版本难以追踪、人工检索效率低、问答结果缺乏依据」这四个问题，做成一条可溯源、可核验、可审计的链路：

```
导入 → 解析 → 版本挂载 → 检索 → 证据约束问答 → 答案溯源
```



> **合规定位**：本项目是**法律信息辅助工具，不是法律服务提供方**。每个正式输出都带「不构成法律意见」声明；问的是「本人情形」就不给个人结论，只回条文与下一步。这条边界是**结构性约束**，不是提示词里的请求——见[合规与数据边界](#合规与数据边界)。

---

## 目录

- [核心能力](#核心能力)
- [合规与数据边界](#合规与数据边界)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [本机开发](#本机开发)
- [测试与质量门禁](#测试与质量门禁)
- [评测体系](#评测体系)
- [关键设计决策](#关键设计决策)
- [已知限制](#已知限制)
- [文档](#文档)

---

## 核心能力

| 能力            | 说明                                                                                                                                        |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| **结构化解析**     | 自研解析层识别「编 / 章 / 节 / 条」四级结构、排除目录重复标题，**分块对齐到「条」**&#x5E76;把章节路径写进元数据；条款正文与引用锚点按条号首次出现位置单独重算。支持 PDF（pypdfium2，pdfplumber 可切换）、DOCX、HTML、纯文本。 |
| **版本可追溯**     | 以「法域 + 规范化名称」建法律本体身份，解析后自动挂载版本树（本体 / 版本 / 条款身份 / 条款版本）。**引用一律绑定不可变的条款版本行**，不绑「最新页面」；日期未知即存 NULL，不虚构。                                      |
| **检索**        | 精确字段（名称 / 文号 / 稳定 ID / 条号）+ 中文关键词（去空白 + 多词 AND）+ 向量（BGE-M3）。关键词与向量走**级联**：先关键词，命中为空才向量兜底。三层查询改写取并集、不替代原查询。                                 |
| **证据约束问答**    | 检索一条都没命中就**不调用模型**；只把命中的条文原文交给模型；引用取**检索命中的条款版本**，不是模型写的条号，因此天然可核验。                                                                       |
| **多重门禁**      | ① 效力状态由回答层**确定性强制**、不问模型（依据全部非现行有效则直接拒答）；② 证据预算装配（**整条进或整条不进**，装不下如实说明并限定回答范围）；③ 引用核验（法名简称全等 + 同一句内的条号）；④ 结构化输出（模型只回填证据编号，引用标题由服务端渲染）。    |
| **异步入库流水线**   | 导入只登记任务，解析由后台 worker 消费：`FOR UPDATE SKIP LOCKED` 原子领取 + 租约与心跳 + 租约过期回收重试 + 指数退避 + 失败分类（资源不足记 `resource_exhausted`，不伪装成业务结果）。              |
| **运行记录与流式进度** | 问答走显式状态机，不在转移表里的转移**当场抛异常**；运行记录只记状态、证据引用与配置快照，**不记正文**（只留哈希）。SSE 随转移推 `state`、终态才推 `answer`。                                             |
| **多轮追问**      | 同一会话内继承上文，但**只把「补全后的当前问题」送进提示词，历史答案绝不进**（答案含结论，会被当依据用而未经本轮授权复核）；会话历史只进短期缓存、不落主库。                                                          |
| **Wiki 与审核**  | 草稿 → 提交 → 发布 / 驳回状态机；发布前检查至少一条引用、且审核人能访问每条引用的原件；作者不能审自己的修订。更新提醒有两条信号：引用版本被取代、**引用条款正文变了**（只比版本号发现不了第二种）。                                  |
| **权限**        | RBAC + 组织范围过滤 + 对象级授权 + 页面级授权。授权**在数据库里复核**（join 原件 + 授权范围），检索前、证据装配时、输出前都要过；AI 不决定访问权限。                                                  |
| **脱敏**        | 解析产物入库前经脱敏节点（**先定位、后脱敏** + 可逆映射表）：正文存脱敏文本、原文本不落库。                                                                                         |

---

## 合规与数据边界

- **定位**：法律信息辅助工具，**不提供法律服务**。`Answer.disclaimer` 与 `Answer.generated_at` 是**默认字段**——结构上发布不出一份不带声明的结论。
- **数据范围**：只存放**公共法律数据**（法律法规、司法解释、公开裁判文书），**不存放客户资料、案件卷宗或合同**。第一版为**单租户、部门级内部工具**。
- **公共数据表不设 `organization_id`**（全库一份、不按组织复制）；`answer_runs` 属用户私有数据，带 `organization_id`。
- **审计只记元数据与哈希**，不记正文。待审队列需要的正文走**短期缓存**（TTL 到期即焚，Redis 关掉 RDB/AOF 保证不落盘），**不上主库**。
- **模型全部本地**：默认关闭任何外部 API，**代码里没有「未配置本地模型就回落到外部服务」的分支**——配置缺失就是不可用，如实降级成「只给证据 + 转人工」。这条线由测试用源码扫描守住（禁词 `openai` / `anthropic` / `dashscope` / `api_key` / `https://api.`）。

---

## 技术栈

- **后端**：Python 3.12 · FastAPI · SQLAlchemy 2.0（async）+ asyncpg · Alembic（18 个迁移）· Pydantic v2
- **数据**：PostgreSQL 17 + pgvector · Redis 7（**仅作 TTL 键值缓存，不作队列**）
- **前端**：React 18 · TypeScript · Vite 6 · Ant Design 5
- **模型（本地）**：Qwen2.5-7B-Instruct（Q4_K_M，经 Ollama）生成 · BGE-M3 嵌入（sentence-transformers）
- **解析**：pypdfium2（默认）/ pdfplumber（可切换）· python-docx · lxml
- **认证**：Argon2id 口令 · 服务端会话 · CSRF 防护 · 管理员 TOTP 第二因素
- **工程**：ruff（后端）· eslint + prettier（前端）· pytest · GitHub Actions CI

---

## 快速开始


需要 Docker 与 Docker Compose。**所有命令在 `legalmind/` 目录下执行。**

```bash
cd legalmind

# 1. 准备 .env：设置随机数据库密码（以及 MFA_ENCRYPTION_KEY，见 .env.example）
cp .env.example .env

# 2. 启动全部服务（PostgreSQL / Redis / 迁移 / API / 前端）
docker compose up --build -d

# 3. 创建用户（系统不开放注册；密码交互输入，至少 12 位）
docker compose exec api python -m app.cli create-user --org 示例机构 --username admin --role system_admin

# 4. 启动后台 worker（另开一个终端；导入只登记任务，不启动 worker 则任务一直停在 pending）
docker compose exec api python -m app.cli run-worker
```

- 前端：<http://127.0.0.1:5173>
- API 文档：<http://127.0.0.1:8000/docs>

`system_admin` 与 `knowledge_admin` 首次登录须绑定 TOTP（需在 `.env` 设置 `MFA_ENCRYPTION_KEY`）。认证器与恢复码都丢失时由运维重置：

```bash
docker compose exec api python -m app.cli reset-mfa --username admin
```

停止（**不要随意加 `-v`**，会删除数据库卷）：

```bash
docker compose down
```

---

## 本机开发

数据库跑在 Docker、API 与 worker 跑在宿主机，改代码不用重建镜像。

```bash
cd legalmind
docker compose -f compose.yaml -f compose.localdb.yaml up -d postgres

cd backend
python -m venv .venv                                                   # 首次
.venv/Scripts/python.exe -m pip install -r requirements.lock           # 依赖走锁文件（见 CONTRIBUTING）
.venv/Scripts/python.exe -m pip install --no-deps -e .                 # 再装项目本体

set -a && . ../.env && set +a          # ⚠️ .env 是 CRLF，必要时先 tr -d '\r'
export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}"

.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
.venv/Scripts/python.exe -m app.cli run-worker      # 另开一个终端
```

前端：

```bash
cd legalmind/frontend
npm install
npm run dev
```

⚠️ **改了后端代码要重启 uvicorn 与 worker**——两个进程都没开 `--reload`。

---

## 测试与质量门禁

```bash
# 后端
cd legalmind/backend
ruff check . && ruff format --check .
python -m pytest

# 前端
cd legalmind/frontend
npm run lint && npm run format:check && npm run build

# 提交前门禁（跑的就是上面这几条，只对改动的文件）
.venv/Scripts/pre-commit install        # 首次，在 legalmind/backend 下
pre-commit run --all-files
```

改过界面布局，再跑一次前端验收（需要活的后端与 vite，**不在 CI 里**）：

```bash
cd legalmind/frontend && npm run acceptance
```

集成测试连接独立的 `legalmind_test` 库，未设置 `TEST_DATABASE_URL` 时自动跳过：

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE legalmind_test"   # 仅首次
export TEST_DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/legalmind_test"
python -m pytest
```

CI（`.github/workflows/ci.yml`）在 push 与 PR 上跑同一套门禁：后端 lint / 迁移漂移检查 / 测试，前端 lint / 构建。

---

## 评测体系

关键选型全部以**可复现的评测数据**支撑，而不是凭感觉。金标准在 `evaluations/datasets/`，脚本在 `backend/scripts/`，报告落 `evaluations/reports/`（不入库）。

| 数据集                            | 条数 | 用途                     | 脚本                      |
| ------------------------------ | -- | ---------------------- | ----------------------- |
| `retrieval_queries.json`       | 28 | 中文关键词选型（横向比 5 个候选）     | `evaluate_retrieval.py` |
| `retrieval_questions.json`     | 24 | 问句式语义召回（关键词 vs BGE-M3） | `evaluate_retrieval.py` |
| `retrieval_abbreviations.json` | 12 | 简称查询改写                 | `evaluate_retrieval.py` |
| `generation_quality.json`      | 22 | 生成质量（证据钉住、绕过检索，只量生成）   | `evaluate_answering.py` |
| `claim_verification.json`      | 28 | 断言级语义校验                | `evaluate_semantics.py` |

另外还有**只读**的入库质量门禁（`quality_gate.py`：校验来源登记、元数据与解析质量，判 `passed` / `degraded` / `failed`）与解析器基准（`benchmark_parsers.py`）。

**几组实测数字**：

- 中文关键词方案：「去空白 + 多词 AND」在 28 条上 **Recall 1.0 / 准确率 1.0**（原始子串只有 0.5，被 PDF 折行空格吃掉一半）。
- 结构化输出：只用 `format: "json"` 时 22 条里 11 条输出残缺（值恰好为空数组时漏冒号）；改为传**完整 JSON Schema** 走约束解码后，解析失败率 **0.500 → 0.000**，引用召回 **0.912 → 0.971**，过度拒答率 **0.059 → 0.000**。
- 生成质量：模型侧 6 项确定性指标（引用召回 / 凭空引用 / 关键要素覆盖 / 禁项触犯 / 拒答正确 / 过度拒答）首次实跑全部达标。
- 语料：16 份真实法律文本识别出 13 个法律本体、14 个版本、**2207 条条款身份与 2345 条条款版本**，条号连续、无识别异常。

---

## 关键设计决策

**「没有测量支撑的组件不装」**&#x662F;本项目一贯口径。以下都是**评估过、然后明确不引入**的：

| 组件                        | 为什么不装                                                                                                       |
| ------------------------- | ----------------------------------------------------------------------------------------------------------- |
| **Elasticsearch**         | PostgreSQL 自带全文检索把整段中日韩字符当成一个词元，中文切不出词；`pg_trgm` 只是速度选项（2403 条上顺序扫描 21 ms），未达引入触发条件。改用「去空白 + 多词 AND」。       |
| **RRF 融合**                | 召回与排序其实与级联**完全一致**（Recall / Rank@1 / MRR 都是 1.000），差别在**候选集合大小**：词面集关键词段 1.5 条/查询、RRF 43.9 条，前 5 条 70% 是噪声，会挤占下游证据预算；简称集上还会把 Rank@1 从 0.889 拖到 0.309。改为**级联**：先关键词，命中为空才向量兜底。 |
| **Neo4j**                 | 图谱引用边实测**对命中质量零增量、对准确率负增量**（1.000 → 0.864），故只作上下文、不进排序。规模也没到（117 条边、一跳 join、40 ms）。                         |
| **重排序进默认**                | 模型比检索模型弱时**会把排序搞坏**。评估要看 Rank@1 与 MRR，不看「命中 / 返回条数」。                                                        |
| **LangGraph / LangChain** | 当前链路是线性的六步，「状态定义与转移条件由**本系统**显式声明」用自研状态机（`ANSWER_RUN_TRANSITIONS`）已经满足。**触发点是「多分支 + 需要中断恢复 + 需要流式进度」同时成立**。 |
| **Celery**                | 队列用自研 worker（`FOR UPDATE SKIP LOCKED` + 租约 + 指数退避）。Redis 只作 TTL 缓存，不作 broker。                               |
| **react-router**          | 只有 6 个页面，自写 hash 路由约 60 行。触发点是出现嵌套路由或路由级数据加载。                                                               |

---

## 已知限制

分三类看：**设计取舍**是「想清楚了故意不做」，**未实现**是「还没做」，**工程债**是「做得不够细」。三者性质不同，面试时也值得分开讲。

### 设计取舍（有实测支撑，刻意不做）

- **重排序不进默认**：模型比检索模型弱时会把排序搞坏；评估要看 Rank@1 与 MRR。
- **图谱引用边只作上下文、不进排序**：实测对命中质量零增量、对准确率负增量。
- **后台 worker 单进程单并发**：16 GB 单机基线下的取舍。`FOR UPDATE SKIP LOCKED` 已实现，为将来多实例留了口子。
- **没有外部 API 回落分支**：本地模型不可用就如实降级成「只给证据 + 转人工」，不偷偷外发。

### 未实现（能力缺口）

- **解析只到第 1 层**：OCR（第 2 层）与结构化版面（第 3 层）未做。扫描件（无文本层）标 `needs_review`，**OCR 就位前不得作为证据来源**。
- **结构只覆盖 编 / 章 / 节 / 条**：款 / 项 / 目 未建模，条款身份只覆盖「条」——**目前引用不到「第 X 条第 2 款」**。
- **语义校验（第二层核验）已建但未接入**：缺陷检出率 0.857，未达 0.9 门槛，故不拦结论。
- **版本元数据靠启发式提取，没有人工确认环节**：低置信度只置 `review_status='pending'`，且**目前没有待审队列界面**；`approved` 是自动确认，不等于人工复核。
- **效力状态只在落库时算一次**：时间推进导致的状态变化（「已公布未生效」到期转有效）不会自动更新。
- 前端未做的界面：来源登记 / 编辑、原件下载、授权名单管理、Wiki 审核发布入口、法律版本人工复核队列。

### 工程债（做得不够细）

- **脱敏只覆盖正则可识别的身份证号、案号、联系方式**；姓名类实体需 NER，就位前不得把解析产物作为对外发布内容。
- **审计表暂无数据库级防篡改权限**；Outbox 只写不消费。
- 可选的 `embeddings` extra（torch / sentence-transformers）**不在锁文件里**——体积以 GB 计，只在跑向量通路时才装。

---

## 文档

- **[`legalmind/README.md`](legalmind/README.md)** —— 当前能力的权威来源：已实现 / 未实现逐条清单、启动与运维命令
- **[`doc/法律知识库系统需求分析文档.md`](doc/法律知识库系统需求分析文档.md)** —— FR-01 ~ FR-16、分阶段交付、非功能需求与验收标准
- **[`doc/法律知识库系统设计文档.md`](doc/法律知识库系统设计文档.md)** —— 架构、数据模型、检索与问答流水线、资源档位、扩展触发条件、实施阶段 P0–P7
- **[`doc/前端界面说明.md`](doc/前端界面说明.md)** —— 前端规格与验收标准、响应式断点、实测记录
- **[`doc/开发与部署指南.md`](doc/开发与部署指南.md)** —— 脚手架、启动与验证步骤
- **[`doc/技术决策与踩坑记录.md`](doc/技术决策与踩坑记录.md)** —— 选型理由与踩坑记录（**工作记录，非规范**）
- **[`CONTRIBUTING.md`](CONTRIBUTING.md)** —— 开发约定、常用命令、工程规则
- **[`CODE_REVIEW.md`](CODE_REVIEW.md)** —— 代码审查标准与流程

---

## 数据与许可


- **代码以 [MIT 许可](LICENSE) 发布**：可自由使用、修改、分发，保留版权声明即可。
- **本仓库不包含法律原文语料**：`法律文献/` 已在 `.gitignore` 中，接入真实资料前须先确认来源授权说明与数据分级。**语料不在本许可范围内**（本仓库也不分发它）。
