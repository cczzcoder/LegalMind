import { api } from "./client";
import type { AccessScope, WikiCitation, WikiPage, WikiRevision } from "./types";

export function listPages(limit = 100) {
  return api<WikiPage[]>(`/wiki/pages?limit=${limit}`);
}

export function getPage(pageId: string) {
  return api<WikiPage>(`/wiki/pages/${pageId}`);
}

export function listRevisions(pageId: string, limit = 100) {
  return api<WikiRevision[]>(`/wiki/pages/${pageId}/revisions?limit=${limit}`);
}

export function getPublished(pageId: string) {
  return api<WikiRevision>(`/wiki/pages/${pageId}/published`);
}

export function createPage(body: { title: string; body: string; access_scope: AccessScope }) {
  return api<WikiRevision>("/wiki/pages", { method: "POST", body: JSON.stringify(body) });
}

/** 保存新修订；`expected_revision` 用于乐观并发——**409 表示别人先改了，不自动合并**。 */
export function createRevision(pageId: string, body: { expected_revision: number; body: string }) {
  return api<WikiRevision>(`/wiki/pages/${pageId}/revisions`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function listCitations(pageId: string, revisionNumber: number) {
  return api<WikiCitation[]>(`/wiki/pages/${pageId}/revisions/${revisionNumber}/citations`);
}

/** 覆盖式登记引用；引用的是**具体条款版本**（设计 §5.3）。 */
export function setCitations(pageId: string, revisionNumber: number, ids: string[]) {
  return api<WikiCitation[]>(`/wiki/pages/${pageId}/revisions/${revisionNumber}/citations`, {
    method: "PUT",
    body: JSON.stringify({ provision_version_ids: ids }),
  });
}

export function submitRevision(pageId: string, revisionNumber: number) {
  return api<WikiRevision>(`/wiki/pages/${pageId}/revisions/${revisionNumber}/submit`, {
    method: "POST",
  });
}

export function publishRevision(pageId: string, revisionNumber: number) {
  return api<WikiRevision>(`/wiki/pages/${pageId}/revisions/${revisionNumber}/publish`, {
    method: "POST",
  });
}

/** 驳回；`note` 是给作者的理由，也是事后审计要看的东西。 */
export function rejectRevision(pageId: string, revisionNumber: number, note: string | null) {
  return api<WikiRevision>(`/wiki/pages/${pageId}/revisions/${revisionNumber}/reject`, {
    method: "POST",
    body: JSON.stringify({ note }),
  });
}

export function listPending(limit = 100) {
  return api<{ page_id: string; revision_number: number; title: string }[]>(
    `/wiki/revisions/pending?limit=${limit}`,
  );
}

/** 待复核标记：引用的依据被取代或正文变了（设计 §10.2）。 */
export function listStale(limit = 100) {
  return api<WikiPage[]>(`/wiki/pages/stale?limit=${limit}`);
}
