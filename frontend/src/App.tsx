import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AgentStep,
  ApiError,
  ChatFilters,
  ChatGroup,
  ChatItem,
  ChatMessage,
  DocumentItem,
  Health,
  Note,
  ReasoningValue,
  SearchOut,
  SettingsIn,
  SettingsOut,
  archiveChat as archiveChatApi,
  createChatGroup,
  createNote,
  deleteChat,
  deleteChatGroup,
  deleteDocument,
  deleteNote,
  getChat,
  getHealth,
  getSettings,
  getSkills,
  hasFilters,
  AGENT_MODE_LABELS,
  listArchivedChats,
  listChatGroups,
  listChats,
  listDocuments,
  listModels,
  listNotes,
  duplicateExistingId,
  reindexDocuments,
  searchAll,
  setChatGroup,
  streamChat,
  stopChatStream,
  newRequestId,
  unarchiveChat as unarchiveChatApi,
  updateChatGroup,
  updateNote,
  updateSettings,
  uploadDocument,
} from "./api";
import { compressImage } from "./image";
import { fileSuffix, labelProvider, type View } from "./utils";
import Sidebar, { type Notice } from "./components/Sidebar";
import TopBanner from "./components/TopBanner";
import ChatView from "./components/ChatView";
import NotesView from "./components/NotesView";
import DocsView, { type DocsSection } from "./components/DocsView";
import GraphView, { type GraphFocus } from "./components/GraphView";
import TrashView from "./components/TrashView";
import SettingsView from "./components/SettingsView";
import SearchModal from "./components/SearchModal";
import SettingsDialog, { type EditRole } from "./components/SettingsDialog";
import DataDirDialog from "./components/DataDirDialog";
import RerankDialog from "./components/RerankDialog";
import WebDialog from "./components/WebDialog";
import AgentDialog from "./components/AgentDialog";
import SkillsDialog from "./components/SkillsDialog";
import GenerateNoteDialog from "./components/GenerateNoteDialog";
import FolderImportDialog from "./components/FolderImportDialog";
import type { KbSource, ComposerImage, QueueEntry } from "./components/Composer";

/** 条目 47：一路进行中的生成——按会话绑定（新会话拿到 id 前挂在发起它的视图实例上）。
 *  跨会话可以并行（对齐 Codex：切走不打断、后台继续生成）；同一会话仍只允许一路（37 排队口径）。 */
type StreamRec = {
  invalidated: boolean;
  deletionEpoch: number;
  stopPending?: Promise<boolean | null>;
  /** = 本次提问的 request_id，停止端点靠它叫停 */
  key: string;
  /** 所属会话；新会话在 meta 事件前为 null */
  chatId: number | null;
  /** 发送时所在视图实例的 token（新会话在 meta 前靠它认视图） */
  freshToken: number;
  controller: AbortController;
  /** 条目 42：用户点过停止 → 本轮收尾后队列不自动续发 */
  stopRequested: boolean;
  /** 条目 37 修订：「↑ 立即」抢占了这一轮——收尾后立刻按序发出这些排队消息（先于队列续发）。
   *  挂在记录上而不是读队列，是为了让「恰好正常收尾」的那一轮也不会被自动续发抢走；
   *  用数组而不是单值：同一拍连点多张卡片时按点击顺序依次发出，后一条不把前一条顶掉。 */
  pendingSend: QueueEntry[];
  /** 这条回答气泡的乐观 id（重挂 / 落库前界面上的身份） */
  botId: number;
  /** 回归修复：本轮用户消息快照（与乐观回显同一份数据）。
   *  服务端要等一轮收尾才落用户消息 ⇒ 流式期间切走再回来，重拉的 messages 里没有它；
   *  openChat 重挂回填靠这个字段先补在途用户气泡、再补回答半成品。 */
  userMsg: ChatMessage | null;
  /** Agent 生效模式（切走再回来重挂时回填 agent_trace.mode） */
  mode: string;
  /** 增量缓冲：不管用户在看哪都照记——切走再回来靠它把已生成内容接回界面 */
  buf: { content: string; reasoning: string; steps: AgentStep[] };
};

/** 条目 47：把缓冲里已收到的增量拼成一条未完成的回答气泡（重挂用；什么都没收到则 null）。 */
function partialFromStream(rec: StreamRec): ChatMessage | null {
  const { content, reasoning, steps } = rec.buf;
  if (!content && !reasoning && steps.length === 0) return null;
  return {
    id: rec.botId,
    role: "assistant",
    content,
    reasoning: reasoning || null,
    agent_trace: steps.length ? { mode: rec.mode, steps: [...steps] } : null,
    created_at: new Date().toISOString(),
  };
}

/** 条目 47：这路流此刻是否属于「正在看的视图」——已有会话按 id；还没拿到 id 的新会话按发起视图实例。 */
function streamOnView(view: { chatId: number | null; token: number }, rec: StreamRec): boolean {
  if (rec.invalidated) return false;
  if (view.chatId === null) return rec.chatId === null && rec.freshToken === view.token;
  return rec.chatId === view.chatId;
}

// ⚠️ 与后端 `app/pipeline/parsing.py` 的 `SUPPORTED_SUFFIXES` **必须逐项等价**（44 项）。
// 两份是**独立字面量**（无共享常量）⇒ 改一处漏一处**无红灯** ⇒ 由
// `backend/tests/pipeline/test_code_suffixes_t3_2.py` 与 `test_frontend_whitelist_t3_2.py` 两端钉住。
const ALLOWED_DOC = [
  ".txt", ".md", ".markdown", ".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".csv",
  ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp",
  // 工具升级-3 · 回归检查（单② 定档 (c)）：代码 16 / 配置 7 / 前端资源 6
  ".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".cs", ".rb", ".php",
  ".sh", ".sql", ".kt", ".swift",
  ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml",
  ".jsx", ".tsx", ".vue", ".css", ".scss", ".html",
];
const MAX_BYTES = 20 * 1024 * 1024;
const DOCKER_HINT = "请先启动 Docker 服务";
const KEY_HINT = "请先配置 API Key";

/** 条目 34：导航历史条目——恢复一个视图所需的最小参数集 */
type NavLocation =
  | { view: "chat"; chatId: number | null }
  | { view: "notes"; noteId: number | "new" | null }
  | { view: "docs"; section: DocsSection; docId: number | null }
  | { view: "graph"; focus: GraphFocus }
  | { view: "trash" }
  | { view: "settings" };

/** 条目 34：两个历史条目是否指同一位置（重复点同一会话/笔记时不重复入栈） */
function sameLocation(a: NavLocation, b: NavLocation) {
  if (a.view !== b.view) return false;
  if (a.view === "chat" && b.view === "chat") return a.chatId === b.chatId;
  if (a.view === "notes" && b.view === "notes") return a.noteId === b.noteId;
  if (a.view === "docs" && b.view === "docs") return a.section === b.section && a.docId === b.docId;
  if (a.view === "graph" && b.view === "graph") {
    if (a.focus === null || b.focus === null) return a.focus === b.focus;
    return a.focus.type === b.focus.type && a.focus.id === b.focus.id;
  }
  return true; // trash / settings：无参数视图
}

export default function App() {
  const [view, setView] = useState<View>("chat");
  const [banner, setBanner] = useState("");
  const [health, setHealth] = useState<Health | null>(null);
  const [notes, setNotes] = useState<Note[]>([]);
  const [category, setCategory] = useState("全部");
  const [selectedId, setSelectedId] = useState<number | "new" | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftContent, setDraftContent] = useState("");
  const [draftCategory, setDraftCategory] = useState("");
  const [saving, setSaving] = useState(false);
  const [docs, setDocs] = useState<DocumentItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [reindexing, setReindexing] = useState(false);
  const [chats, setChats] = useState<ChatItem[]>([]);
  // 条目 40：已归档会话与分组（「最近」区纯侧栏组织，不影响提问与检索范围）
  const [archivedChats, setArchivedChats] = useState<ChatItem[]>([]);
  const [groups, setGroups] = useState<ChatGroup[]>([]);
  const [chatId, setChatId] = useState<number | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatInput, setChatInput] = useState("");
  // 条目 48：装回输入框后聚焦；每「重新编辑」一次 +1，透传给 Composer
  const [composerFocusTick, setComposerFocusTick] = useState(0);
  // 条目 47：视图实例 token——每次 openChat 都是一个新视图实例；「正在看的会话」以
  // activeViewRef 为准（state 只是渲染投影），在途流的增量只写回属于它的视图。
  const [viewToken, setViewToken] = useState(1);
  const deletionEpoch = useRef(0);
  const deletedChatVersions = useRef(new Map<number, number>());
  // 条目 38：附件区改数组——粘贴 / 拖拽 / 选择器都「追加」，多张一起发
  const [images, setImages] = useState<ComposerImage[]>([]);
  // 条目 37：排队消息（流式/思考中再发送进入排队）——仅会话内内存态，不落库，刷新即清
  const [chatQueue, setChatQueue] = useState<QueueEntry[]>([]);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQ, setSearchQ] = useState("");
  const [searchHits, setSearchHits] = useState<SearchOut | null>(null);
  const [searching, setSearching] = useState(false);
  const [recentOpen, setRecentOpen] = useState(true);
  const [kbSource, setKbSource] = useState<KbSource>("local");
  const [chatFilters, setChatFilters] = useState<ChatFilters>({});
  /** 条目 26：对话页联网开关（会话级，初值来自设置 WEB_ENABLE；用户翻过后不再被刷新覆盖） */
  const [chatWebEnabled, setChatWebEnabled] = useState(false);
  const chatWebInited = useRef(false);
  /** 条目 18：对话页 Agent 模式（会话级；""=跟随设置）。默认跟随设置即可，不需要初值同步。 */
  const [chatAgentMode, setChatAgentMode] = useState("");
  const [settingsData, setSettingsData] = useState<SettingsOut | null>(null);
  const [docsSection, setDocsSection] = useState<DocsSection>("home");
  /** 笔记预览里 [[文档链接]] 的受控入口：非空时 DocsView 直接进入该文档详情 */
  const [initialDocId, setInitialDocId] = useState<number | null>(null);
  /** 条目 16：详情页「在图谱中查看」的聚焦目标 */
  const [graphFocus, setGraphFocus] = useState<GraphFocus>(null);
  const [editing, setEditing] = useState<EditRole | null>(null);
  const [editingData, setEditingData] = useState(false);
  const [editingRerank, setEditingRerank] = useState(false);
  const [editingWeb, setEditingWeb] = useState(false);
  const [editingAgent, setEditingAgent] = useState(false);
  const [editingSkills, setEditingSkills] = useState(false);
  const [skillsSummary, setSkillsSummary] = useState("");
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [models, setModels] = useState<string[] | null>(null);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState("");
  const [modelsScope, setModelsScope] = useState("");
  const [modelsNote, setModelsNote] = useState("");
  /** 条目 29：正在生成笔记的文档 + 上传后排队等生成的后续文档 */
  const [generateDoc, setGenerateDoc] = useState<{ id: number; filename: string } | null>(null);
  const [generateQueue, setGenerateQueue] = useState<{ id: number; filename: string }[]>([]);
  /** 条目 39：「导入文件夹 → AI 自动整理成笔记」弹窗 */
  const [importFolderOpen, setImportFolderOpen] = useState(false);
  /** 条目 41：批量删除进行中（进度期禁用批量按钮，防重复触发） */
  const [batchDeleting, setBatchDeleting] = useState(false);

  // 条目 34：侧栏前进/后退的导航历史栈。本体放 ref（后退/前进只是移动指针，不算新导航），
  // navTick 变化只为刷新按钮可用态。
  const navStack = useRef<{ past: NavLocation[]; current: NavLocation; future: NavLocation[] }>({
    past: [],
    current: { view: "chat", chatId: null },
    future: [],
  });
  const [navTick, setNavTick] = useState(0);
  void navTick; // 仅用于触发重渲染；读栈直接读 ref
  /** 快捷键等跨帧调用走 latest-ref，确保拿到最新一帧的后退/前进闭包 */
  const navActionsRef = useRef<{ back(): void; forward(): void }>({ back: () => {}, forward: () => {} });

  // 条目 37：排队消息的镜像走 ref——流结束后的自动续发发生在旧闭包里，读 state 会
  // 拿到过期值（排队条目被吞、占位判断失真都出过这类坑）。
  const queueRef = useRef<QueueEntry[]>([]);
  const queueSeq = useRef(1);
  /** 条目 47：当前视图实例（chatId + token）；openChat 与 meta 就地成形时同步更新 */
  const activeViewRef = useRef<{ chatId: number | null; token: number }>({ chatId: null, token: 1 });
  // 回归修复：每会话 recovered 拉取的时序闸——同会话挂起期间又发起了更新的一次拉取
  //（recovered 是会话级 ⇒ 队列续发的下一轮 done 也会拉），旧的那份解析后直接放弃：
  // 新份数据 ≥ 旧份，先合并旧份会把历史并重复。
  const recoveredFetchSeq = useRef<Map<number, number>>(new Map());
  // 条目 44：顶层视图的实时镜像——「正在眼前」必须连视图种类一起判（切到知识库/笔记等
  // 非会话视图时 activeViewRef 仍指向最后一个会话，不能据此当成还在看）
  const viewRef = useRef<View>("chat");
  viewRef.current = view;
  const viewTokenSeq = useRef(1);
  // 条目 47：进行中的流，跨会话可并行（对齐 Codex）；同一会话仍只允许一路。
  // 记录对象就地改字段（chatId / stopRequested / buf），增删走 setStreams 触发重渲染——
  // Composer 的发送/停止键与 ChatView 的 loading 都按「当前视图有没有自己的流」重算。
  const [streams, setStreams] = useState<StreamRec[]>([]);
  const streamsRef = useRef<StreamRec[]>([]);
  /** 排队条目的唯一变更入口：state 与 ref 同步走这一处 */
  function setQueueItems(next: QueueEntry[]) {
    queueRef.current = next;
    setChatQueue(next);
  }

  /** 流记录的唯一变更入口：state 与 ref 同步走这一处（条目 47） */
  function setStreamItems(next: StreamRec[]) {
    streamsRef.current = next;
    setStreams(next);
  }

  // 条目 44：非当前会话任务完成的通知——纯内存态，刷新即清；done / error 各一条，手动停止不发
  const [notices, setNotices] = useState<Notice[]>([]);
  const noticeSeq = useRef(1);
  // 条目 44：chats 的镜像——通知在流回调里产生，submitChat 闭包里的 chats 是发送那帧的旧值，
  // 查会话标题要读最新列表（refreshChats 里同步）
  const chatsRef = useRef<ChatItem[]>([]);

  /** 条目 44：非当前会话的任务收尾 → 铃铛推一条通知（标题优先取 chats 里的，拿不到记「新会话」） */
  function pushNotice(chatId: number, kind: "done" | "error", text: string) {
    const title = chatsRef.current.find((item) => item.id === chatId)?.title ?? "新会话";
    const id = noticeSeq.current++; // id 在 updater 外生成（StrictMode 下 updater 可能被调两次）
    setNotices((prev) => [{ id, chatId, title, kind, text, time: Date.now(), read: false }, ...prev]);
  }

  /** 条目 44：标记单条通知已读 */
  function markNoticeRead(id: number) {
    setNotices((prev) => prev.map((item) => (item.id === id ? { ...item, read: true } : item)));
  }

  /** 条目 44：全部标记已读 */
  function markAllNoticesRead() {
    setNotices((prev) => prev.map((item) => (item.read ? item : { ...item, read: true })));
  }

  /** 条目 47：当前视图自己有没有一路生成在跑——别的会话在后台跑不算（那时发送=再开一路） */
  function viewBusy(): boolean {
    return streamsRef.current.some((rec) => streamOnView(activeViewRef.current, rec));
  }

  /** 条目 44：此刻是否「正在眼前看这路流的会话」——会话视图且就是该会话；切到任何
   *  非会话视图（笔记/知识库/图谱/回收站/设置）都算不在眼前，完成/失败要提醒。 */
  function watchingOnScreen(rec: StreamRec): boolean {
    return viewRef.current === "chat" && streamOnView(activeViewRef.current, rec);
  }

  /** 新导航入栈：与当前位置相同（如重复点同一会话）则跳过；向前走出新路径时清空前进栈 */
  function pushNav(next: NavLocation) {
    if (next.view !== "chat") viewTokenSeq.current += 1;
    const stack = navStack.current;
    if (sameLocation(stack.current, next)) return;
    stack.past.push(stack.current);
    stack.future = [];
    stack.current = next;
    setNavTick((tick) => tick + 1);
  }

  /** 按历史条目恢复视图；恢复过程中触发的 pushNav 会因位置相同被去重，不会二次入栈 */
  function applyLocation(loc: NavLocation) {
    if (loc.view !== "chat") viewTokenSeq.current += 1;
    switch (loc.view) {
      case "chat":
        void openChat(loc.chatId);
        break;
      case "notes":
        if (loc.noteId === "new") startNewNote();
        else if (loc.noteId === null) {
          setSelectedId(null);
          setDraftTitle("");
          setDraftContent("");
          setDraftCategory("");
        } else jumpNote(loc.noteId);
        break;
      case "docs":
        setDocsSection(loc.section);
        setInitialDocId(loc.docId);
        setView("docs");
        break;
      case "graph":
        setGraphFocus(loc.focus);
        setView("graph");
        break;
      default:
        setView(loc.view); // trash / settings：无参数视图
    }
  }

  function goBack() {
    const stack = navStack.current;
    if (!stack.past.length) return;
    stack.future.push(stack.current);
    const target = stack.past.pop();
    if (!target) return;
    stack.current = target;
    setNavTick((tick) => tick + 1);
    applyLocation(target);
  }

  function goForward() {
    const stack = navStack.current;
    if (!stack.future.length) return;
    stack.past.push(stack.current);
    const target = stack.future.pop();
    if (!target) return;
    stack.current = target;
    setNavTick((tick) => tick + 1);
    applyLocation(target);
  }

  // 条目 34：Alt+←/→ 后退/前进（preventDefault 覆盖浏览器历史导航；输入框聚焦时也生效）
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      if (event.key === "ArrowLeft") navActionsRef.current.back();
      else navActionsRef.current.forward();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
  useEffect(() => {
    navActionsRef.current = { back: goBack, forward: goForward };
  });

  /** 条目 34：侧栏主导航统一入口（知识库固定回首页分区并清掉文档详情入口） */
  function navigateFromSidebar(next: View) {
    if (next === "docs") {
      pushNav({ view: "docs", section: "home", docId: null });
      setDocsSection("home");
      setInitialDocId(null);
    } else if (next === "notes") {
      pushNav({ view: "notes", noteId: selectedId });
    } else if (next === "graph") {
      pushNav({ view: "graph", focus: graphFocus });
    } else if (next === "trash") {
      pushNav({ view: "trash" });
    } else {
      pushNav({ view: "settings" });
    }
    setView(next);
  }

  const bannerTimerRef = useRef<number | null>(null);

  /** 横幅提示；`autoHideMs` 非空时到点自动消失（成功类提示用，错误仍靠手动关）。 */
  const flash = useCallback((message: string, autoHideMs?: number) => {
    setBanner(message);
    if (bannerTimerRef.current != null) {
      window.clearTimeout(bannerTimerRef.current);
      bannerTimerRef.current = null;
    }
    if (autoHideMs && autoHideMs > 0) {
      bannerTimerRef.current = window.setTimeout(() => {
        bannerTimerRef.current = null;
        setBanner((prev) => (prev === message ? "" : prev));
      }, autoHideMs);
    }
  }, []);

  useEffect(() => {
    return () => {
      if (bannerTimerRef.current != null) window.clearTimeout(bannerTimerRef.current);
    };
  }, []);

  async function refreshHealth() {
    try {
      const next = await getHealth();
      setHealth(next);
      setBanner((prev) => {
        if (next.database !== "ok" || next.redis !== "ok") {
          return DOCKER_HINT;
        }
        if (!next.llm_configured) return KEY_HINT;
        if (
          prev === "后端服务未启动，请先运行后端" ||
          prev === KEY_HINT ||
          prev === DOCKER_HINT ||
          prev === "数据库或缓存还没就绪，笔记和问答可能不可用"
        ) {
          return "";
        }
        return prev;
      });
    } catch (err) {
      setHealth(null);
      flash(err instanceof ApiError ? err.message : "后端服务未启动，请先运行后端");
    }
  }

  async function refreshNotes() {
    const rows = await listNotes();
    setNotes(rows);
    return rows;
  }

  async function refreshDocs() {
    setDocs(await listDocuments());
  }

  async function refreshChats() {
    // 条目 40：一次刷齐三份数据——「最近」（未归档）、「已归档」与分组（含未归档计数）
    const [rows, archivedRows, groupRows] = await Promise.all([
      listChats(),
      listArchivedChats(),
      listChatGroups(),
    ]);
    chatsRef.current = rows; // 条目 44：通知取会话标题用的最新列表
    setChats(rows);
    setArchivedChats(archivedRows);
    setGroups(groupRows);
  }

  async function refreshSettings() {
    try {
      const next = await getSettings();
      setSettingsData(next);
      // 条目 26：对话页联网初值来自设置；用户已手动翻过则不覆盖
      if (!chatWebInited.current) {
        setChatWebEnabled(Boolean(next.web?.enabled));
        chatWebInited.current = true;
      }
    } catch {
      // 设置读不到不阻断主流程；对话页切模型时会给出提示
    }
    try {
      const skillsOut = await getSkills();
      setSkillsSummary(
        `${skillsOut.counts.enabled} 项可用 · 本机 ${skillsOut.counts.local} · MCP ${skillsOut.counts.mcp}`,
      );
    } catch {
      setSkillsSummary("");
    }
  }

  async function fetchModels() {
    if (models !== null || modelsLoading) return;
    setModelsLoading(true);
    setModelsError("");
    try {
      const result = await listModels();
      setModels(result.models);
      setModelsScope(result.service_name);
      setModelsNote(result.note);
    } catch (err) {
      setModelsError(err instanceof ApiError ? err.message : "拉取模型列表失败");
    } finally {
      setModelsLoading(false);
    }
  }

  async function pickModel(model: string) {
    if (model === health?.llm_model) return;
    const bound = settingsData?.roles.chat;
    if (settingsData?.mode === "services" && !bound) {
      flash("对话模型还没绑定服务，请先到设置页选一条服务并指定对话模型");
      return;
    }
    try {
      const next =
        settingsData?.mode === "services" && bound
          ? await updateSettings({ roles: { ...settingsData.roles, chat: { service_id: bound.service_id, model } } })
          : await updateSettings({ chat: { model } });
      setSettingsData(next);
      await refreshHealth();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "切换模型失败");
    }
  }

  /** 思考程度（条目 5）：param 传空 = 清空（回到厂商默认）；取值来自厂商规格，界面上不翻译。 */
  async function pickReasoning(param: string, value: ReasoningValue | null) {
    try {
      setSettingsData(await updateSettings({ reasoning: { param, value } }));
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "设置思考程度失败");
    }
  }

  /** 保存设置；`onFail` 让弹窗能把可读原因显示在自己脚下（横幅也会照常提示）。 */
  async function saveSettings(payload: SettingsIn, onFail?: (message: string) => void): Promise<boolean> {
    setSettingsSaving(true);
    try {
      const next = await updateSettings(payload);
      setSettingsData(next);
      if (payload.web?.enabled !== undefined) setChatWebEnabled(next.web.enabled);
      await refreshHealth();
      // 厂商或接口地址可能变了，模型列表作废，下次打开重新拉取
      setModels(null);
      setModelsScope("");
      setModelsNote("");
      // 数据目录的保存结果带一句说明（搬了几条向量等），比通用文案有用
      // 成功类提示 2 秒自动消失，不必手动点关闭
      flash(next.data?.notice || "设置已保存，立即生效", 2000);
      return true;
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "保存设置失败";
      flash(message);
      onFail?.(message);
      return false;
    } finally {
      setSettingsSaving(false);
    }
  }

  useEffect(() => {
    void refreshHealth();
    // 对话页的模型菜单要按「对话角色绑定的服务」切换模型，启动就得拿到绑定关系
    void refreshSettings();
    const timer = window.setInterval(() => void refreshHealth(), 30000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        if (view === "notes" || view === "chat") await refreshNotes();
        if (view === "docs" || view === "settings") await refreshDocs();
        if (view === "chat") await refreshChats();
        if (view === "settings") await refreshSettings();
      } catch (err) {
        flash(err instanceof ApiError ? err.message : "加载失败");
      }
    })();
  }, [view]);

  const categories = useMemo(() => {
    const found = new Set(notes.map((item) => item.category).filter(Boolean));
    return ["全部", ...Array.from(found)];
  }, [notes]);

  /** 过滤 chips 用的分类选项（不含「全部」，空列表表示不限制）。 */
  const noteCategories = useMemo(
    () => Array.from(new Set(notes.map((item) => item.category).filter(Boolean))),
    [notes],
  );

  const visibleNotes = useMemo(() => {
    if (category === "全部") return notes;
    return notes.filter((item) => item.category === category);
  }, [notes, category]);

  const selectedNote = typeof selectedId === "number" ? notes.find((item) => item.id === selectedId) ?? null : null;

  const dirty = useMemo(() => {
    if (selectedId === "new") return Boolean(draftTitle.trim() || draftContent.trim() || draftCategory.trim());
    if (!selectedNote) return false;
    return (
      draftTitle !== selectedNote.title ||
      draftContent !== selectedNote.content ||
      draftCategory !== selectedNote.category
    );
  }, [selectedId, selectedNote, draftTitle, draftContent, draftCategory]);

  function openNote(note: Note) {
    pushNav({ view: "notes", noteId: note.id }); // 条目 34
    setSelectedId(note.id);
    setDraftTitle(note.title);
    setDraftContent(note.content);
    setDraftCategory(note.category);
  }

  function startNewNote() {
    pushNav({ view: "notes", noteId: "new" }); // 条目 34
    setSelectedId("new");
    setDraftTitle("");
    setDraftContent("");
    setDraftCategory(category === "全部" ? "" : category);
  }

  async function saveNote() {
    const title = draftTitle.trim();
    const content = draftContent.trim();
    if (!title) {
      flash("请填写笔记标题");
      return;
    }
    if (!content) {
      flash("请填写笔记内容");
      return;
    }
    setSaving(true);
    try {
      const payload = { title, content, category: draftCategory.trim() };
      const saved =
        selectedId === "new" || selectedId === null
          ? await createNote(payload)
          : await updateNote(selectedId, payload);
      const rows = await refreshNotes();
      const current = rows.find((item) => item.id === saved.id) || saved;
      openNote(current);
      setBanner("");
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function removeNote(id: number) {
    if (!window.confirm("确定删除这篇笔记？将移入回收站，可随时恢复。")) return;
    try {
      await deleteNote(id);
      flash("已移入回收站，可稍后恢复或彻底删除", 2600);
      const rows = await refreshNotes();
      if (selectedId === id) {
        if (rows[0]) openNote(rows[0]);
        else {
          setSelectedId(null);
          setDraftTitle("");
          setDraftContent("");
          setDraftCategory("");
        }
      }
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "删除失败");
    }
  }

  async function handleFiles(files: FileList | File[], opts?: { generateNotes?: boolean }) {
    const list = Array.from(files);
    if (!list.length) return;
    setUploading(true);
    // 条目 29：勾了「上传后用 AI 生成笔记」时，上传成功的文档（含 409 覆盖重建的）依次进生成弹窗
    const uploaded: { id: number; filename: string }[] = [];
    try {
      for (const file of list) {
        const suffix = fileSuffix(file.name);
        if (!ALLOWED_DOC.includes(suffix) || file.size > MAX_BYTES) {
          flash(file.name + "：上传失败，请检查文件格式或大小");
          continue;
        }
        try {
          const saved = await uploadDocument(file);
          uploaded.push({ id: saved.id, filename: saved.filename });
        } catch (err) {
          // 同内容已存在：让用户选「跳过」还是「覆盖重建」，不静默覆盖
          if (err instanceof ApiError && duplicateExistingId(err) !== null) {
            const overwrite = window.confirm(
              err.message + "\n\n点「确定」覆盖重建（旧文档会被替换），点「取消」跳过这个文件。",
            );
            if (!overwrite) {
              flash(file.name + "：已跳过（知识库中已有同内容文档）");
              continue;
            }
            try {
              const rebuilt = await uploadDocument(file, true);
              uploaded.push({ id: rebuilt.id, filename: rebuilt.filename });
              flash(file.name + "：已覆盖重建");
            } catch (retryErr) {
              flash(file.name + "：" + (retryErr instanceof ApiError ? retryErr.message : "覆盖重建失败"));
            }
            continue;
          }
          flash(file.name + "：上传失败，请检查文件格式或大小");
        }
      }
      await refreshDocs();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "上传失败，请检查文件格式或大小");
    } finally {
      setUploading(false);
    }
    if (opts?.generateNotes && uploaded.length) {
      const [first, ...rest] = uploaded;
      setGenerateDoc(first);
      setGenerateQueue(rest);
    }
  }

  /** 条目 29：打开某文档的 AI 生成笔记弹窗（文档列表行 / 清洗报告头部入口）。 */
  function openGenerate(doc: { id: number; filename: string }) {
    setGenerateDoc(doc);
  }

  /** 弹窗关闭（取消或保存成功）后弹出到队首的下一个文档；队列为空才真正收尾。 */
  function advanceGenerateQueue() {
    if (generateQueue.length > 0) {
      const [next, ...rest] = generateQueue;
      setGenerateDoc(next);
      setGenerateQueue(rest);
    } else {
      setGenerateDoc(null);
    }
  }

  /** 生成弹窗的「保存到笔记」：已有该文档的 AI 笔记则覆盖，否则新建；成功后跳转到该笔记。 */
  async function handleGenerateSave(payload: {
    title: string;
    content: string;
    category: string;
    tags: string[] | null;
    generation_meta: Record<string, unknown>;
  }): Promise<boolean> {
    if (!generateDoc) return false;
    const existing = notes.find((item) => item.source_document_id === generateDoc.id);
    try {
      const saved = existing
        ? await updateNote(existing.id, {
            title: payload.title,
            content: payload.content,
            category: payload.category,
            tags: payload.tags,
            generation_meta: payload.generation_meta,
          })
        : await createNote({
            title: payload.title,
            content: payload.content,
            category: payload.category,
            source_document_id: generateDoc.id,
            tags: payload.tags,
            generation_meta: payload.generation_meta,
          });
      const rows = await refreshNotes();
      setBanner("");
      jumpNote(saved.id, rows);
      return true;
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "保存笔记失败");
      return false;
    }
  }

  async function handleReindex() {
    if (!window.confirm("重建向量索引？会重新生成全部文档与笔记的向量，并清理失效的向量。")) return;
    setReindexing(true);
    try {
      const result = await reindexDocuments(null);
      const parts = [
        `文档 ${result.documents} 篇`,
        `笔记 ${result.notes} 条`,
        `向量 ${result.vectors} 条`,
        `清理孤儿 ${result.orphans_removed} 条`,
      ];
      if (result.recleaned) parts.push(`重新清洗 ${result.recleaned} 篇`);
      if (result.failed) parts.push(`失败 ${result.failed} 项`);
      // 回归修复：跳过 / 失败的明细直接给用户看（此前只 console.warn——
      // 「失败 1 项」到底是哪篇、为什么，用户无从知晓）
      const detail = result.messages?.length ? "。详情：" + result.messages.join("；") : "";
      flash("重建索引完成：" + parts.join("，") + detail);
      await refreshDocs();
      // 回归修复：重建会改写 clean_report ⇒ 立刻拉一次 health——
      // 别让「建议重建索引」横幅干等 30s 轮询才更新/消失（重建真清零时横幅立即撤下）
      await refreshHealth();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "重建索引失败");
    } finally {
      setReindexing(false);
    }
  }

  async function removeDoc(id: number, filename: string) {
    if (!window.confirm("确定删除「" + filename + "」？将移入回收站，可随时恢复。")) return;
    try {
      await deleteDocument(id);
      flash("已移入回收站，可稍后恢复或彻底删除", 2600);
      await refreshDocs();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "删除失败");
    }
  }

  /** 条目 41：批量删除。一次确认（写明数量、文件名摘要与后果），逐篇走与单篇相同的删除接口。 */
  async function removeDocsBatch(ids: number[], names: string[]): Promise<boolean> {
    if (ids.length === 0 || batchDeleting) return false;
    const nameById = new Map(ids.map((id, index) => [id, names[index] ?? `文档 ${id}`]));
    const preview = names.slice(0, 5).join("、") + (names.length > 5 ? ` 等 ${names.length} 篇` : "");
    const confirmed = window.confirm(
      `确定删除已选的 ${ids.length} 篇文档？\n` +
        "将先移入回收站（不占检索与上传去重）；要彻底删除请到回收站操作。\n\n" +
        preview,
    );
    if (!confirmed) return false;
    setBatchDeleting(true);
    const failed: string[] = [];
    try {
      for (const id of ids) {
        try {
          await deleteDocument(id);
        } catch {
          failed.push(nameById.get(id) ?? `文档 ${id}`);
        }
      }
      await refreshDocs();
      if (failed.length > 0) {
        flash(
          `${failed.length} 篇删除失败：${failed.slice(0, 3).join("、")}${failed.length > 3 ? "…" : ""}（其余已删除）`,
        );
      }
    } finally {
      setBatchDeleting(false);
    }
    return true;
  }

  async function openChat(id: number | null) {
    setView("chat");
    // 回归检查：加载期间身份与消息保持同属旧会话；仅最后一次成功读取可以一起切换。
    const token = ++viewTokenSeq.current;
    if (id === null) {
      pushNav({ view: "chat", chatId: null });
      activeViewRef.current = { chatId: null, token };
      setViewToken(token);
      setQueueItems([]);
      setChatId(null);
      setMessages([]);
      return;
    }
    const deletedVersion = deletedChatVersions.current.get(id);
    try {
      const detail = await getChat(id);
      // await 期间用户可能又切走了：结果只交给还停在这个会话的视图
      if (viewTokenSeq.current !== token || deletedChatVersions.current.get(id) !== deletedVersion) return;
      const list = [...detail.messages];
      // 条目 47：这个会话有一路还在生成——把缓冲里已收到的增量接回界面（后续增量经守卫继续追加）
      const running = streamsRef.current.find((item) => item.chatId === id);
      // 回归修复：在途轮的用户消息要等收尾才落库 ⇒ 重拉的 messages 里没有它；
      // 先把快照补回（与请求体同源），再补回答半成品——顺序即时间序。
      // rec 会随流收尾被移除 ⇒ 这里只在「生成还在跑」时生效，落库后以服务端为准。
      if (running?.userMsg) list.push(running.userMsg);
      const partial = running ? partialFromStream(running) : null;
      if (partial) list.push(partial);
      if (activeViewRef.current.chatId !== id) setQueueItems([]);
      activeViewRef.current = { chatId: detail.id, token };
      setViewToken(token);
      pushNav({ view: "chat", chatId: detail.id });
      setChatId(detail.id);
      setMessages(list);
    } catch (err) {
      if (viewTokenSeq.current === token) flash(err instanceof ApiError ? err.message : "无法打开会话");
    }
  }

  async function removeSession(id: number) {
    if (!window.confirm("确定删除这个会话？将移入回收站，可随时恢复。")) return;
    try {
      await deleteChat(id);
      deletedChatVersions.current.set(id, ++deletionEpoch.current);
      for (const rec of streamsRef.current) {
        if (rec.chatId !== id) continue;
        rec.invalidated = true;
        rec.pendingSend = [];
        void stopChatStream(rec.key).catch(() => {});
        rec.controller.abort();
      }
      setStreamItems(streamsRef.current.filter((rec) => !rec.invalidated));
      recoveredFetchSeq.current.delete(id);
      setNotices((prev) => prev.filter((notice) => notice.chatId !== id));
      flash("已移入回收站，可稍后恢复或彻底删除", 2600);
      if (activeViewRef.current.chatId === id) {
        // 回归修复：删的是「当前打开的会话」——只清 state 不够：activeViewRef 仍指向
        // 已删会话，紧接着发送会带着死会话 id 去 POST ⇒ 必然 404。照 openChat 的空态形状
        // 把视图实例一并作废（bump token，在途流的增量也不再写回这个视图）。
        const token = ++viewTokenSeq.current;
        activeViewRef.current = { chatId: null, token };
        setViewToken(token);
        setChatId(null);
        setMessages([]);
        // 排队消息仅属于当前会话（同 openChat 的换会话口径）——会话已删，队列条目已不可能发出，一并清
        setQueueItems([]);
      }
      // 条目 34：导航栈里指向已删会话的条目一并清掉，后退/前进不再恢复到已删会话（404「无法打开会话」）
      const stack = navStack.current;
      stack.past = stack.past.filter((loc) => !(loc.view === "chat" && loc.chatId === id));
      stack.future = stack.future.filter((loc) => !(loc.view === "chat" && loc.chatId === id));
      if (stack.current.view === "chat" && stack.current.chatId === id) {
        stack.current = { view: "chat", chatId: null };
      }
      setNavTick((tick) => tick + 1);
      await refreshChats();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "删除失败");
    }
  }

  /* ---- 条目 40：会话分组 + 归档（纯侧栏组织；删除仍走 removeSession 进回收站） ---- */

  async function createGroup(name: string) {
    try {
      await createChatGroup(name);
      await refreshChats();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "创建分组失败");
    }
  }

  async function renameGroup(id: number, name: string) {
    try {
      await updateChatGroup(id, { name });
      await refreshChats();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "重命名分组失败");
    }
  }

  async function removeGroup(id: number) {
    if (!window.confirm("确定删除这个分组？组内会话将回到「最近」平铺，不会被删除。")) return;
    try {
      await deleteChatGroup(id);
      await refreshChats();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "删除分组失败");
    }
  }

  async function moveChat(id: number, groupId: number | null) {
    try {
      await setChatGroup(id, groupId);
      await refreshChats();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "移动会话失败");
    }
  }

  async function archiveChat(id: number) {
    try {
      await archiveChatApi(id);
      flash("已归档，可在「已归档」里查看", 2600);
      await refreshChats();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "归档失败");
    }
  }

  async function unarchiveChat(id: number) {
    try {
      await unarchiveChatApi(id);
      await refreshChats();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "取消归档失败");
    }
  }

  /** 条目 38：追加附件——粘贴 / 拖拽 / 文件选择器三条路都汇到这里，一次可带多张；
   *  连续多次追加累积而不是覆盖。单张失败只提示那一张，其余照常进附件区。 */
  async function onPickImages(files: File[]) {
    if (!files.length) return;
    let failed = 0;
    for (const file of files) {
      if (!file.type.startsWith("image/")) {
        failed += 1;
        continue;
      }
      try {
        const data = await compressImage(file);
        setImages((prev) => [...prev, { name: file.name, data }]);
      } catch {
        failed += 1;
      }
    }
    if (failed === files.length) flash("图片过大或无法读取，请换一张再试");
    else if (failed > 0) flash(`${failed} 张图片无法读取，已跳过`);
  }

  async function submitChat(override?: string, queuedImages?: string[], carryPending?: QueueEntry[]) {
    const text = (override ?? chatInput).trim();
    // 回归修复：交互路径发送前把完整附件（含 name）快照——发送随即清空附件区，
    // 流中途失败时凭它把图片装回输入区（队列续发路径不带附件区、不参与回填，登记不动）。
    const sentImages = queuedImages !== undefined ? null : images;
    // 条目 37：从队列续发时带它自己的图片（base64 列表），不再动附件区里用户新放的图
    const imageList =
      queuedImages !== undefined ? queuedImages : images.map((item) => item.data);
    if (!text && imageList.length === 0) {
      flash("请输入问题");
      return;
    }
    // 条目 47：这轮发送在哪个视图实例发起，回答就只属于哪个会话——增量一律写回该视图，
    // 用户切走后缓冲照记、回来重挂。同一会话已有一路在跑时直接忽略（正常只可能是
    // 排队续发的竞态；跨会话并行不走这里挡）。
    const origin = activeViewRef.current;
    if (streamsRef.current.some((rec) => streamOnView(origin, rec))) return;
    const userId = -Date.now();
    // 条目 42：本次提问的唯一标识——停止按钮用它叫停服务端生成；AbortController 仅作
    // 停止指令送不到时的本地断流兜底（正常停止走 /api/chat/stop，由服务端留档部分回答）
    const requestId = newRequestId();
    const controller = new AbortController();
    const rec: StreamRec = {
      invalidated: false,
      deletionEpoch: deletionEpoch.current,
      key: requestId,
      chatId: origin.chatId,
      freshToken: origin.token,
      controller,
      stopRequested: false,
      // 条目 37 修订：上一轮被「立即」抢下时没抢到立即位的那些排队消息带进来，
      // 本轮一收尾就按点击顺序接着发（同一条流上单路串行，语义仍是「都不干等」）
      pendingSend: carryPending ? [...carryPending] : [],
      botId: userId - 1,
      userMsg: null,
      mode: chatAgentMode || settingsData?.agent?.mode || "off",
      buf: { content: "", reasoning: "", steps: [] },
    };
    setStreamItems([...streamsRef.current, rec]);
    setChatInput("");
    if (queuedImages === undefined) setImages([]);
    const scopedFilters = chatFilters;
    // 条目 44：一路流最多发一条通知——done 与 error 正常互斥，这里再兜一道
    //（防「done 已到、连接又在收尾时断开」之类的异常路径双发）
    let noticeSent = false;
    try {
      const now = new Date().toISOString();
      // 条目 35：乐观回显 `/slug` 对应的技能标签（以 done 为准再补一次）
      const slashMatch = (text || "").trim().match(/^\/([a-zA-Z0-9][a-zA-Z0-9_-]*)/);
      const optimisticSkills = slashMatch ? [`local:${slashMatch[1]}`] : null;
      // 条目 26：对话页联网开关打开时，显式带上 builtin:web（与 / 解析合并）
      const webSkill = chatWebEnabled ? "builtin:web" : null;
      const requestSkills = webSkill
        ? [...new Set([...(optimisticSkills ?? []), webSkill])]
        : optimisticSkills;
      // 回归修复：乐观回显的同一份数据快照到这路流的记录上——
      // 流式期间切走再回来时，openChat 靠它重建在途用户气泡（服务端此时还没落库）
      const userBubble: ChatMessage = {
        id: userId,
        role: "user",
        content: text || "（图片）",
        created_at: now,
        filters: hasFilters(scopedFilters) ? scopedFilters : null,
        skill_ids: requestSkills,
        // 条目 53：乐观回显也把这次发出的图挂上（与请求体同源、保序），
        // 发出去立刻就能在消息区看见；之后刷新 / 切会话再进由落库回读兜住
        images: imageList.length ? imageList : null,
      };
      rec.userMsg = userBubble;
      setMessages((prev) => [...prev, userBubble]);
      // 条目 19：思考增量可能先于回答到达，所以两种增量都要能自己「建出」这条回答；
      // 条目 47：按 id 全表找（重挂回来的气泡不在末尾也认得），找不到才新建
      const patchBot = (patch: (item: ChatMessage) => ChatMessage) => {
        setMessages((prev) => {
          if (!streamOnView(activeViewRef.current, rec)) return prev;
          if (prev.some((item) => item.id === rec.botId)) {
            return prev.map((item) => (item.id === rec.botId ? patch(item) : item));
          }
          return [
            ...prev,
            patch({ id: rec.botId, role: "assistant", content: "", created_at: now }),
          ];
        });
      };
      /** 条目 47：用户还停在这路流的视图上才动界面；切走了就只记缓冲，回来时重挂 */
      const onView = (fn: () => void) => {
        if (streamOnView(activeViewRef.current, rec)) fn();
      };
      await streamChat(
        {
          message: text,
          chat_id: origin.chatId,
          images: imageList.length ? imageList : null,
          filters: hasFilters(scopedFilters) ? scopedFilters : null,
          skill_ids: requestSkills,
          agent_mode: chatAgentMode === "" ? null : chatAgentMode,
          request_id: requestId,
        },
        {
          onMeta(id) {
            if (rec.invalidated) return;
            if ((deletedChatVersions.current.get(id) ?? 0) > rec.deletionEpoch) {
              rec.invalidated = true;
              rec.pendingSend = [];
              rec.controller.abort();
              return;
            }
            rec.chatId = id;
            // chatId 是就地改在记录上的，数组身份没变——换一个新数组让派生状态
            //（侧栏运行转圈等按 streams 重算的东西）立刻跟上，别吃记忆化的旧值
            setStreamItems([...streamsRef.current]);
            // 发起视图还停在「新对话」空态 → 就地长成这个会话（发送即进入任务）；
            // 用户已切走则不打扰——回答照常生成落库，侧栏里随时可进
            if (activeViewRef.current.token === origin.token && activeViewRef.current.chatId === null) {
              activeViewRef.current = { chatId: id, token: origin.token };
              setChatId(id);
              // 条目 34：新会话刚拿到 id 时同步栈内当前位置，避免后退时把这段对话恢复成空白新会话
              if (navStack.current.current.view === "chat" && navStack.current.current.chatId === null) {
                navStack.current.current = { view: "chat", chatId: id };
              }
              void refreshChats().catch(() => {}); // 侧栏立刻出现这个新会话
            }
          },
          onReasoning(chunk) {
            if (rec.invalidated) return;
            rec.buf.reasoning += chunk;
            onView(() => patchBot((item) => ({ ...item, reasoning: (item.reasoning || "") + chunk })));
          },
          onAgent(step) {
            if (rec.invalidated) return;
            rec.buf.steps = [...rec.buf.steps, step];
            onView(() =>
              patchBot((item) => ({
                ...item,
                agent_trace: {
                  mode: rec.mode,
                  steps: [...(item.agent_trace?.steps ?? []), step],
                },
              })),
            );
          },
          onToken(chunk) {
            if (rec.invalidated) return;
            rec.buf.content += chunk;
            onView(() => patchBot((item) => ({ ...item, content: item.content + chunk })));
          },
          async onDone(result) {
            if (rec.invalidated) return;
            // 回归检查：停止是否被接受由 stop 响应决定，done 不抢先覆盖仍在途的停止意图。
            onView(() => {
              setChatId(result.chat_id);
              if (result.skill_ids?.length) {
                // 回归修复：快照跟着补——回填出的在途用户气泡也带得上 done 解析的技能标签
                if (rec.userMsg) rec.userMsg = { ...rec.userMsg, skill_ids: result.skill_ids };
                setMessages((prev) =>
                  prev.map((item) =>
                    item.id === userId ? { ...item, skill_ids: result.skill_ids } : item,
                  ),
                );
              }
              patchBot((item) => ({
                ...item,
                refs: result.refs,
                // 流式增量可能被截断或与归一后的全文略有差异，以 done 里的为准
                reasoning: result.reasoning || item.reasoning || "",
                thinking: result.thinking ?? item.thinking ?? null,
                generation_status: result.generation_status,
                finish_reason: result.finish_reason,
                skill_ids: result.skill_ids?.length ? result.skill_ids : item.skill_ids ?? null,
                // 条目 18：done 为 null（非 Agent 回答）时保留流式攒下的步骤，避免折叠块闪没
                agent_trace: result.agent_trace ?? item.agent_trace ?? null,
                // 阶段 3 条目 3.6：角落来源标签只认后端这个字段（`done` 之前是空的 ⟹ 不渲染，
                // 不会先闪一个错值）；这里只是把它原样落到这条消息上，不做任何换算
                answer_source: result.answer_source ?? item.answer_source ?? null,
              }));
            });
            // 条目 44：done 已到——这路流此后再出错也不补发失败通知
            noticeSent = true;
            // 02 v1.0.12（回归修复 拍板⑨，2026-09-27）：done.recovered=true ⇒ 该会话带
            // 恢复痕迹——拉一次消息让「上次崩溃的恢复标记」免刷新可见（只读拉取；
            // 失败静默：刷新后仍能看到，不打扰本轮收尾）。
            if (result.recovered && rec.chatId !== null) {
              const seq = (recoveredFetchSeq.current.get(rec.chatId) ?? 0) + 1;
              recoveredFetchSeq.current.set(rec.chatId, seq);
              try {
                const detail = await getChat(rec.chatId);
                if ((deletedChatVersions.current.get(rec.chatId) ?? 0) > rec.deletionEpoch) {
                  rec.invalidated = true;
                  return;
                }
                if (
                  seq === recoveredFetchSeq.current.get(rec.chatId) &&
                  streamOnView(activeViewRef.current, rec)
                ) {
                  // 回归修复：按 id 归并，不做整体替换——本轮的乐观气泡（userId / rec.botId）
                  // 由落库真身接替，挂起期间 finally 已续发的新一轮乐观气泡（含其半成品）
                  // 按 id 保留，不再被不含它的服务端快照抹掉。归并对重复解析幂等：
                  // 已在 detail 里的条目不进 extras，旧快照晚到也不会把历史并重复。
                  setMessages((prev) => {
                    if (!streamOnView(activeViewRef.current, rec)) return prev;
                    const landed = new Set(detail.messages.map((item) => item.id));
                    const extras = prev.filter(
                      (item) =>
                        item.id !== userId && item.id !== rec.botId && !landed.has(item.id),
                    );
                    return [...detail.messages, ...extras];
                  });
                }
              } catch {
                /* 静默：恢复标记刷新后仍可见 */
              }
            }
            // SSE 不等待异步 onDone；finally 已移走记录时，删除仍须作废这次恢复回读。
            if (rec.chatId !== null && (deletedChatVersions.current.get(rec.chatId) ?? 0) > rec.deletionEpoch) {
              rec.invalidated = true;
              return;
            }
            // 条目 44：不是正在看的会话 → 铃铛推一条「已完成」；正看着的不打扰
            //（onStopped 不发通知：手动停止只发生在当前视图，不会命中这里）
            if (!rec.invalidated && rec.chatId !== null && !watchingOnScreen(rec)) {
              const status = result.generation_status ?? result.thinking?.generation_status;
              pushNotice(
                rec.chatId,
                status === "failed" ? "error" : "done",
                status === "incomplete" ? "回答已截断" : status === "failed" ? "生成失败" : status === "stopped" ? "已停止" : "已完成",
              );
            }
          },
          onStopped(result) {
            if (rec.invalidated) return;
            // 条目 42：用户停止——给这条回答打上「已停止」标记（已生成的部分原样保留，
            // 服务端已把部分内容以 kind="stopped" 落库，刷新后仍能看到）；
            // 一个增量都没收到时没有这条气泡，用横幅告知即可。
            onView(() => {
              setMessages((prev) =>
                prev.map((item) => (item.id === rec.botId ? {
                  ...item,
                  kind: "stopped",
                  generation_status: result.generation_status ?? "stopped",
                  finish_reason: result.finish_reason ?? null,
                } : item)),
              );
              flash("已停止生成", 2600);
            });
          },
        },
        { signal: controller.signal },
      );
      if (streamOnView(activeViewRef.current, rec)) setBanner("");
    } catch (err) {
      if (rec.invalidated) return;
      const stillThere = watchingOnScreen(rec);
      if (controller.signal.aborted) {
        // 条目 42：停止指令没送到、本地断流兜底的路径。回归修复：此路径后端**什么都不落库**
        //（收尾才落库，断开 / 本地 abort 不会走到 persist）⇒ 不拉取、不全量替换——乐观用户
        // 气泡与已生成的半成品原样留在界面；本地保留的部分刷新即失（与后端语义一致的真实状态）。
        // 不回填输入框，队列也保留不自动续发。
        if (stillThere) {
          setMessages((prev) => prev.map((item) => item.id === rec.botId ? {
            ...item, kind: "stopped", generation_status: "stopped", finish_reason: "local_abort",
          } : item));
          flash("已停止（已生成的部分未能保存）");
        }
      } else if (stillThere) {
        setChatInput(text);
        // 失败不写入成功回答；本地提示保留半成品并明确标为失败，刷新以服务端历史为准。
        const failedMessage: ChatMessage = {
          ...(partialFromStream(rec) ?? {
            id: rec.botId, role: "assistant", content: "", created_at: new Date().toISOString(),
          }),
          generation_status: "failed",
          finish_reason: err instanceof ApiError && typeof err.payload?.finish_reason === "string"
            ? err.payload.finish_reason : "error",
        };
        const withFailure = (items: ChatMessage[]) => [
          ...items.filter((item) => item.id !== rec.botId), failedMessage,
        ];
        // 回归修复：失败轮带过图就把快照装回附件区（图片数据此前随清空直接丢失）。
        // 只在快照非空时回填：纯文字轮失败不去动附件区，避免抹掉生成期间用户新放的图。
        if (sentImages && sentImages.length) setImages(sentImages);
        if (rec.chatId) {
          try {
            const detail = await getChat(rec.chatId);
            if (streamOnView(activeViewRef.current, rec)) setMessages(withFailure(detail.messages));
          } catch {
            if (streamOnView(activeViewRef.current, rec)) setMessages((prev) => withFailure(prev.filter((item) => item.id > 0)));
          }
        } else {
          setMessages((prev) => withFailure(prev.filter((item) => item.id > 0)));
        }
        if (!rec.invalidated) flash(err instanceof ApiError ? err.message : "发送失败");
      } else {
        // 条目 47：失败发生在后台会话的流上——界面不在那路流，横幅点明是后台任务，不静默
        const reason = err instanceof ApiError ? err.message : "连接中断或网络异常";
        flash("后台会话发送失败：" + reason);
        // 条目 44：铃铛补一条持久记录；meta 未到（chatId 为 null）无处跳转，不发
        if (!noticeSent && rec.chatId !== null) {
          noticeSent = true;
          pushNotice(
            rec.chatId,
            "error",
            "失败：" + (reason.length > 60 ? reason.slice(0, 60) + "…" : reason),
          );
        }
      }
    } finally {
      if (rec.stopPending) await rec.stopPending;
      setStreamItems(streamsRef.current.filter((item) => item.key !== rec.key));
      if (!rec.invalidated) {
        void refreshChats().catch(() => {});
        // 条目 37 修订：这一轮是被「↑ 立即」抢下的——收尾后立刻发出那条（先于队列续发）；
        // 它发完正常收尾时，留在队列里的其余条目照旧按序自动续发。
        // 注意这里不能写 return：finally 里 return 会把 catch 分支的异常一起吞掉。
        const pending = rec.pendingSend;
        rec.pendingSend = [];
        if (pending.length) {
          const [first, ...rest] = pending;
          if (streamOnView(activeViewRef.current, rec)) {
            // 连点「立即」时抢不到立即位的那些**带进新的一轮**（见 submitChat 的 carryPending）：
            // 按点击顺序在本条收尾后接着发，不落回队列、也不让刚点掉的卡片再闪回来
            void submitChat(first.text, first.images, rest.length ? rest : undefined);
          } else {
            // 极端竞态：用户在这一瞬切走了会话（队列也随切换清空）——不把这条发进别的
            // 会话，如实提示一次，不静默吞掉用户敲的字
            flash(`这条「立即」消息没有发出：已切换到其它会话（${pending.length} 条）`);
          }
        } else if (rec.stopRequested) {
          // 条目 42：用户主动停止时队列原样保留——是否继续由用户决定
          //（Composer 空闲且队列非空时给「继续发送」）。
          rec.stopRequested = false;
        } else if (streamOnView(activeViewRef.current, rec)) {
          // 条目 37：流结束（完成 / 出错）后按队列顺序自动发出下一条——但只属于这路流
          // 自己的会话（条目 47）：用户已切走就不续（队列也早已随切换清空）。此刻上一条
          // 正文已收尾，续发不打断任何已生成内容。
          const next = queueRef.current[0];
          if (next) {
            setQueueItems(queueRef.current.slice(1));
            void submitChat(next.text, next.images);
          }
        }
      }
    }
  }

  /** 条目 42/47：停止「正在看的这个会话」的生成。先置停止意图（本轮收尾后队列不自动
   *  续发），再请求服务端留档部分回答；停止指令本身送不到时才本地断流兜底。
   *  后台其他会话的流不受影响（各自 request_id 独立）。 */
  const stopChat = useCallback(async (): Promise<boolean | null> => {
    const rec = streamsRef.current.find((item) => streamOnView(activeViewRef.current, item));
    if (!rec) return false;
    if (rec.stopPending) return rec.stopPending;
    rec.stopRequested = true;
    rec.stopPending = (async () => {
      try {
        const result = await stopChatStream(rec.key);
        if (!result.stopped) {
          rec.stopRequested = false;
          if (streamOnView(activeViewRef.current, rec)) flash("当前生成未被停止，可能已结束，请稍后确认。", 3200);
        }
        return result.stopped;
      } catch {
        rec.controller.abort();
        return null;
      } finally {
        rec.stopPending = undefined;
      }
    })();
    return rec.stopPending;
  }, [flash]);

  /** 条目 48：对最新一条用户消息「重新编辑」——在跑的先按 42 停止，把原文装回输入框并聚焦。
   *  历史只增不改：改完发送就是一条新消息，原来那条不动；队列保留（与 42 停止口径一致）。 */
  const editUserMessage = useCallback((text: string) => {
    const view = activeViewRef.current;
    if (streamsRef.current.some((rec) => streamOnView(view, rec))) {
      void stopChat().then((stopped) => {
        if (stopped && activeViewRef.current === view) flash("已停止当前生成；修改后发送即为新一轮提问", 3200);
      });
    }
    setChatInput(text);
    setComposerFocusTick((tick) => tick + 1);
  }, [stopChat, flash]);

  /** 条目 37：统一发送入口——本会话空闲直发；本会话流式 / 思考中不抢答也不丢弃，进入排队。
   *  position="head"（Ctrl+回车）= 方向调整，插到队首下一轮最先被看到；
   *  position="tail"（普通发送 / 回车）= 追加队尾。
   *  条目 47：占用只看当前会话自己的流——新对话空态发送永远直接开跑（再开一个新会话），
   *  不被后台其他会话挡进队列。 */
  function requestSend(override?: string, position: "tail" | "head" = "tail") {
    const text = (override ?? chatInput).trim();
    const hasImages = images.length > 0;
    if (!text && !hasImages) {
      flash("请输入问题");
      return;
    }
    if (viewBusy()) {
      const item: QueueEntry = {
        id: queueSeq.current++,
        text,
        images: images.map((entry) => entry.data),
        kind: position === "head" ? "direction" : "followup",
      };
      setChatInput("");
      setImages([]);
      setQueueItems(
        position === "head" ? [item, ...queueRef.current] : [...queueRef.current, item],
      );
      return;
    }
    void submitChat(text);
  }

  /** 条目 37 修订：「↑ 立即」= 立刻发出这条（不再只是挪到队首等下一轮）。
   *  本会话有流在跑 → 先按 42 停止当前生成（已生成部分留档、截断处标「已停止」），
   *  等这一轮彻底收尾再立刻发出该条（见 submitChat 的 finally，pendingSend 分支）。
   *  本会话空闲（比如刚停止过、队列被保留）→ 直接发出这条，与「继续发送」同路、但指定条目。
   *  队列其余条目保持原序留在队列里，随后照常续发；连点多条时按点击顺序依次发出（不丢字）。 */
  function queueSendNow(id: number) {
    const current = queueRef.current;
    const item = current.find((entry) => entry.id === id);
    if (!item) return;
    // 先出队：否则本轮收尾的自动续发会把这条当队首再发一遍
    setQueueItems(current.filter((entry) => entry.id !== id));
    const rec = streamsRef.current.find((entry) => streamOnView(activeViewRef.current, entry));
    if (!rec) {
      void submitChat(item.text, item.images);
      return;
    }
    rec.pendingSend = [...rec.pendingSend, item];
    void stopChat();
  }

  function queueUpdate(id: number, text: string) {
    setQueueItems(
      queueRef.current.map((item) => (item.id === id ? { ...item, text: text.trim() } : item)),
    );
  }

  function queueRemove(id: number) {
    setQueueItems(queueRef.current.filter((item) => item.id !== id));
  }

  function queueReorder(from: number, to: number) {
    const current = queueRef.current;
    if (from === to || from < 0 || to < 0 || from >= current.length || to >= current.length) return;
    const next = [...current];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    setQueueItems(next);
  }

  /** 条目 42：空闲且队列非空（停止后保留的队列）时，手动发出队首那条。 */
  function queueContinue() {
    const next = queueRef.current[0];
    if (!next || viewBusy()) return;
    setQueueItems(queueRef.current.slice(1));
    void submitChat(next.text, next.images);
  }

  async function runSearch() {
    const q = searchQ.trim();
    if (!q) {
      setSearchHits({ notes: [], chunks: [] });
      return;
    }
    setSearching(true);
    try {
      setSearchHits(await searchAll(q));
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "搜索失败");
    } finally {
      setSearching(false);
    }
  }

  /** preloaded：调用方手里已有刚刷新的笔记列表时直接用，避免 stale notes 找不到/开到旧内容 */
  function jumpNote(id: number, preloaded?: Note[]) {
    pushNav({ view: "notes", noteId: id }); // 条目 34
    setView("notes");
    setSearchOpen(false);
    const note = (preloaded ?? notes).find((item) => item.id === id);
    if (note) {
      openNote(note);
      return;
    }
    void (async () => {
      try {
        const rows = await refreshNotes();
        const found = rows.find((item) => item.id === id);
        if (found) openNote(found);
      } catch (err) {
        flash(err instanceof ApiError ? err.message : "无法打开笔记");
      }
    })();
  }

  function later() {
    flash("这个功能记在扩展里，后续再做");
  }

  function openDocs(section: DocsSection = "home") {
    pushNav({ view: "docs", section, docId: null }); // 条目 34
    setDocsSection(section);
    setView("docs");
  }

  /** 笔记预览里点了指向文档的 [[链接]]：切到知识库页并直接打开该文档详情。
   *  文档列表由 view 切到 docs 时的 effect 顺带刷新。 */
  function openDocument(id: number) {
    pushNav({ view: "docs", section: "local", docId: id }); // 条目 34
    setDocsSection("local");
    setInitialDocId(id);
    setView("docs");
  }

  const openRefCurrent = useRef<(type: "note" | "document", id?: number) => void>(() => {});
  openRefCurrent.current = (type, id) => {
    if (type === "note") {
      if (id != null) jumpNote(id);
      else {
        pushNav({ view: "notes", noteId: selectedId });
        setView("notes");
      }
    } else if (id != null) openDocument(id);
    else openDocs("local");
  };
  const openChatRef = useCallback((type: "note" | "document", id?: number) => {
    openRefCurrent.current(type, id);
  }, []);

  /** 条目 16：切到图谱并聚焦该节点（局部图由 GraphView 自己打开） */
  function showInGraph(type: "note" | "document", id: number) {
    pushNav({ view: "graph", focus: { type, id } }); // 条目 34
    setGraphFocus({ type, id });
    setView("graph");
  }

  // 从文档详情离开知识库页后清掉受控入口，避免下次进知识库又被带进旧文档
  useEffect(() => {
    if (view !== "docs") setInitialDocId(null);
  }, [view]);

  const modelChip = !health
    ? "后端未连接"
    : !health.llm_configured
      ? KEY_HINT
      : labelProvider(health.llm_provider) + (health.llm_model ? " · " + health.llm_model : "");

  const statusDot: "ok" | "warn" | "err" = !health
    ? "err"
    : health.database !== "ok" || health.redis !== "ok" || !health.llm_configured
      ? "warn"
      : "ok";

  const isFreshChat = view === "chat" && chatId === null && messages.length === 0;
  // 条目 47：当前视图自己是否在生成——停止键 ■、排队判断、思考占位都按它；
  // 后台其他会话的流不计入（切走即回到「空闲」观感，再发送就是新的一路）。
  const viewStreaming = streams.some((rec) => streamOnView(activeViewRef.current, rec));
  // 条目 47 追加：正在生成回答的会话 id 集合——侧栏行首图标转圈（对齐 Codex），
  // 不管用户此刻在看哪，后台会话在跑也一眼可辨；meta 前的新会话还没有行，不必显示。
  const streamingChatIds = useMemo(
    () => streams.filter((rec) => rec.chatId !== null).map((rec) => rec.chatId as number),
    [streams],
  );

  // 条目 34：后退/前进按钮可用态（栈变化时 navTick 会触发重渲染刷新这里）
  const canBack = navStack.current.past.length > 0;
  const canForward = navStack.current.future.length > 0;

  return (
    <div className="flex h-screen min-h-0 bg-canvas text-ink">
      <Sidebar
        view={view}
        chatId={chatId}
        isFreshChat={isFreshChat}
        streamingChatIds={streamingChatIds}
        chats={chats}
        archivedChats={archivedChats}
        groups={groups}
        recentOpen={recentOpen}
        statusDot={statusDot}
        statusText={modelChip}
        onToggleRecent={() => setRecentOpen((open) => !open)}
        onNewChat={() => void openChat(null)}
        onOpenChat={(id) => void openChat(id)}
        onDeleteChat={(id) => void removeSession(id)}
        onNavigate={navigateFromSidebar}
        canBack={canBack}
        canForward={canForward}
        onBack={goBack}
        onForward={goForward}
        onOpenSearch={() => setSearchOpen(true)}
        onLater={later}
        /* 条目 40：分组与归档 */
        onCreateGroup={(name) => void createGroup(name)}
        onRenameGroup={(id, name) => void renameGroup(id, name)}
        onDeleteGroup={(id) => void removeGroup(id)}
        onMoveChat={(id, groupId) => void moveChat(id, groupId)}
        onArchiveChat={(id) => void archiveChat(id)}
        onUnarchiveChat={(id) => void unarchiveChat(id)}
        /* 条目 44：非当前会话任务完成的铃铛通知 */
        notices={notices}
        onNoticeRead={markNoticeRead}
        onNoticesReadAll={markAllNoticesRead}
      />

      <div className="flex min-h-0 min-w-0 flex-1 flex-col bg-canvas">
        {banner ? <TopBanner message={banner} onClose={() => setBanner("")} /> : null}

        <main className="min-h-0 min-w-0 flex-1 overflow-hidden">
          {view === "chat" ? (
            <ChatView
              viewToken={viewToken}
              messages={messages}
              loading={viewStreaming}
              input={chatInput}
              images={images}
              modelChip={modelChip}
              currentModel={health?.llm_model ?? ""}
              kbSource={kbSource}
              models={models}
              modelsLoading={modelsLoading}
              modelsError={modelsError}
              modelsScope={modelsScope}
              modelsNote={modelsNote}
              reasoning={settingsData?.reasoning ?? null}
              onInputChange={setChatInput}
              onSend={(override) => requestSend(override)}
              onStop={() => void stopChat()}
              // 条目 37：排队（队列卡片在 Composer 上方；仅内存态，换会话/刷新即清）
              queue={chatQueue}
              onQueueSendNow={queueSendNow}
              onQueueUpdate={queueUpdate}
              onQueueRemove={queueRemove}
              onQueueReorder={queueReorder}
              onQueueContinue={queueContinue}
              onQueueDirection={() => requestSend(undefined, "head")}
              onPickImages={(files) => void onPickImages(files)}
              onRemoveImage={(index) =>
                setImages((prev) => prev.filter((_, position) => position !== index))
              }
              onPickSource={setKbSource}
              onFetchModels={() => void fetchModels()}
              onPickModel={(model) => void pickModel(model)}
              onPickReasoning={(param, value) => void pickReasoning(param, value)}
              onOpenSettings={() => {
                pushNav({ view: "settings" }); // 条目 34
                setView("settings");
              }}
              onOpenDocs={(source) => openDocs(source ?? "home")}
              onOpenRef={openChatRef}
              filters={chatFilters}
              categories={noteCategories}
              webEnabled={chatWebEnabled}
              onToggleWeb={() => setChatWebEnabled((on) => !on)}
              agentMode={chatAgentMode}
              agentDefaultLabel={AGENT_MODE_LABELS[settingsData?.agent?.mode ?? "off"] ?? "直答"}
              onAgentMode={setChatAgentMode}
              onChangeFilters={setChatFilters}
              onEditUser={editUserMessage}
              focusSignal={composerFocusTick}
              notice={health && !health.llm_configured ? KEY_HINT : ""}
            />
          ) : view === "notes" ? (
            <NotesView
              notes={visibleNotes}
              allNotes={notes}
              docs={docs}
              categories={categories}
              category={category}
              selectedId={selectedId}
              selectedNote={selectedNote}
              draftTitle={draftTitle}
              draftContent={draftContent}
              draftCategory={draftCategory}
              saving={saving}
              dirty={dirty}
              onCategory={setCategory}
              onNew={startNewNote}
              onOpen={openNote}
              onDelete={(id) => void removeNote(id)}
              onTitleChange={setDraftTitle}
              onContentChange={setDraftContent}
              onDraftCategoryChange={setDraftCategory}
              onSave={() => void saveNote()}
              onJumpNote={jumpNote}
              onOpenDocument={openDocument}
              onShowInGraph={(id) => showInGraph("note", id)}
              onNotify={flash}
            />
          ) : view === "docs" ? (
            <DocsView
              docs={docs}
              dragging={dragging}
              uploading={uploading}
              reindexing={reindexing}
              onDragState={setDragging}
              onFiles={(files, opts) => void handleFiles(files, opts)}
              onGenerateNote={openGenerate}
              onImportFolder={() => setImportFolderOpen(true)}
              onDelete={(id, filename) => void removeDoc(id, filename)}
              onBatchDelete={removeDocsBatch}
              batchDeleting={batchDeleting}
              onReindex={() => void handleReindex()}
              section={docsSection}
              initialDocId={initialDocId}
              onInitialDocClosed={() => {
                // 条目 34：关掉受控文档详情也算一次导航，后退可重新打开
                pushNav({ view: "docs", section: docsSection, docId: null });
                setInitialDocId(null);
              }}
              cleanHint={health?.clean?.needs_reindex ? health.clean.hint : ""}
              onSection={(section) => {
                pushNav({ view: "docs", section, docId: null }); // 条目 34：知识库分区切换入栈
                setDocsSection(section);
              }}
              onShowInGraph={(id) => showInGraph("document", id)}
            />
          ) : view === "graph" ? (
            <GraphView
              focus={graphFocus}
              onOpenNote={(id) => jumpNote(id)}
              onOpenDocument={openDocument}
              onNotify={flash}
              onClearFocus={() => setGraphFocus(null)}
            />
          ) : view === "trash" ? (
            <TrashView
              onNotify={flash}
              onChanged={(kind) => {
                // 会话从回收站恢复/清掉后，侧栏「最近」要立刻跟上（笔记/文档列表进对应页时自会刷新）
                if (kind === "chat") void refreshChats();
              }}
            />
          ) : (
            <SettingsView
              health={health}
              settings={settingsData}
              skillsSummary={skillsSummary}
              onEditRole={setEditing}
              onEditData={() => setEditingData(true)}
              onEditRerank={() => setEditingRerank(true)}
              onEditWeb={() => setEditingWeb(true)}
              onEditAgent={() => setEditingAgent(true)}
              onEditSkills={() => setEditingSkills(true)}
            />
          )}
        </main>
      </div>

      {editing && settingsData ? (
        <SettingsDialog
          role={editing}
          settings={settingsData}
          saving={settingsSaving}
          onSave={saveSettings}
          onClose={() => setEditing(null)}
        />
      ) : null}

      {editingData && settingsData ? (
        <DataDirDialog data={settingsData.data} onSave={saveSettings} onClose={() => setEditingData(false)} />
      ) : null}

      {editingRerank && settingsData ? (
        <RerankDialog
          rerank={settingsData.rerank}
          onSave={saveSettings}
          onClose={() => setEditingRerank(false)}
        />
      ) : null}

      {editingWeb && settingsData ? (
        <WebDialog
          web={settingsData.web}
          onSave={saveSettings}
          onClose={() => setEditingWeb(false)}
        />
      ) : null}

      {editingAgent && settingsData ? (
        <AgentDialog
          agent={settingsData.agent}
          onSave={saveSettings}
          onClose={() => setEditingAgent(false)}
        />
      ) : null}

      {editingSkills ? <SkillsDialog onClose={() => setEditingSkills(false)} /> : null}

      {generateDoc ? (
        <GenerateNoteDialog
          doc={generateDoc}
          existingNote={
            notes.find((item) => item.source_document_id === generateDoc.id) ?? null
          }
          onSave={handleGenerateSave}
          onClose={advanceGenerateQueue}
        />
      ) : null}

      {importFolderOpen ? (
        <FolderImportDialog
          llmModel={health?.llm_model ?? undefined}
          getExistingNote={(docId) => notes.find((item) => item.source_document_id === docId) ?? null}
          onOpenNote={(id) => {
            // 先刷新再跳，避免 jumpNote 拿旧列表打开旧内容
            void refreshNotes()
              .then((rows) => jumpNote(id, rows))
              .catch(() => jumpNote(id));
          }}
          onFinished={() => {
            void Promise.all([refreshDocs(), refreshNotes()]).catch((err) =>
              flash(err instanceof ApiError ? err.message : "刷新失败"),
            );
          }}
          onClose={() => setImportFolderOpen(false)}
        />
      ) : null}

      {searchOpen ? (
        <SearchModal
          q={searchQ}
          searching={searching}
          hits={searchHits}
          onQChange={setSearchQ}
          onSearch={() => void runSearch()}
          onClose={() => setSearchOpen(false)}
          onOpenNote={jumpNote}
          onOpenDoc={(documentId) => {
            // 回归修复：带上命中的文档 id 跳对应文档（此前 document_id 被丢弃，
            // 点哪个命中都落 docs 页上一次的状态）；命中缺 document_id（类型允许 null）
            // 时退回旧行为——落知识库页上一次状态。
            setSearchOpen(false);
            if (documentId === null) {
              pushNav({ view: "docs", section: docsSection, docId: initialDocId }); // 条目 34
              setView("docs");
              return;
            }
            openDocument(documentId);
          }}
        />
      ) : null}
    </div>
  );
}
