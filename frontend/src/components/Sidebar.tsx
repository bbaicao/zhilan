import { useEffect, useMemo, useState } from "react";
import {
  Archive,
  Bell,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Ellipsis,
  Folder,
  FolderPlus,
  Library,
  Loader2,
  MessageSquare,
  Pencil,
  Search,
  Settings,
  Share2,
  SquarePen,
  StickyNote,
  Trash2,
  Undo2,
  Waves,
} from "lucide-react";
import type { ChatGroup, ChatItem } from "../api";
import { cx, type View } from "../utils";

/** 条目 44：一条任务完成通知——非当前会话的流 done / error 时由 App 推入，内存态，刷新即清 */
export type Notice = {
  id: number;
  /** 收尾的会话；点条目跳回 */
  chatId: number;
  /** 会话标题（chats 里查不到时记「新会话」） */
  title: string;
  kind: "done" | "error";
  /** 状态行文案：「已完成」或「失败：…」 */
  text: string;
  /** 收尾时刻（epoch 毫秒） */
  time: number;
  read: boolean;
};

type SidebarProps = {
  view: View;
  chatId: number | null;
  isFreshChat: boolean;
  /** 条目 47 追加：正在生成回答的会话 id——行首图标换运行转圈（对齐 Codex），后台会话在跑也一眼可辨 */
  streamingChatIds: number[];
  /** 未归档会话（组内 + 未分组一起；归档的走 archivedChats） */
  chats: ChatItem[];
  /** 条目 40：已归档会话（「已归档」视图） */
  archivedChats: ChatItem[];
  /** 条目 40：会话分组（纯侧栏组织，不改变提问与检索范围） */
  groups: ChatGroup[];
  recentOpen: boolean;
  statusDot: "ok" | "warn" | "err";
  statusText: string;
  onToggleRecent(): void;
  onNewChat(): void;
  onOpenChat(id: number): void;
  onDeleteChat(id: number): void;
  onNavigate(view: View): void;
  /** 条目 34：应用内导航历史的后退/前进（栈在 App.tsx 维护），无历史可退/可进时置灰 */
  canBack: boolean;
  canForward: boolean;
  onBack(): void;
  onForward(): void;
  onOpenSearch(): void;
  onLater(): void;
  /* ---- 条目 40：分组与归档 ---- */
  onCreateGroup(name: string): void;
  onRenameGroup(id: number, name: string): void;
  /** 删除分组（App 里确认：组内会话回「最近」平铺，不删会话） */
  onDeleteGroup(id: number): void;
  /** 会话移入分组；groupId 为 null = 移出分组 */
  onMoveChat(id: number, groupId: number | null): void;
  onArchiveChat(id: number): void;
  onUnarchiveChat(id: number): void;
  /* ---- 条目 44：非当前会话任务完成的铃铛通知 ---- */
  /** 通知列表（最新在前，App 内存态，刷新即清） */
  notices: Notice[];
  /** 标记单条通知已读 */
  onNoticeRead(id: number): void;
  /** 全部标记已读 */
  onNoticesReadAll(): void;
};

const DOT_CLASS = {
  ok: "bg-ok",
  warn: "bg-warn",
  err: "bg-rose",
} as const;

/** 条目 40：分组折叠状态持久化键（存折叠中的分组 id 列表） */
const COLLAPSED_KEY = "zhilan-chat-groups-collapsed";

function readCollapsed(): number[] {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((item): item is number => typeof item === "number") : [];
  } catch {
    return [];
  }
}

function navBtn(active: boolean) {
  return cx(
    "flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-[13px] transition-colors",
    active ? "bg-fog/10 text-ink" : "text-body hover:bg-fog/[0.06] hover:text-ink",
  );
}

function menuItem() {
  return "flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[12.5px] text-body transition-colors hover:bg-fog/[0.06] hover:text-ink";
}

export default function Sidebar(props: SidebarProps) {
  const {
    view,
    chatId,
    isFreshChat,
    streamingChatIds,
    chats,
    archivedChats,
    groups,
    recentOpen,
    statusDot,
    statusText,
    onToggleRecent,
    onNewChat,
    onOpenChat,
    onDeleteChat,
    onNavigate,
    canBack,
    canForward,
    onBack,
    onForward,
    onOpenSearch,
    // 条目 44：铃铛改为真实的通知面板，占位用的 onLater 不再使用（props 类型与 App 传参保持不变）
    onCreateGroup,
    onRenameGroup,
    onDeleteGroup,
    onMoveChat,
    onArchiveChat,
    onUnarchiveChat,
    notices,
    onNoticeRead,
    onNoticesReadAll,
  } = props;

  // 条目 40：区域内视图（最近 / 已归档）与折叠状态（localStorage 持久化）
  const [showArchived, setShowArchived] = useState(false);
  const [collapsedIds, setCollapsedIds] = useState<number[]>(readCollapsed);
  // 条目 40：行内建组 / 重命名输入
  const [creating, setCreating] = useState(false);
  const [createName, setCreateName] = useState("");
  const [renaming, setRenaming] = useState<{ id: number; name: string } | null>(null);
  // 行 hover ⋯ 菜单：kind 区分会话行 / 分组标题行，sub=true 时切到「移动到分组」二级列表
  const [menu, setMenu] = useState<{ kind: "chat" | "group"; id: number; sub: boolean } | null>(null);
  // 条目 44：铃铛通知下拉面板
  const [noticeOpen, setNoticeOpen] = useState(false);

  // 点击菜单外 / 按 Esc 关闭（⋯ 按钮本身不关，交给 click 切换，避免 mousedown 先关导致关不掉）
  useEffect(() => {
    if (!menu) return;
    function onDown(event: MouseEvent) {
      const target = event.target as HTMLElement | null;
      if (!target) return;
      if (target.closest("[data-menu-panel]")) return;
      if (target.closest("[data-menu-anchor]")) return;
      setMenu(null);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setMenu(null);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  // 条目 44：点面板外 / 按 Esc 关闭通知面板（铃铛按钮本身不关，交给 click 切换，避免 mousedown 先关导致关不掉）
  useEffect(() => {
    if (!noticeOpen) return;
    function onDown(event: MouseEvent) {
      const target = event.target as HTMLElement | null;
      if (!target) return;
      if (target.closest("[data-notice-panel]")) return;
      if (target.closest("[data-notice-anchor]")) return;
      setNoticeOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setNoticeOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [noticeOpen]);

  function toggleGroupCollapsed(id: number) {
    setCollapsedIds((prev) => {
      const next = prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id];
      try {
        window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify(next));
      } catch {
        // 写不进 localStorage（隐私模式等）时静默：本次会话内仍然生效
      }
      return next;
    });
  }

  function commitCreate() {
    const name = createName.trim();
    setCreating(false);
    setCreateName("");
    if (name) onCreateGroup(name);
  }

  function commitRename() {
    if (!renaming) return;
    const { id, name } = renaming;
    setRenaming(null);
    const trimmed = name.trim();
    if (trimmed) onRenameGroup(id, trimmed);
  }

  // 条目 44：未读通知数（角标 / 面板头计数 / 「全部已读」的显隐都按它）
  const unreadNotices = notices.filter((item) => !item.read).length;

  // 分组按 sort 升序、同序按创建先后；会话按后端给的最近顺序（id 倒序）分桶
  const groupsSorted = useMemo(
    () => [...groups].sort((a, b) => a.sort - b.sort || a.id - b.id),
    [groups],
  );
  const { byGroup, ungrouped } = useMemo(() => {
    const known = new Set(groups.map((group) => group.id));
    const grouped = new Map<number, ChatItem[]>();
    const loose: ChatItem[] = [];
    for (const item of chats) {
      const gid = item.group_id ?? null;
      if (gid !== null && known.has(gid)) {
        const list = grouped.get(gid) ?? [];
        list.push(item);
        grouped.set(gid, list);
      } else {
        loose.push(item);
      }
    }
    return { byGroup: grouped, ungrouped: loose };
  }, [chats, groups]);

  function chatRow(item: ChatItem, indent: boolean) {
    const active = view === "chat" && chatId === item.id;
    const menuOpen = menu?.kind === "chat" && menu.id === item.id;
    const currentGroup = groups.find((group) => group.id === (item.group_id ?? -1));
    // 条目 47 追加：这路会话有生成在跑 → 行首图标换运行转圈
    const streaming = streamingChatIds.includes(item.id);
    return (
      <div
        key={item.id}
        className={cx(
          "group/chat relative mb-0.5 flex items-center rounded-lg transition-colors",
          indent && "ml-2 w-[calc(100%-8px)]",
          active ? "bg-fog/10" : "hover:bg-fog/[0.06]",
        )}
      >
        <button
          type="button"
          onClick={() => {
            setMenu(null);
            onOpenChat(item.id);
          }}
          className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left"
        >
          {streaming ? (
            <Loader2
              size={14}
              className="shrink-0 animate-spin text-brand"
              strokeWidth={2}
              aria-label="会话正在生成"
            />
          ) : (
            <MessageSquare size={14} className="shrink-0 text-faint" strokeWidth={1.75} />
          )}
          <span className={cx("truncate text-[13px]", active ? "text-ink" : "text-body")}>{item.title}</span>
        </button>
        <button
          type="button"
          data-menu-anchor
          aria-label="会话菜单"
          onClick={() =>
            setMenu(menuOpen ? null : { kind: "chat", id: item.id, sub: false })
          }
          className="mr-1 hidden rounded p-1 text-mute hover:text-ink group-hover/chat:block"
        >
          <Ellipsis size={13} strokeWidth={1.75} />
        </button>
        {menuOpen ? (
          <div
            data-menu-panel
            className="absolute right-1 top-full z-30 w-40 rounded-xl border border-line bg-panel p-1 shadow-pop animate-fade-up"
          >
            {menu?.sub ? (
              <>
                <p className="px-2.5 pb-1 pt-1.5 text-[11px] text-faint">移动到分组</p>
                {groupsSorted.length === 0 ? (
                  <p className="px-2.5 pb-1.5 text-[12px] leading-5 text-faint">还没有分组，点标题行的 ＋ 新建。</p>
                ) : (
                  groupsSorted.map((group) => (
                    <button
                      key={group.id}
                      type="button"
                      onClick={() => {
                        setMenu(null);
                        if (group.id !== item.group_id) onMoveChat(item.id, group.id);
                      }}
                      className={cx(menuItem(), group.id === item.group_id && "text-faint")}
                    >
                      <Folder size={13} strokeWidth={1.75} className="shrink-0 text-mute" />
                      <span className="truncate">{group.name}</span>
                    </button>
                  ))
                )}
                {currentGroup ? (
                  <button
                    type="button"
                    onClick={() => {
                      setMenu(null);
                      onMoveChat(item.id, null);
                    }}
                    className={menuItem()}
                  >
                    <Undo2 size={13} strokeWidth={1.75} className="shrink-0 text-mute" />
                    移出分组
                  </button>
                ) : null}
              </>
            ) : (
              <>
                <button
                  type="button"
                  onClick={() => {
                    setMenu(null);
                    onArchiveChat(item.id);
                  }}
                  className={menuItem()}
                >
                  <Archive size={13} strokeWidth={1.75} className="shrink-0 text-mute" />
                  归档
                </button>
                <button
                  type="button"
                  onClick={() => setMenu({ kind: "chat", id: item.id, sub: true })}
                  className={menuItem()}
                >
                  <Folder size={13} strokeWidth={1.75} className="shrink-0 text-mute" />
                  移动到分组…
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setMenu(null);
                    onDeleteChat(item.id);
                  }}
                  className={cx(menuItem(), "hover:text-rose")}
                >
                  <Trash2 size={13} strokeWidth={1.75} className="shrink-0 text-mute" />
                  删除
                </button>
              </>
            )}
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <aside className="flex w-[248px] shrink-0 flex-col border-r border-line bg-sidebar">
      {/* 顶部：品牌 + 快捷图标 */}
      <div className="flex h-12 items-center justify-between pl-3 pr-2">
        {/* 品牌区：纯展示，不可点击 */}
        <div className="flex select-none items-center gap-2 px-1.5 py-1 text-[15px] font-medium">
          <span className="flex h-5 w-5 items-center justify-center rounded-md bg-brand-soft">
            <Waves size={12} className="text-brand" strokeWidth={2} />
          </span>
          知澜
        </div>
        <div className="flex items-center gap-0.5">
          {/* 条目 34：应用内导航历史的后退/前进（Alt+← / Alt+→），无历史时置灰不可点 */}
          <button
            type="button"
            onClick={onBack}
            disabled={!canBack}
            title="后退（Alt+←）"
            aria-label="后退"
            className="rounded-md p-1.5 text-mute transition-colors hover:bg-fog/5 hover:text-ink disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
          >
            <ChevronLeft size={16} strokeWidth={1.75} />
          </button>
          <button
            type="button"
            onClick={onForward}
            disabled={!canForward}
            title="前进（Alt+→）"
            aria-label="前进"
            className="rounded-md p-1.5 text-mute transition-colors hover:bg-fog/5 hover:text-ink disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
          >
            <ChevronRight size={16} strokeWidth={1.75} />
          </button>
          <button
            type="button"
            onClick={onOpenSearch}
            aria-label="搜索"
            className="rounded-md p-1.5 text-mute transition-colors hover:bg-fog/5 hover:text-ink"
          >
            <Search size={16} strokeWidth={1.75} />
          </button>
          {/* 条目 44：铃铛——非当前会话任务完成的提醒入口；未读数角标，9+ 封顶 */}
          <div className="relative">
            <button
              type="button"
              data-notice-anchor
              aria-label="通知"
              onClick={() => setNoticeOpen((open) => !open)}
              className="relative rounded-md p-1.5 text-mute transition-colors hover:bg-fog/5 hover:text-ink"
            >
              <Bell size={16} strokeWidth={1.75} />
              {unreadNotices > 0 ? (
                <span className="absolute -right-1 -top-1 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-brand px-1 text-[10px] font-medium leading-none text-canvas">
                  {unreadNotices > 9 ? "9+" : unreadNotices}
                </span>
              ) : null}
            </button>
            {noticeOpen ? (
              <div
                data-notice-panel
                className="absolute right-0 top-full z-50 mt-1.5 w-60 overflow-hidden rounded-2xl border border-line bg-panel shadow-card animate-fade-up"
              >
                <div className="flex h-9 items-center justify-between border-b border-hairline pl-3 pr-1.5">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[12.5px] font-medium text-ink">通知</span>
                    {unreadNotices > 0 ? (
                      <span className="rounded-full bg-brand/15 px-1.5 text-[10.5px] leading-4 text-brand">
                        {unreadNotices}
                      </span>
                    ) : null}
                  </div>
                  {unreadNotices > 0 ? (
                    <button
                      type="button"
                      onClick={onNoticesReadAll}
                      className="rounded px-1.5 py-0.5 text-[11.5px] text-mute transition-colors hover:bg-fog/[0.06] hover:text-ink"
                    >
                      全部已读
                    </button>
                  ) : null}
                </div>
                <div className="max-h-72 overflow-auto scrollbar-thin">
                  {notices.length === 0 ? (
                    <p className="px-3 py-3 text-[12px] leading-5 text-faint">暂无通知</p>
                  ) : (
                    notices.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => {
                          setNoticeOpen(false);
                          onNoticeRead(item.id);
                          onOpenChat(item.chatId);
                        }}
                        className="flex w-full items-start gap-2 px-3 py-2 text-left transition-colors hover:bg-fog/[0.06]"
                      >
                        <span
                          className={cx(
                            "mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full",
                            item.read ? "bg-transparent" : item.kind === "error" ? "bg-rose" : "bg-brand",
                          )}
                        />
                        <span className="min-w-0 flex-1">
                          <span
                            className={cx(
                              "block truncate text-[12.5px] leading-5",
                              item.read ? "text-body" : "font-medium text-ink",
                            )}
                          >
                            {item.title}
                          </span>
                          <span
                            className={cx(
                              "block truncate text-[11.5px] leading-4",
                              item.kind === "error" ? "text-rose" : item.read ? "text-faint" : "text-mute",
                            )}
                          >
                            {item.text}
                          </span>
                        </span>
                        <span className="shrink-0 pt-0.5 text-[10.5px] leading-4 text-faint">
                          {new Date(item.time).toLocaleTimeString("zh-CN", {
                            hour: "2-digit",
                            minute: "2-digit",
                          })}
                        </span>
                      </button>
                    ))
                  )}
                </div>
              </div>
            ) : null}
          </div>
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col px-2 pb-2">
        {/* 主导航 */}
        <nav className="space-y-0.5">
          <button type="button" className={cx("w-full", navBtn(isFreshChat))} onClick={onNewChat}>
            <SquarePen size={16} strokeWidth={1.75} className="text-mute" />
            新对话
          </button>
          <button type="button" className={cx("w-full", navBtn(view === "notes"))} onClick={() => onNavigate("notes")}>
            <StickyNote size={16} strokeWidth={1.75} className="text-mute" />
            笔记
          </button>
          <button type="button" className={cx("w-full", navBtn(view === "docs"))} onClick={() => onNavigate("docs")}>
            <Library size={16} strokeWidth={1.75} className="text-mute" />
            知识库
          </button>
          <button type="button" className={cx("w-full", navBtn(view === "graph"))} onClick={() => onNavigate("graph")}>
            <Share2 size={16} strokeWidth={1.75} className="text-mute" />
            图谱
          </button>
          {/* 条目 25：回收站入口 */}
          <button type="button" className={cx("w-full", navBtn(view === "trash"))} onClick={() => onNavigate("trash")}>
            <Trash2 size={16} strokeWidth={1.75} className="text-mute" />
            回收站
          </button>
        </nav>

        {/* 最近会话标题区：折叠开关 + 已归档入口（条目 40）+ 新建分组 */}
        <div className="mt-5 flex h-7 items-center justify-between rounded-md pl-2 pr-1 text-[12px] text-mute">
          {showArchived ? (
            <button
              type="button"
              onClick={() => setShowArchived(false)}
              className="flex items-center gap-1 transition-colors hover:text-ink"
              title="返回最近"
            >
              <ChevronLeft size={13} strokeWidth={1.75} />
              已归档
            </button>
          ) : (
            <button
              type="button"
              onClick={onToggleRecent}
              className="flex items-center gap-1 transition-colors hover:text-ink"
            >
              <span>最近</span>
              {recentOpen ? (
                <ChevronDown size={13} strokeWidth={1.75} />
              ) : (
                <ChevronRight size={13} strokeWidth={1.75} />
              )}
            </button>
          )}
          {!showArchived ? (
            <div className="flex items-center gap-0.5">
              {/* 已归档入口：区域内切到归档视图，角标 = 归档条数 */}
              <button
                type="button"
                onClick={() => setShowArchived(true)}
                title="已归档"
                className="flex items-center gap-1 rounded-md px-1.5 py-0.5 transition-colors hover:bg-fog/[0.06] hover:text-ink"
              >
                <Archive size={13} strokeWidth={1.75} />
                已归档
                {archivedChats.length > 0 ? (
                  <span className="rounded-full bg-fog/10 px-1.5 text-[10.5px] leading-4 text-body">
                    {archivedChats.length}
                  </span>
                ) : null}
              </button>
              {/* 新建分组 */}
              <button
                type="button"
                onClick={() => {
                  setCreating(true);
                  setCreateName("");
                  if (!recentOpen) onToggleRecent(); // 收起时先展开，否则输入框看不见
                }}
                title="新建分组"
                aria-label="新建分组"
                className="rounded-md p-1 transition-colors hover:bg-fog/[0.06] hover:text-ink"
              >
                <FolderPlus size={13} strokeWidth={1.75} />
              </button>
            </div>
          ) : null}
        </div>

        {recentOpen ? (
          <div className="mt-0.5 min-h-0 flex-1 overflow-auto scrollbar-thin">
            {showArchived ? (
              /* 归档视图：取消归档 + 删除（删除走既有确认，进回收站） */
              archivedChats.length === 0 ? (
                <p className="px-2 py-2 text-[12px] leading-5 text-faint">没有已归档的会话。</p>
              ) : (
                archivedChats.map((item) => (
                  <div
                    key={item.id}
                    className="group/arch relative mb-0.5 flex items-center rounded-lg transition-colors hover:bg-fog/[0.06]"
                  >
                    <button
                      type="button"
                      onClick={() => onOpenChat(item.id)}
                      className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left"
                    >
                      {streamingChatIds.includes(item.id) ? (
                        <Loader2
                          size={14}
                          className="shrink-0 animate-spin text-brand"
                          strokeWidth={2}
                          aria-label="会话正在生成"
                        />
                      ) : (
                        <Archive size={14} className="shrink-0 text-faint" strokeWidth={1.75} />
                      )}
                      <span className="truncate text-[13px] text-body">{item.title}</span>
                    </button>
                    <div className="mr-1 hidden items-center gap-0.5 group-hover/arch:flex">
                      <button
                        type="button"
                        onClick={() => onUnarchiveChat(item.id)}
                        title="取消归档"
                        aria-label="取消归档"
                        className="rounded p-1 text-mute transition-colors hover:text-ink"
                      >
                        <Undo2 size={13} strokeWidth={1.75} />
                      </button>
                      <button
                        type="button"
                        onClick={() => onDeleteChat(item.id)}
                        title="删除（移入回收站）"
                        aria-label="删除会话"
                        className="rounded p-1 text-mute transition-colors hover:text-rose"
                      >
                        <Trash2 size={13} strokeWidth={1.75} />
                      </button>
                    </div>
                  </div>
                ))
              )
            ) : (
              <>
                {creating ? (
                  <input
                    autoFocus
                    value={createName}
                    onChange={(event) => setCreateName(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") commitCreate();
                      if (event.key === "Escape") {
                        setCreating(false);
                        setCreateName("");
                      }
                    }}
                    onBlur={commitCreate}
                    placeholder="分组名称，回车创建"
                    aria-label="新建分组名称"
                    className="mb-0.5 w-full rounded-lg border border-edge bg-pit px-2 py-1.5 text-[12px] text-ink placeholder:text-faint outline-none"
                  />
                ) : null}
                {groupsSorted.map((group) => {
                  const collapsed = collapsedIds.includes(group.id);
                  const menuOpen = menu?.kind === "group" && menu.id === group.id;
                  return (
                    <div key={group.id}>
                      <div className="group/ghead relative mb-0.5 flex h-7 items-center rounded-lg pl-1 pr-1 transition-colors hover:bg-fog/[0.06]">
                        {renaming?.id === group.id ? (
                          <input
                            autoFocus
                            value={renaming.name}
                            onChange={(event) => setRenaming({ id: group.id, name: event.target.value })}
                            onKeyDown={(event) => {
                              if (event.key === "Enter") commitRename();
                              if (event.key === "Escape") setRenaming(null);
                            }}
                            onBlur={commitRename}
                            aria-label="重命名分组"
                            className="w-full rounded-md border border-edge bg-pit px-1.5 py-1 text-[12px] text-ink outline-none"
                          />
                        ) : (
                          <>
                            <button
                              type="button"
                              onClick={() => toggleGroupCollapsed(group.id)}
                              title={collapsed ? "展开分组" : "折叠分组"}
                              className="flex min-w-0 flex-1 items-center gap-1 px-1 text-left"
                            >
                              {collapsed ? (
                                <ChevronRight size={12} strokeWidth={1.75} className="shrink-0 text-faint" />
                              ) : (
                                <ChevronDown size={12} strokeWidth={1.75} className="shrink-0 text-faint" />
                              )}
                              <span className="truncate text-[12px] font-medium text-body">{group.name}</span>
                              <span className="shrink-0 text-[11px] text-faint">{group.chat_count}</span>
                            </button>
                            <button
                              type="button"
                              data-menu-anchor
                              aria-label="分组菜单"
                              onClick={() => setMenu(menuOpen ? null : { kind: "group", id: group.id, sub: false })}
                              className="hidden rounded p-1 text-mute transition-colors hover:text-ink group-hover/ghead:block"
                            >
                              <Ellipsis size={13} strokeWidth={1.75} />
                            </button>
                          </>
                        )}
                        {menuOpen ? (
                          <div
                            data-menu-panel
                            className="absolute right-1 top-full z-30 w-36 rounded-xl border border-line bg-panel p-1 shadow-pop animate-fade-up"
                          >
                            <button
                              type="button"
                              onClick={() => {
                                setRenaming({ id: group.id, name: group.name });
                                setMenu(null);
                              }}
                              className={menuItem()}
                            >
                              <Pencil size={13} strokeWidth={1.75} className="shrink-0 text-mute" />
                              重命名
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                setMenu(null);
                                onDeleteGroup(group.id);
                              }}
                              className={cx(menuItem(), "hover:text-rose")}
                            >
                              <Trash2 size={13} strokeWidth={1.75} className="shrink-0 text-mute" />
                              删除分组
                            </button>
                          </div>
                        ) : null}
                      </div>
                      {!collapsed
                        ? (byGroup.get(group.id) ?? []).map((item) => chatRow(item, true))
                        : null}
                    </div>
                  );
                })}
                {ungrouped.map((item) => chatRow(item, false))}
                {chats.length === 0 && groups.length === 0 && !creating ? (
                  <p className="px-2 py-2 text-[12px] leading-5 text-faint">还没有会话，从「新对话」开始。</p>
                ) : null}
              </>
            )}
          </div>
        ) : (
          <div className="flex-1" />
        )}

        {/* 底部：设置 + 服务状态 */}
        <div className="mt-auto space-y-0.5 border-t border-hairline pt-2">
          <button
            type="button"
            className={cx("w-full", navBtn(view === "settings"))}
            onClick={() => onNavigate("settings")}
          >
            <Settings size={16} strokeWidth={1.75} className="text-mute" />
            设置
          </button>
          <div className="flex h-8 items-center gap-2 px-2.5 text-[12px] text-mute">
            <span className={cx("h-1.5 w-1.5 shrink-0 rounded-full", DOT_CLASS[statusDot])} />
            <span className="truncate">{statusText}</span>
          </div>
        </div>
      </div>
    </aside>
  );
}
