/**
 * 后端接口类型（手写）。
 *
 * ⚠️ **已知债务（CODE_REVIEW m7）**：这些类型是手写的，接口变更时可能悄悄失同步，
 * 应当从 `GET /openapi.json` 生成。等接口稳定后再引入生成工具。
 */

export type AccessScope = "organization" | "restricted";

/** 与后端 `app/modules/identity/schemas.py` 的 `CurrentUser` 对齐。 */
export type CurrentUser = {
  id: string;
  username: string;
  roles: string[];
  csrf_token: string;
  /**
   * 当前角色对应的**权限集合**（后端算好返回）。
   * 前端用它决定菜单与按钮是否渲染；**强制校验仍在后端**——不复制「角色→权限」映射。
   */
  permissions: string[];
  /** ok：可用业务接口；enroll：需先绑定 TOTP；verify：需输入验证码 */
  mfa_status: "ok" | "enroll" | "verify";
};

export type MfaEnrollment = { secret: string; otpauth_uri: string };

/** 设计 §9.1 的执行状态。终态之后不再转移。 */
export type AnswerState =
  | "CREATED"
  | "CLARIFYING"
  | "RETRIEVING"
  | "RERANKING"
  | "ASSEMBLING_EVIDENCE"
  | "GENERATING"
  | "VERIFYING"
  | "ANSWERED"
  | "PARTIAL"
  | "NEEDS_REVIEW"
  | "INSUFFICIENT_EVIDENCE"
  | "FAILED";

export type AnswerRun = {
  id: string;
  state: AnswerState;
  previous_state: AnswerState | null;
  published: boolean;
  blocked_by: string | null;
  review_required: boolean;
  /** 复核人（未复核为 null） */
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  evidence_count: number;
  seconds: number | null;
  created_at: string;
  /** 在等用户补充（CLARIFYING）——此时 `answer` 里是**澄清问题**，不是结论 */
  clarifying: boolean;
  content_available: boolean;
  /** 这次运行属于哪个会话（设计 §9.6 多轮追问）；单轮为 null */
  session_id: string | null;
  question: string | null;
  answer: string | null;
};

export type SubmitQuestion = {
  question: string;
  limit?: number;
  max_new_tokens?: number;
  model?: string | null;
  /** 会话 id（设计 §9.6）。**留空即单轮**——多轮是可选增强，不是主链路 */
  session_id?: string | null;
};

export type CitationAnchor = {
  instrument_id: string;
  legal_version_id: string;
  provision_version_id: string;
  provision_identity_id: string;
  artifact_id: string;
  chunk_id: string;
};

export type ProvisionHit = {
  instrument_title: string;
  instrument_type: string;
  jurisdiction: string;
  issuing_body: string;
  document_number: string | null;
  version_label: string;
  legal_status: string;
  review_status: string;
  promulgated_on: string | null;
  effective_from: string | null;
  effective_to: string | null;
  provision_type: string;
  provision_number: string;
  provision_display: string | null;
  text: string;
  text_sha256: string;
  citation: CitationAnchor;
};

export type SearchQuery = {
  instrument_title?: string | null;
  document_number?: string | null;
  article_number?: string | null;
  keyword?: string | null;
  semantic?: string | null;
  instrument_types?: string[] | null;
  include_not_yet_effective?: boolean;
  limit?: number;
};

export type SearchResponse = {
  hits: ProvisionHit[];
  truncated: boolean;
  /** exact / keyword / vector——级联下要知道这次是原文命中还是语义兜底 */
  path: string;
};

export type WikiPage = {
  id: string;
  title: string;
  head_revision: number;
  published_revision: number | null;
  review_due_at: string | null;
  review_due_reason: string | null;
  access_scope: AccessScope;
  created_at: string;
};

export type WikiRevision = {
  id: string;
  page_id: string;
  number: number;
  body: string;
  status: string;
  author_id: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  created_at: string;
};

export type WikiCitation = { provision_version_id: string; created_at: string };

/** SSE 事件（设计 §9.4）。`answer` **只在终态**推送；门禁未过时里面是拒答说明。 */
export type StreamEvent =
  | { name: "state"; data: { state: AnswerState; previous_state: AnswerState | null; at: string } }
  | {
      name: "answer";
      data: {
        state: AnswerState;
        published: boolean;
        blocked_by: string | null;
        review_required: boolean;
        evidence_count: number;
        content_available: boolean;
        question: string | null;
        answer: string | null;
        note: string | null;
      };
    }
  | { name: "clarify"; data: { state: AnswerState; question: string | null; resume: string } }
  | { name: "timeout"; data: { state: AnswerState; detail: string } }
  | { name: "error"; data: { detail: string } }
  | { name: "done"; data: Record<string, never> };

// ---- 文献管理（设计 §7、§13；《前端界面说明》§5.8）----

export type SourceType = "official" | "republished" | "internal";
export type TrustLevel = "high" | "medium" | "low";
export type Sensitivity = "public" | "internal" | "confidential";
// `AccessScope` 文件开头已有定义（后端 `documents/schemas.py` 的同一个字面量），这里不重复声明

/** 来源登记（FR-01）。**授权说明必填**——没有它就不能入库（设计 §20.3）。 */
export type SourceRecord = {
  id: string;
  name: string;
  source_type: SourceType;
  trust_level: TrustLevel;
  url: string | null;
  publisher: string | null;
  license_note: string;
  last_checked_at: string | null;
  created_at: string;
};

/** 原件。**这是不可变的登记记录**，不是解析产物（设计 §5.3）。 */
export type DocumentRecord = {
  id: string;
  source_id: string;
  original_filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  sensitivity: Sensitivity;
  access_scope: AccessScope;
  acquired_at: string | null;
  created_by: string;
  created_at: string;
};

/** 导入的返回：原件已登记，**解析任务只是登记了、还没跑**（202）。 */
export type ImportResult = {
  document: DocumentRecord;
  job_id: string;
};

/** 解析任务。`status` 见 `jobs/service.py`；**资源不足会记 `resource_exhausted`**，不伪装成功（FR-13）。 */
export type JobRecord = {
  id: string;
  job_type: string;
  status: string;
  attempt_count: number;
  max_attempts: number;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};
