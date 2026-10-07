# LegalMind 代码审查待办清单

**来源**：`CODE_REVIEW_BASELINE.md`（2026-10-03 基线审查）
**范围**：基线审查发现的项。已关闭的 B1、M1、M2、M4、M5、M6 及合规缺口不在此列；**第一组 7 项已修复（89a82df）**，行保留作处理记录。
**维护方式**：完成一项就在状态列标注提交号；**不要删行**，保留处理记录。
**分级口径**：见 `CODE_REVIEW.md` 第 1 节。

---

## 一、随手可修（低风险，改动面 ≤ 10 行）

建议合并成 1–2 次提交，随日常改动顺带处理。**已全部修复（89a82df）**，7 项合并为 1 次提交。

| 编号 | 位置 | 影响 | 建议改法 | 改动面 | 状态 |
| --- | --- | --- | --- | --- | --- |
| m1 | `identity/service.py:102-104` | `verify_password(...) and user.is_active` 依赖短路求值规避 `user` 为 `None`；当前正确，但改动这段逻辑时容易引入 `AttributeError` | **勿直接前置判空**（会跳过哈希校验、破坏时间恒定/用户枚举）：先 `password_ok = verify_password(user.password_hash if user else None, password)`，再 `succeeded = password_ok and user is not None and user.is_active` | 1 行 | 已修 89a82df |
| m5 | `documents/router.py:170` | `job.payload["document_id"]` 缺键会抛 `KeyError` → 500，而不是 404 | 改用 `.get()`，缺失时按 404 处理 | 2 行 | 已修 89a82df |
| m3 | `authorization/grants.py:102-103` | 授权已存在时直接返回、**不写审计**，重复授权动作无痕迹 | 补一条幂等的 `*.access_granted` 审计事件 | ~5 行 | 已修 89a82df |
| m8 | `authorization/grants.py:61-74, 128-134` | `list_grants` / `revoke` 不按 `organization_id` 过滤，依赖调用方先做组织校验（当前调用方均已校验，属纵深防御缺失） | 函数内补注释说明该前置条件，或加 org 条件做纵深防御 | 2–6 行 | 已修 89a82df |
| m4 | `identity/mfa.py:66` | 恢复码为 `secrets.token_hex(5)`，仅 40 bit 熵（有失败限流兜底） | 提到 `token_hex(8)`；已发放的旧码不受影响，下次重置生效 | 1 行 | 已修 89a82df |
| m10 | `migrations/env.py:29-33` | 未启用 `compare_server_default`，`server_default` 漂移不会被 `alembic check` 发现 | 在两处 `context.configure` 中开启 | 2 行 | 已修 89a82df |
| m6 | `frontend/src/App.tsx:179-181` | `canWrite` 硬编码角色名单，与后端 `ROLE_PERMISSIONS` 重复；后端改权限后前端提示会不同步（**仅影响 UX，后端仍强制校验**） | 加注释声明"仅作展示提示，不得作为权限依据"；彻底解决需后端返回能力标志 | 注释 1 行 | 已修 89a82df |

**小计**：7 项，合计约 20 行改动。**已全部修复（89a82df）**。

---

## 二、需要一次决策（引入工具链或改约定）

| 编号 | 位置 | 影响 | 建议改法 | 改动面 | 状态 |
| --- | --- | --- | --- | --- | --- |
| m2 | `identity/service.py:270-292` | `list_users` 对每个用户各查一次角色（N+1）；用户数上百后列表接口变慢 | 一次 join 查询后在内存分组（`_to_user_out` 抽出纯构造，单用户路径不变） | ~10 行 | 已修 8e81047 |
| n1 | `models.py`（多数实体） | `organization_id` 多为"仅索引、无外键"（只有 `User.organization_id` 有外键），风格不一致，跨表一致性靠应用保证 | **已定策略：业务表统一建外键**——`organization_id` 与"人"引用列（created_by/author_id/granted_by/actor_id）一律 `ondelete=RESTRICT`；删除策略见设计 15.3，交由显式删除流程。多态 `resource_id` 无法建外键 | 迁移 0006 | 已修 46937cf |
| n2 | 全仓库（后端） | `ruff format` 显示 12 个文件待格式化；CONTRIBUTING.md 只要求 `ruff check` | 引入 `ruff format` 作为格式基线，**单独一次提交**（刷新 12 个文件，勿与其他改动混合） | 1 次提交 | 已修 16c498a |
| n3 | `frontend/` | 前端无 eslint / prettier / 测试，静态检查空白 | 引入 eslint + prettier（+ 视需要加 vitest） | 需一次决策 | 已修 702b936（未加 vitest） |
| m7 | `frontend/src/App.tsx:16-46` | 前端类型手写，未从 OpenAPI 生成，接口变更易失同步 | 引入类型生成（如 openapi-typescript） | 需引入工具链 | 待办，建议接口稳定后再做 |

**建议顺序**：~~n2~~（16c498a）→ ~~m2~~（8e81047）→ ~~n3~~（702b936）→ ~~n1~~（46937cf）→ m7。

---

## 三、功能补齐（建议随 P5 一并做，不属缺陷修复）

| 编号 | 位置 | 影响 | 建议改法 | 改动面 | 状态 |
| --- | --- | --- | --- | --- | --- |
| **M3** | `authorization/service.py:29-38` | `auditor` 权限集为**空**、`legal_reviewer` 仅只读；需求 §3 要求的"审计人员查看审计与追溯记录""法律审核员审核资料版本、Wiki 与待审回答"均不可用（README 已如实声明未实现） | 新增 `audit.read` / `review.decide` 权限常量、对应接口与前端入口；与 P5 的 Wiki 审核发布一起设计 | 大（权限 + 接口 + 前端 + 迁移） | **已修**（V1.15）：新增 `review.decide`（`legal_reviewer` / `knowledge_admin`）与 `audit.read`，`auditor` 补上只读 + `audit.read`（**不含原件下载**）；Wiki 审核发布与待审队列接口已落地。**前端入口仍未做**（前端目前只有骨架） |
| m9 | `models.py:67-70` | `ck_wiki_draft_only` 把"仅草稿"固化为数据库约束，P5 实现发布时必须迁移移除 | 在代码注释与设计文档中标注该约束的移除计划 | 注释 | **已修**（V1.15，迁移 0016）：移除该约束，改为 `ck_wiki_revision_status` 限定四态；回滚时会先把非草稿修订改回 `draft` |

---

## 四、另需一次决策的基础设施项

以下不在本次审查发现内，但 `CODE_REVIEW.md` §4.2 已列为待引入，是"机制落地"的另一半：

| 项 | 作用 | 备注 |
| --- | --- | --- |
| CI（GitHub Actions） | 每个 PR 自动跑 lint + 测试 | **已引入**（`.github/workflows/ci.yml`，push/PR 触发；后端另跑 `alembic check`） |
| pre-commit | 提交前自动 lint / format | 依赖 n2 先定格式基线 |
| 依赖锁定 | 生成锁文件、固定镜像摘要 | README 已列为待办 |

CI 运行环境：已按 GitHub 托管 runner + 默认 PyPI/npm 源配置；若改用国内自建 runner，按 `ci.yml` 顶部注释改镜像。其余两项引入前仍需确认。

---

## 汇总

| 分组 | 项数 | 说明 |
| --- | --- | --- |
| 一、随手可修 | 0 | 已全部修复（89a82df），原 7 项约 20 行 |
| 二、需一次决策 | 1 | n1、n2、m2、n3 已完成；余类型生成（m7） |
| 三、P5 功能补齐 | 0 | M3 已修（V1.15，**前端入口未做**）、m9 已修（迁移 0016） |
| 四、基础设施 | 2 | CI 已引入；余 pre-commit、依赖锁定 |

**已关闭（不在本清单）**：B1、M1、M2、M4、M5、M6、合规缺口 —— 见 `CODE_REVIEW_BASELINE.md`。
