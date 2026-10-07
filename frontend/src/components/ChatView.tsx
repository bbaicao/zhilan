import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Archive, ChevronDown } from "lucide-react";
import type { ChatDetail, ChatFilters, ChatMessage, ReasoningState, ReasoningValue } from "../api";
import { getChat, getSettings, peekLatestChatDetail } from "../api";
import type { KbSource } from "./Composer";
import Composer, { type ComposerImage, type QueueEntry } from "./Composer";
import MessageItem from "./MessageItem";
import TopBanner from "./TopBanner";
import TranslateDialog from "./TranslateDialog";
import WelcomeScreen from "./WelcomeScreen";
import { cx } from "../utils";

type ChatViewProps = {
  viewToken: number;
  messages: ChatMessage[];
  loading: boolean;
  input: string;
  /** 条目 38：附件区（多张待发图片，追加式） */
  images: ComposerImage[];
  modelChip: string;
  currentModel: string;
  kbSource: KbSource;
  models: string[] | null;
  modelsLoading: boolean;
  modelsError: string;
  /** 这批型号来自哪条供应商 / 没得可选时的说明 */
  modelsScope: string;
  modelsNote: string;
  /** 思考程度（条目 5）：当前对话型号的官方思考参数 */
  reasoning: ReasoningState | null;
  filters: ChatFilters;
  categories: string[];
  /** 条目 26：本次会话是否启用联网检索 */
  webEnabled: boolean;
  /** 条目 18：本次会话的 Agent 模式（""=跟随设置） */
  agentMode: string;
  /** 条目 18：设置页配置的默认 Agent 模式名（跟随设置时在菜单里显示） */
  agentDefaultLabel: string;
  onInputChange(value: string): void;
  onSend(override?: string): void;
  /** 条目 42：停止当前正在进行的生成（发送按钮在生成中变停止键） */
  onStop(): void;
  /** 条目 37：排队中的消息（流式/思考中再发送进入排队；仅会话内内存态） */
  queue: QueueEntry[];
  /** 条目 37 修订：「↑ 立即」：立刻发出该条（有回答在生成时先停掉它再马上发） */
  onQueueSendNow(id: number): void;
  /** 就地编辑后保存 */
  onQueueUpdate(id: number, text: string): void;
  onQueueRemove(id: number): void;
  /** 拖拽把手排序 */
  onQueueReorder(from: number, to: number): void;
  /** 条目 42：空闲且队列非空（如停止后保留的队列）时，发出队首那条 */
  onQueueContinue(): void;
  /** Ctrl+回车：把当前输入作为「方向调整」插到队首 */
  onQueueDirection(): void;
  /** 条目 38：追加附件（粘贴 / 拖拽 / 选择器共用），一次可带多张 */
  onPickImages(files: File[]): void;
  onRemoveImage(index: number): void;
  onPickSource(source: KbSource): void;
  onFetchModels(): void;
  onPickModel(model: string): void;
  onPickReasoning(param: string, value: ReasoningValue | null): void;
  onOpenSettings(): void;
  onOpenDocs(source?: KbSource): void;
  onOpenRef(type: string, id?: number | null): void;
  onChangeFilters(next: ChatFilters): void;
  /** 条目 26：切换联网检索 */
  onToggleWeb(): void;
  /** 条目 18：切换本次会话的 Agent 模式（""=跟随设置） */
  onAgentMode(value: string): void;
  /** 条目 48：把最新用户消息原文交还 App 装回输入框（重新编辑） */
  onEditUser(text: string): void;
  /** 条目 48：装回输入框后聚焦；变化一次 = 聚焦一次（初始 0 不触发） */
  focusSignal?: number;
  notice?: string;
};

function Thinking() {
  return (
    <div className="flex items-center gap-2 text-[13px] text-mute animate-fade-in">
      <span className="flex items-center gap-1">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="h-1.5 w-1.5 rounded-full bg-mute animate-think-dot"
            style={{ animationDelay: `${i * 160}ms` }}
          />
        ))}
      </span>
      答案正在清晰中~
    </div>
  );
}

/**
 * 条目 24 记忆压缩：已压缩早期对话的折叠条，点开看摘要全文，再点收起（无摘要不渲染）。
 *
 * ChatView 拿不到当前 chatId（App 不传），从 getChat 的最近结果里认，并用消息 id
 * 交集确认这份详情确实属于正在看的会话（防止换会话后缓存串台）；会话内刚发生的
 * 压缩靠消息条数变化时顺手重拉一次详情来及时显示。
 */
function CompressSummaryBar({ messages }: { messages: ChatMessage[] }) {
  const [summary, setSummary] = useState<{ id: number; text: string; upto: number | null } | null>(null);
  const [open, setOpen] = useState(false);
  const fetchedId = useRef<number | null>(null);
  const prevLength = useRef(-1);
  const summaryChatId = useRef<number | null>(null);

  useEffect(() => {
    const cached = peekLatestChatDetail();
    const viewIds = new Set(messages.filter((item) => item.id > 0).map((item) => item.id));
    const apply = (detail: ChatDetail | null) => {
      const matches = Boolean(detail && detail.messages.some((item) => viewIds.has(item.id)));
      const next =
        matches && detail?.context_summary
          ? { id: detail.id, text: detail.context_summary, upto: detail.compressed_up_to ?? null }
          : null;
      if ((next?.id ?? null) !== summaryChatId.current) {
        // 换了会话（或摘要消失）才动折叠状态，流式更新不打扰用户手动展开
        summaryChatId.current = next?.id ?? null;
        setOpen(false);
      }
      setSummary((prev) => {
        if (prev && next && prev.id === next.id && prev.text === next.text && prev.upto === next.upto) return prev;
        return next;
      });
    };
    apply(cached);
    const lengthChanged = prevLength.current !== messages.length;
    prevLength.current = messages.length;
    if (!cached || (fetchedId.current === cached.id && !lengthChanged)) return;
    fetchedId.current = cached.id;
    getChat(cached.id)
      .then(apply)
      .catch(() => {});
  }, [messages]);

  if (!summary) return null;
  return (
    <div className="mb-8 overflow-hidden rounded-xl border border-line bg-panel">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-white/[0.03]"
      >
        <Archive size={12} className="shrink-0 text-brand" strokeWidth={1.75} />
        <span className="text-[12px] text-mute">已压缩早期对话</span>
        <span className="text-[11px] text-faint">
          {summary.upto != null ? `覆盖到第 ${summary.upto} 条 · ` : ""}点看摘要
        </span>
        <ChevronDown
          size={13}
          className={cx("ml-auto shrink-0 text-faint transition-transform", open && "rotate-180")}
          strokeWidth={1.75}
        />
      </button>
      {open ? (
        <div className="border-t border-hairline px-3 py-2">
          <div className="whitespace-pre-wrap break-words text-[12.5px] leading-6 text-mute">{summary.text}</div>
        </div>
      ) : null}
    </div>
  );
}

export default function ChatView(props: ChatViewProps) {
  const { messages, loading, onOpenRef } = props;
  // 追加：输出期间不再强制拽到底。滚动容器自己监听位置——只有用户本来就贴着底部
  // （阈值 80px）才跟随新内容；自己发送 / 排队续发（最后一条是 user）仍然一次性置底；
  // 上滑阅读时出现「回到底部」按钮，点了才恢复跟随。
  const scrollRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  useLayoutEffect(() => {
    setAtBottom(true);
    const box = scrollRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [props.viewToken]);
  const showEmpty = messages.length === 0 && !loading;
  const last = messages[messages.length - 1];
  // 条目 48：最新一条用户消息 = 扫描 messages 里最后一个 role==="user" 的 id
  // （流式中有 assistant 气泡在后，不能用「最后一条」判断；乐观回显的 user 也是负数 id，同样按 role 取）
  const latestUserId = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "user") return messages[i].id;
    }
    return null;
  })();
  // 条目 19：已经在吐思考过程时不再叠一个「正在清晰中」的点（那时思考块自己会展开）
  const streamingReasoning = last?.role === "assistant" && Boolean((last.reasoning || "").trim());
  const thinking = loading && !(last?.role === "assistant" && last.content) && !streamingReasoning;

  // 条目 23：DeepL 翻译。Key 状态挂载时拉一次设置（App 未把设置状态传进对话页）；
  // 弹窗挂在本层共享一个实例，MessageItem 通过 onTranslate 把要翻的文本带出来。
  const [translateHasKey, setTranslateHasKey] = useState(false);
  const [translateSource, setTranslateSource] = useState<string | null>(null);
  // 条目 23：译文存为笔记后的横幅提示（照 App 既有 TopBanner 用法，成功类提示几秒自动消失）
  const [savedBanner, setSavedBanner] = useState("");

  useEffect(() => {
    let cancelled = false;
    getSettings()
      .then((out) => {
        if (!cancelled) setTranslateHasKey(Boolean(out.translate?.has_key));
      })
      .catch(() => {
        /* 读不到设置按未配置处理：入口置灰 */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!savedBanner) return;
    const timer = window.setTimeout(() => setSavedBanner(""), 3200);
    return () => window.clearTimeout(timer);
  }, [savedBanner]);

  useEffect(() => {
    const box = scrollRef.current;
    if (!box) return;
    const onScroll = () => {
      const near = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
      setAtBottom((prev) => (prev === near ? prev : near));
    };
    box.addEventListener("scroll", onScroll, { passive: true });
    return () => box.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    const box = scrollRef.current;
    if (!box) return;
    // 自己的新消息（发送 / 排队续发）：无条件置底，保证看得到回答的开头
    if (last && last.role === "user") {
      setAtBottom(true);
      box.scrollTop = box.scrollHeight;
      return;
    }
    // 其余内容增量（流式 token、思考、完成回填）：只在贴底时跟随；跟随用瞬移，
    // 不用 smooth——高频增量叠平滑动画会漂
    if (atBottom) box.scrollTop = box.scrollHeight;
  }, [messages, loading]);

  function jumpToBottom() {
    const box = scrollRef.current;
    if (!box) return;
    setAtBottom(true);
    box.scrollTop = box.scrollHeight;
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {savedBanner ? <TopBanner message={savedBanner} onClose={() => setSavedBanner("")} /> : null}
      <div className="relative min-h-0 min-w-0 flex-1">
        <div ref={scrollRef} className="h-full overflow-auto scrollbar-thin">
          {showEmpty ? (
            <WelcomeScreen notice={props.notice} />
          ) : (
            // 消息列与输入框同宽（px-4 + 880），左右边缘和输入框对齐
            <div className="mx-auto w-full max-w-[912px] px-4 py-8">
              <CompressSummaryBar messages={messages} />
              {messages.map((item) => (
                <MessageItem
                  key={item.id}
                  message={item}
                  onOpenRef={onOpenRef}
                  translateEnabled={translateHasKey}
                  onTranslate={setTranslateSource}
                  latestUser={item.id === latestUserId}
                  onEditUser={props.onEditUser}
                  onNotify={setSavedBanner}
                />
              ))}
              {thinking ? <Thinking /> : null}
            </div>
          )}
        </div>
        {!atBottom && !showEmpty ? (
          <button
            type="button"
            onClick={jumpToBottom}
            aria-label="回到底部"
            className="absolute bottom-3 right-4 flex items-center gap-1 rounded-full border border-line bg-panel px-2.5 py-1.5 text-[11.5px] text-mute shadow-card transition-colors hover:text-ink animate-fade-in"
          >
            <ChevronDown size={12} strokeWidth={2} />
            回到底部
          </button>
        ) : null}
      </div>
      <Composer
        value={props.input}
        loading={props.loading}
        images={props.images}
        modelChip={props.modelChip}
        currentModel={props.currentModel}
        kbSource={props.kbSource}
        models={props.models}
        modelsLoading={props.modelsLoading}
        modelsError={props.modelsError}
        modelsScope={props.modelsScope}
        modelsNote={props.modelsNote}
        reasoning={props.reasoning}
        filters={props.filters}
        categories={props.categories}
        webEnabled={props.webEnabled}
        agentMode={props.agentMode}
        agentDefaultLabel={props.agentDefaultLabel}
        onAgentMode={props.onAgentMode}
        onChange={props.onInputChange}
        onSubmit={props.onSend}
        onStop={props.onStop}
        queue={props.queue}
        onQueueSendNow={props.onQueueSendNow}
        onQueueUpdate={props.onQueueUpdate}
        onQueueRemove={props.onQueueRemove}
        onQueueReorder={props.onQueueReorder}
        onQueueContinue={props.onQueueContinue}
        onQueueDirection={props.onQueueDirection}
        onPickImages={props.onPickImages}
        onRemoveImage={props.onRemoveImage}
        onPickSource={props.onPickSource}
        onFetchModels={props.onFetchModels}
        onPickModel={props.onPickModel}
        onPickReasoning={props.onPickReasoning}
        onOpenSettings={props.onOpenSettings}
        onOpenDocs={props.onOpenDocs}
        onChangeFilters={props.onChangeFilters}
        onToggleWeb={props.onToggleWeb}
        focusSignal={props.focusSignal}
      />
      {translateSource !== null ? (
        <TranslateDialog
          sourceText={translateSource}
          onClose={() => setTranslateSource(null)}
          onSavedNote={(title) => setSavedBanner(`已存为笔记《${title}》`)}
        />
      ) : null}
    </div>
  );
}
