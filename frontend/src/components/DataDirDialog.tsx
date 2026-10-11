import { X } from "lucide-react";
import type { DataState, SettingsIn } from "../api";

/** 现有 data 契约只读；设置页不提供目录保存或搬迁。 */
export default function DataDirDialog({
  data,
  onClose,
}: {
  data: DataState;
  onSave(payload: SettingsIn): Promise<boolean>;
  onClose(): void;
}) {
  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={onClose}>
      <div className="mx-auto mt-16 w-full max-w-xl px-4 pb-12" onClick={(event) => event.stopPropagation()}>
        <div role="dialog" aria-modal="true" aria-labelledby="data-dir-title" className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 id="data-dir-title" className="text-[15px] font-semibold text-ink">向量库位置</h2>
              <p className="mt-0.5 text-[11.5px] leading-4 text-faint">
                当前 Qdrant 配置与实际使用位置
              </p>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="shrink-0 rounded-md p-1 text-mute hover:bg-fog/5 hover:text-ink"
            >
              <X size={16} strokeWidth={1.75} />
            </button>
          </div>

          <dl className="space-y-3 border-y border-line py-3.5">
            <div>
              <dt className="text-[12px] text-faint">配置值</dt>
              <dd className="mt-1 break-all font-mono text-[12.5px] leading-5 text-mute">{data.chroma_dir || "未配置"}</dd>
            </div>
            <div>
              <dt className="text-[12px] text-faint">实际位置</dt>
              <dd className="mt-1 break-all font-mono text-[12.5px] leading-5 text-body">{data.chroma_path || "未配置"}</dd>
            </div>
            <div className="text-[11.5px] leading-5 text-faint">
              <p>{data.exists ? "本地目录已存在" : "未检测到本地目录；远程服务地址不作目录检查"}</p>
              <p>{data.vectors < 0 ? "向量数量未统计" : `${data.vectors} 条向量`}</p>
            </div>
          </dl>

          <p className="mt-3 text-[11.5px] leading-5 text-faint">
            向量库位置由本机 .env 的 <span className="font-mono">QDRANT_PATH / QDRANT_URL</span> 配置，修改后需重启。
            相对 QDRANT_PATH 在安装版中按程序所在目录解析，开发环境按项目根目录解析。
            此窗口只读，不创建、复制或搬迁数据。
          </p>
          {data.notice ? <p className="mt-3 break-words text-[11.5px] leading-5 text-mute">{data.notice}</p> : null}

          <div className="mt-5 flex justify-end">
            <button
              type="button"
              onClick={onClose}
              className="rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12]"
            >
              关闭
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
