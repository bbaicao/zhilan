import { useEffect, useState } from "react";
import { ArchiveRestore, Inbox, Loader2, RefreshCw, Trash2 } from "lucide-react";
import {
  ApiError,
  emptyTrash,
  getTrash,
  purgeTrash,
  restoreTrash,
  type TrashItem,
  type TrashKind,
  type TrashOut,
} from "../api";
import { cx, formatTime } from "../utils";

type TrashViewProps = {
  /** 操作结果提示：与全站共用顶部横幅 */
  onNotify(message: string): void;
  /** 站内数据变了（恢复/彻底删除/清空）：App 刷新常驻列表——会话恢复后侧栏「最近」要立刻回来 */
  onChanged?(kind: TrashKind): void;
};

type TabKey = "notes" | "documents" | "chats";

const TABS: { key: TabKey; label: string; kind: TrashKind; empty: string }[] = [
  { key: "notes", label: "笔记", kind: "note", empty: "回收站里没有笔记。删除的笔记会先移到这里，可随时恢复。" },
  { key: "documents", label: "文档", kind: "document", empty: "回收站里没有文档。删除的文档会先移到这里，可随时恢复。" },
  { key: "chats", label: "会话", kind: "chat", empty: "回收站里没有会话。删除的会话会先移到这里，可随时恢复。" },
];

function titleOf(item: TrashItem) {
  return item.title || "（无标题）";
}

export default function TrashView({ onNotify, onChanged }: TrashViewProps) {
  const [data, setData] = useState<TrashOut | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<TabKey>("notes");

  // onNotify 只进横幅（flash 用 ref + setState 实现，闭包旧化无影响），refresh 不进依赖
  async function refresh() {
    setLoading(true);
    try {
      setData(await getTrash());
    } catch (err) {
      onNotify(err instanceof ApiError ? err.message : "回收站加载失败");
    } finally {
      setLoading(false);
    }
  }

  // 挂载即刷新：进回收站先看到当下真实内容
  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const total = data ? data.notes.length + data.documents.length + data.chats.length : 0;
  const active = TABS.find((item) => item.key === tab) ?? TABS[0];
  const rows: TrashItem[] = data ? data[tab] : [];

  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    try {
      await action();
    } finally {
      setBusy(false);
    }
  }

  async function restore(item: TrashItem) {
    await run(async () => {
      try {
        await restoreTrash(active.kind, item.id);
        onNotify(`已恢复「${titleOf(item)}」`);
        onChanged?.(active.kind);
        await refresh();
      } catch (err) {
        onNotify(err instanceof ApiError ? err.message : "恢复失败");
      }
    });
  }

  async function purge(item: TrashItem) {
    // 与全站删除确认同口径：把后果写清楚
    if (!window.confirm(`彻底删除「${titleOf(item)}」？将连同分块、向量、链接记录一起消失，不可恢复。`)) return;
    await run(async () => {
      try {
        await purgeTrash(active.kind, item.id);
        onNotify(`已彻底删除「${titleOf(item)}」`);
        onChanged?.(active.kind);
        await refresh();
      } catch (err) {
        onNotify(err instanceof ApiError ? err.message : "彻底删除失败");
      }
    });
  }

  async function emptyAll() {
    const count = total;
    if (count === 0) return;
    if (!window.confirm(`清空回收站？${count} 条项目将连同分块、向量、链接记录一起彻底删除，不可恢复。`)) return;
    await run(async () => {
      try {
        const result = await emptyTrash();
        onNotify(`已清空回收站：笔记 ${result.notes}、文档 ${result.documents}、会话 ${result.chats}`);
        onChanged?.("chat");
        await refresh();
      } catch (err) {
        onNotify(err instanceof ApiError ? err.message : "清空回收站失败");
      }
    });
  }

  return (
    <div className="mx-auto flex h-full w-full max-w-[820px] min-h-0 flex-col px-6 py-5">
      {/* 头部：标题 + 清空 */}
      <div className="flex items-center gap-2">
        <Trash2 size={18} strokeWidth={1.75} className="text-brand" />
        <h1 className="text-[20px] font-semibold text-ink">回收站</h1>
        <div className="flex-1" />
        <button
          type="button"
          onClick={() => void refresh()}
          aria-label="刷新回收站"
          disabled={loading || busy}
          className="flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[12.5px] text-mute transition-colors hover:bg-fog/[0.06] hover:text-ink disabled:opacity-50"
        >
          <RefreshCw size={13} strokeWidth={1.75} className={loading ? "animate-spin" : ""} />
          刷新
        </button>
        <button
          type="button"
          onClick={() => void emptyAll()}
          disabled={total === 0 || busy}
          className="flex h-8 items-center gap-1.5 rounded-lg border border-rose/30 bg-rose-soft px-2.5 text-[12.5px] text-rose transition-colors hover:border-rose/50 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Trash2 size={13} strokeWidth={1.75} />
          清空回收站
        </button>
      </div>
      <p className="mt-1 text-[11.5px] leading-4 text-faint">
        删除的笔记 / 文档 / 会话先移到这里；彻底删除才会连同分块、向量、链接记录一起清掉。
      </p>

      {/* 三个 tab：笔记 N / 文档 N / 会话 N */}
      <div className="mt-3 flex items-center gap-1 border-b border-line">
        {TABS.map((item) => {
          const count = data ? data[item.key].length : 0;
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => setTab(item.key)}
              className={cx(
                "-mb-px border-b-2 px-3 py-2 text-[13px] transition-colors",
                tab === item.key
                  ? "border-brand text-ink"
                  : "border-transparent text-mute hover:text-ink",
              )}
            >
              {item.label} {count}
            </button>
          );
        })}
      </div>

      {/* 列表 */}
      <div className="mt-3 min-h-0 flex-1 overflow-auto scrollbar-thin">
        {loading && !data ? (
          <div className="flex h-40 items-center justify-center gap-2 text-[13px] text-mute">
            <Loader2 size={15} className="animate-spin" />
            正在加载…
          </div>
        ) : rows.length === 0 ? (
          <div className="flex h-40 flex-col items-center justify-center gap-2 text-mute">
            <Inbox size={22} strokeWidth={1.5} className="text-faint" />
            <p className="text-[12.5px]">{active.empty}</p>
          </div>
        ) : (
          <ul className="space-y-1.5 pb-4">
            {rows.map((item) => (
              <li
                key={item.id}
                className="group flex items-center gap-3 rounded-lg border border-line bg-panel px-3 py-2.5 transition-colors hover:border-fog/15"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[13px] text-ink" title={titleOf(item)}>
                    {titleOf(item)}
                  </p>
                  <p className="mt-0.5 flex items-center gap-2 text-[11px] text-faint">
                    {item.category ? <span className="truncate">分类：{item.category}</span> : null}
                    <span className="shrink-0">删除于 {formatTime(item.deleted_at)}</span>
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => void restore(item)}
                  disabled={busy}
                  aria-label={`恢复${active.label}`}
                  className="flex h-7 shrink-0 items-center gap-1 rounded-md px-2 text-[12px] text-body transition-colors hover:bg-fog/[0.08] hover:text-ink disabled:opacity-50"
                >
                  <ArchiveRestore size={13} strokeWidth={1.75} />
                  恢复
                </button>
                <button
                  type="button"
                  onClick={() => void purge(item)}
                  disabled={busy}
                  aria-label={`彻底删除${active.label}`}
                  className="flex h-7 shrink-0 items-center gap-1 rounded-md px-2 text-[12px] text-mute transition-colors hover:bg-rose-soft hover:text-rose disabled:opacity-50"
                >
                  <Trash2 size={13} strokeWidth={1.75} />
                  彻底删除
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
