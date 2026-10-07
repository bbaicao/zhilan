import { useEffect, useState } from "react";
import { BrainCircuit, ChevronDown } from "lucide-react";
import type { ThinkingMeta } from "../api";
import { cx } from "../utils";

/**
 * 思考过程（条目 19）：厂商自己给的思考内容，原样展示、可折叠，界面不生成也不改写。
 *
 * 折叠口径：**答案还没到就展开**（等待时能看见模型在想什么），答案一到就自动收起成一行；
 * 用户手动点过之后就不再自动改变，免得边想边看时被抢走控制权。
 * 只有元数据（例如「已自动加深」）而没有思考原文时，退化成一行小字说明，不做折叠。
 */
export default function ReasoningBlock({
  reasoning,
  thinking,
  hasAnswer,
}: {
  reasoning?: string | null;
  thinking?: ThinkingMeta | null;
  hasAnswer: boolean;
}) {
  const text = (reasoning || "").trim();
  const note = (thinking?.note || "").trim();
  const deepened = Boolean(thinking?.auto_deepen);

  const [open, setOpen] = useState(!hasAnswer);
  const [manual, setManual] = useState(false);
  useEffect(() => {
    if (!manual) setOpen(!hasAnswer);
  }, [hasAnswer, manual]);

  if (!text) {
    if (!deepened || !note) return null;
    return (
      <div className="mb-3 flex items-start gap-1.5 text-[11.5px] leading-5 text-faint">
        <BrainCircuit size={12} className="mt-0.5 shrink-0 text-brand/70" strokeWidth={1.75} />
        <span>{note}</span>
      </div>
    );
  }

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
        <BrainCircuit size={12} className="shrink-0 text-brand" strokeWidth={1.75} />
        <span className="text-[12px] text-mute">思考过程</span>
        <span className="text-[11px] text-faint">{text.length} 字</span>
        {deepened ? (
          <span className="rounded-full bg-brand-soft px-1.5 py-0.5 text-[10.5px] text-brand">自动加深</span>
        ) : null}
        <ChevronDown
          size={13}
          className={cx("ml-auto shrink-0 text-faint transition-transform", open && "rotate-180")}
          strokeWidth={1.75}
        />
      </button>
      {open ? (
        <div className="border-t border-hairline px-3 py-2">
          <div className="whitespace-pre-wrap break-words text-[12.5px] leading-6 text-mute">{text}</div>
          {note ? <div className="mt-1.5 text-[11px] leading-5 text-faint">{note}</div> : null}
        </div>
      ) : null}
    </div>
  );
}
