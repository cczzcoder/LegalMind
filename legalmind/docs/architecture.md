# LegalMind 架构基线

## 核心原则

1. PostgreSQL 与原始文件是权威记录。
2. 搜索和向量索引是可重建的派生数据。
3. 法律版本与 Wiki 修订分别建模。
4. 模型不得直接发布知识或决定访问权限。
5. 证据不足时追问、限定回答、拒答或转人工。
6. 完成核验后才能发布正式答案。

## 当前实现

router -> service -> SQLAlchemy

Wiki 的业务变更、AuditEvent、OutboxEvent 在同一个事务提交。
现阶段 Outbox 只记录事件，不投递，不宣称已实现可靠消息消费。
现阶段审计只保存事件，不宣称不可篡改。

## 后续模块

identity:
  正式身份、会话、账号生命周期。

authorization:
  RBAC、对象权限、检索范围及权限版本。

sources / documents:
  来源、许可、文件、哈希、解析修订和原文定位。

legal_corpus:
  LegalInstrument、LegalVersion、ProvisionIdentity、
  ProvisionVersion、ProvisionRelation、ApplicabilityRecord。

retrieval:
  精确查询、关键词查询、向量召回、融合、重排序、
  上下文补齐和授权复核。

answering:
  问题澄清、结构化主张、引用校验、语义核验、
  风险分流、拒答。

review:
  资料、Wiki 和高风险回答审核。

evaluation:
  人工金标准、回归测试、版本和权限专项。

## 下一阶段

先补正式权限，再补法律原文与版本管理。
随后接入后台任务和检索。
最后开放证据约束问答与 Wiki 正式发布。
