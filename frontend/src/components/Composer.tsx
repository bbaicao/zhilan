import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import {
  ArrowUp,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CloudUpload,
  ExternalLink,
  Filter,
  Globe,
  GripVertical,
  Image as ImageIcon,
  Library,
  Link2,
  Loader2,
  Pencil,
  Play,
  Plus,
  Settings,
  Sparkles,
  Square,
  Trash2,
  X,
} from "lucide-react";
import {
  describeFilters,
  getSkills,
  hasFilters,
  AGENT_MODE_LABELS,
  listKnowledgeBases,
  slashableSkills,
  type ChatFilters,
  type KnowledgeBaseItem,
  type ReasoningState,
  type ReasoningValue,
  type SkillPack,
} from "../api";
import { imageFilesFromList, imagesFromClipboard } from "../image";
import { cx } from "../utils";

export type KbSource = "local" | "import" | "external";

/** 条目 38：附件区里的一张待发图片（data 为压缩后的 dataURL）。 */
export type ComposerImage = { name: string; data: string };

/** 条目 37：排队中的一条消息（仅会话内内存态，不落库，刷新即清）。
 *  kind="direction" 是 Ctrl+回车 的「方向调整」，插队首、带角标。 */
export type QueueEntry = {
  id: number;
  text: string;
  images: string[];
  kind: "followup" | "direction";
};

const SOURCES: Array<{
  id: KbSource;
  label: string;
  icon: typeof Library;
  available: boolean;
}> = [
  { id: "local", label: "本机数据库", icon: Library, available: true },
  { id: "import", label: "外部数据库", icon: CloudUpload, available: false },
  { id: "external", label: "在线数据库", icon: Link2, available: false },
];

/** M4 标签最小集在界面上的选项：类型 + 知识库 + 分类（分类由现有笔记动态带出）。 */
const SOURCE_TYPE_OPTIONS: Array<{ id: string; label: string }> = [
  { id: "document", label: "文档" },
  { id: "note", label: "笔记" },
];

const DEFAULT_KB_OPTIONS: Array<{ id: number; label: string }> = [{ id: 1, label: "本机知识库" }];

/** 条目 18：Agent 模式的会话级选项；""=跟随设置（请求体传 null），其余为强制指定。 */
const AGENT_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "", label: "跟随设置" },
  { value: "off", label: "直答" },
  { value: "react", label: "ReAct 工具循环" },
  { value: "plan_execute", label: "计划执行" },
];

type ComposerProps = {
  value: string;
  loading: boolean;
  /** 条目 38：附件区（多张待发图片，追加式） */
  images: ComposerImage[];
  modelChip: string;
  currentModel: string;
  kbSource: KbSource;
  models: string[] | null;
  modelsLoading: boolean;
  modelsError: string;
  /** 这批型号来自哪条供应商（空 = .env 手工配置） */
  modelsScope: string;
  /** 没得可选时的说明，例如「这条供应商还没勾选型号」 */
  modelsNote: string;
  /** 思考程度：当前对话型号的官方思考参数（条目 5），null = 还没读到设置 */
  reasoning: ReasoningState | null;
  filters: ChatFilters;
  categories: string[];
  /** 条目 26：本次会话是否启用联网检索（会话级开关，初值来自设置） */
  webEnabled: boolean;
  /** 条目 18：本次会话的 Agent 模式（""=跟随设置） */
  agentMode?: string;
  onAgentMode?(value: string): void;
  /** 条目 18：设置页配置的默认 Agent 模式名（跟随设置时显示） */
  agentDefaultLabel?: string;
  onChange(value: string): void;
  onSubmit(override?: string): void;
  /** 条目 42：停止当前生成（生成中发送位变停止键 ■） */
  onStop(): void;
  /** 条目 37：排队中的消息（流式 / 思考中再发送进入排队） */
  queue: QueueEntry[];
  /** 条目 37 修订：「↑ 立即」：**立刻发出这条**——有回答在生成时先停掉它（部分留档）再马上发 */
  onQueueSendNow(id: number): void;
  onQueueUpdate(id: number, text: string): void;
  onQueueRemove(id: number): void;
  /** 拖拽把手排序 */
  onQueueReorder(from: number, to: number): void;
  /** 条目 42：空闲且队列非空（如停止后保留的队列）时，发出队首那条 */
  onQueueContinue(): void;
  /** Ctrl+回车：当前输入作为「方向调整」插到队首 */
  onQueueDirection(): void;
  /** 条目 38：追加附件——粘贴 / 拖拽 / 文件选择器三条路都汇到这里（一次可带多张） */
  onPickImages(files: File[]): void;
  /** 删除附件区里的某一张（按下标） */
  onRemoveImage(index: number): void;
  onPickSource(source: KbSource): void;
  onFetchModels(): void;
  onPickModel(model: string): void;
  /** 思考程度：param 传空 = 清空（回到厂商默认） */
  onPickReasoning(param: string, value: ReasoningValue | null): void;
  onOpenSettings(): void;
  onOpenDocs(source?: KbSource): void;
  onChangeFilters(next: ChatFilters): void;
  /** 条目 26：切换联网检索（仅开关，不打开设置） */
  onToggleWeb(): void;
  /** 条目 48：装回输入框后聚焦；变化一次 = 聚焦一次（初始 0 不触发） */
  focusSignal?: number;
};

function MenuShell({
  children,
  onClose,
  align = "left",
}: {
  children: React.ReactNode;
  onClose(): void;
  align?: "left" | "right";
}) {
  useEffect(() => {
    const opener = document.activeElement;
    return () => {
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  }, []);
  useEffect(() => {
    function onKey(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <>
      <button type="button" aria-label="关闭菜单" className="fixed inset-0 z-30" onClick={onClose} />
      <div
        className={cx(
          "absolute bottom-[calc(100%+8px)] z-40 w-64 rounded-xl border border-line bg-panel p-1.5 shadow-pop animate-fade-up",
          align === "left" ? "left-0" : "right-0",
        )}
      >
        {children}
      </div>
    </>
  );
}

/** 一级菜单里的一行：左边名称、右边当前值、末尾 › 表示点进去还有一层。 */
function ChoiceRow({ label, value, onClick }: { label: string; value: string; onClick(): void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left transition-colors hover:bg-fog/[0.06]"
    >
      <span className="shrink-0 text-[13.5px] text-ink">{label}</span>
      <span className="min-w-0 flex-1 truncate text-right text-[13.5px] text-mute">{value}</span>
      <ChevronRight size={15} strokeWidth={1.75} className="shrink-0 text-faint" />
    </button>
  );
}

/** 二级页的返回条：点标题回到一级菜单。 */
function PaneHeader({ title, onBack }: { title: string; onBack(): void }) {
  return (
    <button
      type="button"
      onClick={onBack}
      className="mb-0.5 flex w-full items-center gap-1 rounded-lg px-2 py-1.5 text-left text-[12.5px] text-mute transition-colors hover:bg-fog/[0.06] hover:text-ink"
    >
      <ChevronLeft size={14} strokeWidth={1.75} className="shrink-0" />
      {title}
    </button>
  );
}

/** 思考程度：选项、取值、默认值全部照厂商规格渲染——界面不自己编档位（条目 5）。
 *  型号没登记官方参数时不给选项，改为提示「到 .env 手写 CHAT_REASONING」。 */
function ReasoningPane({
  state,
  onPick,
}: {
  state: ReasoningState | null;
  onPick(param: string, value: ReasoningValue | null): void;
}) {
  const [draft, setDraft] = useState("");

  useEffect(() => {
    setDraft(state?.current != null ? String(state.current) : "");
  }, [state?.current, state?.param]);

  if (!state) {
    return <div className="px-2.5 pb-2 text-[11px] text-faint">读不到设置</div>;
  }

  const manualValue = state.manual_active && state.manual ? state.manual.value : null;
  const canApplyDraft = draft.trim() !== "" && !Number.isNaN(Number(draft));

  return (
    <div>
      <div className="px-2.5 pb-1 text-[11px] text-faint">
        {state.registered ? `${state.vendor} 官方参数 ${state.param}` : "该型号未登记官方参数"}
      </div>

      {state.registered ? (
        <>
          <button
            type="button"
            onClick={() => onPick("", null)}
            className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[12.5px] text-body transition-colors hover:bg-fog/[0.06]"
          >
            <span className={cx("min-w-0 flex-1 truncate", state.current == null && "text-ink")}>
              厂商默认
            </span>
            {state.default != null ? (
              <span className="shrink-0 text-[11px] text-faint">{String(state.default)}</span>
            ) : null}
            {state.current == null ? <Check size={14} className="shrink-0 text-brand" strokeWidth={2} /> : null}
          </button>

          {state.kind === "int" ? (
            <div className="flex items-center gap-1.5 px-2.5 pb-1.5">
              <input
                type="number"
                inputMode="numeric"
                min={state.minimum ?? undefined}
                max={state.maximum ?? undefined}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder={`${state.minimum ?? ""} ~ ${state.maximum ?? ""}`}
                className="min-w-0 flex-1 rounded-md bg-fog/[0.06] px-2 py-1 text-[12.5px] text-ink outline-none placeholder:text-faint"
              />
              <button
                type="button"
                disabled={!canApplyDraft}
                onClick={() => onPick(state.param, Number(draft))}
                className={cx(
                  "rounded-md px-2 py-1 text-[12px] transition-colors",
                  canApplyDraft ? "text-brand hover:bg-fog/5" : "cursor-not-allowed text-faint",
                )}
              >
                应用
              </button>
            </div>
          ) : (
            state.options.map((option) => {
              const active = state.current === option.value;
              return (
                <button
                  key={String(option.value)}
                  type="button"
                  onClick={() => onPick(state.param, option.value)}
                  className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[12.5px] text-body transition-colors hover:bg-fog/[0.06]"
                >
                  <span className={cx("min-w-0 flex-1 truncate", active && "text-ink")}>{option.label}</span>
                  {option.hint ? (
                    <span className="max-w-[110px] truncate text-[11px] text-faint">{option.hint}</span>
                  ) : null}
                  {active ? <Check size={14} className="shrink-0 text-brand" strokeWidth={2} /> : null}
                </button>
              );
            })
          )}

          {state.source ? (
            <a
              href={state.source}
              target="_blank"
              rel="noreferrer"
              className="mt-0.5 flex items-center gap-1.5 px-2.5 pb-1.5 pt-1 text-[11px] text-mute transition-colors hover:text-ink"
            >
              <ExternalLink size={11} strokeWidth={1.75} />
              厂商文档
            </a>
          ) : null}
        </>
      ) : (
        <div className="whitespace-pre-line px-2.5 pb-1.5 text-[11px] leading-4 text-faint">
          {manualValue != null && state.manual
            ? `按 .env 手填生效：${state.manual.param} = ${String(manualValue)}`
            : "可在 .env 手写 CHAT_REASONING 调这个型号"}
        </div>
      )}
    </div>
  );
}

/** 条目 18：Agent 模式二级菜单——跟随设置或本次强制某一模式，只影响当前会话。 */
function AgentPane({
  value,
  defaultLabel,
  onPick,
}: {
  value: string;
  defaultLabel: string;
  onPick(next: string): void;
}) {
  return (
    <div>
      {AGENT_OPTIONS.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value || "follow"}
            type="button"
            onClick={() => onPick(option.value)}
            className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[12.5px] text-body transition-colors hover:bg-fog/[0.06]"
          >
            <span className={cx("min-w-0 flex-1 truncate", active && "text-ink")}>{option.label}</span>
            {option.value === "" && defaultLabel ? (
              <span className="max-w-[110px] truncate text-[11px] text-faint">{defaultLabel}</span>
            ) : null}
            {active ? <Check size={14} className="shrink-0 text-brand" strokeWidth={2} /> : null}
          </button>
        );
      })}
    </div>
  );
}

/** 条目 37 修订：一张排队卡片。把手可拖拽排序；「↑ 立即」= **立刻发出这条**——
 *  正在生成时先停掉当前回答（已生成部分留档并标「已停止」）再马上发它，不再干等下一轮；
 *  编辑就地改；「方向调整」角标只给 Ctrl+回车 进来的那类条目。 */
function QueueCard({
  item,
  index,
  busy,
  onSendNow,
  onUpdate,
  onRemove,
  onReorder,
}: {
  item: QueueEntry;
  index: number;
  /** 本会话此刻有回答在生成——「立即」会先停掉它，按钮提示据此变化 */
  busy: boolean;
  onSendNow(id: number): void;
  onUpdate(id: number, text: string): void;
  onRemove(id: number): void;
  onReorder(from: number, to: number): void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.text);
  const [dragging, setDragging] = useState(false);
  const summary = item.text || `（${item.images.length} 张图片）`;

  function startEditing() {
    setDraft(item.text);
    setEditing(true);
  }

  function save() {
    const text = draft.trim();
    if (text) onUpdate(item.id, text);
    setEditing(false);
  }

  return (
    <div
      draggable={dragging}
      onDragStart={(event: DragEvent<HTMLDivElement>) => {
        event.dataTransfer.setData("text/plain", String(index));
        event.dataTransfer.effectAllowed = "move";
      }}
      onDragEnd={() => setDragging(false)}
      onDragOver={(event: DragEvent<HTMLDivElement>) => event.preventDefault()}
      onDrop={(event: DragEvent<HTMLDivElement>) => {
        event.preventDefault();
        const from = Number(event.dataTransfer.getData("text/plain"));
        if (!Number.isNaN(from)) onReorder(from, index);
      }}
      className="rounded-xl border border-line bg-panel px-2.5 py-1.5 shadow-card animate-fade-in"
    >
      <div className="flex items-start gap-1.5">
        <button
          type="button"
          data-drag-handle
          aria-label="拖拽排序"
          title="拖拽调整发送顺序"
          onMouseDown={() => setDragging(true)}
          onMouseUp={() => setDragging(false)}
          className="mt-1 shrink-0 cursor-grab text-faint hover:text-mute active:cursor-grabbing"
        >
          <GripVertical size={14} strokeWidth={1.75} />
        </button>
        {editing ? (
          <div className="min-w-0 flex-1">
            <textarea
              autoFocus
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  save();
                }
                if (event.key === "Escape") setEditing(false);
              }}
              rows={2}
              className="w-full resize-none rounded-lg bg-fog/[0.06] px-2 py-1.5 text-[13px] leading-5 text-ink outline-none placeholder:text-faint"
            />
            <div className="mt-1 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setEditing(false)}
                className="rounded-md px-2 py-0.5 text-[12px] text-mute transition-colors hover:bg-fog/5 hover:text-ink"
              >
                取消
              </button>
              <button
                type="button"
                disabled={!draft.trim()}
                onClick={save}
                className={cx(
                  "rounded-md px-2 py-0.5 text-[12px] transition-colors",
                  draft.trim() ? "text-brand hover:bg-fog/5" : "cursor-not-allowed text-faint",
                )}
              >
                保存
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                {item.kind === "direction" ? (
                  <span className="shrink-0 rounded-full bg-brand/15 px-1.5 py-0.5 text-[10px] text-brand">
                    方向调整
                  </span>
                ) : (
                  <span className="shrink-0 rounded-full bg-fog/[0.08] px-1.5 py-0.5 text-[10px] text-mute">
                    排队中
                  </span>
                )}
                <span className="min-w-0 truncate text-[13px] text-body" title={summary}>
                  {summary}
                </span>
              </div>
              {item.images.length > 0 ? (
                <div className="mt-0.5 flex items-center gap-1 text-[11px] text-faint">
                  <ImageIcon size={11} strokeWidth={1.75} />
                  {item.images.length} 张图片
                </div>
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-0.5">
              <button
                type="button"
                onClick={() => onSendNow(item.id)}
                title={busy ? "立即发送（将停止当前生成）" : "立即发送这条"}
                aria-label="立即发送这条排队消息"
                className="flex items-center gap-0.5 rounded-md px-1.5 py-1 text-[11.5px] text-mute transition-colors hover:bg-fog/5 hover:text-ink"
              >
                <ArrowUp size={12} strokeWidth={2} />
                立即
              </button>
              <button
                type="button"
                onClick={startEditing}
                aria-label="编辑这条排队消息"
                className="rounded-md p-1 text-mute transition-colors hover:bg-fog/5 hover:text-ink"
              >
                <Pencil size={12} strokeWidth={1.75} />
              </button>
              <button
                type="button"
                onClick={() => onRemove(item.id)}
                aria-label="删除这条排队消息"
                className="rounded-md p-1 text-mute transition-colors hover:bg-fog/5 hover:text-ink"
              >
                <Trash2 size={12} strokeWidth={1.75} />
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default function Composer(props: ComposerProps) {
  const {
    value,
    loading,
    images,
    modelChip,
    currentModel,
    kbSource,
    models,
    modelsLoading,
    modelsError,
    modelsScope,
    modelsNote,
    reasoning,
    onChange,
    onSubmit,
    onStop,
    queue,
    onQueueSendNow,
    onQueueUpdate,
    onQueueRemove,
    onQueueReorder,
    onQueueContinue,
    onQueueDirection,
    onPickImages,
    onRemoveImage,
    onPickSource,
    onFetchModels,
    onPickModel,
    onPickReasoning,
    onOpenSettings,
    onOpenDocs,
    filters,
    categories,
    onChangeFilters,
    webEnabled,
    onToggleWeb,
    focusSignal,
    agentMode = "",
    onAgentMode,
    agentDefaultLabel = "",
  } = props;
  const imageRef = useRef<HTMLInputElement>(null);
  const areaRef = useRef<HTMLTextAreaElement>(null);
  const [openMenu, setOpenMenu] = useState<"source" | "model" | "filter" | null>(null);
  /** 模型菜单的两级：root=「模型 / 推理等级 / Agent 模式」三行，models / reasoning / agent 是点进去的清单 */
  const [modelPane, setModelPane] = useState<"root" | "models" | "reasoning" | "agent">("root");
  const [kbOptions, setKbOptions] = useState(DEFAULT_KB_OPTIONS);
  /** 条目 35：`/` 技能菜单（输入以 / 开头时弹出） */
  const [skills, setSkills] = useState<SkillPack[]>([]);
  const [slashIndex, setSlashIndex] = useState(0);
  /** 条目 45：技能菜单顶部搜索框的文字；开菜单瞬间以 textarea 里 `/` 后已有的文字预填 */
  const [slashSearch, setSlashSearch] = useState("");

  useEffect(() => {
    void listKnowledgeBases()
      .then((rows: KnowledgeBaseItem[]) => {
        if (!rows.length) return;
        setKbOptions(rows.map((item) => ({ id: item.id, label: item.name })));
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    void getSkills()
      .then((out) => setSkills(slashableSkills(out.items)))
      .catch(() => setSkills([]));
  }, []);

  // 条目 37：流式期间发送按钮不再禁用——点它就是把当前输入追加到队尾
  const canSend = Boolean(value.trim() || images.length);
  const activeSource = SOURCES.find((item) => item.id === kbSource) || SOURCES[0];
  const filterText = describeFilters(filters);
  const filtered = hasFilters(filters);

  /** 条目 35：输入框以 `/` 开头且后面还没空格时弹技能菜单 */
  const slashOpen = !loading && value.startsWith("/") && !value.includes(" ") && value.length <= 32;
  // 条目 45：有效查询以顶部搜索框文字为准（开菜单时已用 `/` 后文字预填，续打无缝落在搜索框）
  const slashQuery = slashOpen ? slashSearch.toLowerCase() : "";
  const slashHits = slashOpen
    ? skills.filter(
        (item) =>
          !slashQuery ||
          item.slug.toLowerCase().includes(slashQuery) ||
          item.name.toLowerCase().includes(slashQuery) ||
          item.description.toLowerCase().includes(slashQuery),
      )
    : [];
  /** 条目 45：过滤后高亮夹取到合法范围（列表变短时高亮不悬空，Enter/Tab 也取不到空项） */
  const slashActive = Math.min(slashIndex, slashHits.length - 1);

  // 条目 45：开菜单瞬间把 textarea 里 `/` 后已有的文字灌进搜索框，关菜单时清掉。
  // 依赖只留 slashOpen：value 每敲一个字都变，进依赖会不停重置搜索框，
  // 初值改从 textarea DOM 上读来规避；用 useLayoutEffect 保证首帧画的就是过滤后的列表。
  useLayoutEffect(() => {
    if (slashOpen) {
      setSlashSearch((areaRef.current?.value ?? "").slice(1));
      setSlashIndex(0);
    } else {
      setSlashSearch("");
    }
  }, [slashOpen]);

  function insertSkill(item: SkillPack) {
    onChange(`/${item.slug} `);
    setOpenMenu(null);
    requestAnimationFrame(() => areaRef.current?.focus());
  }

  function toggleIn<T>(list: T[] | undefined, item: T): T[] | undefined {
    const current = list ?? [];
    const next = current.includes(item) ? current.filter((entry) => entry !== item) : [...current, item];
    return next.length ? next : undefined;
  }

  function patchFilters(patch: ChatFilters) {
    const next: ChatFilters = { ...filters, ...patch };
    (Object.keys(next) as Array<keyof ChatFilters>).forEach((key) => {
      const value = next[key];
      if (value === undefined || (Array.isArray(value) && value.length === 0)) {
        delete next[key];
      }
    });
    onChangeFilters(next);
  }

  useEffect(() => {
    const area = areaRef.current;
    if (!area) return;
    area.style.height = "auto";
    area.style.height = Math.min(area.scrollHeight, 200) + "px";
  }, [value]);

  // 条目 48：focusSignal 变化（且非初始值）时聚焦输入框并把光标放到末尾。
  // 用 ref 记住已处理过的值：切走再回到会话导致 Composer 重挂时不会凭空抢焦点。
  const lastFocusSignal = useRef(focusSignal ?? 0);
  useEffect(() => {
    if (!focusSignal || focusSignal === lastFocusSignal.current) return;
    lastFocusSignal.current = focusSignal;
    const area = areaRef.current;
    if (!area) return;
    area.focus();
    const end = area.value.length;
    area.setSelectionRange(end, end);
  }, [focusSignal]);

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing) return;
    if (slashOpen && slashHits.length) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setSlashIndex((i) => (i + 1) % slashHits.length);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setSlashIndex((i) => (i - 1 + slashHits.length) % slashHits.length);
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        const pick = slashHits[slashActive];
        if (pick) insertSkill(pick);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        onChange("");
        return;
      }
    }
    // 条目 37：Ctrl+回车 = 方向调整入队（插队首）；空闲时等同普通发送
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      if (loading) onQueueDirection();
      else onSubmit();
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      onSubmit();
    }
  }

  /** 条目 45：搜索框里的键盘——↑↓ 在命中项间循环移动高亮，Enter/Tab 插入高亮项（复用 insertSkill），
   *  Esc 先清空搜索（列表回全部、高亮回 0），空了再关闭（与 textarea Esc 同语义：清掉斜杠文字）。 */
  function onSearchKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.nativeEvent.isComposing) return; // 中文输入法组字期间不抢键，交还输入法
    if (event.key === "ArrowDown" && slashHits.length) {
      event.preventDefault();
      setSlashIndex((i) => (Math.min(i, slashHits.length - 1) + 1) % slashHits.length);
      return;
    }
    if (event.key === "ArrowUp" && slashHits.length) {
      event.preventDefault();
      setSlashIndex((i) => (Math.min(i, slashHits.length - 1) - 1 + slashHits.length) % slashHits.length);
      return;
    }
    if (event.key === "Enter" || (event.key === "Tab" && slashHits.length)) {
      // Enter 一律 preventDefault：别把外层 form 提交出去；Tab 无命中时放行走默认聚焦
      event.preventDefault();
      const pick = slashHits[slashActive];
      if (pick) insertSkill(pick);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      if (slashSearch) {
        setSlashSearch("");
        setSlashIndex(0);
      } else {
        onChange("");
      }
    }
  }

  /** 粘贴图片（截图 / 复制来的图）：条目 38——剪贴板里**全部**图片都追加进附件区。
   *  剪贴板里没有图片时不拦，纯文本粘贴照旧。 */
  function onPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const files = imagesFromClipboard(event.clipboardData?.items);
    if (!files.length) return;
    event.preventDefault();
    onPickImages(files);
  }

  /** 条目 38：把图片拖到输入框上也能追加（与粘贴 / 选择器同一条路）。 */
  function onDropImages(event: DragEvent<HTMLFormElement>) {
    const files = imageFilesFromList(event.dataTransfer?.files);
    if (!files.length) return;
    event.preventDefault();
    onPickImages(files);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    onSubmit();
  }

  function toggleMenu(menu: "source" | "model" | "filter") {
    const next = openMenu === menu ? null : menu;
    setOpenMenu(next);
    if (next === "model") setModelPane("root");
    // 必须放在 setState 之外：updater 是在渲染阶段跑的，里面再 setState 会触发
    // 「Cannot update a component while rendering a different component」告警并可能丢更新。
    if (next === "model") onFetchModels();
  }

  /** 触发按钮上跟在型号后面的推理等级；没设置就不显示（对齐 DeepSeek 客户端的「模型 + 等级」）。 */
  const levelChip =
    reasoning?.current != null
      ? String(reasoning.current)
      : reasoning?.manual_active && reasoning.manual
        ? String(reasoning.manual.value)
        : "";
  /** 一级菜单「推理等级」右侧显示的当前值。 */
  const reasoningValue = !reasoning
    ? "读不到设置"
    : reasoning.registered
      ? reasoning.current != null
        ? String(reasoning.current)
        : "厂商默认"
      : levelChip || "未登记";
  /** 一级菜单「Agent 模式」右侧显示的当前值（条目 18）。 */
  const agentModeValue =
    agentMode === ""
      ? `跟随设置（${agentDefaultLabel || "直答"}）`
      : AGENT_OPTIONS.find((option) => option.value === agentMode)?.label || agentMode;

  return (
    <form
      onSubmit={submit}
      onDragOver={(event) => {
        // 只有拖着文件进来才拦默认行为（纯文本拖拽照旧走编辑器选区）
        if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
      }}
      onDrop={onDropImages}
      className="px-4 pb-5 pt-1"
    >
      <div className="mx-auto w-full max-w-[880px]">
        {/* 条目 37：排队卡片——叠放在输入框上方、与输入区同宽；空闲且队列为空时不渲染 */}
        {queue.length > 0 ? (
          <div className="mb-2 space-y-1.5">
            {queue.map((item, index) => (
              <QueueCard
                key={item.id}
                item={item}
                index={index}
                busy={loading}
                onSendNow={onQueueSendNow}
                onUpdate={onQueueUpdate}
                onRemove={onQueueRemove}
                onReorder={onQueueReorder}
              />
            ))}
            <div className="flex items-center gap-2 px-1 text-[11px] text-faint">
              <span className="min-w-0 flex-1">
                {loading
                  ? "排队消息仅在本会话内生效（不落库）：当前回答结束后按序自动发出；「↑ 立即」会停掉当前生成、马上发出该条"
                  : "队列已暂停（不落库）：点「↑ 立即」马上发出该条或「继续发送」发出队首，也可逐条编辑 / 删除"}
              </span>
              {!loading ? (
                <button
                  type="button"
                  onClick={onQueueContinue}
                  title="发出队首的排队消息"
                  className="flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-[11.5px] text-brand transition-colors hover:bg-fog/5"
                >
                  <Play size={11} strokeWidth={2} />
                  继续发送
                </button>
              ) : null}
            </div>
          </div>
        ) : null}
        {/* 输入框主体 */}
        <div className="rounded-[22px] bg-composer px-3 pb-2.5 pt-2.5 shadow-card">
          {/* 条目 38：附件缩略图条——多张横排，每张右上角 × 可单删；>1 张时提示一次发给视觉模型几张 */}
          {images.length > 0 ? (
            <div className="mx-1 mb-2 flex flex-wrap items-center gap-2.5">
              {images.map((item, index) => (
                <div key={`${item.name}-${index}`} className="relative h-14 w-14 shrink-0" title={item.name}>
                  <img
                    src={item.data}
                    alt={item.name}
                    className="h-14 w-14 rounded-lg border border-hairline object-cover"
                  />
                  <button
                    type="button"
                    onClick={() => onRemoveImage(index)}
                    aria-label={`移除图片 ${index + 1}`}
                    title="移除这张图片"
                    className="absolute -right-1.5 -top-1.5 flex h-[18px] w-[18px] items-center justify-center rounded-full border border-hairline bg-panel text-mute shadow-card transition-colors hover:text-ink"
                  >
                    <X size={11} strokeWidth={2} />
                  </button>
                </div>
              ))}
              {images.length > 1 ? (
                <span className="text-[11.5px] text-faint">将一次发给视觉模型 {images.length} 张</span>
              ) : null}
            </div>
          ) : null}

          <div className="relative">
            {slashOpen ? (
              <div className="absolute bottom-full left-0 right-0 z-20 mb-2 overflow-hidden rounded-xl border border-line bg-panel shadow-pop">
                <div className="border-b border-hairline px-3 py-1.5 text-[11px] text-faint">选择技能</div>
                {/* 条目 45：顶部搜索框——开菜单即聚焦（autoFocus），续打直接在这里过滤 */}
                <div className="border-b border-hairline px-2 py-1.5">
                  <input
                    type="text"
                    autoFocus
                    value={slashSearch}
                    onChange={(event) => {
                      setSlashSearch(event.target.value);
                      setSlashIndex(0);
                    }}
                    onKeyDown={onSearchKeyDown}
                    placeholder="搜索技能…"
                    className="w-full rounded-md bg-fog/[0.06] px-2 py-1.5 text-[13px] text-ink outline-none placeholder:text-faint"
                  />
                </div>
                {slashHits.length > 0 ? (
                  <div className="max-h-56 overflow-auto scrollbar-thin">
                    {slashHits.map((item, index) => (
                      <button
                        key={item.id}
                        type="button"
                        onMouseDown={(e) => {
                          e.preventDefault();
                          insertSkill(item);
                        }}
                        onMouseEnter={() => setSlashIndex(index)}
                        className={cx(
                          "flex w-full items-start gap-2 px-3 py-2 text-left transition-colors",
                          index === slashActive ? "bg-fog/[0.06]" : "hover:bg-fog/[0.04]",
                        )}
                      >
                        <Sparkles
                          size={13}
                          className={cx("mt-0.5 shrink-0", index === slashActive ? "text-brand" : "text-mute")}
                          strokeWidth={1.75}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-1.5">
                            <span className="text-[13px] text-ink">{item.name}</span>
                            <span className="text-[11px] text-faint">/{item.slug}</span>
                          </div>
                          <div className="truncate text-[11.5px] text-mute">{item.description}</div>
                        </div>
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="px-3 py-3 text-center text-[12px] text-faint">无匹配技能</div>
                )}
              </div>
            ) : null}
          </div>

          <textarea
            ref={areaRef}
            value={value}
            onChange={(event) => {
              onChange(event.target.value);
              setSlashIndex(0);
            }}
            onKeyDown={onKeyDown}
            onPaste={onPaste}
            placeholder={loading ? "继续输入以排队后续修改" : "随心输入，/ 可选用技能"}
            rows={1}
            className="max-h-[200px] min-h-[40px] w-full bg-transparent px-2 py-1.5 text-[15px] leading-6 text-ink outline-none placeholder:text-faint scrollbar-thin"
          />

          <div className="mt-1 flex items-center gap-2">
            <button
              type="button"
              onClick={() => imageRef.current?.click()}
              aria-label="添加图片"
              className="flex h-8 w-8 items-center justify-center rounded-full text-mute transition-colors hover:bg-fog/5 hover:text-ink"
            >
              <Plus size={18} strokeWidth={1.75} />
            </button>
            <input
              ref={imageRef}
              type="file"
              accept="image/*"
              multiple
              className="hidden"
              onChange={(event) => {
                onPickImages(imageFilesFromList(event.target.files));
                event.target.value = "";
              }}
            />

            {/* 知识库来源选择 */}
            <div className="relative">
              <button
                type="button"
                onClick={() => toggleMenu("source")}
                className={cx(
                  "flex items-center gap-1.5 rounded-md px-1.5 py-1 text-[13px] text-brand transition-colors",
                  openMenu === "source" ? "bg-fog/5" : "hover:bg-fog/5",
                )}
              >
                <activeSource.icon size={14} strokeWidth={1.75} />
                {activeSource.label}
                <ChevronDown size={12} strokeWidth={1.75} className={cx("transition-transform", openMenu === "source" && "rotate-180")} />
              </button>
              {openMenu === "source" ? (
                <MenuShell onClose={() => setOpenMenu(null)}>
                  <div className="px-2.5 pb-1 pt-2 text-[11px] text-faint">知识库来源</div>
                  {SOURCES.map((item) => {
                    const Icon = item.icon;
                    const active = item.id === kbSource;
                    return (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => {
                          setOpenMenu(null);
                          if (!item.available) {
                            onOpenDocs(item.id);
                            return;
                          }
                          onPickSource(item.id);
                        }}
                        className={cx(
                          "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] text-body transition-colors hover:bg-fog/[0.06]",
                        )}
                      >
                        <Icon size={15} strokeWidth={1.75} className={active ? "text-brand" : "text-mute"} />
                        <span className="min-w-0 flex-1 truncate">{item.label}</span>
                        {item.available ? (
                          active ? <Check size={14} className="text-brand" strokeWidth={2} /> : null
                        ) : (
                          <span className="rounded-full bg-fog/[0.06] px-1.5 py-0.5 text-[10px]">未接入</span>
                        )}
                      </button>
                    );
                  })}
                  <div className="mt-1 border-t border-hairline pt-1">
                    <button
                      type="button"
                      onClick={() => {
                        setOpenMenu(null);
                        onOpenDocs();
                      }}
                      className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[12.5px] text-mute transition-colors hover:bg-fog/[0.06] hover:text-ink"
                    >
                      <Library size={14} strokeWidth={1.75} />
                      管理知识库
                    </button>
                  </div>
                </MenuShell>
              ) : null}
            </div>

            {/* 元数据过滤范围（M4）：过滤条件随提问一起下推到两路检索 */}
            <div className="relative">
              <button
                type="button"
                onClick={() => toggleMenu("filter")}
                title={filtered ? `检索范围：${filterText}` : "按类型 / 知识库 / 分类收窄检索范围"}
                className={cx(
                  "flex max-w-[260px] items-center gap-1.5 rounded-md px-1.5 py-1 text-[13px] transition-colors",
                  filtered ? "text-brand" : "text-mute hover:text-ink",
                  openMenu === "filter" ? "bg-fog/5" : "hover:bg-fog/5",
                )}
              >
                <Filter size={14} strokeWidth={1.75} className="shrink-0" />
                <span className="truncate">{filtered ? filterText : "筛选范围"}</span>
                <ChevronDown size={12} strokeWidth={1.75} className={cx("shrink-0 transition-transform", openMenu === "filter" && "rotate-180")} />
              </button>
              {openMenu === "filter" ? (
                <MenuShell onClose={() => setOpenMenu(null)}>
                  <div className="px-2.5 pb-1 pt-2 text-[11px] text-faint">检索范围（多个条件按与生效）</div>

                  <div className="px-2.5 pb-1 pt-1.5 text-[11px] text-mute">内容类型</div>
                  <div className="flex flex-wrap gap-1.5 px-2.5 pb-2">
                    {SOURCE_TYPE_OPTIONS.map((option) => {
                      const active = (filters.source_types ?? []).includes(option.id);
                      return (
                        <button
                          key={option.id}
                          type="button"
                          onClick={() =>
                            patchFilters({ source_types: toggleIn(filters.source_types, option.id) })
                          }
                          className={cx(
                            "rounded-full px-2.5 py-1 text-[12px] transition-colors",
                            active ? "bg-brand/15 text-brand" : "bg-fog/[0.06] text-body hover:bg-fog/[0.1]",
                          )}
                        >
                          {option.label}
                        </button>
                      );
                    })}
                  </div>

                  <div className="px-2.5 pb-1 text-[11px] text-mute">知识库</div>
                  <div className="flex flex-wrap gap-1.5 px-2.5 pb-2">
                    {kbOptions.map((option) => {
                      const active = (filters.kb_ids ?? []).includes(option.id);
                      return (
                        <button
                          key={option.id}
                          type="button"
                          onClick={() => patchFilters({ kb_ids: toggleIn(filters.kb_ids, option.id) })}
                          className={cx(
                            "rounded-full px-2.5 py-1 text-[12px] transition-colors",
                            active ? "bg-brand/15 text-brand" : "bg-fog/[0.06] text-body hover:bg-fog/[0.1]",
                          )}
                        >
                          {option.label}
                        </button>
                      );
                    })}
                  </div>

                  <div className="px-2.5 pb-1 text-[11px] text-mute">笔记分类</div>
                  {categories.length ? (
                    <div className="max-h-32 overflow-auto px-2.5 pb-2 scrollbar-thin">
                      <div className="flex flex-wrap gap-1.5">
                        {categories.map((name) => {
                          const active = (filters.categories ?? []).includes(name);
                          return (
                            <button
                              key={name}
                              type="button"
                              onClick={() =>
                                patchFilters({ categories: toggleIn(filters.categories, name) })
                              }
                              className={cx(
                                "max-w-[160px] truncate rounded-full px-2.5 py-1 text-[12px] transition-colors",
                                active ? "bg-brand/15 text-brand" : "bg-fog/[0.06] text-body hover:bg-fog/[0.1]",
                              )}
                            >
                              {name}
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  ) : (
                    <div className="px-2.5 pb-2 text-[12px] text-faint">还没有带分类的笔记</div>
                  )}

                  {filters.categories?.length ? (
                    <div className="px-2.5 pb-2 text-[11px] leading-4 text-faint">
                      选了分类就只看笔记（文档没有分类维度）
                    </div>
                  ) : null}

                  <div className="mt-1 flex items-center justify-between border-t border-hairline px-2.5 pt-2">
                    <span className="text-[11px] text-faint">{filtered ? filterText : "当前：全部资料"}</span>
                    <button
                      type="button"
                      disabled={!filtered}
                      onClick={() => onChangeFilters({})}
                      className={cx(
                        "rounded-md px-2 py-1 text-[12px] transition-colors",
                        filtered ? "text-brand hover:bg-fog/5" : "cursor-not-allowed text-faint",
                      )}
                    >
                      清除
                    </button>
                  </div>
                </MenuShell>
              ) : null}
            </div>

            {/* 条目 26：联网搜索开关——只切换，不打开设置；紧挨模型选择器左侧 */}
            <button
              type="button"
              onClick={onToggleWeb}
              aria-label="联网提供搜索"
              aria-pressed={webEnabled}
              title="开启后提问会补充公开网页结果（配置在设置页）"
              className={cx(
                "ml-auto flex items-center gap-1 rounded-md px-1.5 py-1 text-[12.5px] transition-colors",
                webEnabled
                  ? "bg-brand/15 text-brand"
                  : "text-faint hover:bg-fog/5 hover:text-mute",
              )}
            >
              <Globe size={14} strokeWidth={1.75} className="shrink-0" />
              <span className="hidden sm:inline">联网提供搜索</span>
            </button>

            {/* 模型 / 推理等级：一级菜单两行，点进去才是清单（对齐 DeepSeek 客户端的形态） */}
            <div className="relative">
              <button
                type="button"
                onClick={() => toggleMenu("model")}
                className={cx(
                  "flex max-w-[260px] items-center gap-1.5 rounded-md px-1.5 py-1 text-[12.5px] text-mute transition-colors hover:bg-fog/5 hover:text-ink",
                  openMenu === "model" && "bg-fog/5 text-ink",
                )}
              >
                <span className="truncate">
                  {currentModel
                    ? [
                        currentModel,
                        levelChip,
                        // 显式选了 ReAct / 计划执行时在触发按钮上跟一个当前模式（对齐「型号 + 等级」的形态）
                        agentMode === "react" || agentMode === "plan_execute"
                          ? AGENT_MODE_LABELS[agentMode]
                          : "",
                      ]
                        .filter(Boolean)
                        .join(" ")
                    : modelChip}
                </span>
                <ChevronDown size={12} strokeWidth={1.75} className={cx("shrink-0 transition-transform", openMenu === "model" && "rotate-180")} />
              </button>
              {openMenu === "model" ? (
                <MenuShell align="right" onClose={() => setOpenMenu(null)}>
                  {modelPane === "root" ? (
                    <div className="p-0.5">
                      <ChoiceRow label="模型" value={currentModel || "未配置"} onClick={() => setModelPane("models")} />
                      <ChoiceRow label="推理等级" value={reasoningValue} onClick={() => setModelPane("reasoning")} />
                      <ChoiceRow label="Agent 模式" value={agentModeValue} onClick={() => setModelPane("agent")} />
                    </div>
                  ) : modelPane === "models" ? (
                    <>
                      <PaneHeader title="模型" onBack={() => setModelPane("root")} />
                      <div className="px-2.5 pb-1 text-[11px] text-faint">
                        {modelsScope ? `来自「${modelsScope}」已勾选的型号` : "当前供应商的型号"}
                      </div>
                      {modelsLoading ? (
                        <div className="flex items-center gap-2 px-2.5 py-3 text-[12.5px] text-mute">
                          <Loader2 size={13} className="animate-spin" strokeWidth={2} />
                          正在拉取模型列表…
                        </div>
                      ) : modelsError ? (
                        <div className="px-2.5 py-2.5 text-[12px] leading-5 text-mute">{modelsError}</div>
                      ) : models && models.length > 0 ? (
                        <div className="max-h-60 overflow-auto scrollbar-thin">
                          {models.map((model) => {
                            const active = model === currentModel;
                            return (
                              <button
                                key={model}
                                type="button"
                                onClick={() => {
                                  onPickModel(model);
                                  setOpenMenu(null);
                                }}
                                className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[13px] text-body transition-colors hover:bg-fog/[0.06]"
                              >
                                <span className={cx("min-w-0 flex-1 truncate", active && "text-ink")}>{model}</span>
                                {active ? <Check size={14} className="shrink-0 text-brand" strokeWidth={2} /> : null}
                              </button>
                            );
                          })}
                        </div>
                      ) : (
                        <div className="px-2.5 py-2.5 text-[12px] leading-5 text-mute">
                          {modelsNote || "没有可用模型：到设置页给这条供应商勾选要用的型号。"}
                        </div>
                      )}
                      <div className="mt-1 border-t border-hairline pt-1">
                        <button
                          type="button"
                          onClick={() => {
                            setOpenMenu(null);
                            onOpenSettings();
                          }}
                          className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[12.5px] text-mute transition-colors hover:bg-fog/[0.06] hover:text-ink"
                        >
                          <Settings size={14} strokeWidth={1.75} />
                          在设置页管理模型服务
                        </button>
                      </div>
                    </>
                  ) : modelPane === "agent" ? (
                    <>
                      <PaneHeader title="Agent 模式" onBack={() => setModelPane("root")} />
                      <AgentPane
                        value={agentMode}
                        defaultLabel={agentDefaultLabel}
                        onPick={(next) => {
                          onAgentMode?.(next);
                          // 与推理等级的选择一致：回到一级菜单，不收起整个菜单
                          setModelPane("root");
                        }}
                      />
                    </>
                  ) : (
                    <>
                      <PaneHeader title="推理等级" onBack={() => setModelPane("root")} />
                      <ReasoningPane state={reasoning} onPick={onPickReasoning} />
                    </>
                  )}
                </MenuShell>
              ) : null}
            </div>

            {/* 条目 42：生成中发送位变停止键（↑ → ■）；点击中断当前生成，
                已生成的部分由服务端留档并在界面上标「已停止」 */}
            {loading ? (
              <button
                type="button"
                onClick={onStop}
                aria-label="停止生成"
                title="停止生成（已生成的部分会保留）"
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-rose/15 text-rose transition-colors hover:bg-rose/25"
              >
                <Square size={12} strokeWidth={2.5} fill="currentColor" />
              </button>
            ) : (
              <button
                type="submit"
                disabled={!canSend}
                aria-label="发送"
                className={cx(
                  "flex h-8 w-8 shrink-0 items-center justify-center rounded-full transition-colors",
                  canSend ? "bg-send text-canvas hover:bg-send-hover" : "bg-fog/10 text-faint",
                )}
              >
                <ArrowUp size={16} strokeWidth={2} />
              </button>
            )}
          </div>
        </div>
      </div>
    </form>
  );
}
