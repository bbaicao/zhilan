import { useState } from "react";
import { AlertCircle, ExternalLink, Loader2, X } from "lucide-react";
import type { RerankProviderInfo, RerankState, SettingsIn } from "../api";
import { cx } from "../utils";

/** 界面优化条目 28：检索增强弹窗——重排序开关、服务商与型号。
 *
 * 型号候选、开启条件、代价、缺依赖原因全部来自后端登记表（app/rag/rerank_spec.py），
 * 界面只负责呈现，不自己编文案、不发明选项。
 */

const inputCls =
  "h-9 w-full rounded-lg border border-line bg-fog/[0.04] px-3 text-[13.5px] text-ink outline-none transition-colors placeholder:text-faint focus:border-edge";

function Toggle({ on, onClick, label }: { on: boolean; onClick(): void; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      onClick={onClick}
      className={cx(
        "relative h-[22px] w-[38px] shrink-0 rounded-full transition-colors",
        on ? "bg-brand" : "bg-fog/[0.14]",
      )}
    >
      <span
        className={cx(
          "absolute top-[3px] h-4 w-4 rounded-full bg-canvas transition-all",
          on ? "left-[18px]" : "left-[3px]",
        )}
      />
    </button>
  );
}

export default function RerankDialog({
  rerank,
  onSave,
  onClose,
}: {
  rerank: RerankState;
  onSave(payload: SettingsIn, onFail?: (message: string) => void): Promise<boolean>;
  onClose(): void;
}) {
  const [enabled, setEnabled] = useState(rerank.enabled);
  const [provider, setProvider] = useState(rerank.provider);
  const [model, setModel] = useState(rerank.model);
  const [baseUrl, setBaseUrl] = useState(rerank.base_url);
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const spec: RerankProviderInfo | undefined =
    rerank.providers.find((row) => row.id === provider) ?? rerank.providers[0];
  const target = model.trim();
  const changed =
    enabled !== rerank.enabled ||
    provider !== rerank.provider ||
    target !== rerank.model ||
    baseUrl.trim().replace(/\/+$/, "") !== rerank.base_url ||
    apiKey.trim() !== "";

  function pickProvider(next: RerankProviderInfo) {
    setProvider(next.id);
    setError("");
    // 换服务商时型号必须跟着换：两家型号不能互相套用，留着上一家的名字只会被厂商拒绝
    if (!target || !next.models.includes(target)) setModel(next.default_model);
  }

  async function save() {
    setError("");
    setSaving(true);
    // 后端拒绝时把可读原因留在弹窗里（缺依赖、缺 Key 这类提示比较长，用户正在看这里）
    const ok = await onSave(
      { rerank: { enabled, provider, model: target, base_url: baseUrl.trim(), api_key: apiKey.trim() } },
      setError,
    );
    setSaving(false);
    if (ok) onClose();
  }

  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={onClose}>
      <div className="mx-auto mt-16 w-full max-w-xl px-4 pb-12" onClick={(event) => event.stopPropagation()}>
        <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-semibold text-ink">检索增强 · 重排序</h2>
              <p className="mt-0.5 text-[11.5px] leading-4 text-faint">
                对向量 + 关键词的融合结果逐条重打相关分再排序，保存后立即生效并写入本机 .env。
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
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-[13.5px] text-ink">启用重排序</div>
                <div className="mt-0.5 text-[11.5px] leading-4 text-faint">
                  {enabled
                    ? `开启后低于 ${rerank.threshold} 的相关分不再作为引用资料。`
                    : "默认关闭：只用向量与关键词融合排序，不额外调用任何模型。"}
                </div>
              </div>
              <Toggle on={enabled} label="启用重排序" onClick={() => setEnabled((on) => !on)} />
            </div>
          </div>

          <div className="mt-3 rounded-xl bg-fog/[0.03] p-3.5">
            <div className="mb-2 px-0.5 text-[12.5px] text-body">服务商</div>
            <div className="space-y-2">
              {rerank.providers.map((row) => {
                const on = row.id === provider;
                return (
                  <button
                    key={row.id}
                    type="button"
                    onClick={() => pickProvider(row)}
                    className={cx(
                      "flex w-full items-start gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors",
                      on ? "border-brand/60 bg-brand-soft" : "border-line hover:bg-fog/[0.05]",
                    )}
                  >
                    <span
                      className={cx(
                        "mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full border",
                        on ? "border-brand" : "border-line",
                      )}
                    >
                      {on ? <span className="h-1.5 w-1.5 rounded-full bg-brand" /> : null}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                        <span className="text-[13px] text-ink">{row.label}</span>
                        {row.dependency_ok ? (
                          <span className="rounded-full bg-fog/[0.07] px-1.5 py-0.5 text-[10.5px] text-mute">
                            无需额外依赖
                          </span>
                        ) : (
                          <span className="rounded-full bg-warn/15 px-1.5 py-0.5 text-[10.5px] text-warn">
                            缺依赖
                          </span>
                        )}
                      </span>
                      <span className="mt-0.5 block text-[11.5px] leading-4 text-faint">{row.summary}</span>
                    </span>
                  </button>
                );
              })}
            </div>

            {spec && !spec.dependency_ok ? (
              <p className="mt-2.5 flex items-start gap-1.5 text-[11.5px] leading-4 text-warn">
                <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                {spec.dependency_detail}
              </p>
            ) : null}

            <div className="mt-3.5 grid gap-3">
              <div>
                <div className="mb-1.5 px-0.5 text-[12.5px] text-body">重排序型号</div>
                <input
                  value={model}
                  onChange={(event) => setModel(event.target.value)}
                  list="rerank-model-options"
                  spellCheck={false}
                  placeholder={spec ? `例如 ${spec.default_model}` : "填写型号"}
                  className={cx(inputCls, "font-mono text-[12.5px]")}
                />
                <datalist id="rerank-model-options">
                  {(spec?.models ?? rerank.models).map((name) => (
                    <option key={name} value={name} />
                  ))}
                </datalist>
                <p className="mt-1.5 px-0.5 text-[11px] leading-4 text-faint">
                  {spec?.needs_key
                    ? "下拉里是该服务商的常用型号，也可以手打厂商新上的型号。"
                    : "本地模型的 HuggingFace 名称；不在候选里也可以手打。"}
                </p>
              </div>

              <div>
                <div className="mb-1.5 px-0.5 text-[12.5px] text-body">
                  接口地址 <span className="text-faint">（可留空）</span>
                </div>
                <input
                  value={baseUrl}
                  onChange={(event) => setBaseUrl(event.target.value)}
                  spellCheck={false}
                  placeholder={rerank.effective_base_url || spec?.base_url_hint || "留空即沿用向量模型那条供应商的地址"}
                  className={cx(inputCls, "font-mono text-[12.5px]")}
                />
                <p className="mt-1.5 px-0.5 text-[11px] leading-4 text-faint">
                  {rerank.effective_base_url
                    ? `留空即沿用：${rerank.effective_base_url}`
                    : "留空即沿用「向量模型」那条供应商的地址。"}
                </p>
              </div>

              <div>
                <div className="mb-1.5 flex items-center justify-between gap-3 px-0.5">
                  <span className="text-[12.5px] text-body">API Key</span>
                  <span className="truncate text-[11px] text-faint">
                    {rerank.has_key ? `已配置（${rerank.key_source}）` : "未配置"}
                  </span>
                </div>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  spellCheck={false}
                  autoComplete="off"
                  placeholder={rerank.has_key ? "留空即保留当前这把" : "留空即沿用向量模型那条供应商的 Key"}
                  className={cx(inputCls, "font-mono text-[12.5px]")}
                />
                <p className="mt-1.5 px-0.5 text-[11px] leading-4 text-faint">
                  Key 只写本机 .env，接口不回传、界面也不回显。
                </p>
              </div>
            </div>
          </div>

          {spec ? (
            <div className="mt-3 rounded-xl bg-fog/[0.03] p-3.5">
              <div className="text-[12.5px] text-body">开启条件与代价</div>
              <ul className="mt-1.5 space-y-1.5 text-[11.5px] leading-4 text-faint">
                <li>{spec.requirement}</li>
                <li>{spec.cost}</li>
                <li>{spec.note}</li>
              </ul>
              <a
                href={spec.source}
                target="_blank"
                rel="noreferrer"
                className="mt-2 inline-flex items-center gap-1 text-[11.5px] text-brand hover:underline"
              >
                厂商文档 <ExternalLink size={11} strokeWidth={1.75} />
              </a>
            </div>
          ) : null}

          {enabled && !target ? (
            <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-warn">
              <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
              还没有填型号，开启后不会生效。
            </p>
          ) : null}

          {error ? (
            <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-rose">
              <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
              {error}
            </p>
          ) : null}

          <div className="mt-5 flex items-center gap-2">
            <span className="flex-1 text-[11px] leading-4 text-faint">
              {changed ? "保存后立即生效，并写入本机 .env。" : "没有改动。"}
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
