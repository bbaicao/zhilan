import { useEffect, useRef, useState } from "react";
import {
  Check,
  CheckCircle2,
  CircleAlert,
  Copy,
  Languages,
  Loader2,
  NotebookPen,
  RotateCcw,
  X,
} from "lucide-react";
import { ApiError, createNote, translateText, type TranslateOut } from "../api";

/** 条目 23：DeepL 翻译弹窗（中英互译）。两阶段：准备（源文本 + 目标语言）→ 结果（译文 + 复制 / 存为笔记 / 返回重翻）。
 *  请求进行中不允许关弹窗（照 FolderImportDialog 纪律）。 */

const LANG_LABEL: Record<string, string> = {
  zh: "中文",
  en: "英文",
};

/** 检测语言代码 → 中文说明（DeepL 回 "EN" / "PT" 这类大写码，按前缀匹配容忍 "en-US"）。 */
function langLabel(code: string) {
  const lower = (code || "").toLowerCase();
  const hit = Object.keys(LANG_LABEL).find((key) => lower.startsWith(key));
  return hit ? LANG_LABEL[hit] : code || "未知";
}

/** CJK 字符占比启发式：中文为主默认译英，否则默认译中。 */
function defaultTarget(text: string): "zh" | "en" {
  const chars = Array.from(text.replace(/\s/g, ""));
  if (chars.length === 0) return "zh";
  const cjk = chars.filter((ch) => /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/.test(ch)).length;
  return cjk / chars.length >= 0.3 ? "en" : "zh";
}

/** 笔记标题：`翻译：` + 源文本前 20 字去换行。 */
function noteTitle(source: string) {
  const flat = source.replace(/\s+/g, " ").trim();
  return `翻译：${flat.slice(0, 20)}${flat.length > 20 ? "…" : ""}`;
}

/** 原文逐行加 blockquote 前缀（空行保留成单独的 `>`）。 */
function quoteOriginal(source: string) {
  return source
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => (line.trim() ? `> ${line}` : ">"))
    .join("\n");
}

type TranslateDialogProps = {
  sourceText: string;
  onClose(): void;
  /** 存为笔记成功后回调：title 用于横幅提示，noteId 可用于跳转到新笔记。 */
  onSavedNote?(title: string, noteId?: number): void;
};

export default function TranslateDialog({ sourceText, onClose, onSavedNote }: TranslateDialogProps) {
  const [phase, setPhase] = useState<"prepare" | "result">("prepare");
  const [target, setTarget] = useState<"zh" | "en">(() => defaultTarget(sourceText));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<TranslateOut | null>(null);
  // 存为笔记
  const [savingNote, setSavingNote] = useState(false);
  const [savedTitle, setSavedTitle] = useState("");
  const [saveError, setSaveError] = useState("");
  // 复制反馈：idle | ok | fail，2 秒后自动回落
  const [copyState, setCopyState] = useState<"idle" | "ok" | "fail">("idle");
  const copyTimer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
    };
  }, []);

  /** 请求进行中不允许关（点遮罩与右上角 X 都挡住） */
  const locked = busy || savingNote;

  function close() {
    if (locked) return;
    onClose();
  }

  async function run() {
    const text = sourceText.trim();
    if (!text || busy) return;
    setBusy(true);
    setError("");
    try {
      const out = await translateText({ text, target });
      setResult(out);
      setPhase("result");
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "翻译失败，请稍后重试";
      // 仅「尚未配置」开头的后端 detail（未配 Key 的 400）改写成去处更明确的文案；
      // 其余原样透出——鉴权失败/超时等错误里也含「Key」字样，贪匹配会把真原因吞掉
      setError(message.startsWith("尚未配置") ? "尚未配置 DeepL Key，请到设置 → 翻译完成配置" : message);
    } finally {
      setBusy(false);
    }
  }

  function backToPrepare() {
    setPhase("prepare");
    setResult(null);
    setError("");
    setSavedTitle("");
    setSaveError("");
    setCopyState("idle");
  }

  async function copyResult() {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(result.text);
      setCopyState("ok");
    } catch {
      setCopyState("fail");
    }
    if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopyState("idle"), 2000);
  }

  async function saveAsNote() {
    if (!result || savingNote || savedTitle) return;
    setSavingNote(true);
    setSaveError("");
    try {
      const title = noteTitle(sourceText);
      const content = `${result.text}\n\n---\n\n${quoteOriginal(sourceText)}`;
      const saved = await createNote({ title, content, category: "翻译" });
      setSavedTitle(saved.title);
      onSavedNote?.(saved.title, saved.id);
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : "保存笔记失败");
    } finally {
      setSavingNote(false);
    }
  }

  const subtitle =
    phase === "prepare"
      ? "DeepL 中英互译，结果可复制或存为笔记。"
      : `检测到${langLabel(result?.detected_source_language ?? "")}，已译为${LANG_LABEL[result?.target ?? "zh"]}。`;

  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={locked ? undefined : close}>
      <div className="mx-auto mt-16 w-full max-w-2xl px-4 pb-12" onClick={(event) => event.stopPropagation()}>
        <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="flex items-center gap-2 text-[15px] font-semibold text-ink">
                <Languages size={16} className="text-brand" strokeWidth={1.75} />
                翻译
              </h2>
              {subtitle ? <p className="mt-0.5 truncate text-[11.5px] leading-4 text-faint">{subtitle}</p> : null}
            </div>
            <button
              type="button"
              onClick={close}
              disabled={locked}
              aria-label="关闭"
              className="rounded-md p-1 text-mute hover:bg-fog/5 hover:text-ink disabled:opacity-50"
            >
              <X size={16} strokeWidth={1.75} />
            </button>
          </div>

          {phase === "prepare" ? (
            <>
              <div className="rounded-xl bg-fog/[0.03] p-3.5">
                <div className="mb-2 flex items-center justify-between gap-3 px-0.5">
                  <span className="text-[12.5px] text-body">源文本</span>
                  <span className="text-[11px] text-faint">{sourceText.length} 字</span>
                </div>
                <div className="max-h-44 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-pit p-2.5 text-[12.5px] leading-5 text-mute scrollbar-thin select-text">
                  {sourceText}
                </div>
              </div>

              <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 px-0.5">
                <span className="text-[12.5px] text-body">翻译为</span>
                <label className="flex cursor-pointer select-none items-center gap-1.5 text-[12.5px] text-body">
                  <input
                    type="radio"
                    name="translate-target"
                    checked={target === "zh"}
                    onChange={() => setTarget("zh")}
                    className="h-3.5 w-3.5 accent-brand"
                  />
                  中文
                </label>
                <label className="flex cursor-pointer select-none items-center gap-1.5 text-[12.5px] text-body">
                  <input
                    type="radio"
                    name="translate-target"
                    checked={target === "en"}
                    onChange={() => setTarget("en")}
                    className="h-3.5 w-3.5 accent-brand"
                  />
                  English
                </label>
                <span className="text-[11px] leading-4 text-faint">已按内容语言预选</span>
              </div>

              {error ? (
                <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-rose">
                  <CircleAlert size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                  {error}
                </p>
              ) : null}

              <div className="mt-5 flex items-center gap-2">
                <span className="flex-1 text-[11px] leading-4 text-faint">仅支持中文与英文互译。</span>
                <button
                  type="button"
                  onClick={close}
                  className="rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12]"
                >
                  取消
                </button>
                <button
                  type="button"
                  disabled={busy || !sourceText.trim()}
                  onClick={() => void run()}
                  className="flex items-center gap-1.5 rounded-full bg-send px-4 py-1.5 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover disabled:opacity-60"
                >
                  {busy ? (
                    <Loader2 size={13} className="animate-spin" strokeWidth={2} />
                  ) : (
                    <Languages size={13} strokeWidth={1.75} />
                  )}
                  {busy ? "翻译中…" : "翻译"}
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="rounded-xl bg-fog/[0.03] p-3.5">
                <div className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-pit p-2.5 text-[13.5px] leading-6 text-ink scrollbar-thin select-text">
                  {result?.text}
                </div>
                <div className="mt-2 flex items-center gap-1.5 px-0.5 text-[11px] text-faint">
                  <Languages size={11} strokeWidth={1.75} className="shrink-0" />
                  检测到{langLabel(result?.detected_source_language ?? "")} → 目标语言{LANG_LABEL[result?.target ?? "zh"]}
                </div>
              </div>

              {savedTitle ? (
                <p className="mt-3 flex items-start gap-1.5 text-[12px] leading-4 text-ok">
                  <CheckCircle2 size={13} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                  已存为笔记《{savedTitle}》
                </p>
              ) : null}
              {saveError ? (
                <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-rose">
                  <CircleAlert size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                  {saveError}
                </p>
              ) : null}

              <div className="mt-5 flex flex-wrap items-center gap-2">
                <span className="flex-1 text-[11px] leading-4 text-faint">
                  {copyState === "fail" ? "复制失败：请手动选中文本复制。" : "译文已可选中，也可以直接复制。"}
                </span>
                <button
                  type="button"
                  onClick={backToPrepare}
                  className="flex items-center gap-1.5 rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12]"
                >
                  <RotateCcw size={12} strokeWidth={1.75} />
                  返回
                </button>
                <button
                  type="button"
                  onClick={() => void copyResult()}
                  className="flex items-center gap-1.5 rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12]"
                >
                  {copyState === "ok" ? (
                    <Check size={13} className="text-ok" strokeWidth={2} />
                  ) : (
                    <Copy size={13} strokeWidth={1.75} />
                  )}
                  {copyState === "ok" ? "已复制" : "复制"}
                </button>
                <button
                  type="button"
                  disabled={savingNote || Boolean(savedTitle)}
                  onClick={() => void saveAsNote()}
                  className="flex items-center gap-1.5 rounded-full bg-send px-4 py-1.5 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover disabled:opacity-60"
                >
                  {savingNote ? (
                    <Loader2 size={13} className="animate-spin" strokeWidth={2} />
                  ) : (
                    <NotebookPen size={13} strokeWidth={1.75} />
                  )}
                  {savedTitle ? "已存为笔记" : savingNote ? "保存中…" : "存为笔记"}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
