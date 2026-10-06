# LegalMind

法律知识库与辅助研究系统。

## 当前版本

v0.1.0：本地开发基础版，不可直接用于生产。

当前实现覆盖设计实施阶段 P0–P5，P6 已开始：需求阶段 A 已完成；阶段 B 的 P2（认证授权、文件存储、备份恢复）已完成；P3 的数据模型已完整建立（原文定位 0007、法律版本树 0008、条款关系与适用性 0009），**第 1 层解析器、法律结构识别、后台 worker、法律版本落库与精确字段检索已实现**（`app/modules/parsing/` 解析结果经入库前脱敏后写入 `parse_revisions` / `chunks` / `chunk_spans`；`app/modules/legal_corpus/` 识别 编/章/节/条、对齐分块并挂到 `legal_instruments` / `legal_versions` / `provision_identities` / `provision_versions`；`app/workers/` 消费 `document.parse` 任务；`app/modules/retrieval/` 提供 `POST /api/v1/search`，`app/modules/legal_corpus/quality.py` + `backend/scripts/quality_gate.py` 提供只读入库质量门禁），"导入 → 解析 → 挂版本 → 查得到"已闭环；**中文关键词检索已实现**（V1.12，方案经金标准选型）；**向量检索已接线**（V1.13，`semantic` 字段走级联：先关键词、命中为空才向量兜底）、**图谱引用边已落库**（V1.14，`app.cli extract-citations`，只作上下文不进排序）；**Wiki 审核发布与更新提醒已实现**（V1.15 / V1.16，迁移 0016 / 0017）；**P6 架构锁定为本地模型、默认关闭外部 API**（V1.17，§9.5），并打通了最小可用的证据约束问答（`app.cli ask`），**生成质量评测已建立**（V1.18：金标准 22 条 + `backend/scripts/evaluate_answering.py`，**首次实跑模型侧 6 项指标全部达标**）；**效力状态由回答层强制**（V1.19）；**P6 的编排、引用校验、语义核验、异步 answer-run 尚未实现**（拒答策略已有第一刀：无依据、或依据全部非现行有效时拒答）；阶段 C 未开始。进度口径见《系统设计文档》17.2。

## 已实现

- FastAPI + PostgreSQL + SQLAlchemy
- Alembic 迁移（0001 Wiki/审计/Outbox，0002 身份与会话，0003 MFA 与页面授权，0004 来源/原件/任务，0005 审计索引，0006 业务表外键，0007 解析版本/分块/原文定位，0008 法律版本/条款，0009 条款关系/适用性，0010 公共数据表去组织字段，0011 来源/原件/任务去组织字段，0012 脱敏映射表，0013 版本效力状态与本体身份键，0014 文号规范化字段与索引，0015 向量扩展与条款向量表，0016 Wiki 审核发布状态机与修订引用，0017 引用正文快照与待复核标记）
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
- 来源登记（FR-01）：来源类型、可信等级、授权说明、最后核查时间；来源登记信息与原件来源归属均可更正（`PUT /sources/{id}`、`PUT /documents/{id}/source` 与对应 CLI，需 source.manage，变更写审计含前后值——§20.3 要求来源更新留痕；授权说明不允许清空）
- 撤下原件（设计 15.3）：**整批**删除原件、解析版本、分块、定位与脱敏映射，取消其尚未开始的解析任务，清理因版本删除而孤立的条款身份（`app.cli withdraw-document`，支持 `--from-source` 按来源整批、`--dry-run` 预演）；同一版本还有合法来源原件时自动从它重建版本树，没有的才连同法律本体一并删除。用于来源被认定无权使用的情形（需 source.manage，操作写审计并记录对象键）
- 原始文件导入与下载（FR-02）：流式上传限流、压缩炸弹/宏/PDF 主动内容检查、原子写盘加哈希校验；文档级授权与页面级授权共用 AccessGrant
- 备份与恢复：`app.cli backup` / `restore`，含原件清单与 SHA-256 校验、恢复前冲突检查与 dry-run
- 文档解析（第 1 层，设计 2、6）：`DocumentParser` 接口 + 原生实现——PDF 后端锁定 pypdfium2（pdfplumber 可切换）、DOCX（python-docx）、HTML（lxml）、纯文本；解析结果落库为 `parse_revisions` / `chunks` / `chunk_spans`，审计与 Outbox 同事务；块级定位（页序号、字符偏移、归一化 bbox）
- 解析入库前脱敏（设计 21.3）：`chunks.text` 存脱敏文本、原文本不落库（由映射表可逆重建）、`chunk_spans` 偏移仍指向原文本；映射与分块、审计同事务
- 法律结构识别（设计 5.2、6、7）：识别 编/章/节/条 四级、排除「目录」重复标题，**分块对齐到「条」**并把章节路径写入 `chunks.structure_path`；16 份真实法律文本上条号连续、无识别异常
- 法律版本落库（设计 5.1、5.2、7、8.3）：解析后自动挂到法律本体/版本/条款身份/条款版本；本体身份为「法域 + 规范化名称」；版本标识与效力状态取自正文公布信息、文件名仅作回退；**低置信度置 `review_status='pending'` 待人工复核**（目前无待审队列/界面）；同一版本多原件按「效力状态 > 公布日期 > docx 优于 pdf > 导入时间」择优并做效力状态证据合并，**来源不同才算冲突**（同一来源的两种格式不算）；择优规则变化后用 `app.cli relink-versions` 按当前规则重挂（从解析产物重建、不重新解析、不删数据，支持 `--dry-run`）。16 份真实样本实测：13 个本体、14 个版本、2207 个条款身份、2345 个条款版本
- 后台任务 worker（设计 3.1、12.2）：单进程单并发、按需启动（`python -m app.cli run-worker`）；`FOR UPDATE SKIP LOCKED` 原子领取、租约与心跳、租约过期回收重试、指数退避、失败分类（资源不足记 `resource_exhausted`，不伪装成业务结果）
- 精确字段检索（设计 8.1、8.3、13）：`POST /api/v1/search` 按规范化名称 / 文号 / 稳定 ID / 条号精确匹配，叠加法域、资料类型、效力日期、效力状态与授权过滤；**默认屏蔽「已公布未生效」**，审核状态不过滤但始终回传；授权在数据库中复核（join 原件 + 授权范围）。写入与检索共用同一套规范化口径（迁移 0014 新增 `legal_instruments.document_number_normalized`）。**中文关键词（V1.12）、查询改写（V1.13）、向量级联（V1.13）与图谱引用边（V1.14）见下条**
- 中文关键词检索（设计 8.2、8.3）：`POST /api/v1/search` 的 `keyword` 与精确字段、过滤条件 **AND 叠加**；方案**先建金标准再选型**（`evaluations/datasets/retrieval_queries.json` 28 条查询，`backend/scripts/evaluate_retrieval.py` 横向比 5 个候选），选中「去空白 + 多词 AND」（Recall 1.0 / 准确率 1.0）。**不引入 Elasticsearch、不建索引**；⚠️ PDF 文本的折行空格（实测「民用航 空器」）不处理会丢一半召回
- 入库质量门禁（设计 7、17.1、20.3）：`app/modules/legal_corpus/quality.py` + `backend/scripts/quality_gate.py` **只读**校验来源登记、元数据与解析质量，判 `passed` / `degraded` / `failed`（来源/授权说明/解析产物/挂版本等硬要求不过即 `failed`，降级待审判 `degraded`）；报告落 `evaluations/reports/`，门禁不通过时退出码非零。**只校验、不阻断**
- Wiki 审核发布与更新提醒（设计 10.2、10.3）：修订走 草稿 → 提交 → 发布/驳回 状态机，提交即锁定；**发布前做引用检查**（至少一条引用，且审核人能访问每条引用的原件）；**作者不能审自己的修订**；引用了读者无权访问的原件时**整页不可见**。`app.cli flag-stale-pages` 检测「依据被取代」与「引用正文已变更」并标记待复核（**只标记、不动正文**，重新发布清标），`GET /api/v1/wiki/pages/stale` 暴露列表
- 本地模型的证据约束问答（设计 9.5，P6 第一刀）：`app/modules/answering/` + `python -m app.cli ask`——检索 → **只把命中的条文原文交给模型** → 生成 → 引用取**检索命中的条款版本**（不是模型写的条号，因此可核验）；**没有依据就拒答且不调用模型**；**模型不可用时如实降级成「只给证据 + 转人工」，不回落任何外部服务**
- 效力状态门禁（设计 8.3、9.5）：**由回答层确定性强制、不问模型**——依据里有非现行有效版本（`repealed` / `not_yet_effective`）就在答案前给出提示，**全部非现行有效则直接拒答、不生成结论**。实测模型不会理会证据块里的「尚未生效」标注（见下条），所以安全属性不能交给提示词。⚠️ `unknown` **不算**非现行有效（只表示没提取到施行日期）
- 生成质量评测（设计 9.5「本地生成模型单独评测，达标后再接入正式问答」）：金标准 `evaluations/datasets/generation_quality.json`（22 条，**证据钉住、绕过检索**，只量生成）+ `backend/scripts/evaluate_answering.py`（`--check-only` 只校验、`--gate` 未达标退非零）+ 纯打分 `app/modules/evaluation/scoring.py`。6 项确定性指标（引用召回 / 凭空引用 / 关键要素覆盖 / 禁项触犯 / 拒答正确 / 过度拒答），温度 0 可复现。**首次实跑模型侧 6 项全部达标**；效力状态提示作**诊断项**报告（模型侧为 0，故由回答层强制）
- 解析内存防护（设计 14.2）：逐页处理并及时释放，字节/页数/字符数硬上限 + 进程内存增长守卫，超限主动中止
- 业务表统一外键（设计 5.3）：organization_id 与"人"引用列（created_by / author_id / granted_by / actor_id），ondelete 一律 RESTRICT
- 前端静态检查：eslint + prettier（`npm run lint` / `npm run format:check`）
- CI（GitHub Actions）：push 与 PR 自动跑后端 lint / 迁移漂移检查 / 测试，以及前端 lint / 构建

## 未实现

- 来源登记、文件导入下载与授权名单的前端管理界面（目前均通过 API）
- Wiki 审核发布的**前端入口**（后端与 API 已实现）
- 法律版本的人工复核界面与待审队列（落库已实现，`review_status='pending'` 目前只能用 SQL 查）
- 版本效力状态的定期重算（「已公布未生效」到期转有效、旧版本随之被取代，目前只在落库时算一次）
- OCR（第 2 层）与结构化版面（第 3 层 Docling，暂缓）
- **P6 的编排、引用校验、语义核验、异步 answer-run**（本地模型、证据约束问答、生成质量评测与效力状态门禁已实现；生成评测**模型侧已达标**，故正式问答可以往下接）
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

更正已导入原件的来源归属，以及更正来源登记信息（均需 source.manage，变更写审计）：

    PUT    /api/v1/documents/{document_id}/source          {"source_id": "<目标来源 ID>"}
    PUT    /api/v1/sources/{source_id}                     {"publisher": "...", "license_note": "..."}
    docker compose exec api python -m app.cli set-document-source --as kadmin --document <原件 ID> --source <目标来源 ID>
    docker compose exec api python -m app.cli update-source --as kadmin --source <来源 ID> --url <URL> --publisher <发布方>

撤下原件（不可逆；先 `--dry-run` 预演，确认后去掉；用于来源被认定无权使用的情形，设计 §15.3）：

    docker compose exec api python -m app.cli withdraw-document --as kadmin --document <原件 ID> --reason "原因" --dry-run
    docker compose exec api python -m app.cli withdraw-document --as kadmin --from-source <来源 ID> --reason "原因"

后台任务 worker（按需启动；导入只登记任务，不启动 worker 时任务一直停在 pending）：

    docker compose exec api python -m app.cli run-worker            # 持续处理
    docker compose exec api python -m app.cli run-worker --once     # 只处理一个任务

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
- 解析只实现了第 1 层（原生）；扫描件（无文本层）标记 `needs_review`，OCR 就位前不得作为证据来源。
- PDF 后端已锁定 pypdfium2（样本基准：与 pdfplumber 文本逐字节一致、定位均满足设计 6 的精度要求、快约 5 倍）；`PARSING_PDF_BACKEND=pdfplumber` 可切换，切换会新建解析版本（基准脚本：`backend/scripts/benchmark_parsers.py`）。
- 脱敏只覆盖正则可识别的身份证号、案号、联系方式；**姓名类实体需 NER**，就位前不得把解析产物作为对外发布内容。
- 后台 worker 为**单进程单并发**，不要并发起多个；worker 被强杀不丢任务（租约过期后回收重试），但取消只作用于尚未开始的任务。
- 印刷页码暂不检测（存 NULL）。
- 结构识别只覆盖 编/章/节/条；**款/项/目 未建模**，**条款身份只覆盖「条」**（章/节号随上级重置）。
- 法律版本元数据（名称、文号、版本标识、效力状态）来自**正文前言的启发式提取**，没有人工确认环节：低置信度只置 `review_status='pending'` 并写审计，**目前没有待审队列或界面**；`review_status='approved'` 是**自动确认**，不等于人工复核。
- 效力状态在**落库时**计算一次；时间推进导致的状态变化（「已公布未生效」到期转有效、旧版本随之被取代）不会自动更新，需重新关联或补定期重算。
- 检索实现了精确字段、中文关键词与向量（级联）通路（设计 §8.2、§8.3），图谱引用边只作上下文、不进排序；检索默认屏蔽 `not_yet_effective`（不得把尚未生效的法律当作现行依据）；审核状态（`pending`）**不过滤但始终回传**——它是数据质量标记、不是效力事实。日期未知的版本保留并在结果里显示 `unknown`。
- 关键词选型结论只在**当前 28 条金标准、这份语料**上成立：语料规模或来源变化后应重跑 `backend/scripts/evaluate_retrieval.py`。它衡量的是**词面命中**，不是语义召回——语义召回由向量通路承担（同一份语料另建了问句式 24 条与简称 12 条两份金标准）。关键词通路当前未建索引（顺序扫描 21 ms/查询）；向量通路无 ANN 索引（不定长 `vector` 列建不了），2403 条顺序扫描 30 ms。
- 入库质量门禁只校验、不阻断：当前语料 15 passed / 4 degraded / 1 failed；`failed` 的是纯图像扫描件的宪法 PDF（解析 0 字符、挂不上版本树，OCR 未实现）。
- 生成质量评测**只量确定性指标**（引用、关键要素、拒答），不评语义忠实性——那需要语义裁判，而 §9.5 锁定本地模型、不引入外部服务。**22 条用例、单一语料、单一模型**，结论只在这份语料与这个模型上成立；它**绕过检索**，因此测不到「检索给错证据时模型会不会照样答」。**效力状态提示不在模型指标里**（那是回答层职责，§8.3），报告里作诊断项照记。语料或模型换了要重跑 `backend/scripts/evaluate_answering.py`。
- 效力状态门禁只在**依据全部非现行有效**时拒答；部分非现行有效时照出结论、只加提示——「哪几条能当依据」的判断仍留给人工。**没有「就是要查已废止旧法」的显式开关**；提示目前只有 CLI 会打印，前端入口未接。
- 不应将包含敏感内容的 .env、数据库或原始文件提交仓库。

## 依赖

当前依赖使用范围约束，尚未生成经过验收的锁文件。
首次安装测试后，应锁定依赖、检查许可证和已知漏洞，
并在正式构建中使用锁文件和固定镜像摘要。

第 1 层解析依赖均为宽松许可、纯 CPU、不联网：pdfplumber（MIT）、pypdfium2（Apache-2.0 / BSD-3-Clause）、python-docx（MIT）、lxml（BSD-3-Clause）。
