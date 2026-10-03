# LegalMind 代码审查基线报告

**审查日期**：2026-10-03
**审查范围**：`legalmind/backend/app/**`、`legalmind/backend/migrations/**`、`legalmind/frontend/src/**`（共约 3 900 行后端 + 586 行前端）
**审查依据**：`CODE_REVIEW.md`（红线 R1–R9、通用质量清单）
**方法**：逐文件通读，按红线与质量清单人工核对；不依赖工具（工具能查的部分见"未覆盖范围"）

---

## 结论摘要

| 级别 | 数量 |
| --- | --- |
| `[BLOCK]` | 1（已修复，见 B1） |
| `[MAJOR]` | 6（M1、M2、M4、M5、M6 已修复）+ 1 项合规缺口已修复 |
| `[MINOR]` | 10 |
| `[NIT]` | 3 |

**总体判断**：核心安全与数据一致性实现**明显高于同类早期项目**——授权三段式、未授权/不存在统一 404、CSRF 双提交、时间恒定密码校验、TOTP 时间步重放防护、恢复码单次使用、文件导入的压缩炸弹/宏/PDF 主动内容检查、原子写盘加哈希校验、审计与 Outbox 同事务、幂等键、乐观并发，均已落实。

问题集中在两处：**异步路径里的阻塞 IO**（唯一阻断项）与**已定义但无能力的角色**（未实现，非伪装完成）。

**未发现**：任何绕过授权的数据路径、任何原地覆盖正式记录、任何手工改表、任何引入新常驻组件、任何把未实现功能包装成完成。

---

## `[BLOCK]` 必须修复

### B1 阻塞式文件 IO 在 async 请求路径中执行

> **已修复（2026-10-03）**：`storage.put` / `open` / `delete` 共 4 处调用点已改为 `await asyncio.to_thread(...)`，不再阻塞事件循环。`ruff check` 通过，104 项测试通过。当时 `read_content` 仍在事务内读取文件，已在后续提交中一并处理（见 M1）。

**位置**：`app/modules/documents/service.py:66`、`:176`
**问题**：`LocalFileStorage.put()` / `open()` 是同步方法（`put` 含 `fsync` 与整文件回读校验，`open` 整文件读入），却在 `async def import_document` / `read_content` 中直接调用，**阻塞事件循环**。
**影响**：设计 14.1 明确 API 为**单进程**（`workers: 1`）。单次最大 50 MB 的上传/下载期间，整个服务的其他请求全部停摆——这不是"慢"，是可用性缺陷。
**依据**：设计 14.1/14.2（单进程资源档位）、FR-13。
**建议**：改用线程池执行，例如

```python
from anyio import to_thread
await to_thread.run_sync(storage.put, key, content, sha256)
```

或改用异步文件库。`put` 中"写入后回读校验哈希"可改为流式增量计算，避免二次读盘。

---

## `[MAJOR]` 合并前修复或书面记录

### M1 文件读取放在数据库事务内

> **已修复（2026-10-03）**：`read_content` 拆为「授权短事务 → 事务外读取并校验 → 审计短事务」，文件处理不再在事务内。`ruff check` 通过，104 项测试通过。

**位置**：`app/modules/documents/service.py:167-194`（`read_content`）
**问题**：`storage.open()` 与 SHA-256 校验都在 `async with session.begin()` 内完成。
**依据**：设计 12.1——"外部模型调用、文件处理和队列发送不放进长数据库事务"；CLAUDE.md 同款表述。此处属于**触碰红线 R2**。
**建议**：拆为「短事务授权 → 事务外读取并校验 → 短事务写审计」。下载审计失败仍不交付文件（保留现有语义）。

### M2 恢复流程可能产生"部分恢复"

> **已修复（2026-10-03）**：目标存储的冲突检查已前移到 `pg_restore` 之前，冲突时中止且**不修改数据库**；复制循环中的冲突分支随之简化为“已存在即跳过”。测试补 `mock_run.assert_not_called()` 锁住该行为。107 项测试通过。

**位置**：`app/cli.py:267-292`
**问题**：`pg_restore --clean` **先执行**（覆盖目标数据库），之后才检查目标存储的文件冲突（第 285-289 行），冲突时 `sys.exit`。此时数据库已恢复、原件未恢复，与第 246 行注释"全部校验通过才继续，不做部分恢复"相矛盾。
**依据**：设计 15.2 恢复顺序。
**建议**：把目标文件冲突检查**前移到 `pg_restore` 之前**，与备份文件哈希校验放在同一阶段。

### M3 `auditor` 与 `legal_reviewer` 角色无实际能力

**位置**：`app/modules/authorization/service.py:29-38`
**问题**：`auditor` 权限集为**空**；`legal_reviewer` 仅含只读权限。但需求 §3 规定审计人员"在授权范围内查看审计和追溯记录"、法律审核员"审核资料版本、Wiki 和待审回答"，FR-07/FR-08 亦有审核要求。当前既无 `audit.read` / `review.decide` 权限常量，也无对应接口。
**性质**：属**未实现**（README 与文档已如实声明，不构成 R8 违反），但角色定义与需求脱节，容易误导使用者以为该角色可用。
**建议**：补权限常量与接口；若排在 P5，至少在角色表旁加注"当前无权限，待 P5 实现"。

### M4 审计表缺少查询索引

> **已修复（2026-10-03）**：新增迁移 `0005_audit_event_index.py`，为 `audit_events` 建立 `(actor_id, action, created_at)` 复合索引；`models.py` 同步声明该索引。

**位置**：`migrations/versions/0001_initial.py:78-98`（`audit_events` 仅建 `organization_id` 索引）
**问题**：`app/modules/identity/mfa.py:79-90` 的 `check_failures` 按 `actor_id + action + created_at` 过滤，审计表无 `actor_id` / `created_at` 索引。该查询**每次 MFA 尝试都会执行**，而审计表只增不减。
**建议**：新增迁移，建立 `(actor_id, action, created_at)` 复合索引。

### M5 统一错误响应与 trace_id 缺失

> **已修复（2026-10-03）**：新增 `app/core/errors.py`——纯 ASGI 的 trace_id 中间件（生成并回写 `X-Trace-Id`）与统一错误体 `{code, message, trace_id}`（校验错误另带 `errors`）；未处理异常记录带 trace_id 的日志且不向客户端泄露细节。前端同步改为读取 `message`。原依赖 `{"detail": ...}` 的 3 处测试断言已改为比较与安全语义相关的字段（trace_id 每请求不同）。测试 107 项通过。

**位置**：`app/main.py`（无异常处理器、无请求 ID 中间件）
**问题**：设计 13 要求"错误返回统一包含 `code`、安全描述和 `trace_id`"，设计 16.1 要求请求/问答/任务可经 trace_id 关联。当前错误体为 FastAPI 默认 `{"detail": ...}`，无 trace_id。
**建议**：加请求 ID 中间件（生成并回写 `X-Trace-Id`）+ 全局异常处理器，统一错误体形状。此项也是前端错误提示可读性的前置条件。

### M6 CLI 导入绕过上传大小限制

> **已修复（2026-10-03）**：`import_documents` 读取前先按文件大小与 `MAX_UPLOAD_BYTES` 比较，超限计入失败并跳过，不再把整文件读入内存；与 API 入口（`documents/router.py` 的 `read_body`）使用同一上限。

**位置**：`app/cli.py:125`（`path.read_bytes()`）
**问题**：CLI 导入整文件读入内存，**未受 `MAX_UPLOAD_BYTES` 约束**；而 API 路径在 `app/modules/documents/router.py:44-58` 做了流式上限控制。同一业务的两条入口校验不一致。
**依据**：一致性；设计 7.3/14.2 资源约束。
**建议**：CLI 复用同一上限（先 `stat().st_size` 判断，再读）。

---

## `[MINOR]` 作者自行决定

> **已修复（2026-10-03，89a82df）**：m1、m3、m4、m5、m6、m8、m10 已处理，逐项状态见 `CODE_REVIEW_BACKLOG.md`。其中 **m1 未采用下表建议**——直接前置判空会跳过哈希校验、破坏时间恒定（用户枚举），实际改为保留 dummy 校验再显式判空。m2、m7 待办。

| # | 位置 | 问题 | 建议 |
| --- | --- | --- | --- |
| m1 | `identity/service.py:102-104` | `verify_password(...) and user.is_active` 依赖短路求值规避 `user` 为 `None`；当前正确但脆弱 | 显式写成 `user is not None and verify_password(...) and user.is_active` |
| m2 | `identity/service.py:269-275` | `list_users` 对每个用户各查一次角色（N+1） | 一次 join 查询后在内存分组 |
| m3 | `authorization/grants.py:102-103` | 已存在授权时直接返回，**不写审计**，重复授权动作无痕迹 | 视需要补一条 `*.access_granted` 幂等事件 |
| m4 | `identity/mfa.py:66` | 恢复码为 `secrets.token_hex(5)`，仅 40 bit 熵 | 提到 `token_hex(8)`；现有失败限流已兜底 |
| m5 | `documents/router.py:170` | `job.payload["document_id"]` 缺键会 `KeyError` → 500 | 用 `.get()` 并转 404 |
| m6 | `App.tsx:179-181` | `canWrite` 硬编码角色名单，与后端 `ROLE_PERMISSIONS` 重复，存在漂移风险 | 仅作 UX 提示；建议注释明确"不得作为权限依据"，或改为后端返回能力标志 |
| m7 | `App.tsx:16-46` | 前端类型手写，未从 OpenAPI 生成 | 引入类型生成，减少契约漂移 |
| m8 | `authorization/grants.py:61-74, 128-134` | `list_grants` / `revoke` 不按 `organization_id` 过滤，依赖调用方先做组织校验（当前调用方均已做） | 补注释说明该前置条件，或在函数内加 org 条件做纵深防御 |
| m9 | `models.py:67-70` | `ck_wiki_draft_only` 把"仅草稿"固化到数据库约束，P5 发布时需迁移 | 在代码与设计文档中标注该约束的移除计划 |
| m10 | `migrations/env.py:29-33` | 未启用 `compare_server_default`，`server_default` 漂移不会被 `alembic check` 发现 | 按需开启 |

---

## `[NIT]`

- `models.py` 中多数 `organization_id` 只有索引、无外键（仅 `User.organization_id` 有外键），风格不一致。
- `ruff format` 显示 13 个文件待格式化（既有）。
- 前端无 eslint / prettier / 测试。

---

## 合规缺口（V1.4 新增要求，2026-10-03 追加）

对照设计文档 §20.2（正式输出须带“不构成法律意见”声明与知识范围说明）复查，发现并处理 1 项：

| 级别 | 位置 | 问题 | 状态 |
| --- | --- | --- | --- |
| `[MAJOR]` | `frontend/src/App.tsx` | 界面无“不构成法律意见”与知识范围提示，不符合 §20.2 | **已修复**（新增提示条；**文案为草稿，待确认**） |

数据来源侧已符合：`Source.license_note` 有非空约束，且 schema 要求 `min_length=1`。
采集器尚未实现（P3 之后），届时须按 §20.3 落实服务条款、访问频率与技术措施限制。

---

## 正向确认（做得好的地方，建议保持）

- **授权**：所有读取/下载/授权管理路径均先做对象级校验；未授权与不存在统一 404（`wiki/service.py:41`、`documents/service.py:136`）。
- **认证**：时间恒定密码校验（`core/security.py:55-60`）、CSRF 双提交 + `compare_digest`、会话空闲与绝对超时、禁用即时吊销会话。
- **MFA**：TOTP 时间步单调递增防重放、恢复码单次使用、密钥 Fernet 加密、失败限流、密钥不可解密时**拒绝而非放行**（`mfa.py:44-46`）。
- **文件**：对象键强制 32 位十六进制、临时文件 + `fsync` + 哈希校验 + 原子替换（`adapters/storage.py`）；导入侧含压缩炸弹、宏、PDF 主动内容检查（`documents/validation.py`）。
- **数据一致性**：关键变更与 `AuditEvent`/`OutboxEvent` 同事务；`Job` 幂等键为「组织+哈希+类型+配置版本」；Wiki 修订乐观并发（`expected_revision`）；仅追加、不覆盖。
- **迁移**：全部走 Alembic，含 downgrade；`ck_artifact_confidential_restricted` 用数据库约束保证"机密必须受限"。
- **配置**：生产模式下 MFA 密钥与 Secure Cookie 缺失时**启动即失败**，而非首次登录才暴露（`core/config.py:53-57`）。

---

## 建议修复顺序

1. ~~**B1**（阻塞 IO）~~ —— **已修复（2026-10-03）**。
2. ~~**M1**~~、~~**M2**~~（**均已修复 2026-10-03**）—— 触碰 R2 与恢复正确性。
3. ~~**M5、M4**~~（**已修复 2026-10-03**）—— 可观测性与查询性能，属基础设施补课。
4. ~~**M6**~~（**已修复 2026-10-03**）、**M3** —— 能力补齐与入口一致性。
5. ~~**MINOR / NIT** 第一组~~（**m1/m3/m4/m5/m6/m8/m10 已修复 2026-10-03，89a82df**）；m2/n1/n2/n3/m7 及 NIT 待办。

---

## 未覆盖范围（本报告不代表这些已通过）

- **自动化检查**：本次为人工审查，未运行覆盖率统计；`ruff format` 的 13 个文件差异未逐一看。
- **动态验证**：未做渗透测试、并发压测、故障注入（重复投递、租约过期、磁盘写满）。
- **未实现模块**：`retrieval`、`answering`、`legal_corpus`、`review`、`evaluation`、`workers` 均为空占位，**不在本次审查范围**；GraphRAG 与 LangGraph 编排尚未开始编码。
- **依赖与供应链**：未核查依赖许可证与已知漏洞（`CODE_REVIEW.md` §4.2 已列为待办）。
- **前端**：仅审查 `App.tsx`，未审查构建配置与样式。
