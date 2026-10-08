import { ApiError, api } from "./client";
import type {
  AccessScope,
  DirectoryEntry,
  DocumentRecord,
  GrantRecord,
  ImportResult,
  JobRecord,
  Sensitivity,
  SourceInput,
  SourceRecord,
} from "./types";

/** 来源登记列表（设计 §13）。**授权说明在卡片上要显示出来**——它是入库依据（§20.3）。 */
export function listSources() {
  return api<SourceRecord[]>("/sources");
}

/** 登记来源（需 `source.manage`）。**`license_note` 必填**——没有授权说明的来源不能用来入库。 */
export function createSource(input: SourceInput) {
  return api<SourceRecord>("/sources", { method: "POST", body: JSON.stringify(input) });
}

/** 编辑来源（需 `source.manage`）；改的是登记信息，不动已入库的原件。 */
export function updateSource(sourceId: string, input: SourceInput) {
  return api<SourceRecord>(`/sources/${sourceId}`, {
    method: "PUT",
    body: JSON.stringify(input),
  });
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

/**
 * 可授权对象名单（需 `document.grant` 或 `wiki.grant`）。
 *
 * ⚠️ **不能用 `GET /users`**：那个要 `user.manage`（只有 `system_admin` 有），而授权权在
 * `knowledge_admin` 手里——有授权权的人本来列不出用户，只能靠粘贴 UUID 发授权。
 */
export function listDirectory() {
  return api<DirectoryEntry[]>("/users/directory");
}

/** 某个原件的授权名单（需 `document.grant`）。 */
export function listGrants(documentId: string) {
  return api<GrantRecord[]>(`/documents/${documentId}/grants`);
}

export function grantAccess(documentId: string, userId: string) {
  return api<GrantRecord>(`/documents/${documentId}/grants/${userId}`, { method: "PUT" });
}

export function revokeAccess(documentId: string, userId: string) {
  return api<void>(`/documents/${documentId}/grants/${userId}`, { method: "DELETE" });
}

/** 改可见范围（需 `document.grant`）。`restricted` 只对授权名单里的人可见。 */
export function setAccessScope(documentId: string, accessScope: AccessScope) {
  return api<DocumentRecord>(`/documents/${documentId}/access`, {
    method: "PUT",
    body: JSON.stringify({ access_scope: accessScope }),
  });
}

/**
 * 下载原件（需 `document.download`）。
 *
 * ⚠️ **不用 `<a href>` 直接指过去**：那样出错时浏览器会把整页导航到一段 JSON 错误体上，
 * 用户看到的是一个白页。这里自己取 blob，**错误走同一套错误体**（`{code, message, trace_id}`），
 * 由调用方按平时的方式提示。
 *
 * ⚠️ **`document.download` 与 `document.read` 是两项权限**：看得见清单不等于能取走原件
 * （审计人员就没有下载权——核对记录不等于取走原件）。
 */
export async function downloadDocument(documentId: string, filename: string): Promise<void> {
  const response = await fetch(`/api/v1/documents/${documentId}/content`, {
    credentials: "same-origin",
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      message?: string;
      code?: string;
      trace_id?: string;
    } | null;
    throw new ApiError(
      response.status,
      payload?.message ?? "下载失败",
      payload?.code,
      payload?.trace_id,
    );
  }

  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  // ⚠️ 立刻回收：不回收的话每下载一次就多占一份内存，直到页面刷新
  URL.revokeObjectURL(url);
}
