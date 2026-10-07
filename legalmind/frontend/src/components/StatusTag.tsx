import { Tag, Tooltip } from "antd";

import type { AnswerState } from "../api/types";
import { LEGAL_STATUS, REVIEW_STATUS, REVISION_STATUS, RUN_STATE } from "../app/statusLabels";

/**
 * 状态标签。**文字与颜色在 `app/statusLabels.ts`**（那里是数据，这里是组件）——
 * 颜色承载语义，集中一处定义，页面里不再各写各的。
 */

export function RunStateTag({ state }: { state: AnswerState }) {
  const meta = RUN_STATE[state] ?? { label: state, color: "default", hint: "" };
  return (
    <Tooltip title={meta.hint}>
      <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
        {meta.label}
      </Tag>
    </Tooltip>
  );
}

/** ⚠️ 非现行有效一律警示色：拿它当依据是 §8.3 要防的事。 */
export function LegalStatusTag({ status }: { status: string }) {
  const meta = LEGAL_STATUS[status] ?? { label: status, color: "default" };
  return (
    <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
      {meta.label}
    </Tag>
  );
}

/** ⚠️ `approved` 是**自动确认**，不等于人工复核（§17.2）。 */
export function ReviewStatusTag({ status }: { status: string }) {
  const meta = REVIEW_STATUS[status] ?? { label: status, color: "default" };
  return (
    <Tooltip title="自动确认不等于人工复核">
      <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
        {meta.label}
      </Tag>
    </Tooltip>
  );
}

export function RevisionStatusTag({ status }: { status: string }) {
  const meta = REVISION_STATUS[status] ?? { label: status, color: "default" };
  return (
    <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
      {meta.label}
    </Tag>
  );
}
