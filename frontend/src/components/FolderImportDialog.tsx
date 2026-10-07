import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  FileText,
  FolderInput,
  FolderOpen,
  Loader2,
  SkipForward,
  Square,
  X,
} from "lucide-react";
import {
  ApiError,
  createNote,
  duplicateExistingId,
  generateNoteFromDoc,
  importFileFromFolder,
  pickDirectory,
  scanImportFolder,
  updateNote,
  type ImportFolderScanOut,
} from "../api";
import { cx } from "../utils";

/** 条目 39：导入文件夹 → AI 自动整理成笔记。四阶段：选路径 → 预览 → 执行（顺序逐篇）→ 摘要。 */

const inputCls =
  "w-full rounded-lg border border-line bg-fog/[0.04] px-3 py-2 text-[13px] text-ink outline-none transition-colors placeholder:text-faint focus:border-edge";

type ExistingNote = { id: number; title: string };

/** 逐篇结果：ok = 已生成笔记；warn = 生成但有遗漏提示；skip / fail 自带原因 */
type ImportOutcome = {
  kind: "ok" | "warn" | "skip" | "fail";
  name: string;
  text: string;
  /** 成功时生成的笔记 id，摘要里点击跳转 */
  noteId?: number;
};

type FolderImportDialogProps = {
  /** 当前对话模型名（健康接口），写进 AI 代价说明里让用户知道按什么计价 */
  llmModel?: string;
  /** 库内已有笔记（App 用 notes 按 source_document_id 匹配），决定保存时覆盖还是跳过 */
  getExistingNote(docId: number): ExistingNote | null;
  /** 摘要里点成功行：跳转到该笔记 */
  onOpenNote(noteId: number): void;
  /** 弹窗关闭后刷新 docs 与 notes */
  onFinished(): void;
  onClose(): void;
};

function formatSize(bytes: number) {
  if (!bytes) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

const OUTCOME_ICON = {
  ok: CheckCircle2,
  warn: AlertTriangle,
  skip: SkipForward,
  fail: CircleAlert,
} as const;

const OUTCOME_CLS: Record<ImportOutcome["kind"], string> = {
  ok: "text-ok",
  warn: "text-warn",
  skip: "text-mute",
  fail: "text-rose",
};

export default function FolderImportDialog({
  llmModel,
  getExistingNote,
  onOpenNote,
  onFinished,
  onClose,
}: FolderImportDialogProps) {
  const [phase, setPhase] = useState<"pick" | "preview" | "running" | "summary">("pick");

  // 阶段一：选路径
  const [dirPath, setDirPath] = useState("");
  /** 条目 39 追加②：是否用 AI 生成笔记（默认开，保持本功能的主线）。关掉=仅导入入库，零模型调用 */
  const [aiNotes, setAiNotes] = useState(true);
  /** 点「选择文件夹…」弹不出系统选择框时的原因；输入框始终可手填 */
  const [pickDetail, setPickDetail] = useState("");
  const [picking, setPicking] = useState(false);
  const [recursive, setRecursive] = useState(true);
  const [overwrite, setOverwrite] = useState(false);
  /** 条目 39 追加：同时生成的篇数（1=顺序）。导入仍排队、同文档保存仍排队，只有生成在并发 */
  const [parallel, setParallel] = useState(3);
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState("");

  // 阶段二：预览
  const [scan, setScan] = useState<ImportFolderScanOut | null>(null);
  const [skipOpen, setSkipOpen] = useState(false);

  // 阶段三：执行
  const [progress, setProgress] = useState<{ done: number; total: number; active: string[] } | null>(null);
  const [outcomes, setOutcomes] = useState<ImportOutcome[]>([]);
  /** 停止：完成当前这篇后停下（不打断进行中的请求） */
  const [stopping, setStopping] = useState(false);
  const [stopped, setStopped] = useState(false);
  const stopRef = useRef(false);
  /** 防连点：一次批量只允许跑一个循环 */
  const runningRef = useRef(false);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [outcomes.length, progress?.done, progress?.active.length]);

  /** 点「选择文件夹…」才弹系统选择框；本机弹不出来（available=false）时如实说明，退回手填 */
  async function choose() {
    if (picking) return;
    setPicking(true);
    setPickDetail("");
    try {
      const result = await pickDirectory("");
      if (!result.available) setPickDetail(result.detail || "打不开系统选择框，请手动填写路径。");
      else if (result.path) setDirPath(result.path);
    } catch (err) {
      setPickDetail(err instanceof ApiError ? err.message : "打不开系统选择框，请手动填写路径。");
    } finally {
      setPicking(false);
    }
  }

  /** 所有关闭路径统一先刷新（onFinished）再卸载；执行中不允许关，先点「停止」 */
  function close() {
    if (phase === "running") return;
    onFinished();
    onClose();
  }

  async function doScan() {
    const path = dirPath.trim();
    if (!path || scanning) return;
    setScanning(true);
    setScanError("");
    try {
      const result = await scanImportFolder(path, recursive);
      setScan(result);
      setSkipOpen(false);
      setPhase("preview");
    } catch (err) {
      setScanError(err instanceof ApiError ? err.message : "扫描文件夹失败");
    } finally {
      setScanning(false);
    }
  }

  /** 并发下的两把锁：导入全局串行（同内容文件并发插入会绕过内容指纹去重、产生重复文档）；
   * 同一文档的保存按文档串行 + 保存前重查批内笔记表（两个 worker 复用同一 409 文档时只建一条笔记）。
   * 生成阶段不加锁——那是耗时大头（几十秒/篇），并发收益全在这里。 */
  function makeLock() {
    let tail = Promise.resolve();
    return function run<T>(task: () => Promise<T>): Promise<T> {
      const next = tail.then(task);
      tail = next.then(
        () => undefined,
        () => undefined,
      );
      return next;
    };
  }

  function sleep(ms: number) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function runImport() {
    if (!scan || scan.files.length === 0 || runningRef.current) return;
    const files = scan.files;
    const overwriteNow = overwrite;
    // 关掉 AI 时整批只剩导入（导入本就全局串行），并发没有意义，强制 1
    const limit = aiNotes ? Math.max(1, Math.min(4, parallel)) : 1;
    runningRef.current = true;
    stopRef.current = false;
    setStopping(false);
    setStopped(false);
    setOutcomes([]);
    setPhase("running");
    setProgress({ done: 0, total: files.length, active: [] });
    const batchNotes = new Map<number, ExistingNote>();
    const importLock = makeLock();
    const saveLocks = new Map<number, (task: () => Promise<ImportOutcome | null>) => Promise<ImportOutcome | null>>();
    const done: ImportOutcome[] = [];
    let next = 0;

    const bump = (mutate: (p: { done: number; total: number; active: string[] }) => void) => {
      setProgress((prev) => {
        const nextValue = { done: prev?.done ?? 0, total: files.length, active: [...(prev?.active ?? [])] };
        mutate(nextValue);
        return nextValue;
      });
    };

    /** 单篇三步（导入→生成→保存）。抛错由 processOne 统一重试 */
    async function attempt(file: (typeof files)[number]): Promise<ImportOutcome> {
      // ① 入库（全局排队）：内容重复（409）时复用库内文档继续生成，不算失败
      let docId: number;
      let reused = false;
      try {
        const doc = await importLock(() => importFileFromFolder(file.path));
        docId = doc.id;
      } catch (err) {
        const existingId = duplicateExistingId(err);
        if (existingId === null) throw err;
        docId = existingId;
        reused = true;
      }
      // 关闭 AI：到此为止，只入库不调用模型（ok 行无 noteId，摘要里不可点跳笔记）
      if (!aiNotes) {
        return { kind: "ok", name: file.name, text: reused ? "已入库（复用库内文档）" : "已入库" };
      }
      // ② 生成笔记草稿（可能耗时几十秒，是并发收益的来源）
      const draft = await generateNoteFromDoc(docId);
      // ③ 保存（按文档排队，队内重查）：已有笔记且未勾覆盖 → 跳过；否则 update / create（溯源不动）
      const saveChain = saveLocks.get(docId) ?? makeLock();
      saveLocks.set(docId, saveChain);
      const outcome = await saveChain(async () => {
        const existing = batchNotes.get(docId) ?? getExistingNote(docId);
        if (existing && !overwriteNow) {
          return { kind: "skip", name: file.name, text: `已有笔记《${existing.title}》，未覆盖` } as ImportOutcome;
        }
        const generationMeta = {
          generated_at: new Date().toISOString(),
          completeness: draft.completeness,
          source_filename: draft.source_filename,
        };
        const saved = existing
          ? await updateNote(existing.id, {
              title: draft.title,
              content: draft.content,
              category: draft.category,
              tags: draft.tags,
              generation_meta: generationMeta,
            })
          : await createNote({
              title: draft.title,
              content: draft.content,
              category: draft.category,
              source_document_id: docId,
              tags: draft.tags,
              generation_meta: generationMeta,
            });
        batchNotes.set(docId, { id: saved.id, title: saved.title });
        const reuseNote = reused ? "（已入库，复用库内文档）" : "";
        return draft.completeness.complete
          ? { kind: "ok", name: file.name, text: "已生成笔记" + reuseNote, noteId: saved.id }
          : ({
              kind: "warn",
              name: file.name,
              text: `可能遗漏 ${draft.completeness.missing.length} 项` + reuseNote,
              noteId: saved.id,
            } as ImportOutcome);
      });
      return outcome as ImportOutcome;
    }

    /** 单篇失败退避重试一次（厂商限流/网络抖动）；再失败记原因继续，绝不中断整批 */
    async function processOne(file: (typeof files)[number]): Promise<ImportOutcome> {
      try {
        return await attempt(file);
      } catch {
        await sleep(4000);
        try {
          return await attempt(file);
        } catch (err) {
          return { kind: "fail", name: file.name, text: err instanceof ApiError ? err.message : "处理失败" };
        }
      }
    }

    async function worker() {
      while (!stopRef.current) {
        const index = next++;
        if (index >= files.length) return;
        const file = files[index];
        bump((p) => p.active.push(file.name));
        const outcome = await processOne(file);
        bump((p) => {
          p.done += 1;
          const at = p.active.indexOf(file.name);
          if (at >= 0) p.active.splice(at, 1);
        });
        done.push(outcome);
        setOutcomes([...done]);
      }
    }

    await Promise.all(Array.from({ length: Math.min(limit, files.length) }, () => worker()));
    setStopped(stopRef.current);
    runningRef.current = false;
    setProgress(null);
    setPhase("summary");
  }

  function requestStop() {
    stopRef.current = true;
    setStopping(true);
  }

  /** 摘要里点成功行：跳转笔记并关闭弹窗 */
  function openNoteFromSummary(noteId: number) {
    onOpenNote(noteId);
    close();
  }

  const files = scan?.files ?? [];
  const skipped = scan?.skipped ?? [];
  const totalBytes = files.reduce((sum, file) => sum + file.size_bytes, 0);
  const okRows = outcomes.filter((row) => row.kind === "ok" || row.kind === "warn");
  const warnCount = outcomes.filter((row) => row.kind === "warn").length;
  const skipCount = outcomes.filter((row) => row.kind === "skip").length;
  const failCount = outcomes.filter((row) => row.kind === "fail").length;
  const unprocessed = Math.max(0, files.length - outcomes.length);

  const subtitle =
    phase === "pick"
      ? aiNotes
        ? "选择本机文件夹，逐篇导入并自动生成 AI 笔记。"
        : "选择本机文件夹，仅导入入库（不调用 AI）。"
      : phase === "preview"
        ? scan?.path || ""
        : phase === "running"
          ? "正在导入并生成笔记，请保持页面打开。"
          : "导入完成。";

  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={phase === "running" ? undefined : close}>
      <div className="mx-auto mt-16 w-full max-w-2xl px-4 pb-12" onClick={(event) => event.stopPropagation()}>
        <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="text-[15px] font-semibold text-ink">导入文件夹</h2>
              {subtitle ? <p className="mt-0.5 truncate text-[11.5px] leading-4 text-faint">{subtitle}</p> : null}
            </div>
            <button
              type="button"
              onClick={close}
              disabled={phase === "running"}
              aria-label="关闭"
              className="rounded-md p-1 text-mute hover:bg-fog/5 hover:text-ink disabled:opacity-50"
            >
              <X size={16} strokeWidth={1.75} />
            </button>
          </div>

          {phase === "pick" ? (
            <>
              <label htmlFor="folder-import-path" className="mb-1.5 block text-[12.5px] text-mute">
                文件夹路径
              </label>
              <div className="flex items-stretch gap-2">
                <input
                  id="folder-import-path"
                  value={dirPath}
                  onChange={(event) => setDirPath(event.target.value)}
                  placeholder="例如 D:\资料\论文"
                  spellCheck={false}
                  className={cx(inputCls, "min-w-0 flex-1 font-mono text-[12.5px]")}
                />
                <button
                  type="button"
                  onClick={() => void choose()}
                  disabled={picking}
                  className="flex shrink-0 items-center gap-1.5 rounded-lg bg-fog/[0.07] px-3 text-[12.5px] text-body transition-colors hover:bg-fog/[0.12] hover:text-ink disabled:opacity-60"
                >
                  {picking ? (
                    <Loader2 size={13} className="animate-spin" strokeWidth={2} />
                  ) : (
                    <FolderOpen size={13} strokeWidth={1.75} />
                  )}
                  {picking ? "选择中…" : "选择文件夹…"}
                </button>
              </div>
              {pickDetail ? (
                <p className="mt-1.5 px-0.5 text-[11px] leading-4 text-faint">{pickDetail}</p>
              ) : null}
              <div className="mt-3">
                <label className="flex cursor-pointer select-none items-center gap-1.5 text-[12.5px] text-body">
                  <input
                    type="checkbox"
                    checked={aiNotes}
                    onChange={(event) => setAiNotes(event.target.checked)}
                    className="h-3.5 w-3.5 accent-brand"
                  />
                  用 AI 生成笔记
                </label>
                <p className="mt-1 px-0.5 text-[11px] leading-4 text-faint">
                  {aiNotes
                    ? `开启后每篇约 2 次对话模型调用（读全文写笔记＋核对完整性，输出上限合计约 6000 token，超长文档按段追加），费用按当前对话模型${llmModel ? `「${llmModel}」` : ""}的厂商单价计。`
                    : "已关闭：仅把文件导入入库，不调用 AI、不产生模型费用。"}
                </p>
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2">
                <label className="flex cursor-pointer select-none items-center gap-1.5 text-[12.5px] text-body">
                  <input
                    type="checkbox"
                    checked={recursive}
                    onChange={(event) => setRecursive(event.target.checked)}
                    className="h-3.5 w-3.5 accent-brand"
                  />
                  包含子文件夹
                </label>
                {aiNotes ? (
                  <label
                    title="勾选后，已有 AI 笔记的文档会用新笔记覆盖旧笔记"
                    className="flex cursor-pointer select-none items-center gap-1.5 text-[12.5px] text-body"
                  >
                    <input
                      type="checkbox"
                      checked={overwrite}
                      onChange={(event) => setOverwrite(event.target.checked)}
                      className="h-3.5 w-3.5 accent-brand"
                    />
                    覆盖已有 AI 笔记
                  </label>
                ) : null}
              </div>
              {aiNotes ? (
                <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
                  <span className="text-[12.5px] text-body" title="同时生成笔记的篇数；越大越快，过猛可能触发厂商限流">
                    同时生成
                  </span>
                  {[1, 2, 3, 4].map((count) => (
                    <label key={count} className="flex cursor-pointer select-none items-center gap-1.5 text-[12.5px] text-body">
                      <input
                        type="radio"
                        name="folder-import-parallel"
                        checked={parallel === count}
                        onChange={() => setParallel(count)}
                        className="h-3.5 w-3.5 accent-brand"
                      />
                      {count} 篇
                    </label>
                  ))}
                  <span className="text-[11px] leading-4 text-faint">1 篇 = 顺序处理；失败会自动重试一次</span>
                </div>
              ) : null}
              {scanError ? (
                <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-rose">
                  <CircleAlert size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                  {scanError}
                </p>
              ) : null}
              <div className="mt-5 flex items-center gap-2">
                <span className="flex-1 text-[11px] leading-4 text-faint">扫描后先预览，不会立刻导入。</span>
                <button
                  type="button"
                  onClick={close}
                  className="rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12]"
                >
                  取消
                </button>
                <button
                  type="button"
                  disabled={!dirPath.trim() || scanning}
                  onClick={() => void doScan()}
                  className="flex items-center gap-1.5 rounded-full bg-send px-4 py-1.5 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover disabled:opacity-60"
                >
                  {scanning ? (
                    <Loader2 size={13} className="animate-spin" strokeWidth={2} />
                  ) : (
                    <FolderInput size={13} strokeWidth={1.75} />
                  )}
                  {scanning ? "正在扫描…" : "扫描文件夹"}
                </button>
              </div>
            </>
          ) : phase === "preview" && scan ? (
            <>
              <div className="rounded-xl bg-fog/[0.03] p-3.5">
                <div className="mb-2 flex items-center justify-between gap-3 px-0.5">
                  <span className="text-[12.5px] text-body">可导入 {files.length} 个文件</span>
                  {files.length > 0 ? <span className="text-[11px] text-faint">共 {formatSize(totalBytes)}</span> : null}
                </div>
                {files.length === 0 ? (
                  <p className="px-0.5 py-2 text-[13px] text-mute">没有可导入的文件。</p>
                ) : (
                  <div className="max-h-56 overflow-auto rounded-lg bg-pit p-1 scrollbar-thin">
                    {files.map((file) => (
                      <div key={file.path} className="flex items-center gap-2 rounded-md px-1.5 py-1">
                        <FileText size={12} strokeWidth={1.75} className="shrink-0 text-faint" />
                        <span className="min-w-0 flex-1 truncate text-[12px] text-ink">{file.name}</span>
                        <span className="shrink-0 text-[11px] text-faint">{formatSize(file.size_bytes)}</span>
                      </div>
                    ))}
                  </div>
                )}
                {scan.truncated ? (
                  <p className="mt-2 flex items-start gap-1.5 text-[11.5px] leading-4 text-warn">
                    <AlertTriangle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                    文件较多，结果已截断：仅列出前 {files.length} 个。建议收窄路径或取消「包含子文件夹」后分批导入。
                  </p>
                ) : null}
              </div>

              {skipped.length > 0 ? (
                <div className="mt-3 rounded-xl bg-fog/[0.03] p-3.5">
                  <button
                    type="button"
                    onClick={() => setSkipOpen((open) => !open)}
                    aria-expanded={skipOpen}
                    className="flex w-full items-center gap-1.5 px-0.5 text-left text-[12.5px] text-body transition-colors hover:text-ink"
                  >
                    <ChevronDown
                      size={14}
                      strokeWidth={1.75}
                      className={cx("shrink-0 transition-transform", skipOpen && "rotate-180")}
                    />
                    跳过 {skipped.length} 个文件
                  </button>
                  {skipOpen ? (
                    <div className="mt-2 max-h-40 overflow-auto rounded-lg bg-pit p-1 scrollbar-thin">
                      {skipped.map((row) => (
                        <div key={row.path} className="px-1.5 py-1">
                          <span className="block truncate text-[12px] text-mute">{row.name}</span>
                          <span className="block text-[11px] leading-4 text-faint">{row.reason}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : null}

              <div className="mt-5 flex items-center gap-2">
                <span className="flex-1 text-[11px] leading-4 text-faint">
                  {aiNotes
                    ? `将导入并用 AI 生成笔记，每篇可能需要几十秒；同时生成 ${Math.max(1, Math.min(4, parallel))} 篇。`
                    : "将仅导入入库：不调用 AI、不产生模型费用。"}
                </span>
                <button
                  type="button"
                  onClick={() => setPhase("pick")}
                  className="rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12]"
                >
                  返回
                </button>
                <button
                  type="button"
                  disabled={files.length === 0}
                  onClick={() => void runImport()}
                  className="flex items-center gap-1.5 rounded-full bg-send px-4 py-1.5 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover disabled:opacity-60"
                >
                  <CheckCircle2 size={13} strokeWidth={1.75} />
                  开始导入并生成笔记
                </button>
              </div>
            </>
          ) : phase === "running" ? (
            <>
              <div className="rounded-xl bg-fog/[0.03] p-3.5">
                <p className="truncate text-[13px] text-ink">
                  已完成 {progress?.done ?? 0}/{progress?.total ?? files.length} 篇
                  {(progress?.active.length ?? 0) > 0
                    ? ` · 正在${aiNotes ? "生成" : "导入"} ${progress?.active.length} 篇`
                    : ""}
                </p>
                <div className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-fog/[0.08]">
                  <div
                    className="h-full rounded-full bg-brand transition-all"
                    style={{
                      width: `${progress && progress.total > 0 ? (progress.done / progress.total) * 100 : 0}%`,
                    }}
                  />
                </div>
                <p className="mt-2 truncate text-[11px] leading-4 text-faint">
                  {progress && progress.active.length > 0
                    ? `正在处理：${progress.active
                        .slice(0, 3)
                        .map((name) => `《${name}》`)
                        .join("")}${progress.active.length > 3 ? " 等" : ""}`
                    : "即将开始下一篇…"}
                </p>
                <p className="mt-1 text-[11px] leading-4 text-faint">
                  {aiNotes
                    ? `同时生成 ${Math.max(1, Math.min(4, parallel))} 篇；单篇失败自动重试一次，仍失败记原因并继续。`
                    : "仅导入入库，不调用 AI；单篇失败自动重试一次，仍失败记原因并继续。"}
                </p>
              </div>

              <div ref={logRef} className="mt-3 max-h-52 overflow-auto rounded-xl bg-fog/[0.02] p-2 scrollbar-thin">
                {outcomes.length === 0 ? (
                  <p className="px-1.5 py-1 text-[12px] text-faint">还没有完成的文件。</p>
                ) : (
                  outcomes.map((row, index) => {
                    const Icon = OUTCOME_ICON[row.kind];
                    return (
                      <div key={index} className="flex items-start gap-2 px-1.5 py-1">
                        <Icon size={13} strokeWidth={1.75} className={cx("mt-0.5 shrink-0", OUTCOME_CLS[row.kind])} />
                        <span className="min-w-0 flex-1 text-[12px] leading-5">
                          <span className="text-ink">{row.name}</span>
                          <span className={cx("ml-1.5", OUTCOME_CLS[row.kind])}>
                            {row.kind === "skip" || row.kind === "fail" ? `${row.kind === "skip" ? "跳过" : "失败"}：${row.text}` : row.text}
                          </span>
                        </span>
                      </div>
                    );
                  })
                )}
              </div>

              <div className="mt-5 flex items-center gap-2">
                <span className="flex-1 text-[11px] leading-4 text-faint">
                  {stopping ? "不再开始新的一篇，进行中的完成后停止。" : "点「停止」不再开始新的一篇，进行中的完成后停下。"}
                </span>
                <button
                  type="button"
                  disabled={stopping}
                  onClick={requestStop}
                  className="flex items-center gap-1.5 rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12] disabled:opacity-60"
                >
                  <Square size={12} strokeWidth={1.75} />
                  停止
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="space-y-1.5 px-0.5">
                <p className="text-[13px] text-ok">
                  成功 {okRows.length} 篇
                  {warnCount > 0 ? `（其中 ${warnCount} 篇有遗漏提示）` : ""}
                </p>
                <p className="text-[13px] text-mute">跳过 {skipCount} 篇</p>
                <p className={cx("text-[13px]", failCount > 0 ? "text-rose" : "text-mute")}>失败 {failCount} 篇</p>
                {stopped && unprocessed > 0 ? (
                  <p className="text-[12px] text-warn">已停止：剩余 {unprocessed} 篇未处理。</p>
                ) : null}
              </div>

              <div className="mt-3 max-h-64 overflow-auto rounded-xl bg-fog/[0.02] p-1.5 scrollbar-thin">
                {outcomes.length === 0 ? (
                  <p className="px-1.5 py-1 text-[12px] text-faint">没有处理任何文件。</p>
                ) : (
                  outcomes.map((row, index) => {
                    const Icon = OUTCOME_ICON[row.kind];
                    const clickable = row.kind !== "skip" && row.kind !== "fail" && row.noteId != null;
                    const body = (
                      <>
                        <Icon size={13} strokeWidth={1.75} className={cx("mt-0.5 shrink-0", OUTCOME_CLS[row.kind])} />
                        <span className="min-w-0 flex-1 text-[12px] leading-5">
                          <span className="text-ink">{row.name}</span>
                          <span className={cx("ml-1.5", OUTCOME_CLS[row.kind])}>
                            {row.kind === "skip" || row.kind === "fail" ? `${row.kind === "skip" ? "跳过" : "失败"}：${row.text}` : row.text}
                          </span>
                        </span>
                        {clickable ? <ChevronRight size={13} strokeWidth={1.75} className="shrink-0 text-faint" /> : null}
                      </>
                    );
                    return clickable ? (
                      <button
                        key={index}
                        type="button"
                        title="查看这篇笔记"
                        onClick={() => row.noteId != null && openNoteFromSummary(row.noteId)}
                        className="flex w-full items-start gap-2 rounded-md px-1.5 py-1 text-left transition-colors hover:bg-fog/[0.05]"
                      >
                        {body}
                      </button>
                    ) : (
                      <div key={index} className="flex items-start gap-2 px-1.5 py-1">
                        {body}
                      </div>
                    );
                  })
                )}
              </div>

              <div className="mt-5 flex items-center gap-2">
                <span className="flex-1 text-[11px] leading-4 text-faint">关闭后会刷新文档与笔记列表。</span>
                <button
                  type="button"
                  onClick={close}
                  className="flex items-center gap-1.5 rounded-full bg-send px-4 py-1.5 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover"
                >
                  完成
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
