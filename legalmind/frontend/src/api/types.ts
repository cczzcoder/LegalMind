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
  question: string | null;
  answer: string | null;
};

export type SubmitQuestion = {
  question: string;
  limit?: number;
  max_new_tokens?: number;
  model?: string | null;
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
