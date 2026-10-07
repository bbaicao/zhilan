import { useEffect, useRef, useState } from "react";
import { AlertCircle, Check, Download, FolderOpen, Loader2, X } from "lucide-react";
import { ApiError, getOfflineModels, importOfflineModels, pickModelArchive, type OfflineModelsStatus } from "../api";

const inputCls =
  "h-9 w-full rounded-lg border border-line bg-fog/[0.04] px-3 text-[13.5px] text-ink outline-none transition-colors placeholder:text-faint focus:border-edge disabled:opacity-50";

function errorMessage(err: unknown) {
  return err instanceof ApiError ? err.message : "操作未完成，请稍后重试。";
}

function phaseLabel(phase: string) {
  return ({
    checking: "正在检查模型包",
    "checking-existing": "正在核对已有模型",
    importing: "正在导入模型",
    completed: "导入完成",
    failed: "导入未完成",
  } as Record<string, string>)[phase] || "正在导入";
}

export default function OfflineModelsDialog({ onClose }: { onClose(): void }) {
  const [status, setStatus] = useState<OfflineModelsStatus | null>(null);
  const [path, setPath] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [picking, setPicking] = useState(false);
  const [starting, setStarting] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const mounted = useRef(false);
  const sequence = useRef(0);
  const dialog = useRef<HTMLDivElement>(null);
  const running = status?.state === "running";
  const busy = running || starting;
  const canStart = Boolean(path.trim()) && Boolean(status) && !loading && !loadError && !busy && !picking;

  useEffect(() => {
    mounted.current = true;
    const previousFocus = document.activeElement;
    dialog.current?.focus();
    return () => {
      mounted.current = false;
      sequence.current += 1;
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
    };
  }, []);

  useEffect(() => {
    if (starting) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function read() {
      const requestId = ++sequence.current;
      try {
        const result = await getOfflineModels();
        if (!active || requestId !== sequence.current) return;
        setStatus(result);
        setLoadError("");
        setLoading(false);
        if (result.state === "running") timer = setTimeout(read, 1000);
      } catch (err) {
        if (!active || requestId !== sequence.current) return;
        setLoadError(`模型状态读取失败：${errorMessage(err)}`);
        setLoading(false);
      }
    }
    void read();
    return () => {
      active = false;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [refresh, starting]);

  async function choose() {
    if (busy || picking || loading || loadError) return;
    setPicking(true);
    setNote("");
    setError("");
    try {
      const result = await pickModelArchive(path.trim());
      if (!mounted.current) return;
      if (!result.available) setNote(result.detail || "文件选择框暂时不可用，请填写模型包的完整路径。");
      else if (result.path) setPath(result.path);
    } catch (err) {
      if (mounted.current) setNote(`文件选择框未能打开：${errorMessage(err)} 可填写模型包的完整路径。`);
    } finally {
      if (mounted.current) setPicking(false);
    }
  }

  async function start() {
    if (!canStart) return;
    // 开始导入时作废在途状态请求，避免旧的 idle 覆盖新任务。
    sequence.current += 1;
    setStarting(true);
    setError("");
    setNote("");
    try {
      const result = await importOfflineModels(path.trim());
      if (mounted.current) setStatus(result);
    } catch (err) {
      if (mounted.current) setError(errorMessage(err));
    } finally {
      if (mounted.current) {
        // 请求异常时也重新确认当前任务，避免请求已被接收后重复启动。
        setLoading(true);
        setStarting(false);
      }
    }
  }

  function retry() {
    setLoading(true);
    setLoadError("");
    setRefresh((value) => value + 1);
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-veil px-4 animate-fade-in"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          event.preventDefault();
          dialog.current?.focus();
        }
      }}
    >
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="offline-models-title"
        aria-describedby="offline-models-description"
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.stopPropagation();
            onClose();
          } else if (event.key === "Tab") {
            const buttons = dialog.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled)");
            if (!buttons?.length) {
              event.preventDefault();
              return;
            }
            const first = buttons[0];
            const last = buttons[buttons.length - 1];
            if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) {
              event.preventDefault();
              last.focus();
            } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) {
              event.preventDefault();
              first.focus();
            }
          }
        }}
        className="max-h-[90vh] w-full max-w-[560px] overflow-y-auto rounded-2xl border border-line bg-panel p-6 shadow-pop outline-none scrollbar-thin animate-fade-up"
      >
        <div className="mb-4 flex items-center justify-between gap-3">
          <h2 id="offline-models-title" className="flex items-center gap-2 text-[17px] font-semibold text-ink">
            <Download size={18} className="text-brand" />
            离线文档解析模型
          </h2>
          <button type="button" onClick={onClose} aria-label="关闭模型导入窗口" className="rounded-lg p-1.5 text-mute hover:bg-fog/[0.05]">
            <X size={18} />
          </button>
        </div>
        <p id="offline-models-description" className="text-[12.5px] leading-6 text-mute">
          导入知澜配套的离线模型包，用于扫描 PDF 和图片文字识别。导入无需联网；对话与向量模型仍需在设置中配置。
        </p>

        <div className="my-4 rounded-xl border border-line bg-fog/[0.03] p-3 text-[12.5px] leading-6">
          <div className="text-body">{status ? `本机已有 ${status.present_files} / ${status.total_files} 件模型文件` : loadError ? "模型状态暂时不可用" : "正在读取本机模型状态…"}</div>
          <div className="mt-1 text-mute">模型存放位置</div>
          <div className="break-all text-ink">{status?.model_dir || "读取后显示"}</div>
        </div>
        {loadError ? (
          <div role="alert" className="mb-4 rounded-xl bg-rose/10 p-3 text-[12.5px] text-rose">
            <div className="flex items-start gap-2"><AlertCircle size={16} className="mt-0.5 shrink-0" />{loadError}</div>
            <button type="button" onClick={retry} disabled={loading} className="mt-2 rounded-lg border border-rose/30 px-3 py-1 disabled:opacity-50">
              {loading ? "正在重试…" : "重新读取状态"}
            </button>
          </div>
        ) : null}

        <label htmlFor="offline-models-path" className="mb-1.5 block text-[12px] text-mute">离线模型包（ZIP）</label>
        <div className="flex items-center gap-2">
          <input
            id="offline-models-path"
            value={path}
            onChange={(event) => setPath(event.target.value)}
            disabled={busy || picking || loading || Boolean(loadError)}
            placeholder="选择模型包，或填写完整文件路径"
            className={inputCls}
          />
          <button
            type="button"
            onClick={() => void choose()}
            disabled={busy || picking || loading || Boolean(loadError)}
            className="flex h-9 shrink-0 items-center gap-1.5 rounded-lg border border-line px-3 text-[12.5px] text-body hover:bg-fog/[0.05] disabled:opacity-50"
          >
            {picking ? <Loader2 size={15} className="animate-spin" /> : <FolderOpen size={15} />}
            {picking ? "正在选择…" : "选择模型包"}
          </button>
        </div>
        <p className="mt-2 break-all text-[11.5px] leading-5 text-faint">
          使用交付目录中的 zhilan-models-offline-20261007.zip。导入会核对包内文件的完整性，已有且一致的文件自动跳过。
        </p>
        {note ? <p role="status" className="mt-3 text-[12px] leading-5 text-warn">{note}</p> : null}
        {error ? <p role="alert" className="mt-3 text-[12.5px] leading-5 text-rose">{error}</p> : null}

        {busy ? (
          <div role="status" className="mt-4 rounded-xl bg-brand-soft p-3 text-[12.5px] leading-6 text-ink">
            <div className="flex items-center gap-2"><Loader2 size={16} className="animate-spin text-brand" />{starting ? "正在开始导入…" : phaseLabel(status?.phase || "")}</div>
            {running ? (
              <>
                <progress className="mt-2 h-2 w-full accent-brand" max={status.total_files} value={status.completed_files} aria-label="模型导入进度" />
                <div>已完成 {status.completed_files} / {status.total_files} 件 · 新导入 {status.imported_files} 件 · 已一致跳过 {status.skipped_files} 件</div>
                {status.detail ? <div className="break-words text-mute">{status.detail}</div> : null}
              </>
            ) : null}
            <div className="mt-1 text-mute">关闭窗口后仍会继续导入，重新打开此入口可查看进度。</div>
          </div>
        ) : status?.state === "succeeded" ? (
          <div role="status" className="mt-4 rounded-xl bg-ok/10 p-3 text-[12.5px] leading-6 text-ok">
            <div className="flex items-center gap-2"><Check size={16} />模型导入完成</div>
            <div>新导入 {status.imported_files} 件 · 已一致跳过 {status.skipped_files} 件</div>
            {status.detail ? <div className="break-words">{status.detail}</div> : null}
          </div>
        ) : status?.state === "failed" ? (
          <div role="alert" className="mt-4 rounded-xl bg-rose/10 p-3 text-[12.5px] leading-6 text-rose">
            <div className="flex items-center gap-2"><AlertCircle size={16} />模型导入未完成</div>
            <div className="break-words">{status.detail || "请检查模型包后重试。"}</div>
            <div>已完成 {status.completed_files} / {status.total_files} 件；可重新选择模型包后再次导入。</div>
          </div>
        ) : null}

        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onClose} className="rounded-lg border border-line px-4 py-2 text-[12.5px] text-body hover:bg-fog/[0.05]">
            {busy ? "关闭窗口，继续导入" : "关闭"}
          </button>
          <button
            type="button"
            onClick={() => void start()}
            disabled={!canStart}
            className="flex items-center gap-2 rounded-lg bg-brand px-4 py-2 text-[12.5px] font-medium text-canvas disabled:opacity-50"
          >
            {busy || loading ? <Loader2 size={15} className="animate-spin" /> : <Download size={15} />}
            {busy ? "导入中…" : loading ? "正在读取状态…" : status?.state === "failed" ? "重新导入" : "开始导入"}
          </button>
        </div>
      </div>
    </div>
  );
}
