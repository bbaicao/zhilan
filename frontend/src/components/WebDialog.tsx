import { useState } from "react";
import { AlertCircle, Check, ChevronLeft, ChevronRight, Download, ExternalLink, Loader2, X } from "lucide-react";
import type {
  SettingsIn,
  WebChannelInfo,
  WebProviderInfo,
  WebState,
} from "../api";
import { ApiError, probeWebModels } from "../api";
import { cx } from "../utils";

/** 界面优化条目 26：联网检索弹窗——两级结构（根视图 → 渠道 pane）。
 *
 * 服务商与代价说明全部来自后端登记表，界面只负责呈现，不自己编文案、不发明选项。
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

/** 数字输入框允许暂时清空/写到一半；提交时再夹回范围，非法时回落原值。 */
function clampInt(raw: string, min: number, max: number, fallback: number): number {
  const n = Number.parseInt(raw.trim(), 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

/** pane 顶部的返回条。 */
function PaneBack({ title, onBack }: { title: string; onBack(): void }) {
  return (
    <button
      type="button"
      onClick={onBack}
      className="mb-3 flex items-center gap-1 rounded-md px-1 py-0.5 text-[12.5px] text-mute transition-colors hover:text-ink"
    >
      <ChevronLeft size={15} strokeWidth={1.75} className="shrink-0" />
      {title}
    </button>
  );
}

/** 代价与说明卡片（两个 pane 共用）。 */
function CostNote({
  cost,
  note,
  keyHint,
  needsKey,
  source,
}: {
  cost: string;
  note: string;
  keyHint?: string;
  needsKey?: boolean;
  source?: string;
}) {
  if (!cost && !note && !source) return null;
  return (
    <div className="mt-3 rounded-xl bg-fog/[0.03] p-3.5">
      <div className="text-[12.5px] text-body">代价与说明</div>
      <ul className="mt-1.5 space-y-1.5 text-[11.5px] leading-4 text-faint">
        {cost ? <li>{cost}</li> : null}
        {note ? <li>{note}</li> : null}
        {needsKey && keyHint ? <li>{keyHint}</li> : null}
      </ul>
      {source ? (
        <a
          href={source}
          target="_blank"
          rel="noreferrer"
          className="mt-2 inline-flex items-center gap-1 text-[11.5px] text-brand hover:underline"
        >
          厂商文档 <ExternalLink size={11} strokeWidth={1.75} />
        </a>
      ) : null}
    </div>
  );
}

export default function WebDialog({
  web,
  onSave,
  onClose,
}: {
  web: WebState;
  onSave(payload: SettingsIn, onFail?: (message: string) => void): Promise<boolean>;
  onClose(): void;
}) {
  /** root = 根视图；api / llm = 点进渠道的 pane（同一弹窗内，不是嵌套弹窗） */
  const [pane, setPane] = useState<"root" | "api" | "llm">("root");

  const [enabled, setEnabled] = useState(web.enabled);
  const [channel, setChannel] = useState(web.channel ?? "api");

  // 传统搜索 API
  const [provider, setProvider] = useState(web.provider);
  const [apiKey, setApiKey] = useState("");
  const [maxResults, setMaxResults] = useState(String(web.max_results));
  const [timeout, setTimeoutSec] = useState(String(web.timeout));

    // 大模型搜索的独立配置；官方 MiMo 可复用已绑定的对话凭据。
  const llmState = web.llm;
  const [llmProvider, setLlmProvider] = useState(llmState?.provider ?? "mimo");
  const [llmBaseUrl, setLlmBaseUrl] = useState(llmState?.base_url ?? "");
  const [llmApiKey, setLlmApiKey] = useState("");
  const [llmModel, setLlmModel] = useState(llmState?.model ?? "");
  const [llmPool, setLlmPool] = useState<string[]>([]);
  const [probing, setProbing] = useState(false);
  const [llmMaxSearches, setLlmMaxSearches] = useState(String(llmState?.max_searches ?? 5));
  const [llmSearchTool, setLlmSearchTool] = useState(llmState?.search_tool ?? "");

  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const apiSpec: WebProviderInfo | undefined =
    web.providers.find((row) => row.id === provider) ?? web.providers[0];

  const nextMax = clampInt(maxResults, 1, 10, web.max_results);
  const nextTimeout = clampInt(timeout, 3, 60, web.timeout);
  const nextLlmMax = clampInt(llmMaxSearches, 1, 6, llmState?.max_searches ?? 5);

  const changed =
    enabled !== web.enabled ||
    channel !== (web.channel ?? "api") ||
    provider !== web.provider ||
    apiKey.trim() !== "" ||
    nextMax !== web.max_results ||
    nextTimeout !== web.timeout ||
    llmBaseUrl.trim().replace(/\/+$/, "") !== (llmState?.base_url ?? "") ||
    llmApiKey.trim() !== "" ||
    llmProvider !== (llmState?.provider ?? "mimo") ||
    llmModel.trim() !== (llmState?.model ?? "") ||
    nextLlmMax !== (llmState?.max_searches ?? 5) ||
    llmSearchTool !== (llmState?.search_tool ?? "");

  /** 用当前填写的地址与 Key（没填就用已保存的）拉模型列表；当前型号保留在候选里。 */
  async function fetchLlmModels() {
    setProbing(true);
    setError("");
    try {
      const result = await probeWebModels({
        base_url: llmBaseUrl.trim() || undefined,
        api_key: llmApiKey.trim() || undefined,
      });
      const pool = [...result.models];
      const current = llmModel.trim();
      if (current && !pool.includes(current)) pool.push(current);
      setLlmPool(pool);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "拉取模型列表失败");
    } finally {
      setProbing(false);
    }
  }

  async function save() {
    setError("");
    setSaving(true);
    const payload: SettingsIn = {
      web: {
        enabled,
        channel,
        provider,
        api_key: apiKey.trim(),
        max_results: nextMax,
        timeout: nextTimeout,
          // 保存完整配置，留空的 Key 由服务端保留。
        llm_base_url: llmBaseUrl.trim(),
        llm_provider: llmProvider,
        llm_api_key: llmApiKey.trim(),
        llm_model: llmModel.trim(),
        llm_max_searches: nextLlmMax,
        llm_search_tool: llmSearchTool,
      },
    };
    const ok = await onSave(payload, setError);
    setSaving(false);
    if (ok) onClose();
  }

  const channelOptions: WebChannelInfo[] =
    web.channel_options ??
    ([
      { id: "api", label: "传统搜索 API", summary: "调用搜索接口获取标题 + 链接 + 摘要" },
      { id: "llm", label: "大模型搜索", summary: "走大模型对话接口做联网检索并整理结果" },
    ] as WebChannelInfo[]);

  const apiRowValue = web.providers.find((row) => row.id === provider)?.label ?? provider;
  // 根视图右列显示当前生效型号（没有就退回 Key 状态）——型号才是最容易配错的那一项
  const llmRowValue =
    (llmState?.model ?? "").trim() || (llmState?.has_key ? "已配置 Key" : "未配置 Key");

  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={onClose}>
      <div className="mx-auto mt-16 w-full max-w-xl px-4 pb-12" onClick={(event) => event.stopPropagation()}>
        <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          {/* ---- Header ---- */}
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-semibold text-ink">联网检索</h2>
              <p className="mt-0.5 text-[11.5px] leading-4 text-faint">
                开启后，本机知识库不够用时会补充公开网页结果；保存后立即生效并写入本机 .env。
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

          {pane === "root" ? (
            <>
              {/* ---- 启用开关 ---- */}
              <div className="rounded-xl bg-fog/[0.03] p-3.5">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-[13.5px] text-ink">启用联网检索</div>
                    <div className="mt-0.5 text-[11.5px] leading-4 text-faint">
                      {enabled
                        ? "开启后可在对话里补充公开网页资料。"
                        : "默认关闭：只用本机知识库，不访问外网。"}
                    </div>
                  </div>
                  <Toggle on={enabled} label="启用联网检索" onClick={() => setEnabled((on) => !on)} />
                </div>
              </div>

              {/* ---- 搜索渠道：两行入口 ---- */}
              <div className="mt-3 rounded-xl bg-fog/[0.03] p-3.5">
                <div className="mb-2 px-0.5 text-[12.5px] text-body">搜索渠道</div>
                <div className="space-y-2">
                  {channelOptions.map((row) => (
                    <button
                      key={row.id}
                      type="button"
                      onClick={() => {
                        setChannel(row.id);
                        if (row.id === "llm" && llmProvider !== "mimo") {
                          setLlmProvider("mimo");
                          setLlmSearchTool("web_search");
                          setLlmBaseUrl("");
                          setLlmModel("");
                          setLlmMaxSearches(String(Math.max(1, Math.min(6, nextLlmMax))));
                        }
                        setPane(row.id === "llm" ? "llm" : "api");
                        setError("");
                      }}
                      className={cx(
                        "flex w-full items-center gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors",
                        channel === row.id
                          ? "border-brand/60 bg-brand-soft"
                          : "border-line hover:bg-fog/[0.05]",
                      )}
                    >
                      <span
                        className={cx(
                          "mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full border",
                          channel === row.id ? "border-brand" : "border-line",
                        )}
                      >
                        {channel === row.id ? (
                          <span className="h-1.5 w-1.5 rounded-full bg-brand" />
                        ) : null}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-[13px] text-ink">{row.label}</span>
                        <span className="mt-0.5 block text-[11.5px] leading-4 text-faint">{row.summary}</span>
                      </span>
                      <span className="shrink-0 max-w-[120px] truncate text-[12.5px] text-mute">
                        {row.id === "llm" ? llmRowValue : apiRowValue}
                      </span>
                      <ChevronRight size={15} strokeWidth={1.75} className="shrink-0 text-faint" />
                    </button>
                  ))}
                </div>
              </div>

              {enabled && web.blocked_by ? (
                <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-warn">
                  <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                  当前开启但不会生效：{web.blocked_by}
                </p>
              ) : null}

              {enabled && web.note ? (
                <p className="mt-2 px-0.5 text-[11px] leading-4 text-faint">{web.note}</p>
              ) : null}
            </>
          ) : pane === "api" ? (
            <>
              <PaneBack title="返回" onBack={() => setPane("root")} />

              <div className="rounded-xl bg-fog/[0.03] p-3.5">
                <div className="mb-2 px-0.5 text-[12.5px] text-body">服务商</div>
                <div className="space-y-2">
                  {web.providers.map((row) => {
                    const on = row.id === provider;
                    return (
                      <button
                        key={row.id}
                        type="button"
                        onClick={() => {
                          setProvider(row.id);
                          setError("");
                        }}
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
                            {row.needs_key ? (
                              <span className="rounded-full bg-warn/15 px-1.5 py-0.5 text-[10.5px] text-warn">
                                需要 Key
                              </span>
                            ) : (
                              <span className="rounded-full bg-fog/[0.07] px-1.5 py-0.5 text-[10.5px] text-mute">
                                无需 Key
                              </span>
                            )}
                          </span>
                          <span className="mt-0.5 block text-[11.5px] leading-4 text-faint">{row.summary}</span>
                        </span>
                      </button>
                    );
                  })}
                </div>

                <div className="mt-3.5 grid gap-3">
                  <div>
                    <div className="mb-1.5 flex items-center justify-between gap-3 px-0.5">
                      <span className="text-[12.5px] text-body">API Key</span>
                      <span className="truncate text-[11px] text-faint">
                        {web.has_key ? "已配置" : "未配置"}
                      </span>
                    </div>
                    <input
                      type="password"
                      value={apiKey}
                      onChange={(event) => setApiKey(event.target.value)}
                      spellCheck={false}
                      autoComplete="off"
                      disabled={!apiSpec?.needs_key}
                      placeholder={
                        web.has_key
                          ? "留空即保留当前这把"
                          : apiSpec?.needs_key
                            ? apiSpec.key_hint || "填写该服务商的 API Key"
                            : "该服务商无需 Key"
                      }
                      className={cx(inputCls, "font-mono text-[12.5px]", !apiSpec?.needs_key && "opacity-50")}
                    />
                    <p className="mt-1.5 px-0.5 text-[11px] leading-4 text-faint">
                      Key 只写本机 .env，接口不回传、界面也不回显。
                    </p>
                  </div>

                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <div className="mb-1.5 px-0.5 text-[12.5px] text-body">单次最多条数</div>
                      <input
                        type="number"
                        min={1}
                        max={10}
                        value={maxResults}
                        onChange={(event) => setMaxResults(event.target.value)}
                        className={inputCls}
                      />
                      <p className="mt-1.5 px-0.5 text-[11px] leading-4 text-faint">范围 1–10 条。</p>
                    </div>
                    <div>
                      <div className="mb-1.5 px-0.5 text-[12.5px] text-body">超时（秒）</div>
                      <input
                        type="number"
                        min={3}
                        max={60}
                        value={timeout}
                        onChange={(event) => setTimeoutSec(event.target.value)}
                        className={inputCls}
                      />
                      <p className="mt-1.5 px-0.5 text-[11px] leading-4 text-faint">范围 3–60 秒。</p>
                    </div>
                  </div>
                </div>
              </div>

              {apiSpec ? (
                <CostNote
                  cost={apiSpec.cost}
                  note={apiSpec.note}
                  keyHint={apiSpec.key_hint}
                  needsKey={apiSpec.needs_key}
                  source={apiSpec.source}
                />
              ) : null}
            </>
          ) : (
            <>
              <PaneBack title="返回" onBack={() => setPane("root")} />

              {!llmState ? (
                <p className="rounded-xl bg-fog/[0.03] p-3.5 text-[12px] leading-5 text-mute">
                  后端尚未返回大模型搜索配置，请稍后重试或检查后端版本。
                </p>
              ) : (
                <div className="rounded-xl bg-fog/[0.03] p-3.5">
                  <div className="grid gap-3.5">
                    <div>
                      <div className="mb-1.5 flex items-center justify-between gap-3 px-0.5">
                        <span className="text-[12.5px] text-body">API Key</span>
                        <span className="truncate text-[11px] text-faint">
                          {llmState.has_key ? "已配置密钥。" : "未配置"}
                        </span>
                      </div>
                      <input
                        type="password"
                        value={llmApiKey}
                        onChange={(event) => setLlmApiKey(event.target.value)}
                        spellCheck={false}
                        autoComplete="off"
                        placeholder={llmState.has_key ? "留空保持当前 API Key" : "填写 MiMo API Key"}
                        className={cx(inputCls, "font-mono text-[12.5px]")}
                      />
                      <p className="mt-1.5 px-0.5 text-[11px] leading-4 text-faint">
                        密钥保存在本机。留空表示保持当前密钥。
                      </p>
                    </div>

                    <div>
                      <div className="mb-1.5 px-0.5 text-[12.5px] text-body">大模型搜索服务商</div>
                      <select value={llmProvider} onChange={(event) => setLlmProvider(event.target.value)} className={inputCls}>
                        {llmProvider !== "mimo" ? <option value={llmProvider}>{llmProvider}（暂不支持）</option> : null}
                        <option value="mimo">MiMo</option>
                      </select>
                      <div className="mb-1.5 mt-3 px-0.5 text-[12.5px] text-body">接口地址</div>
                      <input
                        value={llmBaseUrl}
                        onChange={(event) => setLlmBaseUrl(event.target.value)}
                        spellCheck={false}
                        placeholder="留空则使用提供方默认地址"
                        className={cx(inputCls, "font-mono text-[12.5px]")}
                      />
                      <p className="mt-1.5 px-0.5 text-[11px] leading-4 text-faint">
                        留空则使用提供方默认地址。
                      </p>
                    </div>

                    <div>
                      <div className="mb-1.5 flex items-center justify-between gap-3 px-0.5">
                        <span className="text-[12.5px] text-body">型号</span>
                        <button
                          type="button"
                          onClick={() => void fetchLlmModels()}
                          disabled={probing}
                          className="flex h-7 items-center gap-1.5 rounded-lg bg-fog/[0.07] px-2.5 text-[12px] text-body transition-colors hover:bg-fog/[0.12] hover:text-ink disabled:opacity-60"
                        >
                          {probing ? (
                            <Loader2 size={12} className="animate-spin" strokeWidth={2} />
                          ) : (
                            <Download size={12} strokeWidth={1.75} />
                          )}
                          {probing ? "拉取中…" : "拉取模型"}
                        </button>
                      </div>
                      <input
                        value={llmModel}
                        onChange={(event) => setLlmModel(event.target.value)}
                        spellCheck={false}
                        placeholder="拉取后点选，或直接手填"
                        className={cx(inputCls, "font-mono text-[12.5px]")}
                      />
                      {llmPool.length > 0 ? (
                        <div className="mt-2 max-h-44 overflow-auto rounded-lg bg-pit p-1 scrollbar-thin">
                          {llmPool.map((model) => {
                            const on = llmModel.trim() === model;
                            return (
                              <button
                                key={model}
                                type="button"
                                onClick={() => setLlmModel(model)}
                                className="flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left transition-colors hover:bg-fog/[0.05]"
                              >
                                <span
                                  className={cx(
                                    "mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full border",
                                    on ? "border-brand" : "border-line",
                                  )}
                                >
                                  {on ? <span className="h-1.5 w-1.5 rounded-full bg-brand" /> : null}
                                </span>
                                <span
                                  className={cx(
                                    "truncate font-mono text-[12px]",
                                    on ? "text-ink" : "text-mute",
                                  )}
                                >
                                  {model}
                                </span>
                                {on ? <Check size={12} strokeWidth={2} className="ml-auto shrink-0 text-brand" /> : null}
                              </button>
                            );
                          })}
                        </div>
                      ) : null}
                      <p className="mt-1.5 px-0.5 text-[11px] leading-4 text-faint">
                        用上面填的地址与 Key 拉取（没填就用已保存的）；接口上不存在的型号会被拒绝。
                      </p>
                    </div>

                    <div className="rounded-lg bg-fog/[0.02] px-3 py-2.5">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="text-[12.5px] text-body">服务商联网插件</div>
                          <div className="mt-0.5 text-[11px] leading-4 text-faint">
                            {llmSearchTool
                              ? "搜索请求会带上联网搜索工具，由服务商侧真检索并返回引用。"
                              : "请开启联网插件，普通对话接口不会自动搜索。"}
                          </div>
                        </div>
                        <Toggle
                          on={llmSearchTool !== ""}
                          label="服务商联网插件"
                          onClick={() => setLlmSearchTool((on) => (on ? "" : "web_search"))}
                        />
                      </div>
                      {llmState.search_tool_note ? (
                        <p className="mt-1.5 text-[11px] leading-4 text-faint">{llmState.search_tool_note}</p>
                      ) : null}
                    </div>

                    <div>
                      <div className="mb-1.5 px-0.5 text-[12.5px] text-body">单次请求最多搜索关键词数</div>
                      <input
                        type="number"
                        min={1}
                        max={6}
                        value={llmMaxSearches}
                        onChange={(event) => setLlmMaxSearches(event.target.value)}
                        className={inputCls}
                      />
                      <p className="mt-1.5 px-0.5 text-[11px] leading-4 text-faint">
                        MiMo 每次请求支持 1 到 6 个搜索关键词。
                      </p>
                    </div>
                  </div>
                </div>
              )}
            </>
          )}

          {error ? (
            <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-rose">
              <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
              {error}
            </p>
          ) : null}

          {/* ---- Footer ---- */}
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
