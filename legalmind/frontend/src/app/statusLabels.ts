import type { AnswerState } from "../api/types";

/**
 * 状态标签的**文字与颜色**。
 *
 * 与组件分文件（`react-refresh` 要求一个文件只导出组件），也便于别处只取文字：
 * 颜色承载语义，所以集中一处定义，页面里不再各写各的。
 *
 * 本项目的中文语境约定：**警示色（红/橙）表示「不能当依据用」**——
 * 效力状态非现行有效、运行未通过门禁、审核未通过，都用它。
 */

export const RUN_STATE: Record<AnswerState, { label: string; color: string; hint: string }> = {
  CREATED: { label: "已创建", color: "default", hint: "已受理，等待 worker 领取" },
  CLARIFYING: { label: "待补充", color: "gold", hint: "问题太笼统，正在等用户补充" },
  RETRIEVING: { label: "检索中", color: "processing", hint: "在授权范围内检索条款" },
  RERANKING: { label: "重排序", color: "processing", hint: "保留状态：重排序未进默认" },
  ASSEMBLING_EVIDENCE: { label: "装配证据", color: "processing", hint: "按上下文预算装配证据" },
  GENERATING: { label: "生成中", color: "processing", hint: "本地模型正在生成结论" },
  VERIFYING: { label: "核验中", color: "processing", hint: "检查引用、条号、数值是否落在证据里" },
  ANSWERED: { label: "已作答", color: "success", hint: "结论已发布" },
  PARTIAL: { label: "限定范围作答", color: "warning", hint: "证据装不下，已限制回答范围" },
  NEEDS_REVIEW: { label: "待人工判读", color: "error", hint: "被门禁拦下，不当正式答案发布" },
  INSUFFICIENT_EVIDENCE: {
    label: "依据不足",
    color: "error",
    hint: "没有检索到相关条文，未调用模型",
  },
  FAILED: { label: "失败", color: "error", hint: "运行失败（如本地模型不可用）" },
};

/** 状态的中文名（时间线等地方要单独用）。 */
export function runStateLabel(state: AnswerState): string {
  return RUN_STATE[state]?.label ?? state;
}

export const LEGAL_STATUS: Record<string, { label: string; color: string }> = {
  effective: { label: "现行有效", color: "success" },
  not_yet_effective: { label: "已公布未生效", color: "warning" },
  repealed: { label: "已废止", color: "error" },
  superseded: { label: "已被取代", color: "error" },
  unknown: { label: "效力未知", color: "default" },
};

export const REVIEW_STATUS: Record<string, { label: string; color: string }> = {
  approved: { label: "已确认", color: "success" },
  pending: { label: "待确认", color: "warning" },
  rejected: { label: "已驳回", color: "error" },
};

export const REVISION_STATUS: Record<string, { label: string; color: string }> = {
  draft: { label: "草稿", color: "default" },
  submitted: { label: "待审核", color: "processing" },
  published: { label: "已发布", color: "success" },
  rejected: { label: "已驳回", color: "error" },
};
