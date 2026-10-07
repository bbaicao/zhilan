import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  CircleAlert,
  Eye,
  Loader2,
  NotebookPen,
  Pencil,
  RefreshCw,
  X,
} from "lucide-react";
import { ApiError, generateNoteStream, type GeneratedNoteDraft } from "../api";
import { cx } from "../utils";
import Markdown from "./Markdown";

/** 条目 29：文档 → AI 生成笔记。草稿制：挂载即调生成接口，点「保存到笔记」才经 onSave 落库。
 *  追加（输出全量流式化）：走 SSE 流式端点——生成中的正文逐字出现在只读框里（带阶段播报
 *  与字数），done 后切回可编辑草稿；关闭弹窗 = AbortController 取消，后端随即停止生成。 */

const MISSING_KIND_LABEL: Record<GeneratedNoteDraft["completeness"]["missing"][number]["kind"], string> = {
  number: "数字",
  date: "日期",
  entity: "实体",
  negation: "否定表述",
};

const inputCls =
  "w-full rounded-lg border border-line bg-fog/[0.04] px-3 py-2 text-[13px] text-ink outline-none transition-colors placeholder:text-faint focus:border-edge";

type GenerateNoteDialogProps = {
  doc: { id: number; filename: string };
  /** 该文档已有的 AI 笔记（notes 里 source_document_id 匹配）：非空时明示「保存将覆盖」 */
  existingNote: { id: number; title: string } | null;
  onSave(payload: {
    title: string;
    content: string;
    category: string;
    tags: string[] | null;
    generation_meta: Record<string, unknown>;
  }): Promise<boolean>;
  onClose(): void;
};

export default function GenerateNoteDialog({ doc, existingNote, onSave, onClose }: GenerateNoteDialogProps) {
  const [phase, setPhase] = useState<"loading" | "error" | "ready">("loading");
  const [loadError, setLoadError] = useState("");
  const [draft, setDraft] = useState<GeneratedNoteDraft | null>(null);
  const [title, setTitle] = useState("");
  const [category, setCategory] = useState("");
  const [content, setContent] = useState("");
  /** 编辑 | 预览（样式同笔记页） */
  const [mode, setMode] = useState<"edit" | "preview">("edit");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  /** 重新生成的计数：进 effect 走同一条加载路径（换文档 doc.id 也会触发） */
  const [attempt, setAttempt] = useState(0);
  // 追加（流式）：生成中的正文与阶段播报；streamRef 用于让只读框始终贴着最新输出
  const [streamText, setStreamText] = useState("");
  const [statusText, setStatusText] = useState("正在阅读文档…");
  const streamRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = streamRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [streamText]);

  useEffect(() => {
    let alive = true;
    setPhase("loading");
    setDraft(null);
    setStreamText("");
    setStatusText("正在阅读文档…");
    setLoadError("");
    setSaveError("");
    setMode("edit");
    // 关闭弹窗 / 换文档 / 重新生成 = 取消在途生成（后端随即停止，不留悬空厂商请求）
    const controller = new AbortController();
    generateNoteStream(
      doc.id,
      {
        onStatus: (detail) => {
          if (alive) setStatusText(detail);
        },
        onToken: (text) => {
          if (alive) setStreamText((prev) => prev + text);
        },
        onDone: (data) => {
          if (!alive) return;
          setDraft(data);
          setTitle(data.title);
          setCategory(data.category);
          setContent(data.content);
          setPhase("ready");
        },
      },
      { signal: controller.signal },
    )
      .catch((err) => {
        if (!alive || controller.signal.aborted) return;
        setLoadError(err instanceof ApiError ? err.message : "生成笔记失败，请稍后再试");
        setPhase("error");
      });
    return () => {
      alive = false;
      controller.abort();
    };
  }, [doc.id, attempt]);

  /** 重新生成：confirm 确认丢弃当前草稿后从头再来一遍 */
  function regenerate() {
    if (window.confirm("重新生成会丢弃当前草稿和已有修改，确定继续？")) {
      setAttempt((n) => n + 1);
    }
  }

  async function save() {
    if (!draft || saving || !title.trim()) return;
    setSaving(true);
    setSaveError("");
    const ok = await onSave({
      title: title.trim(),
      content: content.trim(),
      category: category.trim(),
      tags: draft.tags,
      generation_meta: {
        generated_at: new Date().toISOString(),
        completeness: draft.completeness,
        source_filename: draft.source_filename,
      },
    });
    setSaving(false);
    if (ok) onClose();
    else setSaveError("保存失败，请重试");
  }

  return (
    <div
      className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in"
      onClick={saving ? undefined : onClose}
    >
      <div className="mx-auto mt-16 w-full max-w-2xl px-4 pb-12" onClick={(event) => event.stopPropagation()}>
        <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="text-[15px] font-semibold text-ink">AI 生成笔记</h2>
              <p className="mt-0.5 truncate text-[11.5px] leading-4 text-faint">来源文档：{doc.filename}</p>
            </div>
            <button
              type="button"
              onClick={onClose}
              disabled={saving}
              aria-label="关闭"
              className="rounded-md p-1 text-mute hover:bg-fog/5 hover:text-ink disabled:opacity-50"
            >
              <X size={16} strokeWidth={1.75} />
            </button>
          </div>

          {phase === "loading" ? (
            <div>
              <div className="mb-2 flex items-center gap-2 text-[12.5px] text-mute">
                <Loader2 size={13} className="shrink-0 animate-spin" strokeWidth={2} />
                <span className="min-w-0 truncate">{statusText}</span>
                <span className="ml-auto shrink-0 text-[11px] text-faint">已 {streamText.length} 字</span>
              </div>
              <textarea
                ref={streamRef}
                readOnly
                value={streamText}
                placeholder="生成中的笔记正文会在这里逐字出现…"
                spellCheck={false}
                className="h-[320px] w-full resize-none rounded-lg border border-line bg-fog/[0.04] px-3 py-2.5 font-mono text-[12.5px] leading-6 text-ink outline-none placeholder:text-faint scrollbar-thin"
              />
              <div className="mt-4 flex items-center justify-between gap-3">
                <span className="min-w-0 flex-1 text-[11px] leading-4 text-faint">
                  流式生成中，正文逐字可见；现在关闭等于取消，不会保存任何内容。
                </span>
                <button
                  type="button"
                  onClick={onClose}
                  className="shrink-0 rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12]"
                >
                  取消
                </button>
              </div>
            </div>
          ) : phase === "error" ? (
            <div className="flex flex-col items-center gap-3 py-10 text-center">
              <CircleAlert size={22} className="shrink-0 text-rose" strokeWidth={1.75} />
              <p className="max-w-md text-[13px] leading-6 text-rose">{loadError}</p>
              <div className="mt-1 flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setAttempt((n) => n + 1)}
                  className="flex items-center gap-1.5 rounded-full bg-send px-4 py-1.5 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover"
                >
                  <RefreshCw size={13} strokeWidth={1.75} />
                  重试
                </button>
                <button
                  type="button"
                  onClick={onClose}
                  className="rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12]"
                >
                  关闭
                </button>
              </div>
            </div>
          ) : draft ? (
            <>
              {existingNote ? (
                <div className="mb-3 flex items-start gap-2 rounded-xl bg-warn/10 px-3 py-2.5">
                  <AlertTriangle size={13} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                  <p className="text-[12.5px] leading-5 text-warn">
                    将覆盖已生成的笔记《{existingNote.title}》，保存后原内容会被替换。
                  </p>
                </div>
              ) : null}

              {draft.completeness.complete ? (
                <div className="mb-3 flex items-start gap-2 rounded-xl bg-ok/10 px-3 py-2.5">
                  <CheckCircle2 size={13} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                  <p className="text-[12.5px] leading-5 text-ok">
                    完整性核对通过（已核对 {draft.completeness.checked} 项关键信息
                    {draft.completeness.checker === "llm" ? " · AI 提取" : ""}）
                  </p>
                </div>
              ) : (
                <div className="mb-3 rounded-xl bg-warn/10 px-3 py-2.5">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex items-start gap-2 text-[12.5px] font-medium leading-5 text-warn">
                      <AlertTriangle size={13} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                      完整性核对：可能遗漏 {draft.completeness.missing.length} 项
                    </div>
                    <button
                      type="button"
                      onClick={regenerate}
                      title="丢弃当前草稿，重新生成一份"
                      className="flex shrink-0 items-center gap-1 rounded-full border border-warn/40 px-2.5 py-1 text-[11.5px] text-warn transition-colors hover:bg-warn/15"
                    >
                      <RefreshCw size={11} strokeWidth={1.75} />
                      重新生成
                    </button>
                  </div>
                  <div className="mt-1.5 space-y-1">
                    {draft.completeness.missing.map((item, index) => (
                      <div key={index} className="flex items-start gap-1.5">
                        <span className="mt-px shrink-0 rounded bg-warn/15 px-1.5 py-px text-[10px] leading-4 text-warn">
                          {MISSING_KIND_LABEL[item.kind] ?? item.kind}
                        </span>
                        <span className="min-w-0 flex-1 text-[12px] leading-5 text-body">
                          {item.item}
                          {item.context ? <span className="ml-1.5 text-[11px] text-faint">{item.context}</span> : null}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="mb-3">
                <label htmlFor="generate-note-title" className="mb-1.5 block text-[12.5px] text-mute">
                  标题
                </label>
                <input
                  id="generate-note-title"
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  placeholder="笔记标题"
                  className={inputCls}
                />
              </div>
              <div className="mb-3">
                <label htmlFor="generate-note-category" className="mb-1.5 block text-[12.5px] text-mute">
                  分类
                </label>
                <input
                  id="generate-note-category"
                  value={category}
                  onChange={(event) => setCategory(event.target.value)}
                  placeholder="可留空"
                  className={inputCls}
                />
              </div>
              <div className="mb-1.5 flex items-center justify-between gap-3">
                <label htmlFor="generate-note-content" className="min-w-0 truncate text-[12.5px] text-mute">
                  正文（Markdown）
                </label>
                <div className="flex shrink-0 items-center rounded-full bg-fog/[0.05] p-0.5" role="group" aria-label="编辑或预览">
                  <button
                    type="button"
                    onClick={() => setMode("edit")}
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
              </div>
              {mode === "preview" ? (
                <div className="max-h-[46vh] min-h-[220px] overflow-auto rounded-lg border border-line bg-fog/[0.02] px-3.5 py-3 scrollbar-thin">
                  {content.trim() ? (
                    <Markdown content={content} />
                  ) : (
                    <p className="text-[13px] text-faint">暂无内容，切回「编辑」写下第一行。</p>
                  )}
                </div>
              ) : (
                <textarea
                  id="generate-note-content"
                  value={content}
                  onChange={(event) => setContent(event.target.value)}
                  placeholder="笔记正文"
                  spellCheck={false}
                  className="h-[260px] min-h-[220px] w-full resize-y rounded-lg border border-line bg-fog/[0.04] px-3 py-2.5 font-mono text-[12.5px] leading-6 text-ink outline-none transition-colors placeholder:text-faint focus:border-edge scrollbar-thin"
                />
              )}

              {saveError ? (
                <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-rose">
                  <CircleAlert size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                  {saveError}
                </p>
              ) : null}

              <div className="mt-5 flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-[11px] leading-4 text-faint">
                  保存后跳转到该笔记，可继续编辑。
                </span>
                <button
                  type="button"
                  onClick={onClose}
                  disabled={saving}
                  className="rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12] disabled:opacity-50"
                >
                  取消
                </button>
                <button
                  type="button"
                  disabled={saving || !title.trim()}
                  onClick={() => void save()}
                  className="flex items-center gap-1.5 rounded-full bg-send px-4 py-1.5 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover disabled:opacity-60"
                >
                  {saving ? (
                    <Loader2 size={13} className="animate-spin" strokeWidth={2} />
                  ) : (
                    <NotebookPen size={13} strokeWidth={1.75} />
                  )}
                  {saving ? "保存中…" : "保存到笔记"}
                </button>
              </div>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
