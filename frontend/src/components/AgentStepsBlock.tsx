import { useEffect, useState } from "react";
import { Bot, ChevronDown, Eye, FilePenLine, Info, Lightbulb, ListChecks, Wrench } from "lucide-react";
import type { AgentStep, AgentTrace } from "../api";
import { cx } from "../utils";

/**
 * Agent 过程轨迹（条目 18）：ReAct 工具循环 / 计划执行的每一步，原样展示、可折叠。
 *
 * 折叠口径与 ReasoningBlock 一致：**答案还没到就展开**（流式期间能看见最新一步），
 * 答案一到就自动收起成一行；用户手动点过之后就不再自动改变。
 * 轨迹为 null（非 Agent 回答）或没有步骤时不渲染。
 */

const MODE_LABEL: Record<string, string> = {
  react: "ReAct 工具循环",
  plan_execute: "计划执行",
};

const KIND_ICON: Record<string, typeof Lightbulb> = {
  plan: ListChecks,
  thought: Lightbulb,
  tool: Wrench,
  observation: Eye,
  note: Info,
  // 阶段 6（回归检查）：写通道的「文件变更」步骤有**专属**图标键（不是靠未知 kind 回落 Info）。
  file_change: FilePenLine,
};

/**
 * 读文件类工具的步骤载荷里承载「读了哪个文件」的键（阶段 6 · 回归检查）。
 *
 * 后端把该键的值**归一成相对工作区根**之后才进 `agent` 帧（`runner._tool_args_for_steps`）——
 * 所以这里展示的是「工作区里的哪个文件」，而不是主机上的绝对路径。
 * 只对这三个工具取值：别的工具的 `args` 里即便有同名键，语义也未必是「文件路径」。
 */
const READ_FILE_TOOLS = new Set(["read_file", "list_files", "grep_files"]);

/** 从步骤载荷里取「读了哪个文件」；取不到（非读文件工具 / 载荷不是对象 / 值不是非空串）返回空串。 */
function readFilePath(step: AgentStep): string {
  if (!step.tool || !READ_FILE_TOOLS.has(step.tool)) return "";
  const args = step.args;
  if (!args || typeof args !== "object") return "";
  const value = args.path;
  return typeof value === "string" && value.trim() ? value : "";
}

/**
 * 「文件变更」步骤载荷的三个键（阶段 6 · 回归检查）。
 *
 * 🔴 与后端 `runner.WRITE_STEP_KEYS` / `audit.WRITE_META_KEYS` **同一份**：这三个值由写工具
 * handler 从 `WriteOutcome` 取出后原样带下来（后端**不会**自己重算一份）⇒ 界面显示的就是
 * 审计里记的那一份（两侧同源，不是各取各的）。
 */
const CHANGE_KEYS = ["write_target", "write_byte_delta", "write_snapshot_id"] as const;

/** 一次文件变更的可显示形态；三键**全缺**时返回 null（该步骤不是文件变更 / 形状不对）。 */
function fileChange(step: AgentStep): { target: string; delta: number | null; snapshot: string } | null {
  // 专属 kind 判定：只有后端 `runner._file_change_step` 会产出这个 kind。
  if (step.kind !== "file_change") return null;
  const args = step.args;
  if (!args || typeof args !== "object") return null;
  // 三个键**逐个**按 `CHANGE_KEYS` 取（而不是散着写三行字符串字面量）：
  // 这样「界面取的键」与「后端产出的键」是同一张表，改名时两侧一起看得见。
  const raw: Record<string, unknown> = {};
  for (const key of CHANGE_KEYS) raw[key] = args[key];
  const target = typeof raw.write_target === "string" ? raw.write_target : "";
  const delta = typeof raw.write_byte_delta === "number" ? raw.write_byte_delta : null;
  const snapshot = typeof raw.write_snapshot_id === "string" ? raw.write_snapshot_id : "";
  // 三键全缺 ⇒ 不是一个「文件变更」步骤（防「任何 kind 都能进这条分支」）。
  if (!target && delta === null && !snapshot) return null;
  return { target, delta, snapshot };
}

/**
 * 把 diff 文本切成带分类的行（阶段 6 · 回归检查）。
 *
 * 分类口径与后端 `runner._file_change_detail` **完全一致**（只认行首一个字符）：
 * 新增 `+` / 删除 `-` / 其余（含 hunk 头与上下文行）原样。
 * 🔴 刻意**不在这里解析** diff 的语义（不算行数、不重排版）—— 后端已经算好并写在
 * 标题与摘要里；界面只负责**上色**，两处各算一份必然漂移。
 */
function diffLines(diff: string): { text: string; tone: "add" | "del" | "ctx" }[] {
  return diff
    .split("\n")
    .filter((line) => line.length > 0)
    .map((line) => ({
      text: line,
      tone: line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "ctx",
    }));
}

export default function AgentStepsBlock({
  trace,
  hasAnswer,
}: {
  trace: AgentTrace | null | undefined;
  hasAnswer: boolean;
}) {
  const steps = trace && Array.isArray(trace.steps) ? trace.steps : [];

  const [open, setOpen] = useState(!hasAnswer);
  const [manual, setManual] = useState(false);
  useEffect(() => {
    if (!manual) setOpen(!hasAnswer);
  }, [hasAnswer, manual]);

  if (!trace || !steps.length) return null;
  const modeLabel = MODE_LABEL[trace.mode] || trace.mode;

  return (
    <div className="mb-3 overflow-hidden rounded-xl border border-line bg-panel">
      <button
        type="button"
        onClick={() => {
          setManual(true);
          setOpen((value) => !value);
        }}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-fog/[0.03]"
      >
        <Bot size={12} className="shrink-0 text-brand" strokeWidth={1.75} />
        <span className="text-[12px] text-mute">Agent · {modeLabel}</span>
        <span className="text-[11px] text-faint">{steps.length} 步</span>
        <ChevronDown
          size={13}
          className={cx("ml-auto shrink-0 text-faint transition-transform", open && "rotate-180")}
          strokeWidth={1.75}
        />
      </button>
      {open ? (
        <div>
          {steps.map((step, index) => {
            const Icon = KIND_ICON[step.kind] || Info;
            const failed = step.ok === false;
            const readPath = readFilePath(step);
            const change = fileChange(step);
            return (
              <div key={index} className={cx("px-3 py-2", index > 0 && "border-t border-hairline")}>
                <div className="flex items-center gap-2">
                  <Icon
                    size={12}
                    className={cx("shrink-0", failed ? "text-rose" : "text-mute")}
                    strokeWidth={1.75}
                  />
                  {readPath ? (
                    <span className="min-w-0 flex-1 truncate text-[12.5px] text-body">
                      {step.title}
                      <span
                        className="ml-1 text-[11.5px] text-faint"
                        title="读的文件（相对工作区根）"
                      >
                        · {readPath}
                      </span>
                    </span>
                  ) : (
                    <span className="min-w-0 flex-1 text-[12.5px] text-body">{step.title}</span>
                  )}
                  {typeof step.ms === "number" ? (
                    <span className="shrink-0 text-[11px] text-faint">{step.ms}ms</span>
                  ) : null}
                  {failed ? <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-rose" /> : null}
                </div>
                {change ? (
                  <div className="mt-1 space-y-0.5 text-[11.5px] leading-5 text-mute">
                    <div className="break-all">
                      <span className="text-faint">文件 </span>
                      {change.target}
                    </div>
                    {change.delta !== null ? (
                      <div>
                        <span className="text-faint">字节变动 </span>
                        {change.delta >= 0 ? `+${change.delta}` : `${change.delta}`}
                      </div>
                    ) : null}
                    {change.snapshot ? (
                      <div className="break-all">
                        <span className="text-faint">快照 </span>
                        {change.snapshot}
                      </div>
                    ) : null}
                  </div>
                ) : null}
                {step.kind === "file_change" && step.detail ? (
                  // 文件变更的详情是 diff（回归检查）：逐行上色，新增绿 / 删除红 / 其余灰。
                  <div className="mt-1 whitespace-pre-wrap break-all rounded-md bg-fog/[0.04] px-2 py-1 font-mono text-[11.5px] leading-5">
                    {diffLines(step.detail).map((line, lineIndex) => (
                      <div
                        key={lineIndex}
                        className={cx(
                          line.tone === "add" && "text-emerald-400",
                          line.tone === "del" && "text-rose",
                          line.tone === "ctx" && "text-mute",
                        )}
                      >
                        {line.text}
                      </div>
                    ))}
                  </div>
                ) : step.detail ? (
                  <div className="mt-1 whitespace-pre-wrap break-all text-[12px] leading-5 text-mute">
                    {step.detail}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
