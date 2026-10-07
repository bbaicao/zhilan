import { useEffect, useRef, useState } from "react";
import { ChevronDown, Link2 } from "lucide-react";
import type { CitationMode, RefItem } from "../api";
import CitationCard from "./CitationCard";
import { cx } from "../utils";

/**
 * 回答下方的引用资料（条目 31）：默认收起成一行摘要，点摘要条展开 / 收起。
 *
 * 折叠状态只活在这条消息里 —— 组件按 `message.id` 挂载，刷新或新回答都是新实例，
 * 所以永远回到默认收起；不做跨消息的统一偏好（照条目「先不做」口径）。
 * 没有引用时整块不渲染，不留空折叠条。
 *
 * 条目 3.3：新增 `mode` prop（与后端 `resolve_citations` 同规则算出来的解析模式）。
 * `fallback` = 模型正文里一个有效引用编号都没写（或只写了越界号），后端兜底带出了
 * 本次检索的全部来源——这时按「候选资料」展示，并明确说明尚未确认回答使用或支持，
 * 避免把保留资料当成有效引用；`cited` / `none` 保持既有文案。
 */
export default function CitationList({
  items,
  mode,
  onOpen,
}: {
  items: RefItem[];
  mode: CitationMode;
  onOpen(type: string, id?: number | null): void;
}) {
  const [open, setOpen] = useState(false);
  const cardsRef = useRef<HTMLDivElement | null>(null);

  // 展开时把卡片带进视野：消息列表是内部滚动，`nearest` 只做最小滚动，不打扰用户的浏览位置
  useEffect(() => {
    if (!open) return;
    const id = requestAnimationFrame(() =>
      cardsRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }),
    );
    return () => cancelAnimationFrame(id);
  }, [open]);

  if (!items.length) return null;

  return (
    <div className="mt-2.5">
      {/* 条目 3.3：兜底模式必须可读地说明，不许静默（列表照常渲染，只是多这一行） */}
      {mode === "fallback" ? (
        <p className="mb-1.5 px-0.5 text-[11.5px] leading-5 text-mute">
          回答未标注有效来源编号，以下仅为候选资料，尚未确认回答使用了这些资料，也未确认它们支持回答
        </p>
      ) : null}
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        title={mode === "fallback"
          ? (open ? "收起候选资料" : "展开候选资料")
          : (open ? "收起引用资料" : "展开引用资料")}
        className="flex w-full items-center gap-2 rounded-xl border border-line bg-panel px-3.5 py-2.5 text-left transition-colors hover:border-edge-soft hover:bg-fog/[0.04]"
      >
        <Link2 size={13} className="shrink-0 text-brand" strokeWidth={1.75} />
        <span className="shrink-0 text-[12.5px] text-mute">
          {mode === "fallback" ? `候选资料 ${items.length} 条` : `引用 ${items.length} 条资料`}
        </span>
        <span className="min-w-0 flex-1 truncate text-[11.5px] text-faint">· {items[0].title}</span>
        <ChevronDown
          size={13}
          className={cx("shrink-0 text-faint transition-transform", open && "rotate-180")}
          strokeWidth={1.75}
        />
      </button>
      {open ? (
        <div ref={cardsRef}>
          {items.map((ref, index) => (
            <CitationCard
              key={index}
              item={ref}
              onOpen={() => onOpen(ref.type)}
              onOpenRelated={(type, id) => onOpen(type, id)}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
