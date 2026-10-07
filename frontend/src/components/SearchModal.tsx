import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { AlertTriangle, Calendar, FileText, Loader2, Search, StickyNote, Tag, X } from "lucide-react";
import type { SearchHit, SearchOut } from "../api";
import { cx } from "../utils";

type SearchModalProps = {
  q: string;
  searching: boolean;
  hits: SearchOut | null;
  onQChange(q: string): void;
  onSearch(): void;
  onClose(): void;
  onOpenNote(id: number): void;
  /** 回归修复：带上命中文档的 document_id（此前无参 ⇒ 点哪个命中都落上一次的 docs 页状态）；
   *  chunk 命中缺 document_id（类型允许 null）时传 null，由调用方兜底。 */
  onOpenDoc(documentId: number | null): void;
};

type FlatHit =
  | { kind: "note"; id: number; title: string; snippet: string }
  | { kind: "chunk"; id: number; document_id: number | null; title: string; snippet: string };

/** 条目 15：常驻语法提示；点击只把前缀插进搜索词，参数由用户自己填。 */
const SYNTAX_HINTS: { prefix: string; label: string }[] = [
  { prefix: "tag:", label: "tag:标签" },
  { prefix: "path:", label: "path:分类" },
  { prefix: "file:", label: "file:文件名" },
  { prefix: "date:", label: "date:2026-08-01..2026-08-31" },
];

/** 条目 15：结果元数据小字；只显示有值的字段。 */
function HitMeta({ hit }: { hit: SearchHit }) {
  const tags = (hit.tags ?? []).filter(Boolean).slice(0, 3);
  const date = typeof hit.created_at === "string" ? hit.created_at.slice(0, 10) : "";
  if (!hit.category && !tags.length && !date) return null;
  return (
    <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] leading-4 text-faint">
      {hit.category ? <span>{hit.category}</span> : null}
      {tags.map((tag, index) => (
        <span key={`tag${index}`} className="text-brand/80">
          #{tag}
        </span>
      ))}
      {date ? <span>{date}</span> : null}
    </span>
  );
}

export default function SearchModal(props: SearchModalProps) {
  const { q, searching, hits, onQChange, onSearch, onClose, onOpenNote, onOpenDoc } = props;
  const inputRef = useRef<HTMLInputElement>(null);
  const [active, setActive] = useState(-1);

  const flat = useMemo<FlatHit[]>(() => {
    if (!hits) return [];
    return [
      ...hits.notes.map((hit) => ({ kind: "note" as const, id: hit.id, title: hit.title, snippet: hit.snippet })),
      ...hits.chunks.map((hit) => ({
        kind: "chunk" as const,
        id: hit.id,
        document_id: hit.document_id ?? null,
        title: hit.title,
        snippet: hit.snippet,
      })),
    ];
  }, [hits]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    setActive(-1);
  }, [hits]);

  function openHit(hit: FlatHit) {
    if (hit.kind === "note") onOpenNote(hit.id);
    else onOpenDoc(hit.document_id);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((prev) => (flat.length ? (prev + 1) % flat.length : -1));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((prev) => (flat.length ? (prev - 1 + flat.length) % flat.length : -1));
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (active >= 0 && flat[active]) {
      openHit(flat[active]);
      return;
    }
    onSearch();
  }

  /** 条目 15：把操作符前缀追加到搜索词末尾，并把焦点还给输入框。 */
  function insertSyntax(prefix: string) {
    const next = !q || /\s$/.test(q) ? q + prefix : `${q} ${prefix}`;
    onQChange(next);
    inputRef.current?.focus();
  }

  const noteCount = hits?.notes.length ?? 0;
  const chunkCount = hits?.chunks.length ?? 0;
  const conditions = hits?.query?.conditions ?? [];
  const warnings = hits?.query?.warnings ?? [];

  return (
    <div className="fixed inset-0 z-50 bg-veil animate-fade-in" onClick={onClose}>
      <div className="mx-auto mt-24 w-full max-w-xl px-4" onClick={(event) => event.stopPropagation()}>
        <form
          onSubmit={onSubmit}
          className="overflow-hidden rounded-2xl border border-line bg-panel shadow-pop animate-fade-up"
        >
          {/* 输入行 */}
          <div className="flex items-center gap-2.5 border-b border-hairline px-4">
            <Search size={16} className="shrink-0 text-mute" strokeWidth={1.75} />
            <input
              ref={inputRef}
              value={q}
              onChange={(event) => onQChange(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder="搜索笔记和知识库（支持 tag: path: file: date:）"
              className="h-12 min-w-0 flex-1 bg-transparent text-[15px] outline-none placeholder:text-faint"
            />
            {searching ? <Loader2 size={15} className="shrink-0 animate-spin text-mute" strokeWidth={1.75} /> : null}
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭搜索"
              className="shrink-0 rounded-md p-1 text-mute hover:bg-fog/5 hover:text-ink"
            >
              <X size={16} strokeWidth={1.75} />
            </button>
          </div>

          {/* 语法提示行（条目 15）：点一下把前缀插进搜索词 */}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-dashed border-hairline px-4 py-1.5 text-[11px] text-faint">
            {SYNTAX_HINTS.map((item) => (
              <button
                key={item.prefix}
                type="button"
                onClick={() => insertSyntax(item.prefix)}
                title={`插入 ${item.prefix}`}
                className="rounded text-mute transition-colors hover:text-ink"
              >
                {item.label}
              </button>
            ))}
          </div>

          {/* 结果区 */}
          <div className="max-h-[52vh] overflow-auto p-2 scrollbar-thin">
            {conditions.length > 0 ? (
              <div className="flex flex-wrap items-center gap-1.5 px-3 pb-1 pt-2">
                {conditions.map((condition, index) => (
                  <span
                    key={`${condition.kind}${index}`}
                    className="inline-flex items-center gap-1 rounded-full border border-hairline bg-fog/[0.04] px-2 py-0.5 text-[11px] text-mute"
                  >
                    {condition.kind === "date" ? (
                      <Calendar size={11} className="shrink-0 text-faint" strokeWidth={1.75} />
                    ) : (
                      <Tag size={11} className="shrink-0 text-faint" strokeWidth={1.75} />
                    )}
                    {condition.label}：{condition.value}
                  </span>
                ))}
              </div>
            ) : null}
            {warnings.length > 0 ? (
              <div className="space-y-1 px-3 pb-1 pt-1">
                {warnings.map((warning, index) => (
                  <p
                    key={`warn${index}`}
                    className="flex items-start gap-1.5 rounded-lg bg-amber-400/10 px-2.5 py-1.5 text-[11.5px] leading-4 text-amber-300/90"
                  >
                    <AlertTriangle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                    <span className="min-w-0">{warning}</span>
                  </p>
                ))}
              </div>
            ) : null}
            {!hits ? (
              <p className="px-3 py-6 text-center text-[13px] text-faint">输入关键词后回车，搜索笔记与知识库文档。</p>
            ) : flat.length === 0 ? (
              <p className="px-3 py-6 text-center text-[13px] text-faint">
                {hits.hint?.trim() ? hits.hint : "没有找到相关内容。"}
              </p>
            ) : (
              <>
                {noteCount > 0 ? <div className="px-3 pb-1 pt-2 text-[11px] text-faint">笔记 · {noteCount}</div> : null}
                {hits!.notes.map((hit, index) => (
                  <button
                    key={"n" + hit.id}
                    type="button"
                    onClick={() => onOpenNote(hit.id)}
                    onMouseEnter={() => setActive(index)}
                    className={cx(
                      "flex w-full items-start gap-2.5 rounded-xl px-3 py-2.5 text-left transition-colors",
                      active === index ? "bg-fog/[0.07]" : "hover:bg-fog/[0.05]",
                    )}
                  >
                    <StickyNote size={15} className="mt-0.5 shrink-0 text-brand" strokeWidth={1.75} />
                    <span className="min-w-0">
                      <span className="block truncate text-[14px] text-ink">{hit.title}</span>
                      <span className="mt-0.5 block line-clamp-2 text-[12.5px] leading-5 text-mute">{hit.snippet}</span>
                      <HitMeta hit={hit} />
                    </span>
                  </button>
                ))}
                {chunkCount > 0 ? (
                  <div className="px-3 pb-1 pt-2 text-[11px] text-faint">知识库 · {chunkCount}</div>
                ) : null}
                {hits!.chunks.map((hit, index) => {
                  const flatIndex = noteCount + index;
                  return (
                    <button
                      key={"c" + hit.id}
                      type="button"
                      onClick={() => onOpenDoc(hit.document_id ?? null)}
                      onMouseEnter={() => setActive(flatIndex)}
                      className={cx(
                        "flex w-full items-start gap-2.5 rounded-xl px-3 py-2.5 text-left transition-colors",
                        active === flatIndex ? "bg-fog/[0.07]" : "hover:bg-fog/[0.05]",
                      )}
                    >
                      <FileText size={15} className="mt-0.5 shrink-0 text-info" strokeWidth={1.75} />
                      <span className="min-w-0">
                        <span className="block truncate text-[14px] text-ink">{hit.title}</span>
                        <span className="mt-0.5 block line-clamp-2 text-[12.5px] leading-5 text-mute">{hit.snippet}</span>
                        <HitMeta hit={hit} />
                      </span>
                    </button>
                  );
                })}
              </>
            )}
          </div>

          {/* 底部快捷键提示 */}
          <div className="flex items-center gap-4 border-t border-hairline px-4 py-2 text-[11px] text-faint">
            <span>↑↓ 选择</span>
            <span>Enter {active >= 0 ? "打开" : "搜索"}</span>
            <span>Esc 关闭</span>
          </div>
        </form>
      </div>
    </div>
  );
}
