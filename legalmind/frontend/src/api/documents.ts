import { api } from "./client";
import type {
  AccessScope,
  DocumentRecord,
  ImportResult,
  JobRecord,
  Sensitivity,
  SourceRecord,
} from "./types";

/** 来源登记列表（设计 §13）。**授权说明在卡片上要显示出来**——它是入库依据（§20.3）。 */
export function listSources() {
  return api<SourceRecord[]>("/sources");
}

/** 原件列表。可见性由后端按组织与授权范围过滤（设计 §11.2）。 */
export function listDocuments() {
  return api<DocumentRecord[]>("/documents");
}

/**
 * 导入一份原件（设计 §7.1）。
 *
 * ⚠️ **请求体是文件原始字节**（`application/octet-stream`），**不是 multipart**——后端的
 * `read_body(request)` 直接读裸字节。所以这里**不能**用 `FormData`，直接把 `File` 当 body 发。
 *
 * ⚠️ 返回 **202**：原件登记了、**解析任务只是登记，还没执行**（要 worker 在跑）。
 */
export function importDocument(params: {
  sourceId: string;
  file: File;
  sensitivity: Sensitivity;
  accessScope?: AccessScope;
}) {
  const query = new URLSearchParams({
    source_id: params.sourceId,
    filename: params.file.name,
    sensitivity: params.sensitivity,
    access_scope: params.accessScope ?? "organization",
  });
  return api<ImportResult>(`/documents?${query.toString()}`, {
    method: "POST",
    // 显式指定，别让封装盖成 application/json（见 client.ts 的注释）
    headers: { "Content-Type": "application/octet-stream" },
    body: params.file,
  });
}

/** 任务进度。解析要几十秒到几分钟，所以前端轮询它。 */
export function getJob(jobId: string) {
  return api<JobRecord>(`/jobs/${jobId}`);
}
