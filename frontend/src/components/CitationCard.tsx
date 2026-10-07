import { useState } from "react";
import { ChevronDown, ChevronRight, FileText, Globe, StickyNote } from "lucide-react";
import { getRelated, type RefItem, type WikiLinkHit } from "../api";
import { cx } from "../utils";

/** 条目 3.3：这条引用指向的「来源身份」——chunk_id 与模型看到的资料编号（ordinal）。
 *  背景：引用卡此前只展示类型/标题/来源/摘要，折起来就丢掉了「引用的是哪一段」；
 *  这两个字段由后端落库（`Ref.chunk_id` / `Ref.ordinal`），老消息没有则为 null。 */
type RefIdentity = { chunk_id: string; ordinal: number | null };

function relatedType(kind: string): "note" | "document" {
  return kind === "note" ? "note" : "document";
}

function directionLabel(direction: WikiLinkHit["direction"]) {
  // 「本篇」= 引用卡对应的这篇来源：outgoing=本篇链出（该条被本篇引用）、incoming=该条引用了本篇
  if (direction === "outgoing") return "被本篇引用";
  if (direction === "incoming") return "引用本篇";
  return "相关";
}

function kindLabel(item: RefItem) {
  if (item.type === "web" || item.url) return "网页";
  return item.type === "note" ? "笔记" : "文档";
}

export default function CitationCard({
  item,
  onOpen,
  onOpenRelated,
}: {
  item: RefItem;
  onOpen(): void;
  onOpenRelated(type: string, id: number): void;
}) {
  const isWeb = item.type === "web" || Boolean(item.url);
  const isNote = item.type === "note";
  const Icon = isWeb ? Globe : isNote ? StickyNote : FileText;
  const [relatedOpen, setRelatedOpen] = useState(false);
  const [related, setRelated] = useState<WikiLinkHit[] | null>(null);
  const [relatedLoading, setRelatedLoading] = useState(false);
  // 条目 3.3：「来源身份」折叠区，默认收起；历史消息没有 chunk_id → 整块不渲染
  const [identityOpen, setIdentityOpen] = useState(false);
  const identity: RefIdentity | null = item.chunk_id
    ? { chunk_id: item.chunk_id, ordinal: item.ordinal ?? null }
    : null;

  async function toggleRelated() {
    if (relatedOpen) {
      setRelatedOpen(false);
      return;
    }
    setRelatedOpen(true);
    if (related) return;
    setRelatedLoading(true);
    try {
      const out = await getRelated(relatedType(item.type), item.source_id ?? null, item.title);
      setRelated(out.items);
    } catch (err) {
      console.error("related notes", err);
      setRelated([]);
    } finally {
      setRelatedLoading(false);
    }
  }

  function handleClick() {
    if (isWeb && item.url) {
      window.open(item.url, "_blank", "noopener");
      return;
    }
    onOpen();
  }

  return (
    <div className="mt-2.5">
      <button
        type="button"
        onClick={handleClick}
        className="flex w-full items-center gap-3 rounded-xl border border-line bg-panel px-3.5 py-3 text-left transition-colors hover:border-edge-soft hover:bg-fog/[0.04]"
      >
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-fog/[0.05]">
          <Icon
            size={15}
            className={isWeb ? "text-emerald-400" : isNote ? "text-brand" : "text-info"}
            strokeWidth={1.75}
          />
        </span>
        {/* 条目 49：卡片整体是按钮（装饰默认不可选），但正文（标题/来源/摘要）要能选中复制 */}
        <span className="min-w-0 flex-1 select-text">
          <span className="flex items-center gap-2">
            <span className="text-[11px] text-faint">{kindLabel(item)}</span>
            <span className="truncate text-[13.5px] font-medium text-ink">{item.title}</span>
          </span>
          {isWeb && item.url ? (
            <span className="mt-0.5 block truncate text-[11.5px] text-faint">{item.url}</span>
          ) : item.kb_name ? (
            <span className="mt-0.5 block text-[11.5px] text-faint">来自：{item.kb_name}</span>
          ) : null}
          <span className="mt-0.5 block line-clamp-2 text-[12.5px] leading-5 text-mute">{item.snippet}</span>
        </span>
        <ChevronRight size={14} className="shrink-0 text-faint" strokeWidth={1.75} />
      </button>
      {/* 条目 3.3：来源身份（chunk_id / ordinal）——可折叠、默认收起；
          不占用卡片本身的点击跳转（它是独立按钮），也不影响下方「相关笔记」折叠区。
          历史消息 / 旧后端没有 chunk_id → 不渲染，界面与改造前逐字一致。 */}
      {identity ? (
        <>
          <button
            type="button"
            onClick={() => setIdentityOpen((value) => !value)}
            aria-expanded={identityOpen}
            className="mt-1 flex w-full items-center gap-1.5 rounded-lg px-2 py-1 text-left text-[11.5px] text-faint transition-colors hover:text-mute"
          >
            <ChevronDown
              size={12}
              className={cx("shrink-0 transition-transform", identityOpen && "rotate-180")}
              strokeWidth={1.75}
            />
            来源身份
          </button>
          {identityOpen ? (
            <div className="mt-0.5 flex flex-col gap-0.5 rounded-xl border border-hairline bg-fog/[0.02] px-2 py-1.5 text-[11.5px] leading-5 text-faint">
              <span className="break-all select-text">chunk_id：{identity.chunk_id}</span>
              <span>资料编号：{identity.ordinal == null ? "—" : `[${identity.ordinal}]`}</span>
            </div>
          ) : null}
        </>
      ) : null}
      {/* 联网结果没有 wiki 双链，不展示「相关笔记」 */}
      {!isWeb ? (
        <>
          <button
            type="button"
            onClick={() => void toggleRelated()}
            aria-expanded={relatedOpen}
            className="mt-1 flex w-full items-center gap-1.5 rounded-lg px-2 py-1 text-left text-[11.5px] text-faint transition-colors hover:text-mute"
          >
            <ChevronDown
              size={12}
              className={cx("shrink-0 transition-transform", relatedOpen && "rotate-180")}
              strokeWidth={1.75}
            />
            相关笔记
          </button>
          {relatedOpen ? (
            <div className="mt-0.5 rounded-xl border border-hairline bg-fog/[0.02] px-2 py-1.5">
              {relatedLoading ? (
                <p className="px-1 py-1 text-[12px] text-faint">加载中…</p>
              ) : !related?.length ? (
                <p className="px-1 py-1 text-[12px] text-faint">暂无通过链接关联的笔记</p>
              ) : (
                <div className="flex flex-col">
                  {related.map((hit) => (
                    <button
                      key={`${hit.type}-${hit.id}`}
                      type="button"
                      onClick={() => onOpenRelated(hit.type, hit.id)}
                      className="flex w-full items-center gap-2 rounded-lg px-1.5 py-1.5 text-left transition-colors hover:bg-fog/[0.06]"
                    >
                      <span
                        className={cx(
                          "shrink-0 rounded-full px-1.5 py-0.5 text-[10px]",
                          hit.type === "note" ? "bg-brand/15 text-brand" : "bg-info/15 text-info",
                        )}
                      >
                        {hit.type === "note" ? "笔记" : "文档"}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink">{hit.title}</span>
                      <span className="shrink-0 text-[11px] text-faint">{directionLabel(hit.direction)}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
