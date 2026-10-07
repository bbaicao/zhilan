import { useEffect, useState } from "react";
import { AlertTriangle, ChevronLeft, FileWarning, Loader2, NotebookPen, Share2, Sparkles } from "lucide-react";
import type { CleanDetail, CleanStep } from "../api";
import { documentCleanReport } from "../api";
import { cx } from "../utils";

type CleanReportProps = {
  documentId: number;
  filename: string;
  onBack(): void;
  /** 头部「生成笔记」入口（条目 29）：外部库文档等只读来源不传则不显示 */
  onGenerateNote?(): void;
  /** 条目 16：在图谱中查看该文档 */
  onShowInGraph?(id: number): void;
};

const STEP_LABEL: Record<string, string> = {
  normalize: "空白归一",
  header_footer: "页眉页脚",
  duplicate_paragraph: "重复段落",
  control_char: "乱码与控制符",
  table_downgrade: "表格降级",
};

function stepLabel(step: string) {
  return STEP_LABEL[step] || step;
}

function stepNote(item: CleanStep): string {
  const detail = item.detail || {};
  const parts: string[] = [];
  const push = (key: string, text: string) => {
    const value = detail[key];
    if (typeof value === "number" && value > 0) parts.push(`${text} ${value}`);
  };
  if (Array.isArray(detail.patterns) && detail.patterns.length) {
    parts.push(`识别到 ${detail.patterns.length} 种重复行`);
  }
  push("duplicate_blocks_removed", "去重段落");
  push("folding_runs_collapsed", "折叠重复行");
  push("control_chars_removed", "剔除控制符");
  push("repeat_chars_collapsed", "折叠连续字符");
  push("garbled_flagged", "标记乱码段");
  push("table_rows_converted", "转表格行");
  return parts.join(" · ");
}

export default function CleanReportView({ documentId, filename, onBack, onGenerateNote, onShowInGraph }: CleanReportProps) {
  const [detail, setDetail] = useState<CleanDetail | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<"report" | "compare" | "raw">("report");

  useEffect(() => {
    let alive = true;
    setDetail(null);
    setError("");
    documentCleanReport(documentId)
      .then((data) => {
        if (alive) setDetail(data);
      })
      .catch((err: Error) => {
        if (alive) setError(err.message || "读取清洗报告失败");
      });
    return () => {
      alive = false;
    };
  }, [documentId]);

  return (
    <div className="mx-auto flex h-full min-h-0 w-full max-w-[880px] flex-col px-6 py-6">
      <div className="mb-4 flex items-center justify-between gap-3">
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-0.5 text-[18px] font-semibold text-ink"
        >
          <ChevronLeft size={20} strokeWidth={1.75} />
          清洗报告
        </button>
        <div className="flex shrink-0 items-center gap-2">
          {onShowInGraph ? (
            <button
              type="button"
              onClick={() => onShowInGraph(documentId)}
              title="在图谱中查看"
              className="flex items-center gap-1.5 rounded-full bg-fog/[0.06] px-3.5 py-1.5 text-[12.5px] text-mute transition-colors hover:text-ink"
            >
              <Share2 size={13} strokeWidth={1.75} />
              在图谱中查看
            </button>
          ) : null}
          {onGenerateNote ? (
            <button
              type="button"
              onClick={onGenerateNote}
              title="阅读该文档并生成笔记草稿"
              className="flex items-center gap-1.5 rounded-full bg-send px-3.5 py-1.5 text-[12.5px] font-medium text-canvas transition-colors hover:bg-send-hover"
            >
              <NotebookPen size={13} strokeWidth={1.75} />
              生成笔记
            </button>
          ) : null}
        </div>
      </div>
      <p className="mb-4 truncate text-[12.5px] text-mute">{filename}</p>

      {error ? (
        <p className="rounded-xl bg-rose/10 px-3 py-2.5 text-[13px] text-rose">{error}</p>
      ) : !detail ? (
        <div className="flex items-center gap-2 text-[13px] text-mute">
          <Loader2 size={14} className="animate-spin" strokeWidth={2} />
          正在读取…
        </div>
      ) : (
        <>
          <div className="mb-4 flex gap-1.5">
            {(
              [
                ["report", "清洗统计"],
                ["compare", "前后对照"],
                ["raw", "原始文本"],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => setTab(key)}
                className={cx(
                  "rounded-full px-3.5 py-1.5 text-[12.5px] font-medium transition-colors",
                  tab === key ? "bg-send text-canvas" : "bg-fog/[0.06] text-mute hover:text-ink",
                )}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="min-h-0 flex-1 overflow-auto scrollbar-thin">
            {tab === "report" ? (
              <CleanStats detail={detail} />
            ) : tab === "compare" ? (
              <ComparePane raw={detail.raw_content} clean={detail.clean_content} />
            ) : (
              <pre className="whitespace-pre-wrap rounded-xl bg-fog/[0.03] p-3.5 font-mono text-[12.5px] leading-6 text-ink">
                {detail.raw_content || "（该文档为清洗功能上线前入库，没有留档原文）"}
              </pre>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function CleanStats({ detail }: { detail: CleanDetail }) {
  const report = detail.report;
  if (!report) {
    return (
      <p className="rounded-xl bg-warn/10 px-3 py-2.5 text-[13px] text-warn">
        该文档在清洗功能上线前入库，没有清洗记录。可以点「重建索引」重新清洗入库。
      </p>
    );
  }
  const removed = Math.max(0, report.raw_chars - report.clean_chars);
  const ratio = report.raw_chars ? (removed / report.raw_chars) * 100 : 0;

  return (
    <div>
      <div className="mb-3 grid grid-cols-3 gap-2">
        <Metric label="原始字数" value={report.raw_chars} />
        <Metric label="清洗后字数" value={report.clean_chars} />
        <Metric label="减少" value={`${removed}（${ratio.toFixed(1)}%）`} tone={removed > 0 ? "warn" : "ok"} />
      </div>

      <div className="mb-3 flex items-center gap-1.5 text-[11.5px] text-faint">
        <Sparkles size={12} strokeWidth={1.75} />
        规则指纹 {report.rule_fingerprint}
        {report.enabled ? "" : " · 清洗已关闭"}
      </div>

      <div className="mb-1.5 text-[12.5px] font-medium text-mute">各步统计</div>
      {report.steps.map((item) => (
        <div
          key={item.step}
          className="mb-1.5 flex items-start gap-3 rounded-xl bg-fog/[0.03] px-3 py-2.5"
        >
          <div className="min-w-0 flex-1">
            <div className="text-[13px] text-ink">{stepLabel(item.step)}</div>
            {stepNote(item) ? <div className="mt-0.5 text-[11.5px] text-mute">{stepNote(item)}</div> : null}
          </div>
          <div className="shrink-0 text-right text-[11.5px] text-mute">
            {item.lines_removed > 0 ? <div>-{item.lines_removed} 行</div> : <div className="text-faint">无变化</div>}
            {item.chars_removed > 0 ? <div className="text-faint">-{item.chars_removed} 字</div> : null}
          </div>
        </div>
      ))}

      {report.flagged_count > 0 ? (
        <div className="mt-3 rounded-xl bg-warn/10 px-3 py-2.5">
          <div className="flex items-center gap-1.5 text-[12.5px] font-medium text-warn">
            <AlertTriangle size={13} strokeWidth={1.75} />
            {report.flagged_count} 处可疑内容（已标记、未删除）
          </div>
          <div className="mt-1.5 space-y-0.5 text-[11.5px] text-mute">
            {report.flagged.slice(0, 8).map((item, index) => (
              <div key={index}>
                第 {item.line} 行 · {item.reason} · {item.length} 字符
              </div>
            ))}
          </div>
          <p className="mt-1.5 text-[11.5px] text-faint">
            判不准的内容只标记不删，原文仍可在「原始文本」里查看。
          </p>
        </div>
      ) : null}
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: string | number; tone?: "ok" | "warn" }) {
  return (
    <div className="rounded-xl bg-fog/[0.03] px-3 py-2.5">
      <div className="text-[11.5px] text-mute">{label}</div>
      <div
        className={cx(
          "mt-1 text-[15px] font-medium",
          tone === "warn" ? "text-warn" : tone === "ok" ? "text-ok" : "text-ink",
        )}
      >
        {value}
      </div>
    </div>
  );
}

function ComparePane({ raw, clean }: { raw: string; clean: string }) {
  return (
    <div className="grid grid-cols-2 gap-2.5">
      <div>
        <div className="mb-1.5 flex items-center gap-1.5 text-[12.5px] font-medium text-mute">
          <FileWarning size={13} strokeWidth={1.75} />
          原始解析文本
        </div>
        <pre className="whitespace-pre-wrap rounded-xl bg-fog/[0.03] p-3 font-mono text-[12px] leading-6 text-mute">
          {raw || "（无留档）"}
        </pre>
      </div>
      <div>
        <div className="mb-1.5 flex items-center gap-1.5 text-[12.5px] font-medium text-ink">
          <Sparkles size={13} strokeWidth={1.75} />
          清洗后入库文本
        </div>
        <pre className="whitespace-pre-wrap rounded-xl bg-fog/[0.05] p-3 font-mono text-[12px] leading-6 text-ink">
          {clean}
        </pre>
      </div>
    </div>
  );
}
