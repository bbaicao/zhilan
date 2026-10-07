import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
} from "react";
import { Eye, CornerUpLeft, Languages, Pencil, Plus, Share2, StickyNote, Trash2 } from "lucide-react";
import { getBacklinks, getGraph, getSettings, type DocumentItem, type GraphNode, type Note, type WikiLinkHit } from "../api";
import { cx, formatTime } from "../utils";
import Markdown, { normalizeWikiTitle, type WikiTarget } from "./Markdown";
import TranslateDialog from "./TranslateDialog";

type WikiCandidate = { kind: "note" | "doc"; id: number; label: string };

/** 一次补全会话：start 是正文中 `[[` 的下标，query 是 `[[` 到光标之间的过滤词 */
type WikiSuggest = { start: number; query: string };

const WIKI_SUGGEST_LIMIT = 8;
/** `[[` 后面的过滤词太长就不再弹（避免把一段普通文字当成没闭合的 [[） */
const WIKI_QUERY_LIMIT = 80;

type NotesViewProps = {
  notes: Note[];
  /** 全量笔记（不受分类筛选影响）：[[链接]] 存在性判定与补全候选 */
  allNotes: Note[];
  /** 全量文档：[[链接]] 按文件名（去扩展名、精确匹配）判定 */
  docs: DocumentItem[];
  categories: string[];
  category: string;
  selectedId: number | "new" | null;
  selectedNote: Note | null;
  draftTitle: string;
  draftContent: string;
  draftCategory: string;
  saving: boolean;
  dirty: boolean;
  onCategory(category: string): void;
  onNew(): void;
  onOpen(note: Note): void;
  onDelete(id: number): void;
  onTitleChange(value: string): void;
  onContentChange(value: string): void;
  onDraftCategoryChange(value: string): void;
  onSave(): void;
  /** 点击指向笔记的 [[链接]]：跳到对应笔记 */
  onJumpNote(id: number): void;
  /** 点击指向文档的 [[链接]]：打开该文档详情 */
  onOpenDocument(id: number): void;
  /** 点击未创建的 [[链接]] 时的可读提示 */
  onNotify(message: string): void;
  /** 条目 16：在图谱中查看当前笔记 */
  onShowInGraph?(id: number): void;
};

/** 文件名去最后一个扩展名，与后端 _stem 同边界。 */
function stripExt(name: string) {
  const idx = name.lastIndexOf(".");
  return idx >= 0 ? name.slice(0, idx) : name;
}

/** 光标前方最近的一个未闭合 `[[`：作为补全触发点；跨行或已闭合都不算 */
function detectSuggest(value: string, caret: number): WikiSuggest | null {
  const upto = value.slice(0, caret);
  const open = upto.lastIndexOf("[[");
  if (open < 0) return null;
  const query = upto.slice(open + 2);
  if (query.includes("]]") || query.includes("[[") || query.includes("\n")) return null;
  if (query.length > WIKI_QUERY_LIMIT) return null;
  return { start: open, query };
}

export default function NotesView(props: NotesViewProps) {
  const {
    notes,
    allNotes,
    docs,
    categories,
    category,
    selectedId,
    selectedNote,
    draftTitle,
    draftContent,
    draftCategory,
    saving,
    dirty,
    onCategory,
    onNew,
    onOpen,
    onDelete,
    onTitleChange,
    onContentChange,
    onDraftCategoryChange,
    onSave,
    onJumpNote,
    onOpenDocument,
    onNotify,
    onShowInGraph,
  } = props;

  /** 编辑 | 预览：组件内 local state，不持久化 */
  const [mode, setMode] = useState<"edit" | "preview">("edit");
  const [suggest, setSuggest] = useState<WikiSuggest | null>(null);
  const [active, setActive] = useState(0);
  const [backlinks, setBacklinks] = useState<WikiLinkHit[]>([]);
  const [wikiGraph, setWikiGraph] = useState<{ sourceId: number | null; nodes: GraphNode[] }>({
    sourceId: null,
    nodes: [],
  });
  const areaRef = useRef<HTMLTextAreaElement>(null);
  /** 补全插入后要落到的光标位置（等受控 value 更新完再设置，才能落在 ]] 后面） */
  const pendingCaret = useRef<number | null>(null);

  // 条目 23：DeepL 翻译。Key 是否已配置挂载时拉一次设置（App 的 settingsData 没传进来，这里自己取 translate.has_key）。
  const [translateHasKey, setTranslateHasKey] = useState(false);
  /** 非空 = 翻译弹窗开着，值为要翻的源文本（选区或全文） */
  const [translateSource, setTranslateSource] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getSettings()
      .then((out) => {
        if (!cancelled) setTranslateHasKey(Boolean(out.translate?.has_key));
      })
      .catch(() => {
        /* 读不到设置按未配置处理：入口置灰 */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const savedNoteId = typeof selectedId === "number" ? selectedId : null;
  const noteUpdatedAt = selectedNote?.updated_at ?? null;

  useEffect(() => {
    setWikiGraph({ sourceId: savedNoteId, nodes: [] });
    if (savedNoteId == null) return;
    let cancelled = false;
    getGraph()
      .then((out) => {
        if (!cancelled) setWikiGraph({ sourceId: savedNoteId, nodes: out.nodes });
      })
      .catch((err) => {
        console.error("wiki graph", err);
        // 没有库身份就保持未解析；不从全局同名列表猜目标。
      });
    return () => {
      cancelled = true;
    };
  }, [savedNoteId, noteUpdatedAt, allNotes, docs]);

  useEffect(() => {
    if (savedNoteId == null) {
      setBacklinks([]);
      return;
    }
    let cancelled = false;
    getBacklinks("note", savedNoteId)
      .then((out) => {
        if (!cancelled) setBacklinks(out.items);
      })
      .catch((err) => {
        console.error("backlinks", err);
        if (!cancelled) setBacklinks([]);
      });
    return () => {
      cancelled = true;
    };
  }, [savedNoteId, noteUpdatedAt]);

  useEffect(() => {
    if (pendingCaret.current == null) return;
    const pos = pendingCaret.current;
    pendingCaret.current = null;
    const area = areaRef.current;
    if (area) {
      area.focus();
      area.setSelectionRange(pos, pos);
    }
  }, [draftContent]);

  /** 用真实来源的库身份收集候选；同库同名（包括笔记 / 文档）不取首项。 */
  const wikiPool = useMemo<WikiCandidate[]>(() => {
    if (savedNoteId == null || wikiGraph.sourceId !== savedNoteId) return [];
    const source = wikiGraph.nodes.find((node) => node.type === "note" && node.source_id === savedNoteId);
    if (!source) return [];
    const candidates = new Map<string, WikiCandidate | null>();
    for (const node of wikiGraph.nodes) {
      if (node.type === "ghost" || node.source_id == null || node.kb_id !== source.kb_id) continue;
      const label = normalizeWikiTitle(node.type === "document" ? stripExt(node.title) : node.title);
      if (!label) continue;
      candidates.set(label, candidates.has(label) ? null : {
        kind: node.type === "note" ? "note" : "doc",
        id: node.source_id,
        label,
      });
    }
    return Array.from(candidates.values()).filter((item): item is WikiCandidate => item !== null);
  }, [savedNoteId, wikiGraph]);

  /** 未保存来源、不可读关系信息或同名歧义均保持未解析。 */
  const resolveWiki = useCallback(
    (title: string): WikiTarget | null => {
      const q = normalizeWikiTitle(title);
      if (!q) return null;
      const candidate = wikiPool.find((item) => item.label === q);
      return candidate ? { kind: candidate.kind, id: candidate.id } : null;
    },
    [wikiPool],
  );

  const handleOpenWiki = useCallback(
    (target: WikiTarget) => {
      if (target.kind === "note") onJumpNote(target.id);
      else onOpenDocument(target.id);
    },
    [onJumpNote, onOpenDocument],
  );

  const handleMissingWiki = useCallback(
    (title: string) => onNotify(`「${title}」未找到唯一目标，请检查是否同名或尚未创建`),
    [onNotify],
  );

  /** 只补全同库唯一候选，避免插入一个标题却跳到另一个同名实体。 */
  const wikiCandidates = useMemo<WikiCandidate[]>(() => {
    if (!suggest) return [];
    const q = normalizeWikiTitle(suggest.query).toLowerCase();
    const matched = q ? wikiPool.filter((item) => item.label.toLowerCase().includes(q)) : wikiPool;
    return matched.slice(0, WIKI_SUGGEST_LIMIT);
  }, [suggest, wikiPool]);

  function updateSuggest(value: string, caret: number) {
    setSuggest(detectSuggest(value, caret));
    setActive(0);
  }

  function onAreaChange(event: ChangeEvent<HTMLTextAreaElement>) {
    const value = event.target.value;
    onContentChange(value);
    updateSuggest(value, event.target.selectionStart ?? value.length);
  }

  /** 方向键等移动光标后重新检测（输入本身走 onChange）；补全导航键不重算，避免重置选中项 */
  function onAreaKeyUp(event: KeyboardEvent<HTMLTextAreaElement>) {
    const key = event.key;
    if (key !== "ArrowLeft" && key !== "ArrowRight" && key !== "Home" && key !== "End" && key !== "PageUp" && key !== "PageDown") {
      return;
    }
    const area = areaRef.current;
    if (!area) return;
    updateSuggest(draftContent, area.selectionStart ?? 0);
  }

  function onAreaKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (!suggest || wikiCandidates.length === 0) return;
    if (event.nativeEvent.isComposing) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current) => (current + 1) % wikiCandidates.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current) => (current - 1 + wikiCandidates.length) % wikiCandidates.length);
    } else if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      const candidate = wikiCandidates[Math.min(active, wikiCandidates.length - 1)];
      if (candidate) insertCandidate(candidate);
    } else if (event.key === "Escape") {
      event.preventDefault();
      setSuggest(null);
    }
  }

  function onAreaClick() {
    const area = areaRef.current;
    if (!area) return;
    updateSuggest(draftContent, area.selectionStart ?? 0);
  }

  /** 把 `[[过滤词`（到光标）替换为 `[[标题]]`，光标落在 ]] 后面 */
  function insertCandidate(candidate: WikiCandidate) {
    if (!suggest) return;
    const caret = Math.max(areaRef.current?.selectionStart ?? 0, suggest.start + 2);
    const next = draftContent.slice(0, suggest.start) + `[[${candidate.label}]]` + draftContent.slice(caret);
    pendingCaret.current = suggest.start + candidate.label.length + 4;
    onContentChange(next);
    setSuggest(null);
  }

  function onEditorKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      onSave();
    }
  }

  /** 条目 23：翻译入口——编辑模式优先取 textarea 选区（selectionStart/End），没有选中或预览模式用全文。 */
  function openTranslate() {
    if (!translateHasKey) return;
    const area = areaRef.current;
    const selected =
      mode === "edit" && area
        ? draftContent.slice(area.selectionStart ?? 0, area.selectionEnd ?? 0)
        : "";
    setTranslateSource(selected.trim() || draftContent);
  }

  return (
    <div className="grid h-full min-h-0 grid-cols-1 md:grid-cols-[280px_minmax(0,1fr)]">
      {/* 笔记列表 */}
      <section className="flex min-h-0 flex-col border-r border-line">
        <div className="border-b border-hairline p-3">
          <button
            type="button"
            onClick={onNew}
            className="flex h-9 w-full items-center justify-center gap-1.5 rounded-xl bg-fog/10 text-[13px] text-ink transition-colors hover:bg-fog/15"
          >
            <Plus size={15} strokeWidth={1.75} />
            新建笔记
          </button>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {categories.map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => onCategory(item)}
                className={cx(
                  "rounded-full px-2.5 py-1 text-[12px] transition-colors",
                  category === item ? "bg-fog/10 text-ink" : "bg-fog/[0.05] text-mute hover:text-ink",
                )}
              >
                {item}
              </button>
            ))}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-2 scrollbar-thin">
          {notes.length === 0 ? (
            <p className="px-2 py-6 text-center text-[12.5px] text-faint">
              {category === "全部" ? "还没有笔记，点上方「新建笔记」开始。" : "这个分类下没有笔记。"}
            </p>
          ) : (
            notes.map((note) => {
              const activeNote = selectedId === note.id;
              return (
                <div
                  key={note.id}
                  className={cx(
                    "group mb-1.5 flex items-center rounded-xl transition-colors",
                    activeNote ? "bg-fog/10" : "bg-fog/[0.03] hover:bg-fog/[0.06]",
                  )}
                >
                  <button type="button" onClick={() => onOpen(note)} className="min-w-0 flex-1 px-3 py-2.5 text-left">
                    <div className={cx("truncate text-[13.5px]", activeNote ? "font-medium text-ink" : "text-body")}>
                      {note.title}
                    </div>
                    <div className="mt-0.5 truncate text-[11.5px] text-faint">
                      {note.category || "未分类"} · {formatTime(note.updated_at)}
                    </div>
                  </button>
                  <button
                    type="button"
                    onClick={() => onDelete(note.id)}
                    aria-label="删除笔记"
                    className="mr-2 hidden rounded-md p-1 text-mute hover:text-rose group-hover:block"
                  >
                    <Trash2 size={13} strokeWidth={1.75} />
                  </button>
                </div>
              );
            })
          )}
        </div>
      </section>

      {/* 编辑区 */}
      <section className="flex min-h-0 flex-col" onKeyDown={onEditorKeyDown}>
        {selectedId === null ? (
          <div className="m-auto flex flex-col items-center px-6 text-center animate-fade-up">
            <span className="flex h-14 w-14 items-center justify-center rounded-2xl bg-fog/[0.04]">
              <StickyNote size={26} className="text-faint" strokeWidth={1.25} />
            </span>
            <p className="mt-5 text-[16px] text-ink">从左边选一篇，或新建笔记。</p>
            <p className="mt-2 text-[13px] leading-6 text-mute">
              写完保存后，对话就能引用这篇笔记。
              <br />
              编辑时按 Ctrl+S 可以快速保存，输入 [[ 可以链接其他笔记。
            </p>
          </div>
        ) : (
          <div className="mx-auto flex h-full w-full max-w-[820px] min-h-0 flex-col px-6 py-5">
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <input
                value={draftTitle}
                onChange={(event) => onTitleChange(event.target.value)}
                placeholder="笔记标题"
                className="min-w-[180px] flex-1 border-b border-line bg-transparent py-1.5 text-[20px] font-semibold text-ink outline-none transition-colors placeholder:text-faint focus:border-edge"
              />
              <input
                value={draftCategory}
                onChange={(event) => onDraftCategoryChange(event.target.value)}
                placeholder="分类"
                className="w-28 rounded-full bg-fog/[0.05] px-3 py-1.5 text-[13px] text-body outline-none transition-colors placeholder:text-faint focus:bg-fog/[0.08]"
              />
              <div className="flex items-center rounded-full bg-fog/[0.05] p-0.5" role="group" aria-label="编辑或预览">
                <button
                  type="button"
                  onClick={() => {
                    setMode("edit");
                    setSuggest(null);
                  }}
                  aria-pressed={mode === "edit"}
                  className={cx(
                    "flex items-center gap-1 rounded-full px-2.5 py-1 text-[12px] transition-colors",
                    mode === "edit" ? "bg-fog/10 text-ink" : "text-mute hover:text-ink",
                  )}
                >
                  <Pencil size={12} strokeWidth={1.75} />
                  编辑
                </button>
                <button
                  type="button"
                  onClick={() => setMode("preview")}
                  aria-pressed={mode === "preview"}
                  className={cx(
                    "flex items-center gap-1 rounded-full px-2.5 py-1 text-[12px] transition-colors",
                    mode === "preview" ? "bg-fog/10 text-ink" : "text-mute hover:text-ink",
                  )}
                >
                  <Eye size={12} strokeWidth={1.75} />
                  预览
                </button>
              </div>
              <button
                type="button"
                onClick={openTranslate}
                disabled={!translateHasKey || !draftContent.trim()}
                title={translateHasKey ? "翻译选中文本或全文（DeepL 中英互译）" : "未配置 DeepL Key，请到设置 → 翻译"}
                className="flex items-center gap-1 rounded-full bg-fog/[0.05] px-2.5 py-1.5 text-[12.5px] text-body transition-colors hover:bg-fog/[0.08] hover:text-ink disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-fog/[0.05]"
              >
                <Languages size={12} strokeWidth={1.75} />
                翻译
              </button>
              <button
                type="button"
                disabled={saving}
                onClick={onSave}
                className="rounded-full bg-send px-4 py-1.5 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover disabled:opacity-60"
              >
                {saving ? "保存中…" : "保存"}
              </button>
            </div>
            {mode === "preview" ? (
              <div className="min-h-0 flex-1 overflow-auto pb-2 scrollbar-thin">
                {draftContent.trim() ? (
                  <Markdown
                    content={draftContent}
                    resolveWiki={resolveWiki}
                    onOpenWiki={handleOpenWiki}
                    onMissingWiki={handleMissingWiki}
                  />
                ) : (
                  <p className="text-[14px] text-faint">还没有内容，切回「编辑」写下第一行。</p>
                )}
              </div>
            ) : (
              <div className="relative flex min-h-0 flex-1 flex-col">
                <textarea
                  ref={areaRef}
                  value={draftContent}
                  onChange={onAreaChange}
                  onKeyDown={onAreaKeyDown}
                  onKeyUp={onAreaKeyUp}
                  onClick={onAreaClick}
                  onBlur={() => setSuggest(null)}
                  placeholder="在这里写课堂笔记、要点或摘录"
                  className="min-h-0 flex-1 bg-transparent text-[15px] leading-7 text-body outline-none placeholder:text-faint scrollbar-thin"
                />
                {suggest && wikiCandidates.length > 0 ? (
                  <div className="absolute bottom-2 left-0 z-40 w-72 max-w-full rounded-xl border border-line bg-panel p-1.5 shadow-pop animate-fade-up">
                    <div className="px-2 pb-1 pt-0.5 text-[11px] text-faint">链接到笔记或文档（↑↓ 选择，Enter 插入）</div>
                    {wikiCandidates.map((candidate, index) => (
                      <button
                        key={`${candidate.kind}-${candidate.id}`}
                        type="button"
                        onMouseDown={(event) => {
                          // 不让 textarea 失焦，点选后光标还能落在 ]] 后面
                          event.preventDefault();
                          insertCandidate(candidate);
                        }}
                        onMouseEnter={() => setActive(index)}
                        className={cx(
                          "flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] transition-colors",
                          index === active ? "bg-fog/[0.08] text-ink" : "text-body",
                        )}
                      >
                        <span className="min-w-0 flex-1 truncate">{candidate.label}</span>
                        <span
                          className={cx(
                            "shrink-0 rounded-full px-1.5 py-0.5 text-[10px]",
                            candidate.kind === "note" ? "bg-brand/15 text-brand" : "bg-info/15 text-info",
                          )}
                        >
                          {candidate.kind === "note" ? "笔记" : "文档"}
                        </span>
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            )}
            {savedNoteId != null && selectedNote && backlinks.length > 0 ? (
              <div className="mt-2 shrink-0 border-t border-hairline pt-2.5">
                <div className="flex items-center gap-1.5 text-[12px] text-mute">
                  <CornerUpLeft size={12} className="shrink-0 text-brand" strokeWidth={1.75} />
                  回链 · {backlinks.length}
                </div>
                <div className="mt-1.5 flex flex-col gap-0.5">
                  {backlinks.map((hit) => (
                    <button
                      key={`${hit.type}-${hit.id}`}
                      type="button"
                      onClick={() => (hit.type === "note" ? onJumpNote(hit.id) : onOpenDocument(hit.id))}
                      className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-fog/[0.06]"
                    >
                      <span className="shrink-0 rounded-full bg-fog/[0.05] px-1.5 py-0.5 text-[10px] text-mute">
                        {hit.type === "note" ? "笔记" : "文档"}
                      </span>
                      <span className="max-w-[40%] shrink-0 truncate text-[12.5px] text-ink">{hit.title}</span>
                      {hit.snippet ? (
                        <span className="min-w-0 flex-1 truncate text-[11.5px] text-faint">{hit.snippet}</span>
                      ) : null}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}
            <div
              className={cx(
                "flex items-center gap-2 border-t border-hairline pt-2.5 text-[11.5px] text-faint",
                savedNoteId != null && selectedNote && backlinks.length > 0 && "border-t-0 pt-2",
              )}
            >
              {dirty ? (
                <span className="flex items-center gap-1.5 text-warn">
                  <span className="h-1.5 w-1.5 rounded-full bg-warn" />
                  有未保存的修改
                </span>
              ) : selectedNote ? (
                <span>最近保存：{formatTime(selectedNote.updated_at)}</span>
              ) : (
                <span>新笔记，保存后进入列表</span>
              )}
              {savedNoteId != null && onShowInGraph ? (
                <button
                  type="button"
                  onClick={() => onShowInGraph(savedNoteId)}
                  className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11.5px] text-mute transition-colors hover:bg-fog/[0.06] hover:text-ink"
                  title="在图谱中查看"
                >
                  <Share2 size={12} strokeWidth={1.75} />
                  在图谱中查看
                </button>
              ) : null}
              <span className="ml-auto">Ctrl+S 保存</span>
            </div>
          </div>
        )}
      </section>

      {translateSource !== null ? (
        <TranslateDialog
          sourceText={translateSource}
          onClose={() => setTranslateSource(null)}
          onSavedNote={(_title, noteId) => {
            // 新笔记不在 App 当前列表里：jumpNote 找不到会自己刷新列表并打开该笔记
            if (typeof noteId === "number") onJumpNote(noteId);
          }}
        />
      ) : null}
    </div>
  );
}
