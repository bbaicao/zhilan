import { useEffect, useState } from "react";
import { AlertCircle, Download, Loader2, X } from "lucide-react";
import { exportUsageCsv, getUsageSummary, type UsageStat, type UsageSummary } from "../api";
import { cx } from "../utils";

/** 任务清单 27：用量弹窗——近 7 日 tokens 与调用次数（日期 × 类型小表 + 合计行）。
 *
 * 天数固定 7；tokens 拿不到的格子按「未知」展示（典型：流式对话不加 stream_options）。
 */

const KIND_LABELS: Record<"chat" | "vision" | "embed" | "rerank", string> = {
  chat: "对话",
  vision: "视觉",
  embed: "向量",
  rerank: "重排序",
};

const KIND_ORDER = ["chat", "vision", "embed", "rerank"] as const;

const EMPTY_STAT: UsageStat = { calls: 0, prompt_tokens: null, completion_tokens: null, total_tokens: null, unknown_calls: 0 };

/** 同一类型跨天合并：有一天的 tokens 未知，合并结果就标未知（unknown_calls 累加备查）。 */
function mergeKind(summary: UsageSummary, kind: (typeof KIND_ORDER)[number]): UsageStat {
  return summary.by_day.reduce<UsageStat>((acc, day) => {
    const stat = day.by_kind[kind] ?? EMPTY_STAT;
    return {
      calls: acc.calls + stat.calls,
      prompt_tokens: stat.prompt_tokens === null ? null : (acc.prompt_tokens ?? 0) + stat.prompt_tokens,
      completion_tokens: stat.completion_tokens === null ? null : (acc.completion_tokens ?? 0) + stat.completion_tokens,
      total_tokens: stat.total_tokens === null ? null : (acc.total_tokens ?? 0) + stat.total_tokens,
      unknown_calls: acc.unknown_calls + stat.unknown_calls,
    };
  }, { ...EMPTY_STAT });
}

/** 单元格口径：没有调用显示「—」；有调用但拿不到 tokens 显示「未知」；否则给数字。 */
function tokenCell(stat: UsageStat): string {
  if (stat.calls === 0) return "—";
  if (stat.total_tokens === null) return "未知";
  return stat.total_tokens.toLocaleString("zh-CN");
}

function cellClass(stat: UsageStat): string {
  if (stat.calls === 0) return "text-faint";
  return stat.total_tokens === null ? "text-warn" : "text-ink";
}

function statTitle(stat: UsageStat): string | undefined {
  if (stat.calls === 0) return undefined;
  return `${stat.calls} 次调用 · 提示 ${stat.prompt_tokens ?? "未知"} / 补全 ${stat.completion_tokens ?? "未知"}`;
}

export default function UsageDialog({ onClose }: { onClose(): void }) {
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [error, setError] = useState("");
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getUsageSummary(7)
      .then((data) => {
        if (!cancelled) setSummary(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "用量读取失败");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function downloadCsv() {
    setError("");
    setExporting(true);
    try {
      await exportUsageCsv(7);
    } catch (err) {
      setError(err instanceof Error ? err.message : "导出失败");
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={onClose}>
      <div className="mx-auto mt-16 w-full max-w-xl px-4 pb-12" onClick={(event) => event.stopPropagation()}>
        <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-semibold text-ink">用量</h2>
              <p className="mt-0.5 text-[11.5px] leading-4 text-faint">
                近 7 日对话 / 视觉 / 向量 / 重排序的 tokens 与调用次数；厂商没给 tokens 的调用记「未知」。
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

          {error ? (
            <p className="mb-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-rose">
              <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
              {error}
            </p>
          ) : null}

          {summary === null && !error ? (
            <div className="flex items-center justify-center gap-2 rounded-xl bg-fog/[0.03] py-10 text-[12.5px] text-faint">
              <Loader2 size={14} className="animate-spin" strokeWidth={1.75} />
              正在读取用量…
            </div>
          ) : null}

          {summary ? (
            <>
              <div className="overflow-x-auto rounded-xl border border-line">
                <table className="w-full border-collapse text-[12.5px]">
                  <thead>
                    <tr className="bg-fog/[0.04] text-left text-[11.5px] text-mute">
                      <th scope="col" className="px-3 py-2 font-medium">日期</th>
                      {KIND_ORDER.map((kind) => (
                        <th key={kind} scope="col" className="px-3 py-2 font-medium">
                          {KIND_LABELS[kind]}
                          <span className="ml-1 font-normal text-faint">tokens</span>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {summary.by_day.map((day) => (
                      <tr key={day.date} className="border-t border-line">
                        <td className="px-3 py-2 whitespace-nowrap text-mute">{day.date.slice(5)}</td>
                        {KIND_ORDER.map((kind) => {
                          const stat = day.by_kind[kind] ?? EMPTY_STAT;
                          return (
                            <td
                              key={kind}
                              className={cx("px-3 py-2 tabular-nums", cellClass(stat))}
                              title={statTitle(stat)}
                            >
                              {tokenCell(stat)}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                  <tfoot>
                    <tr className="border-t border-line bg-fog/[0.04] text-[12px]">
                      <td className="px-3 py-2 font-medium text-body">合计 tokens</td>
                      {KIND_ORDER.map((kind) => (
                        <td key={kind} className={cx("px-3 py-2 tabular-nums", cellClass(mergeKind(summary, kind)))}>
                          {tokenCell(mergeKind(summary, kind))}
                        </td>
                      ))}
                    </tr>
                    <tr className="text-[12px]">
                      <td className="px-3 py-2 font-medium text-body">调用次数</td>
                      {KIND_ORDER.map((kind) => {
                        const merged = mergeKind(summary, kind);
                        return (
                          <td key={kind} className="px-3 py-2 tabular-nums text-body">
                            {merged.calls === 0 ? "—" : merged.calls.toLocaleString("zh-CN")}
                          </td>
                        );
                      })}
                    </tr>
                  </tfoot>
                </table>
              </div>
              <p className="mt-2.5 px-0.5 text-[11px] leading-4 text-faint">
                全部合计：{summary.total.calls.toLocaleString("zh-CN")} 次调用
                {summary.total.unknown_calls > 0 ? `（其中 ${summary.total.unknown_calls.toLocaleString("zh-CN")} 次 tokens 未知）` : ""}
                {summary.total.total_tokens !== null ? ` · 总 tokens ${summary.total.total_tokens.toLocaleString("zh-CN")}` : " · 总 tokens 未知"}
                。
              </p>
            </>
          ) : null}

          <div className="mt-5 flex items-center gap-2">
            <span className="flex-1 text-[11px] leading-4 text-faint">统计口径：应答成功才记一次。</span>
            <button
              type="button"
              disabled={exporting}
              onClick={() => void downloadCsv()}
              className="flex items-center gap-1.5 rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12] disabled:opacity-60"
            >
              {exporting ? <Loader2 size={13} className="animate-spin" strokeWidth={2} /> : <Download size={13} strokeWidth={1.75} />}
              {exporting ? "导出中…" : "导出 CSV"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
