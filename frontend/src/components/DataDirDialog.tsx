import { useState } from "react";
import { AlertCircle, Check, FolderOpen, Loader2, X } from "lucide-react";
import { ApiError, pickDirectory, type DataState, type SettingsIn } from "../api";
import { cx } from "../utils";

/** 界面优化条目 3：数据服务弹窗——自定义向量库（数据库 / 缓存）的存放位置。 */

const inputCls =
  "h-9 w-full rounded-lg border border-line bg-fog/[0.04] px-3 text-[13.5px] text-ink outline-none transition-colors placeholder:text-faint focus:border-edge";

function formatSize(bytes: number) {
  if (!bytes) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function vectorText(vectors: number) {
  return vectors < 0 ? "还没有向量集合" : `${vectors} 条向量`;
}

export default function DataDirDialog({
  data,
  onSave,
  onClose,
}: {
  data: DataState;
  onSave(payload: SettingsIn): Promise<boolean>;
  onClose(): void;
}) {
  const [value, setValue] = useState(data.chroma_dir);
  const [migrate, setMigrate] = useState(true);
  const [picking, setPicking] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const target = value.trim();
  const changed = target !== data.chroma_dir.trim();
  const hasVectors = data.vectors > 0;

  async function choose() {
    setPicking(true);
    setNote("");
    setError("");
    try {
      const result = await pickDirectory(target || data.chroma_path);
      if (!result.available) {
        // 本机没有可用的图形对话框：如实说明，用户手填即可
        setNote(result.detail || "打不开系统选择框，请手动填写路径。");
      } else if (result.path) {
        setValue(result.path);
      }
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "打不开系统选择框，请手动填写路径。");
    } finally {
      setPicking(false);
    }
  }

  async function save() {
    if (!target) {
      setError("请填写数据目录；不知道填什么就点「选择文件夹」。");
      return;
    }
    setError("");
    setSaving(true);
    const ok = await onSave({ data: { chroma_dir: target, migrate } });
    setSaving(false);
    if (ok) onClose();
  }

  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={onClose}>
      <div className="mx-auto mt-16 w-full max-w-xl px-4 pb-12" onClick={(event) => event.stopPropagation()}>
        <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-semibold text-ink">数据服务</h2>
              <p className="mt-0.5 text-[11.5px] leading-4 text-faint">
                选择数据库 / 缓存的存放位置，保存后立即生效，并写入本机 .env。
              </p>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="rounded-md p-1 text-mute hover:bg-fog/5 hover:text-ink"
            >
              <X size={16} strokeWidth={1.75} />
            </button>
          </div>

          <div className="rounded-xl bg-fog/[0.03] p-3.5">
            <div className="mb-1.5 flex items-center justify-between gap-3 px-0.5">
              <span className="text-[12.5px] text-body">存放位置</span>
              <span className="truncate text-[11px] text-faint">
                当前：{vectorText(data.vectors)} · {formatSize(data.size_bytes)}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <input
                value={value}
                onChange={(event) => setValue(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void save();
                }}
                placeholder="例如 D:\zhilan\data\chroma"
                spellCheck={false}
                className={cx(inputCls, "font-mono text-[12.5px]")}
              />
              <button
                type="button"
                onClick={() => void choose()}
                disabled={picking}
                className="flex h-9 shrink-0 items-center gap-1.5 rounded-lg bg-fog/[0.07] px-3 text-[12.5px] text-body transition-colors hover:bg-fog/[0.12] hover:text-ink disabled:opacity-60"
              >
                {picking ? (
                  <Loader2 size={13} className="animate-spin" strokeWidth={2} />
                ) : (
                  <FolderOpen size={13} strokeWidth={1.75} />
                )}
                {picking ? "选择中…" : "选择文件夹…"}
              </button>
            </div>

            <p className="mt-2 px-0.5 text-[11px] leading-4 text-faint">
              生效目录：<span className="font-mono text-[11px] text-mute">{data.chroma_path}</span>
              {data.exists ? "" : "（目录还没建，保存时会自动创建）"}
            </p>

            {changed ? (
              <div className="mt-3 border-t border-line pt-3">
                <button
                  type="button"
                  onClick={() => setMigrate((on) => !on)}
                  className="flex w-full items-start gap-2 text-left"
                >
                  <span
                    className={cx(
                      "mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border",
                      migrate ? "border-brand bg-brand text-canvas" : "border-line",
                    )}
                  >
                    {migrate ? <Check size={10} strokeWidth={3} /> : null}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-[12.5px] text-body">把现有向量一起复制过去</span>
                    <span className="mt-0.5 block text-[11px] leading-4 text-faint">
                      只复制、不删旧目录，随时可以改回来；复制后会核对条数，对不上就不切换。
                    </span>
                  </span>
                </button>

                {hasVectors && !migrate ? (
                  <p className="mt-2 flex items-start gap-1.5 text-[11.5px] leading-4 text-warn">
                    <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                    新目录不会自动带上这 {data.vectors} 条向量，检索会先退化到纯关键词；要沿用旧数据，
                    就把 {data.chroma_path} 整个复制过去，或保存后到知识库页点「重建索引」。
                  </p>
                ) : null}
              </div>
            ) : null}

            {note ? (
              <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-warn">
                <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                {note}
              </p>
            ) : null}

            {error ? (
              <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-rose">
                <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                {error}
              </p>
            ) : null}
          </div>

          <p className="mt-3 text-[11px] leading-4 text-faint">
            这里改的是向量库（本机缓存）的位置，对应 .env 的 <span className="font-mono">RAG_CHROMA_DIR</span>
            ，相对路径按 backend/ 算。MySQL 与 Redis 的连接仍在 .env 中配置，不受这里影响。
          </p>

          <div className="mt-5 flex items-center gap-2">
            <span className="flex-1 text-[11px] leading-4 text-faint">
              {changed ? "保存后立即生效，并写入本机 .env。" : "路径没改，保存不会有变化。"}
            </span>
            <button
              type="button"
              onClick={onClose}
              className="rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12]"
            >
              取消
            </button>
            <button
              type="button"
              disabled={saving || !changed}
              onClick={() => void save()}
              className="flex items-center gap-1.5 rounded-full bg-send px-4 py-1.5 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover disabled:opacity-60"
            >
              {saving ? <Loader2 size={13} className="animate-spin" strokeWidth={2} /> : null}
              {saving ? "保存中…" : "保存"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
