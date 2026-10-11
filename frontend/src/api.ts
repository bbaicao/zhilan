/**
 * 条目 3.3：引用解析（正则 / 有效编号集合 / 兜底政策 / mode 判定）与 `RefItem` 类型
 * 的唯一真相源移到 `./citation`——那里是纯 TS、零运行时 import，可以被 esbuild 单独
 * 打包成 `.mjs` 做「前后端两侧同构」的逐例比对。本文件只做 re-export，既有导出名
 * （`RefItem` / `asRefs` / `citedRefs`）与签名一律不变，旧调用点不用动。
 */
import type { RefItem } from "./citation";
import { scheduleRevoke, triggerBlobDownload } from "./download";

export type { CitationMode, RefItem } from "./citation";
// `citedRefs` 的实现在 `./citation`（与后端 `cited_refs` 同规则、含兜底），此处 re-export；
// 其余三个是给组件与比对脚本用的入口（`resolveCitations` 还要给 mode）。
export { asRefs, citedRefs, citeMarkers, resolveCitations, validCiteSet } from "./citation";

export class ApiError extends Error {
  payload?: Record<string, unknown>;

  constructor(message: string, payload?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.payload = payload;
  }
}

/** 重复内容上传：409，可据 existingId 走「覆盖重建」。 */
export function duplicateExistingId(err: unknown): number | null {
  if (!(err instanceof ApiError)) return null;
  const id = err.payload?.existing_id;
  return typeof id === "number" ? id : null;
}

export type Health = {
  status: string;
  database: string;
  redis: string;
  database_backend?: "sqlite" | "mysql";
  redis_backend?: "in_process";
  llm_configured: boolean;
  llm_provider: string | null;
  llm_model: string | null;
  embed_provider: string | null;
  embed_model: string | null;
  vision_provider: string | null;
  vision_model: string | null;
  clean?: CleanStatus;
};

export type CleanStatus = {
  enabled: boolean;
  rule_fingerprint: string | null;
  stale_documents: number;
  needs_reindex: boolean;
  hint: string;
};

export type Note = {
  id: number;
  title: string;
  content: string;
  category: string;
  created_at: string;
  updated_at: string;
  /** 条目 29：AI 生成的笔记带来源文档（手写笔记没有）；列表接口可能不回这些字段 */
  source_document_id?: number | null;
  tags?: string[] | null;
  generation_meta?: Record<string, unknown> | null;
};

export type DocumentItem = {
  id: number;
  filename: string;
  file_type: string;
  status: string;
  chunk_count: number;
  vectorized: boolean;
  vector_error?: string | null;
  created_at: string;
  /** 后端列表本就返回分类与标签，前端类型此前漏标（任务清单 17 导出范围要用） */
  category?: string;
  tags?: string[] | null;
  message?: string | null;
  /** 条目 193：知识库归属（后端 DocumentOut.kb_id: int 必填，_to_out 恒发） */
  kb_id: number;
};

export type ChatItem = {
  id: number;
  title: string;
  created_at: string;
  /** 条目 40：分组归属（未分组为 null）；旧后端响应可能没有该字段 */
  group_id?: number | null;
  /** 条目 40：非空 = 已归档（从「最近」隐藏，进「已归档」视图）；旧后端响应可能没有 */
  archived_at?: string | null;
};

/** 引用来源卡片的数据（条目 3.3 起定义在 `./citation`，此处 re-export 保持旧路径可用）。 */

export type WikiLinkHit = {
  type: "note" | "document";
  id: number;
  title: string;
  snippet: string;
  category?: string;
  direction: "incoming" | "outgoing";
};

export type WikiLinksOut = { items: WikiLinkHit[] };

function wikiQuery(type: "note" | "document", id?: number | null, title?: string | null) {
  const params = new URLSearchParams({ type });
  if (id != null) params.set("id", String(id));
  else if (title) params.set("title", title);
  return params.toString();
}

export function getBacklinks(type: "note" | "document", id?: number | null, title?: string | null) {
  return request<WikiLinksOut>(`/api/wiki/backlinks?${wikiQuery(type, id, title)}`);
}

export function getRelated(type: "note" | "document", id?: number | null, title?: string | null) {
  return request<WikiLinksOut>(`/api/wiki/related?${wikiQuery(type, id, title)}`);
}

/** 条目 16：关系图谱节点（笔记 / 文档 / 未创建幽灵标题）。 */
export type GraphNode = {
  id: string;
  type: "note" | "document" | "ghost";
  source_id: number | null;
  title: string;
  category: string;
  tags: string[];
  kb_id: number;
  degree: number;
};

export type GraphEdge = {
  source: string;
  target: string;
  kind: "wiki" | "source";
};

export type GraphData = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  stats: {
    notes: number;
    documents: number;
    ghosts: number;
    edges: number;
    isolated: number;
  };
};

export function getGraph() {
  return request<GraphData>("/api/graph");
}

/** 元数据过滤范围（M4）：留空即该维度不限制，多个维度之间是 AND。 */
export type ChatFilters = {
  kb_ids?: number[];
  source_types?: string[];
  categories?: string[];
  tags?: string[];
  created_after?: string;
  created_before?: string;
};

/** 过滤范围的中文描述：chips 与历史消息回显共用一套文案。 */
export function describeFilters(filters?: ChatFilters | null): string {
  if (!filters) return "";
  const parts: string[] = [];
  if (filters.kb_ids?.length) {
    parts.push("知识库 " + filters.kb_ids.map((id) => `#${id}`).join("、"));
  }
  if (filters.source_types?.length) {
    parts.push(filters.source_types.map((item) => (item === "note" ? "笔记" : "文档")).join("、"));
  }
  if (filters.categories?.length) parts.push("分类：" + filters.categories.join("、"));
  if (filters.tags?.length) parts.push("标签：" + filters.tags.join("、"));
  if (filters.created_after || filters.created_before) parts.push("时间范围");
  return parts.join("；");
}

export function hasFilters(filters?: ChatFilters | null): boolean {
  return Boolean(
    filters &&
      (filters.kb_ids?.length ||
        filters.source_types?.length ||
        filters.categories?.length ||
        filters.tags?.length ||
        filters.created_after ||
        filters.created_before),
  );
}

/** 深度思考（条目 19）：这次回答的思考是怎么来的（厂商给的字段、是否自动加深、为什么）。 */
export type GenerationStatus = "completed" | "incomplete" | "stopped" | "failed";

export type ThinkingMeta = {
  generation_status?: GenerationStatus;
  finish_reason?: string | null;
  /** 厂商的思考参数名，如 thinking.type */
  param?: string;
  /** 本次实际发的档位 */
  value?: ReasoningValue;
  /** 复杂问题自动加深（不是用户设的档） */
  auto_deepen?: boolean;
  /** 判成复杂的理由（自动加深时才有） */
  reasons?: string[];
  /** 人话说明（界面直接显示） */
  note?: string;
  /** 思考内容来自哪个字段，如 reasoning_content */
  field?: string;
  chars?: number;
  truncated?: boolean;
};

/** 条目 18：Agent（ReAct / 计划执行）的一步，出现在首个回答 token 之前流式下发。 */
export type AgentStep = {
  /** plan | thought | tool | observation | note */
  kind: string;
  title: string;
  detail?: string;
  tool?: string | null;
  args?: Record<string, unknown> | null;
  ok?: boolean | null;
  ms?: number | null;
  /** 条目 192：与 tool/result 对账用（后端 add_step 恒下发该键；非工具步骤恒 null） */
  tool_call_id?: string | null;
};

/** 条目 18：一次 Agent 回答的完整轨迹；非 Agent 回答恒 null。 */
export type AgentTrace = {
  /** react | plan_execute */
  mode: string;
  steps: AgentStep[];
};

/** 条目 18：Agent 模式的界面文案（对话页菜单与设置弹窗共用）；未登记的值原样显示。 */
export const AGENT_MODE_LABELS: Record<string, string> = {
  off: "直答",
  react: "ReAct 工具循环",
  plan_execute: "计划执行",
};

/** 条目 53：纯图消息在库里 content 落的就是这个占位串（后端 `/chat{,/stream}` 写的）。
 *  界面渲染时若该条消息带着图、正文又恰好等于它，就不再把这行占位字画出来——
 *  真正的「我发了图」由缩略图本身表达。历史消息与旧客户端都不受影响。 */
export const IMAGE_ONLY_PLACEHOLDER = "（图片）";

export type ChatMessage = {
  id: number;
  role: string;
  content: string;
  refs?: RefItem[] | Record<string, unknown> | null;
  kind?: string | null;
  generation_status?: GenerationStatus;
  finish_reason?: string | null;
  /** 该次提问用的过滤范围（回归检查）：历史消息据此还原当时的检索范围。 */
  filters?: ChatFilters | null;
  /** 厂商给的思考内容原文（条目 19）；厂商不给就是空/缺省，界面不编 */
  reasoning?: string | null;
  /** 这次思考的元数据（条目 19） */
  thinking?: ThinkingMeta | null;
  /** 条目 35：本条消息启用的技能 id；历史消息可能没有 */
  skill_ids?: string[] | null;
  /** 条目 18：Agent 过程轨迹（非 Agent 回答为 null；历史消息回读同形状） */
  agent_trace?: AgentTrace | null;
  /** 条目 53：用户消息随消息发出的图片（有序 data URL，可点开看大图）；
   *  助理消息与 0017 之前的存量消息为 null/缺省 */
  images?: string[] | null;
  /** 阶段 3 条目 3.5：答案级来源标记（角落标签的承载字段，取值由后端分路结果决定）：
   *  `kb` 命中知识库 / `general` 库外回答（模型自身知识）/ `empty_scope` 范围为空 /
   *  `clarify` 澄清追问；历史存量消息与停止轮（`kind="stopped"`）为 null/缺省。
   *  **前端不得用「有没有 refs」反推来源**，也不得改写文案（渲染见条目 3.6）。 */
  answer_source?: string | null;
  /** 阶段 4 条目 4.3：该轮发生过崩溃恢复（工具已开始但没留下结果）——只读后端字段，
   *  由 recovery/synthesized 事件现算，刷新后仍在。历史消息与未恢复的轮次缺省 false。 */
  recovered?: boolean;
  created_at: string;
};

export type ChatDetail = ChatItem & {
  messages: ChatMessage[];
  /** 条目 24 记忆压缩：当前摘要全文（未压缩为 null） */
  context_summary?: string | null;
  /** 条目 24 记忆压缩：已压缩覆盖到的最大消息 id（未压缩为 null） */
  compressed_up_to?: number | null;
};

export type SearchHit = {
  id: number;
  title: string;
  snippet: string;
  category?: string | null;
  document_id?: number | null;
  /** 条目 15：结果元数据，可能为空（列表接口不一定回） */
  created_at?: string | null;
  /** 条目 15：结果标签，可能是空数组 */
  tags?: string[];
};

/** 条目 15：搜索词里被解析出来的一个条件：kind 为 tag / category / file / date。 */
export type SearchCondition = {
  kind: string;
  label: string;
  value: string;
};

/** 条目 15：后端对这次搜索词的解析结果，界面据此回显条件与警告。 */
export type SearchQueryInfo = {
  raw: string;
  keywords: string[];
  conditions: SearchCondition[];
  warnings: string[];
  describe: string;
};

export type SearchOut = {
  notes: SearchHit[];
  chunks: SearchHit[];
  /** 条目 15：搜索词为空时后端不回这个字段（或回 null） */
  query?: SearchQueryInfo | null;
  /** 条目 15：没命中或条件有问题时的提示，其余情况为空 */
  hint?: string;
};

export type ChatResponse = {
  chat_id: number;
  message: string;
  refs: RefItem[];
  kind?: string;
  generation_status?: GenerationStatus;
  finish_reason?: string | null;
  /** 思考内容原文与元数据（条目 19） */
  reasoning?: string;
  thinking?: ThinkingMeta | null;
  /** 条目 35：本条消息启用的技能 */
  skill_ids?: string[];
  /** 条目 18：Agent 过程轨迹（契约 §7.1 九字段之一）——**非流式 `/chat` 恒为 null**：
   *  契约明文「流式接口才落 agent_trace」。回归修复：本类型此前漏声明它。 */
  agent_trace?: AgentTrace | null;
  /** 阶段 3 条目 3.5：答案级来源标记（`kb` / `general` / `empty_scope` / `clarify`）；
   *  特性开关关闭时为 null。渲染见条目 3.6（只读本字段，不用 refs 反推） */
  answer_source?: string | null;
};

const OFFLINE = "后端服务未启动，请先运行后端";

async function parse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `请求失败（${res.status}）`;
    let payload: Record<string, unknown> | undefined;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string" && body.detail.trim()) {
        detail = body.detail;
      } else if (body.detail && typeof body.detail === "object") {
        const obj = body.detail as Record<string, unknown>;
        payload = obj;
        if (typeof obj.message === "string" && obj.message.trim()) {
          detail = obj.message;
        }
      }
    } catch {
      /* keep fallback */
    }
    throw new ApiError(detail, payload);
  }
  if (res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  try {
    const res = await fetch(url, init);
    return await parse<T>(res);
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError(OFFLINE);
  }
}

function jsonBody(data: unknown): RequestInit {
  return {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  };
}

export function getHealth() {
  return request<Health>("/api/health");
}

export function listNotes() {
  return request<Note[]>("/api/notes");
}

export function createNote(data: {
  title: string;
  content: string;
  category: string;
  /** 条目 29：AI 生成笔记的来源文档 */
  source_document_id?: number;
  tags?: string[] | null;
  generation_meta?: Record<string, unknown> | null;
}) {
  return request<Note>("/api/notes", { method: "POST", ...jsonBody(data) });
}

export function updateNote(
  id: number,
  data: {
    title?: string;
    content?: string;
    category?: string;
    tags?: string[] | null;
    generation_meta?: Record<string, unknown> | null;
  },
) {
  return request<Note>(`/api/notes/${id}`, { method: "PUT", ...jsonBody(data) });
}

export function deleteNote(id: number) {
  return request<{ ok: boolean }>(`/api/notes/${id}`, { method: "DELETE" });
}

export function listDocuments() {
  return request<DocumentItem[]>("/api/documents");
}

export type KnowledgeBaseItem = {
  id: number;
  name: string;
  kind: string;
  path_or_url: string;
  collection: string;
  read_only: boolean;
  status: string;
  last_sync_at?: string | null;
};

export function listKnowledgeBases() {
  return request<KnowledgeBaseItem[]>("/api/knowledge-bases");
}

export function createKnowledgeBase(data: { name: string; kind: "imported" | "external"; path_or_url: string; collection?: string }) {
  return request<KnowledgeBaseItem>("/api/knowledge-bases", { method: "POST", ...jsonBody(data) });
}

export function uploadDocument(file: File, overwrite = false) {
  const body = new FormData();
  body.append("file", file);
  const query = overwrite ? "?overwrite=true" : "";
  return request<DocumentItem>(`/api/documents/upload${query}`, { method: "POST", body });
}

/* ---- 条目 39：导入文件夹 → AI 自动整理成笔记 ---- */

export type ImportFolderFile = {
  path: string;
  name: string;
  size_bytes: number;
};

export type ImportFolderSkipped = {
  path: string;
  name: string;
  reason: string;
};

/** truncated = 文件夹里文件太多，只回前 200 个 */
export type ImportFolderScanOut = {
  path: string;
  files: ImportFolderFile[];
  skipped: ImportFolderSkipped[];
  truncated: boolean;
};

export function scanImportFolder(path: string, recursive: boolean) {
  return request<ImportFolderScanOut>("/api/documents/import-folder/scan", {
    method: "POST",
    ...jsonBody({ path, recursive }),
  });
}

/** 按路径导入单个文件（形状同上传）；409 内容重复时用 duplicateExistingId 取库内文档 id 复用。 */
export function importFileFromFolder(path: string) {
  return request<DocumentItem>("/api/documents/import-file", {
    method: "POST",
    ...jsonBody({ path }),
  });
}

export type ReindexOut = {
  documents: number;
  notes: number;
  vectors: number;
  failed: number;
  orphans_removed: number;
  /** 其中顺带按当前清洗规则重新清洗过的文档数 */
  recleaned: number;
  messages: string[];
};

/** 重建向量索引：document_id 为空表示全量（含笔记与孤儿向量清理）。 */
export function reindexDocuments(documentId?: number | null) {
  return request<ReindexOut>("/api/documents/reindex", {
    method: "POST",
    ...jsonBody({ document_id: documentId ?? null }),
  });
}

export function deleteDocument(id: number) {
  return request<{ ok: boolean }>(`/api/documents/${id}`, { method: "DELETE" });
}

export type CleanStep = {
  step: string;
  lines_removed: number;
  chars_removed: number;
  detail?: Record<string, unknown>;
};

export type CleanReport = {
  enabled: boolean;
  rule_fingerprint: string;
  raw_chars: number;
  clean_chars: number;
  raw_lines: number;
  clean_lines: number;
  steps: CleanStep[];
  flagged: { reason: string; line: number; length: number }[];
  flagged_count: number;
};

export type CleanDetail = {
  id: number;
  filename: string;
  raw_content: string;
  clean_content: string;
  report: CleanReport | null;
  chunk_count: number;
};

/** 单篇文档的原始文本与清洗报告（查看原文 / 查看清洗报告）。 */
export function documentCleanReport(id: number) {
  return request<CleanDetail>(`/api/documents/${id}/clean-report`);
}

/** 清洗 dry-run：返回清洗前后对照与每步统计，不入库。 */
export function previewCleaning(file: File) {
  const body = new FormData();
  body.append("file", file);
  return request<{
    filename: string;
    file_type: string;
    chunk_count: number;
    content_hash: string;
    raw_content: string;
    clean_content: string;
    report: CleanReport;
    chunks: string[];
    removed_ratio: number;
  }>("/api/documents/clean-preview", { method: "POST", body });
}

/* ---- 条目 29：上传资料 → AI 生成笔记（草稿制，不落库） ---- */

/** 文档生成的笔记草稿：保存时才经 createNote / updateNote 落库。 */
export type GeneratedNoteDraft = {
  /** 纯文本标题（不含 #） */
  title: string;
  /** 完整 Markdown 正文（不含首行标题；末尾有「## 来源」一节，内嵌 [[文件名]] 双链） */
  content: string;
  /** 预填文档分类，可能为空串 */
  category: string;
  tags: string[] | null;
  source_document_id: number;
  source_filename: string;
  completeness: {
    complete: boolean;
    checker: "llm" | "regex";
    checked: number;
    missing: { item: string; kind: "number" | "date" | "entity" | "negation"; context: string }[];
  };
};

/** 由文档生成笔记草稿（可能耗时几十秒；草稿不入库，保存走 createNote / updateNote）。 */
export function generateNoteFromDoc(documentId: number) {
  return request<GeneratedNoteDraft>(`/api/documents/${documentId}/generate-note`, { method: "POST" });
}

/** 追加（输出全量流式化）：流式生成笔记草稿——正文增量到达即回调，生成中就能看到。
 *  onStatus 为阶段播报（长文档逐段要点 / 合成 / 核对）；done 携带与非流式端点同形状的
 *  完整草稿（标题已剥、来源节已兜底、完整性已核对）。关闭弹窗用 signal 取消（后端
 *  会取消在途生成）。token 是剥标题处理前的原始 Markdown（首行「# 标题」）。 */
export async function generateNoteStream(
  documentId: number,
  handlers: {
    onStatus?(detail: string): void;
    onToken?(text: string): void;
    onDone?(draft: GeneratedNoteDraft): void;
  },
  options?: { signal?: AbortSignal },
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`/api/documents/${documentId}/generate-note/stream`, {
      method: "POST",
      signal: options?.signal,
    });
  } catch (err) {
    if (err instanceof ApiError) throw err;
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(OFFLINE);
  }
  if (!res.ok) {
    await parse(res);
  }
  if (!res.body) {
    throw new ApiError("笔记生成流程异常结束，请重试。");
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let terminal = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const blocks = buf.split("\n\n");
      buf = blocks.pop() || "";
      for (const block of blocks) {
        const line = block.split("\n").find((item) => item.startsWith("data: "));
        if (!line) continue;
        let ev: {
          type?: string;
          text?: string;
          detail?: string;
        } & Partial<GeneratedNoteDraft>;
        try {
          ev = JSON.parse(line.slice(6)) as typeof ev;
        } catch {
          continue;
        }
        if (ev.type === "error") {
          throw new ApiError(ev.detail || "生成笔记失败，请稍后再试");
        }
        if (ev.type === "status" && ev.detail) {
          handlers.onStatus?.(ev.detail);
        }
        if (ev.type === "token" && ev.text) {
          handlers.onToken?.(ev.text);
        }
        if (ev.type === "done") {
          terminal = true;
          handlers.onDone?.({
            title: ev.title ?? "",
            content: ev.content ?? "",
            category: ev.category ?? "",
            tags: ev.tags ?? null,
            source_document_id: ev.source_document_id ?? documentId,
            source_filename: ev.source_filename ?? "",
            completeness: ev.completeness ?? {
              complete: true,
              checker: "regex",
              checked: 0,
              missing: [],
            },
          });
          return;
        }
      }
    }
    if (!terminal) throw new ApiError("笔记生成流程异常结束，请重试。");
  } finally {
    reader.releaseLock();
  }
}

export function searchAll(q: string) {
  return request<SearchOut>(`/api/search?q=${encodeURIComponent(q)}`);
}

export function listChats() {
  return request<ChatItem[]>("/api/chats");
}

// 条目 24：ChatView 拿不到当前 chatId（App 不传），getChat 的最近一次结果先记在这里，
// 供消息列表顶部的「已压缩早期对话」折叠条读取；每次打开 / 回读会话都会刷新。
let latestChatDetail: ChatDetail | null = null;

/** 最近一次 getChat 拉到的会话详情（含压缩摘要字段）；从未拉过时为 null。 */
export function peekLatestChatDetail(): ChatDetail | null {
  return latestChatDetail;
}

export async function getChat(id: number) {
  const detail = await request<ChatDetail>(`/api/chats/${id}`);
  latestChatDetail = detail;
  return detail;
}

export function deleteChat(id: number) {
  return request<{ ok: boolean }>(`/api/chats/${id}`, { method: "DELETE" });
}

/* ---- 条目 25：回收站（删除先进站，可恢复 / 可彻底删除） ---- */

/** 回收站里的一条：笔记 / 文档 / 会话共用的最小字段。 */
export type TrashItem = {
  id: number;
  /** 笔记 = 标题，文档 = 文件名，会话 = 标题 */
  title: string;
  category?: string;
  deleted_at: string;
};

export type TrashOut = {
  notes: TrashItem[];
  documents: TrashItem[];
  chats: TrashItem[];
};

export type TrashKind = "note" | "document" | "chat";

export function getTrash() {
  return request<TrashOut>("/api/trash");
}

export function restoreTrash(kind: TrashKind, id: number) {
  return request<{ ok: boolean }>("/api/trash/restore", {
    method: "POST",
    ...jsonBody({ type: kind, id }),
  });
}

/** 彻底删除：连同分块、向量、链接记录一起清掉，不可恢复。 */
export function purgeTrash(kind: TrashKind, id: number) {
  return request<{ ok: boolean }>(`/api/trash/${kind}/${id}`, { method: "DELETE" });
}

export function emptyTrash() {
  return request<{ ok: boolean; notes: number; documents: number; chats: number }>("/api/trash/empty", {
    method: "POST",
  });
}

export function sendChat(data: {
  message: string;
  chat_id?: number | null;
  image_data?: string | null;
  /** 条目 38：多图一起发（base64 列表，保序）；后端把旧单图字段归一成单元素列表 */
  images?: string[] | null;
  filters?: ChatFilters | null;
  /** 条目 26/35：显式启用的技能 id，如 ["builtin:web"] */
  skill_ids?: string[] | null;
  /** 条目 18：Agent 模式覆盖；不传/null=跟随设置，"off"=强制直答，"react"/"plan_execute"=强制对应模式 */
  agent_mode?: string | null;
}) {
  return request<ChatResponse>("/api/chat", { method: "POST", ...jsonBody(data) });
}

export type StreamHandlers = {
  onMeta?(chatId: number): void;
  /** 回答增量 */
  onToken?(text: string): void;
  /** 思考增量（条目 19）：与回答分两路，顺序即厂商给的顺序 */
  onReasoning?(text: string): void;
  /** Agent 过程的一步（条目 18）：在首个回答 token 之前陆续到达 */
  onAgent?(step: AgentStep): void;
  onDone?(data: {
    chat_id: number;
    refs: RefItem[];
    generation_status?: GenerationStatus;
    finish_reason?: string | null;
    /** 完整思考文本（流式增量已给过一遍，这里是归一后的全文） */
    reasoning?: string;
    thinking?: ThinkingMeta | null;
    skill_ids?: string[];
    /** 条目 18：Agent 轨迹的归一版；非 Agent 回答恒 null */
    agent_trace?: AgentTrace | null;
    /** 阶段 3 条目 3.5/3.6：答案级来源标记（角落标签的承载字段），与落库、`/chat` 响应同值；
     *  `done` 到达前前端没有它，标签因此不会先闪一个错值 */
    answer_source?: string | null;
  /** 阶段 4 条目 4.3：该轮发生过崩溃恢复（工具已开始但没留下结果）——只读后端字段，
   *  由 recovery/synthesized 事件现算，刷新后仍在。历史消息与未恢复的轮次缺省 false。 */
  recovered?: boolean;
  }): void;
  /** 条目 42：用户停止生成。已生成的部分已在服务端落库（kind="stopped"），不再有 done */
  onStopped?(data: { chat_id: number; generation_status?: GenerationStatus; finish_reason?: string | null }): void;
};

/** 条目 42：每次提问的唯一标识，停止时 /api/chat/stop 靠它找到对应的流。 */
export function newRequestId(): string {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
  } catch {
    /* 走下面的兜底 */
  }
  return `req-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export async function streamChat(
  data: {
    message: string;
    chat_id?: number | null;
    image_data?: string | null;
    /** 条目 38：多图一起发（base64 列表，保序）；后端把旧单图字段归一成单元素列表 */
    images?: string[] | null;
    filters?: ChatFilters | null;
    /** 条目 26/35：显式启用的技能 id，如 ["builtin:web"] */
    skill_ids?: string[] | null;
    /** 条目 18：Agent 模式覆盖；不传/null=跟随设置，"off"=强制直答，"react"/"plan_execute"=强制对应模式 */
    agent_mode?: string | null;
    /** 条目 42：本次提问的标识；停止按钮用同一个 id 调 /api/chat/stop */
    request_id?: string | null;
  },
  handlers: StreamHandlers,
  options?: { signal?: AbortSignal },
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/chat/stream", { method: "POST", ...jsonBody(data), signal: options?.signal });
  } catch (err) {
    if (err instanceof ApiError) throw err;
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(OFFLINE);
  }
  if (!res.ok) {
    await parse(res);
  }
  if (!res.body) {
    throw new ApiError("回答中断了，请再试一次");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let reachedEof = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        reachedEof = true;
        break;
      }
      buf += decoder.decode(value, { stream: true });
      const blocks = buf.split("\n\n");
      buf = blocks.pop() || "";
      for (const block of blocks) {
        const line = block.split("\n").find((item) => item.startsWith("data: "));
        if (!line) continue;
        let ev: {
          type?: string;
          chat_id?: number;
          text?: string;
          detail?: string;
          generation_status?: GenerationStatus;
          finish_reason?: string | null;
          refs?: RefItem[];
          reasoning?: string;
          thinking?: ThinkingMeta | null;
          skill_ids?: string[];
          /** 条目 18：agent 事件携带的一步 */
          step?: AgentStep;
          /** 条目 18：done 事件携带的 Agent 轨迹 */
          agent_trace?: AgentTrace | null;
          /** 阶段 3 条目 3.5：done 事件携带的答案级来源标记（旧后端不送时为 undefined） */
          answer_source?: string | null;
          /** 阶段 4 条目 4.3：该轮发生过崩溃恢复（工具已开始但没留下结果）——只读后端字段，
           *  由 recovery/synthesized 事件现算，刷新后仍在。历史消息与未恢复的轮次缺省 false。 */
          recovered?: boolean;
        };
        try {
          ev = JSON.parse(line.slice(6)) as typeof ev;
        } catch {
          continue;
        }
        if (ev.type === "error") {
          throw new ApiError(ev.detail || "发送失败", {
            generation_status: ev.generation_status ?? "failed",
            finish_reason: ev.finish_reason ?? null,
          });
        }
        if (ev.type === "meta" && typeof ev.chat_id === "number") {
          handlers.onMeta?.(ev.chat_id);
        }
        if (ev.type === "reasoning" && ev.text) {
          handlers.onReasoning?.(ev.text);
        }
        if (ev.type === "agent" && ev.step) {
          handlers.onAgent?.(ev.step);
        }
        if (ev.type === "token" && ev.text) {
          handlers.onToken?.(ev.text);
        }
        if (ev.type === "done") {
          handlers.onDone?.({
            chat_id: ev.chat_id || 0,
            refs: Array.isArray(ev.refs) ? ev.refs : [],
            generation_status: ev.generation_status,
            finish_reason: ev.finish_reason,
            reasoning: ev.reasoning || "",
            thinking: ev.thinking || null,
            skill_ids: Array.isArray(ev.skill_ids) ? ev.skill_ids : [],
            agent_trace: ev.agent_trace ?? null,
            // 阶段 3 条目 3.6：来源标记如实透传（旧后端没有该键 → null，界面即「无标签」）
            answer_source: ev.answer_source ?? null,
            // 02 v1.0.12（回归修复 拍板⑨，2026-09-27）：done 帧携带的会话级恢复标记
            // （该会话是否存在 recovery/synthesized 痕迹；旧后端没有该键 → undefined）
            recovered: ev.recovered === true,
          });
          return;
        }
        if (ev.type === "stopped") {
          handlers.onStopped?.({
            chat_id: ev.chat_id || 0,
            generation_status: ev.generation_status ?? "stopped",
            finish_reason: ev.finish_reason,
          });
          return;
        }
      }
    }
  } finally {
    if (!reachedEof) {
      try {
        await reader.cancel();
      } catch {
        /* 保留读取、服务器或回调的原始异常，清理失败仍须释放锁。 */
      }
    }
    reader.releaseLock();
  }
}

/** 条目 42：请求停止一次进行中的流式生成（request_id 对不上时返回 stopped=false，不报错）。 */
export async function stopChatStream(requestId: string) {
  // ponytail: only stop needs a deadline; its callers fall back to local abort on failure.
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10_000);
  try {
    return await request<{ stopped: boolean }>("/api/chat/stop", {
      method: "POST",
      signal: controller.signal,
      ...jsonBody({ request_id: requestId }),
    });
  } finally {
    clearTimeout(timeout);
  }
}

/* ---- 设置与模型列表（设置页「API Key 服务」+ 对话页切换） ---- */

export type ModelRole = "chat" | "embed" | "vision";

export const ROLE_LABEL: Record<ModelRole, string> = {
  chat: "对话模型",
  embed: "向量模型",
  vision: "视觉模型",
};

export const ROLE_HINT: Record<ModelRole, string> = {
  chat: "负责问答与总结，必须配置",
  embed: "负责语义检索；不配则退化为纯关键词检索",
  vision: "负责图片理解；不配则不能提问带图",
};

/** 一条 API Key 服务：真实 Key 只在本机 .env，接口只回 has_key。 */
export type ModelService = {
  id: string;
  name: string;
  provider: string;
  base_url: string;
  has_key: boolean;
  models: string[];
  /** 条目 46：型号用途标记 {型号: [角色...]}；可多标，与 roles 绑定相互独立；旧配置可能没有 */
  model_roles?: Record<string, string[]>;
  /** 归属：在哪个弹窗里配的，就只在哪个弹窗里出现 */
  for_roles: ModelRole[];
  roles: ModelRole[];
};

export type RoleBinding = {
  service_id: string;
  model: string;
};

/** 供应商归属匹配：在哪个弹窗里配的供应商，就只在哪个弹窗里出现。
 *
 * 老数据没写归属时按「对话」算（与后端默认一致），所以不会平白出现在向量弹窗里。
 */
export function serviceInScope(
  service: Pick<ModelService, "for_roles">,
  managed: readonly ModelRole[],
): boolean {
  const scope: readonly ModelRole[] = service.for_roles?.length ? service.for_roles : ["chat", "vision"];
  return managed.some((role) => scope.includes(role));
}

/** 当前真正生效的模型——可能来自服务注册表，也可能来自 .env 手工配置。 */
export type ActiveModel = {
  configured: boolean;
  service_id: string;
  service_name: string;
  provider: string;
  model: string;
  error: string;
};

export type ProviderPreset = {
  kind: string;
  base_url?: string;
  chat_model?: string;
  embed_model?: string;
  vision_model?: string;
};

/** 向量库位置只读信息；目录修改通过本机配置和重启生效。 */
export type DataState = {
  /** .env 里配的值（可能是相对路径） */
  chroma_dir: string;
  /** 实际生效的本地绝对路径，或远程 Qdrant 地址 */
  chroma_path: string;
  exists: boolean;
  files: number;
  size_bytes: number;
  /** -1 = 未统计或无法读取，0 = 已统计的空集合 */
  vectors: number;
  /** 后端提供的只读说明 */
  notice: string;
};

/** 思考程度（条目 5）：选项与取值全部来自厂商文档，界面不自造档位。 */
export type ReasoningValue = string | number | boolean;

export type ReasoningOption = {
  /** 厂商原样取值 */
  value: ReasoningValue;
  label: string;
  /** 中文注解（翻译用，不改写取值） */
  hint: string;
};

export type ReasoningState = {
  /** 当前对话型号是否登记了官方思考参数 */
  registered: boolean;
  vendor: string;
  /** 厂商参数名，如 thinking.type */
  param: string;
  kind: "enum" | "bool" | "int" | "";
  options: ReasoningOption[];
  minimum: number | null;
  maximum: number | null;
  default: ReasoningValue | null;
  default_label: string;
  /** 当前生效值；null = 厂商默认（没设置） */
  current: ReasoningValue | null;
  /** .env 里存了值但当前型号用不上（换过型号等） */
  stale: boolean;
  /** 未登记型号时，.env 里手写的设置 */
  manual: { param: string; value: unknown } | null;
  manual_active: boolean;
  note: string;
  source: string;
};

/** 检索增强（条目 28）：重排序的服务商与代价全部来自后端登记表，界面不自己编。 */
export type RerankProviderInfo = {
  id: string;
  label: string;
  kind: string;
  summary: string;
  /** 该服务商的常用型号（候选，仍可手打） */
  models: string[];
  default_model: string;
  needs_key: boolean;
  base_url_hint: string;
  /** 开启条件 */
  requirement: string;
  /** 代价说明 */
  cost: string;
  note: string;
  source: string;
  /** 这条服务商的依赖是否就绪（本地 bge 要 sentence-transformers） */
  dependency_ok: boolean;
  dependency_detail: string;
};

export type RerankState = {
  enabled: boolean;
  provider: string;
  provider_label: string;
  model: string;
  /** .env 里写的专用地址（可能为空 = 沿用向量模型的） */
  base_url: string;
  /** 实际会请求的地址 */
  effective_base_url: string;
  /** 有没有可用的 Key（只回状态，不回 Key 本体） */
  has_key: boolean;
  /** Key 从哪来：重排序专用 / 沿用某条向量模型供应商 */
  key_source: string;
  models: string[];
  providers: RerankProviderInfo[];
  dependency_ok: boolean;
  dependency_detail: string;
  /** 开启后生效的最低相关分门槛 */
  threshold: number;
  /** 开着且真的能跑起来 */
  effective: boolean;
  /** 开着但跑不起来时的可读原因（空 = 没问题） */
  blocked_by: string;
  note: string;
};

/** 联网检索（条目 26）：服务商与代价全部来自后端登记表，界面不自己编。 */
export type WebProviderInfo = {
  id: string;
  label: string;
  summary: string;
  needs_key: boolean;
  key_hint: string;
  cost: string;
  note: string;
  source: string;
};

/** 搜索渠道选项（传统搜索 API / 大模型搜索）。 */
export type WebChannelInfo = {
  id: string;
  label: string;
  summary: string;
};

/** 大模型搜索服务商。 */
export type LlmWebProviderInfo = {
  id: string;
  label: string;
  summary: string;
  needs_key: boolean;
  key_hint: string;
  cost: string;
  note: string;
  source: string;
  default_model: string;
  models: string[];
  base_url_hint?: string;
};

/** 大模型搜索现状：provider / model / base_url / key / max_searches。 */
export type LlmWebState = {
  provider: string;
  provider_label: string;
  model: string;
  models: string[];
  base_url: string;
  has_key: boolean;
  max_searches: number;
  /** 服务商联网插件（tools 字段）：空=不带；web_search=请求带联网搜索工具 */
  search_tool?: string;
  /** 插件开关的说明文案（后端登记表下发，界面不自己编） */
  search_tool_note?: string;
  providers: LlmWebProviderInfo[];
};

export type WebState = {
  enabled: boolean;
  provider: string;
  provider_label: string;
  has_key: boolean;
  providers: WebProviderInfo[];
  max_results: number;
  timeout: number;
  effective: boolean;
  blocked_by: string;
  note: string;
  /** 搜索渠道："api" | "llm"；后端尚未回时可能是 undefined */
  channel?: string;
  channel_options?: WebChannelInfo[];
  llm?: LlmWebState;
};

/** 条目 18：Agent 的全局默认（模式、循环上限与审计），设置页可改、对话页可按次覆盖。 */
export type AgentSettings = {
  /** off | react | plan_execute */
  mode: string;
  max_rounds: number;
  audit: boolean;
  /** 后端登记的模式选项（值 + 界面名），界面不自造选项 */
  modes: { value: string; label: string }[];
};

export type SettingsOut = {
  services: ModelService[];
  roles: Partial<Record<ModelRole, RoleBinding>>;
  active: Record<ModelRole, ActiveModel>;
  /** services = 走服务注册表；env = 还在用 .env 的扁平配置 */
  mode: "services" | "env";
  providers: Record<string, ProviderPreset>;
  data: DataState;
  reasoning: ReasoningState;
  rerank: RerankState;
  web: WebState;
  agent: AgentSettings;
  /** 条目 23：DeepL 翻译（只回状态，不回 Key 本体）；旧后端响应可能没有 */
  translate?: { has_key: boolean };
  /** 专项工具升级-3 · 阶段 3（回归检查）：工作区根（只读文件工具的边界根）；空串 = 未配置 */
  workspace_root?: string;
};

export type ServiceIn = {
  id?: string;
  name: string;
  provider: string;
  base_url: string;
  /** 留空表示保留原 Key */
  api_key?: string;
  models: string[];
  /** 条目 46：型号用途标记 {型号: [角色...]}；传了即按本服务整表替换 */
  model_roles?: Record<string, string[]>;
  /** 归属角色：对话弹窗里配的给 chat/vision，向量弹窗里配的给 embed */
  for_roles?: ModelRole[];
};

export type SettingsIn = {
  /** 传了就是整表替换；不传表示不改动服务列表 */
  services?: ServiceIn[];
  /** 传了就是整表替换；传空对象表示全部解绑 */
  roles?: Partial<Record<ModelRole, RoleBinding>>;
  /** 旧格式：仅在还在用 .env 扁平配置（mode = "env"）时使用 */
  chat?: { provider?: string; base_url?: string; model?: string; api_key?: string };
  /** 数据服务（条目 3）：向量库 / 缓存的存放位置 */
  data?: { chroma_dir: string; migrate?: boolean };
  /** 思考程度（条目 5）：param 留空 = 清空（回到厂商默认） */
  reasoning?: { param: string; value?: unknown };
  /** 检索增强（条目 28）：字段都可省略；api_key 留空 = 保留原来那把 */
  rerank?: { enabled?: boolean; provider?: string; model?: string; base_url?: string; api_key?: string };
  /** 联网检索（条目 26）：字段都可省略；api_key 留空 = 保留原来那把 */
  web?: {
    enabled?: boolean;
    channel?: string;
    provider?: string;
    api_key?: string;
    max_results?: number;
    timeout?: number;
    llm_provider?: string;
    llm_model?: string;
    llm_base_url?: string;
    llm_api_key?: string;
    llm_max_searches?: number;
    /** 服务商联网插件：空=不带 tools；web_search=请求带联网搜索工具 */
    llm_search_tool?: string;
  };
  /** 条目 18：Agent；字段都可省略，max_rounds 需为 1~12 的整数 */
  agent?: { mode?: string; max_rounds?: number; audit?: boolean };
  /** 条目 23：DeepL 翻译；api_key 留空 = 保留原来那把 */
  translate?: { api_key: string };
  /** 专项工具升级-3 · 阶段 3（回归检查）：工作区根；空串 = 清空（回到「不注册文件工具」） */
  workspace_root?: string;
};

export type ModelProbe = {
  provider?: string;
  base_url?: string;
  /** 不传则用 service_id 对应服务已存好的 Key */
  api_key?: string;
  service_id?: string;
};

export function getSettings() {
  return request<SettingsOut>("/api/settings");
}

export function updateSettings(data: SettingsIn) {
  return request<SettingsOut>("/api/settings", { method: "PUT", ...jsonBody(data) });
}

export type OfflineModelsStatus = {
  state: "idle" | "running" | "succeeded" | "failed";
  phase: string;
  completed_files: number;
  total_files: number;
  imported_files: number;
  skipped_files: number;
  detail: string;
  model_dir: string;
  present_files: number;
};

export function getOfflineModels() {
  return request<OfflineModelsStatus>("/api/settings/offline-models");
}

export function importOfflineModels(path: string) {
  return request<OfflineModelsStatus>("/api/settings/offline-models/import", {
    method: "POST",
    ...jsonBody({ path }),
  });
}

export function pickModelArchive(initial: string) {
  return request<{ available: boolean; path: string; detail: string }>("/api/system/pick-model-archive", {
    method: "POST",
    ...jsonBody({ initial }),
  });
}

/** 对话页模型菜单：当前对话供应商已勾选的型号（不是现场拉的全量清单） */
export type ModelsOut = {
  models: string[];
  service_id: string;
  service_name: string;
  /** 当前生效的那个型号 */
  current: string;
  /** 没得可选时的说明（例如供应商还没勾选型号） */
  note: string;
};

export function listModels() {
  return request<ModelsOut>("/api/models");
}

/** 设置页：按草稿（还没保存的地址 / Key）拉取某个服务下的模型列表 */
export function probeModels(data: ModelProbe) {
  return request<{ models: string[] }>("/api/models", { method: "POST", ...jsonBody(data) });
}

/** 联网检索·大模型搜索：按草稿（还没保存的地址 / Key）拉模型列表；省略则用已保存配置 */
export function probeWebModels(data: { base_url?: string; api_key?: string }) {
  return request<{ models: string[] }>("/api/web/llm/models", { method: "POST", ...jsonBody(data) });
}

/** 弹系统「选择文件夹」；浏览器拿不到绝对路径，所以对话框在后端弹。
 *  available = false 表示本机弹不出来（无图形界面等），界面应退回手填。
 */
export function pickDirectory(initial: string) {
  return request<{ available: boolean; path: string; detail: string }>("/api/system/pick-directory", {
    method: "POST",
    ...jsonBody({ initial }),
  });
}

/* ---- 条目 35：技能 / MCP ---- */

export type McpTool = {
  name: string;
  description: string;
  dangerous: boolean;
  /** 工具级启停（35 修订）：停用的工具不进提示词目录、不进 Agent 工具清单 */
  enabled: boolean;
};

export type SkillPack = {
  id: string;
  slug: string;
  name: string;
  type: "builtin" | "local" | "mcp";
  description: string;
  enabled: boolean;
  locked: boolean;
  available: boolean;
  status: string;
  status_note: string;
  tools: McpTool[];
  path: string;
  command: string[];
  url: string;
  transport: string;
};

export type SkillsOut = {
  items: SkillPack[];
  skill_dir: string;
  skill_dir_exists: boolean;
  counts: {
    total: number;
    enabled: number;
    local: number;
    mcp: number;
    builtin: number;
  };
};

export function getSkills() {
  return request<SkillsOut>("/api/skills");
}

export function rescanSkills() {
  return request<SkillsOut>("/api/skills/scan", { method: "POST" });
}

export type SkillsPut = {
  packs: SkillPack[];
  skill_dir?: string | null;
};

export function putSkills(data: SkillsPut) {
  return request<SkillsOut>("/api/skills", { method: "PUT", ...jsonBody(data) });
}

export function probeMcp(data: { transport: "stdio" | "http"; command?: string[]; url?: string }) {
  return request<{ ok: boolean; tools: McpTool[]; error: string; hint: string }>(
    "/api/skills/mcp/probe",
    { method: "POST", ...jsonBody(data) },
  );
}

/** 对话 `/` 菜单只列可用且已启用的技能；不可用的在设置页置灰说明。 */
export function slashableSkills(items: SkillPack[]): SkillPack[] {
  return items.filter((item) => item.available && item.enabled);
}

/* ---- 条目 17：全库 Markdown 导出 ---- */

/** 一次导出的结果：输出目录、各类数量与逐条失败原因（失败不静默，界面要列出来）。 */
export type ExportMarkdownOut = {
  output_dir: string;
  notes_exported: number;
  documents_exported: number;
  errors: string[];
};

/** 导出整库（笔记 + 文档原文）为指定文件夹里的 Markdown，按分类分子文件夹。
 *  范围字段全部可选：不传 = 全库（任务清单 17 追加）。categories 精确分类（"" = 未分类）；
 *  tags 命中任一；date_from / date_to 为 YYYY-MM-DD（后端也认 YYYY / YYYY-MM）；
 *  note_ids / document_ids 为自选条目。 */
export function exportMarkdown(data: {
  path: string;
  include_notes: boolean;
  include_documents: boolean;
  categories?: string[];
  tags?: string[];
  date_from?: string;
  date_to?: string;
  note_ids?: number[];
  document_ids?: number[];
}) {
  return request<ExportMarkdownOut>("/api/export/markdown", { method: "POST", ...jsonBody(data) });
}

/* ---- 条目 40：会话分组 + 归档（纯侧栏组织，不改变提问与检索范围） ---- */

/** 侧栏「最近」里的一个会话分组；chat_count 只数组内未归档会话。 */
export type ChatGroup = {
  id: number;
  name: string;
  sort: number;
  created_at: string;
  chat_count: number;
};

export function listChatGroups() {
  return request<ChatGroup[]>("/api/chat-groups");
}

export function createChatGroup(name: string) {
  return request<ChatGroup>("/api/chat-groups", { method: "POST", ...jsonBody({ name }) });
}

/** 改名 / 排序（二选一或同传；不传的字段不动）。 */
export function updateChatGroup(id: number, data: { name?: string; sort?: number }) {
  return request<ChatGroup>(`/api/chat-groups/${id}`, { method: "PUT", ...jsonBody(data) });
}

/** 删除分组：组内会话回到「最近」平铺，会话本身不删。 */
export function deleteChatGroup(id: number) {
  return request<{ ok: boolean }>(`/api/chat-groups/${id}`, { method: "DELETE" });
}

/** 把会话移入分组；groupId 传 null 表示移出分组。 */
export function setChatGroup(id: number, groupId: number | null) {
  return request<{ ok: boolean }>(`/api/chats/${id}/group`, {
    method: "PUT",
    ...jsonBody({ group_id: groupId }),
  });
}

export function archiveChat(id: number) {
  return request<{ ok: boolean }>(`/api/chats/${id}/archive`, { method: "POST" });
}

export function unarchiveChat(id: number) {
  return request<{ ok: boolean }>(`/api/chats/${id}/unarchive`, { method: "POST" });
}

/** 已归档会话列表（GET /api/chats?archived=true）；默认的 listChats() 不含归档。 */
export function listArchivedChats() {
  return request<ChatItem[]>("/api/chats?archived=true");
}

/* ---- 任务清单 27：Token 用量（近 7 日统计 + CSV 导出） ---- */

/** 一个统计格：某天某类（或总计）的调用次数与 tokens；tokens 为 null 表示「未知」。 */
export type UsageStat = {
  calls: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  /** 拿不到 tokens 的调用次数（典型：流式对话不加 stream_options） */
  unknown_calls: number;
};

/** 一天的用量：按类型拆开（chat / vision / embed / rerank 四类固定齐全）。 */
export type UsageDayRow = {
  /** YYYY-MM-DD（数据库时区的自然日） */
  date: string;
  by_kind: Record<"chat" | "vision" | "embed" | "rerank", UsageStat>;
};

/** GET /api/usage 的返回：近 N 日按日×类型汇总 + 总计。 */
export type UsageSummary = {
  days: number;
  total: UsageStat;
  by_day: UsageDayRow[];
};

/** 近 N 日（默认 7）用量汇总。 */
export function getUsageSummary(days = 7) {
  return request<UsageSummary>(`/api/usage?days=${days}`);
}

/** 触发用量明细 CSV 下载（后端 UTF-8 with BOM，Excel 直开中文不乱码）。 */
export async function exportUsageCsv(days = 7) {
  const res = await fetch(`/api/usage/export?days=${days}`);
  if (!res.ok) throw new ApiError(`导出失败（HTTP ${res.status}）`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  try {
    triggerBlobDownload(url, `usage-${new Date().toISOString().slice(0, 10)}.csv`);
  } finally {
    scheduleRevoke(url);
  }
}

/* ---- 条目 23：DeepL 翻译（中英互译） ---- */

export interface TranslateOut {
  text: string;
  detected_source_language: string;
  target: "zh" | "en";
}

/** 翻译一段文本；未配置 Key（400）/ 超时（504）/ 其余失败（502）的 detail 都是可读中文。 */
export async function translateText(body: { text: string; target: "zh" | "en" }): Promise<TranslateOut> {
  return request<TranslateOut>("/api/translate", { method: "POST", ...jsonBody(body) });
}
