import { useEffect, useState } from "react";
import { BarChart3, Bot, ChevronRight, Database, Download, Filter, FolderDown, Globe, Languages, Palette, Puzzle, Sparkles, Waves } from "lucide-react";
import { ROLE_HINT, type Health, type SettingsOut } from "../api";
import { cx, labelProvider } from "../utils";
import { getResolvedTheme, getThemePref, THEME_CHANGE_EVENT } from "../theme";
import type { EditRole } from "./SettingsDialog";
import AppearanceDialog from "./AppearanceDialog";
import DeepLDialog from "./DeepLDialog";
import ExportDialog from "./ExportDialog";
import OfflineModelsDialog from "./OfflineModelsDialog";
import UsageDialog from "./UsageDialog";

type Dot = "ok" | "warn" | "err" | "off";

const DOT_CLASS: Record<Dot, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  err: "bg-rose",
  off: "bg-faint",
};

/** 模型配置入口：一条角色一行，点进去配供应商与型号。 */
function RoleRow({
  role,
  settings,
  down,
  onEditRole,
}: {
  role: EditRole;
  settings: SettingsOut;
  down: boolean;
  onEditRole(role: EditRole): void;
}) {
  const active = settings.active[role];
  const vision = settings.active.vision;
  const owner = active.service_id ? settings.services.find((item) => item.id === active.service_id) : undefined;

  let desc = active.error || ROLE_HINT[role];
  if (down) {
    desc = "后端未连接";
  } else if (active.configured) {
    const main = `${labelProvider(active.provider) || active.provider} · ${active.model}${
      owner ? `（来自「${owner.name}」）` : "（来自 .env 手工配置）"
    }`;
    const parts = role === "chat" ? [main, vision.configured ? `视觉 ${vision.model}` : "视觉未配置"] : [main];
    desc = parts.join(" ｜ ");
  }

  const ok = Boolean(active.configured);
  return (
    <button
      type="button"
      onClick={() => onEditRole(role)}
      aria-label={`配置${role === "chat" ? "对话" : "向量"}模型`}
      className="flex w-full items-center gap-3.5 rounded-2xl bg-fog/[0.03] px-4 py-3.5 text-left transition-colors hover:bg-fog/[0.05]"
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-fog/[0.05] text-mute">
        {role === "chat" ? <Sparkles size={16} strokeWidth={1.75} /> : <Waves size={16} strokeWidth={1.75} />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[14px] font-medium text-ink">{role === "chat" ? "对话模型" : "向量模型"}</div>
        <div className="mt-0.5 truncate text-[12.5px] text-mute">{desc}</div>
      </div>
      <ChevronRight size={16} className="shrink-0 text-faint" strokeWidth={1.75} />
      <span
        className={cx(
          "h-2 w-2 shrink-0 rounded-full",
          DOT_CLASS[down ? "err" : ok ? "ok" : role === "chat" ? "warn" : "off"],
        )}
      />
    </button>
  );
}

/** 数据服务入口：MySQL / Redis 状态 + 向量库（数据库 / 缓存）现在存在哪，点进去改存放位置。 */
function DataRow({
  health,
  settings,
  down,
  onEditData,
}: {
  health: Health | null;
  settings: SettingsOut | null;
  down: boolean;
  onEditData(): void;
}) {
  const dbOk = health?.database === "ok";
  const redisOk = health?.redis === "ok";
  const data = settings?.data;
  const vectors = data ? (data.vectors < 0 ? "还没有向量集合" : `${data.vectors} 条向量`) : "";

  let desc = "后端未连接";
  if (health) {
    const parts = [`MySQL ${health.database || "未知"} · Redis ${health.redis || "未知"}`];
    if (data) parts.push(`向量库存放于 ${data.chroma_path}（${vectors}）`);
    desc = parts.join(" ｜ ");
  }

  return (
    <button
      type="button"
      onClick={onEditData}
      title={desc}
      aria-label="配置数据库 / 缓存的存放位置"
      className="flex w-full items-center gap-3.5 rounded-2xl bg-fog/[0.03] px-4 py-3.5 text-left transition-colors hover:bg-fog/[0.05]"
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-fog/[0.05]">
        <Database size={16} className="text-ok" strokeWidth={1.75} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[14px] font-medium text-ink">数据库 / 缓存</div>
        <div className="mt-0.5 truncate text-[12.5px] text-mute">{desc}</div>
      </div>
      {health ? (
        <span className="flex shrink-0 items-center gap-2 text-[11px] text-faint">
          <span className="flex items-center gap-1">
            <span className={cx("h-1.5 w-1.5 rounded-full", dbOk ? "bg-ok" : "bg-rose")} />
            MySQL
          </span>
          <span className="flex items-center gap-1">
            <span className={cx("h-1.5 w-1.5 rounded-full", redisOk ? "bg-ok" : "bg-rose")} />
            Redis
          </span>
        </span>
      ) : null}
      <ChevronRight size={16} className="shrink-0 text-faint" strokeWidth={1.75} />
      <span
        className={cx(
          "h-2 w-2 shrink-0 rounded-full",
          DOT_CLASS[down ? "err" : dbOk && redisOk ? "ok" : "warn"],
        )}
      />
    </button>
  );
}

/** 检索增强入口（条目 28）：重排序开关与型号，点进去配置。 */
function RerankRow({
  settings,
  down,
  onEditRerank,
}: {
  settings: SettingsOut | null;
  down: boolean;
  onEditRerank(): void;
}) {
  const rerank = settings?.rerank;
  let desc = settings === null ? (down ? "后端未连接" : "正在读取配置…") : "";
  if (rerank) {
    if (rerank.enabled) {
      desc = rerank.effective
        ? `已开启 · ${rerank.provider_label} · ${rerank.model}`
        : `已开启但不会生效：${rerank.blocked_by || "配置不完整"}`;
    } else {
      desc = `已关闭 · 只用向量 + 关键词融合排序${
        rerank.model ? `（已存型号 ${rerank.model}，开开关即用）` : ""
      }`;
    }
  }
  const on = Boolean(rerank?.enabled);
  const warn = on && !rerank?.effective;

  return (
    <button
      type="button"
      onClick={onEditRerank}
      title={desc}
      aria-label="配置检索增强（重排序）"
      className="flex w-full items-center gap-3.5 rounded-2xl bg-fog/[0.03] px-4 py-3.5 text-left transition-colors hover:bg-fog/[0.05]"
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-fog/[0.05]">
        <Filter size={16} className={cx(on ? "text-brand" : "text-mute")} strokeWidth={1.75} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[14px] font-medium text-ink">搜索重排序</div>
        <div className={cx("mt-0.5 truncate text-[12.5px]", warn ? "text-warn" : "text-mute")}>{desc}</div>
      </div>
      <ChevronRight size={16} className="shrink-0 text-faint" strokeWidth={1.75} />
      <span className={cx("h-2 w-2 shrink-0 rounded-full", DOT_CLASS[down ? "err" : warn ? "warn" : on ? "ok" : "off"])} />
    </button>
  );
}

/** 联网检索入口（条目 26）：开关、服务商与条数，点进去配置。 */
function WebRow({
  settings,
  down,
  onEditWeb,
}: {
  settings: SettingsOut | null;
  down: boolean;
  onEditWeb(): void;
}) {
  const web = settings?.web;
  let desc = settings === null ? (down ? "后端未连接" : "正在读取配置…") : "";
  if (web) {
    if (web.enabled) {
      const channelLabel =
        web.channel === "llm"
          ? "大模型搜索"
          : web.channel === "api"
            ? "传统搜索 API"
            : null;
      const channelPart = channelLabel ? ` · ${channelLabel}` : "";
      desc = web.effective
        ? `已开启 · ${web.provider_label}${channelPart}`
        : `已开启但不会生效：${web.blocked_by || "配置不完整"}`;
    } else {
      desc = "已关闭 · 只用本机知识库（/web 需先开启）";
    }
  }
  const on = Boolean(web?.enabled);
  const warn = on && !web?.effective;

  return (
    <button
      type="button"
      onClick={onEditWeb}
      title={desc}
      aria-label="配置联网检索"
      className="flex w-full items-center gap-3.5 rounded-2xl bg-fog/[0.03] px-4 py-3.5 text-left transition-colors hover:bg-fog/[0.05]"
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-fog/[0.05]">
        <Globe size={16} className={cx(on ? "text-brand" : "text-mute")} strokeWidth={1.75} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[14px] font-medium text-ink">联网检索</div>
        <div className={cx("mt-0.5 truncate text-[12.5px]", warn ? "text-warn" : "text-mute")}>{desc}</div>
      </div>
      <ChevronRight size={16} className="shrink-0 text-faint" strokeWidth={1.75} />
      <span className={cx("h-2 w-2 shrink-0 rounded-full", DOT_CLASS[down ? "err" : warn ? "warn" : on ? "ok" : "off"])} />
    </button>
  );
}

/** 条目 18：Agent 入口——默认模式、循环上限与审计，点进去配置。 */
function AgentRow({
  settings,
  down,
  onEditAgent,
}: {
  settings: SettingsOut | null;
  down: boolean;
  onEditAgent(): void;
}) {
  const agent = settings?.agent;
  let desc = settings === null ? (down ? "后端未连接" : "正在读取配置…") : "默认直答 · 走现有 RAG 流程";
  if (agent) {
    const label = agent.modes?.find((row) => row.value === agent.mode)?.label || "直答";
    desc = `默认${label} · 循环上限 ${agent.max_rounds} 轮 · 审计${agent.audit ? "已开启" : "已关闭"}`;
  }
  const on = Boolean(agent && agent.mode !== "off");

  return (
    <button
      type="button"
      onClick={onEditAgent}
      title={desc}
      aria-label="配置 Agent 模式"
      className="flex w-full items-center gap-3.5 rounded-2xl bg-fog/[0.03] px-4 py-3.5 text-left transition-colors hover:bg-fog/[0.05]"
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-fog/[0.05]">
        <Bot size={16} className={cx(on ? "text-brand" : "text-mute")} strokeWidth={1.75} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[14px] font-medium text-ink">Agent 模式</div>
        <div className="mt-0.5 truncate text-[12.5px] text-mute">{desc}</div>
      </div>
      <ChevronRight size={16} className="shrink-0 text-faint" strokeWidth={1.75} />
      <span className={cx("h-2 w-2 shrink-0 rounded-full", DOT_CLASS[down ? "err" : on ? "ok" : "off"])} />
    </button>
  );
}

/** 条目 35：技能 / MCP 入口。 */
function SkillsRow({
  down,
  summary,
  onEditSkills,
}: {
  down: boolean;
  summary: string;
  onEditSkills(): void;
}) {
  const desc = down ? "后端未连接" : summary || "点进去管理本机 skill 与 MCP server";
  return (
    <button
      type="button"
      onClick={onEditSkills}
      title={desc}
      aria-label="配置技能 / MCP"
      className="flex w-full items-center gap-3.5 rounded-2xl bg-fog/[0.03] px-4 py-3.5 text-left transition-colors hover:bg-fog/[0.05]"
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-fog/[0.05]">
        <Puzzle size={16} className="text-mute" strokeWidth={1.75} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[14px] font-medium text-ink">技能 / MCP</div>
        <div className="mt-0.5 truncate text-[12.5px] text-mute">{desc}</div>
      </div>
      <ChevronRight size={16} className="shrink-0 text-faint" strokeWidth={1.75} />
      <span className={cx("h-2 w-2 shrink-0 rounded-full", DOT_CLASS[down ? "err" : "ok"])} />
    </button>
  );
}

/** 条目 17：全库导出入口——把笔记与文档原文按分类导出为本机 Markdown 文件夹。 */
function ExportRow({ onExport }: { onExport(): void }) {
  return (
    <button
      type="button"
      onClick={onExport}
      aria-label="导出全库为 Markdown"
      className="flex w-full items-center gap-3.5 rounded-2xl bg-fog/[0.03] px-4 py-3.5 text-left transition-colors hover:bg-fog/[0.05]"
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-fog/[0.05]">
        <FolderDown size={16} className="text-mute" strokeWidth={1.75} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[14px] font-medium text-ink">导出 · Markdown</div>
        <div className="mt-0.5 truncate text-[12.5px] text-mute">按分类导出笔记与文档原文</div>
      </div>
      <ChevronRight size={16} className="shrink-0 text-faint" strokeWidth={1.75} />
    </button>
  );
}

/** 任务清单 27：用量入口——近 7 日 chat / vision / embed / rerank 的 tokens 与调用次数。 */
function UsageRow({ onOpen }: { onOpen(): void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label="查看模型用量"
      className="flex w-full items-center gap-3.5 rounded-2xl bg-fog/[0.03] px-4 py-3.5 text-left transition-colors hover:bg-fog/[0.05]"
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-fog/[0.05]">
        <BarChart3 size={16} className="text-mute" strokeWidth={1.75} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[14px] font-medium text-ink">用量</div>
        <div className="mt-0.5 truncate text-[12.5px] text-mute">近 7 日 tokens 与调用次数</div>
      </div>
      <ChevronRight size={16} className="shrink-0 text-faint" strokeWidth={1.75} />
    </button>
  );
}

/** 条目 36：外观行副标题——当前生效主题；跟随系统时注明。 */
function themeSummary() {
  const theme = getResolvedTheme() === "dark" ? "深色" : "浅色";
  return getThemePref() === "system" ? `跟随系统 · 当前${theme}` : `当前${theme}`;
}

/** 条目 36：外观入口——浅色 / 深色 / 跟随系统，点进去三选一。 */
function AppearanceRow({ summary, onOpen }: { summary: string; onOpen(): void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label="配置外观主题"
      className="flex w-full items-center gap-3.5 rounded-2xl bg-fog/[0.03] px-4 py-3.5 text-left transition-colors hover:bg-fog/[0.05]"
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-fog/[0.05]">
        <Palette size={16} className="text-mute" strokeWidth={1.75} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[14px] font-medium text-ink">外观</div>
        <div className="mt-0.5 truncate text-[12.5px] text-mute">{summary}</div>
      </div>
      <ChevronRight size={16} className="shrink-0 text-faint" strokeWidth={1.75} />
    </button>
  );
}

/** 条目 23：DeepL 翻译入口——Key 只存本机 .env，点进去配置；hasKey 为 null 表示还没读到设置。 */
function TranslateRow({
  hasKey,
  down,
  onOpen,
}: {
  hasKey: boolean | null;
  down: boolean;
  onOpen(): void;
}) {
  const desc =
    hasKey === null
      ? down
        ? "后端未连接"
        : "正在读取配置…"
      : hasKey
        ? "已配置（DeepL）"
        : "未配置 · 配置后可翻译选中文本与回答";
  return (
    <button
      type="button"
      onClick={onOpen}
      title={desc}
      aria-label="配置 DeepL 翻译"
      className="flex w-full items-center gap-3.5 rounded-2xl bg-fog/[0.03] px-4 py-3.5 text-left transition-colors hover:bg-fog/[0.05]"
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-fog/[0.05]">
        <Languages size={16} className={cx(hasKey ? "text-brand" : "text-mute")} strokeWidth={1.75} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[14px] font-medium text-ink">翻译</div>
        <div className="mt-0.5 truncate text-[12.5px] text-mute">{desc}</div>
      </div>
      <ChevronRight size={16} className="shrink-0 text-faint" strokeWidth={1.75} />
      <span className={cx("h-2 w-2 shrink-0 rounded-full", DOT_CLASS[down ? "err" : hasKey ? "ok" : "off"])} />
    </button>
  );
}

export default function SettingsView({
  health,
  settings,
  skillsSummary,
  onEditRole,
  onEditData,
  onEditRerank,
  onEditWeb,
  onEditAgent,
  onEditSkills,
}: {
  health: Health | null;
  settings: SettingsOut | null;
  skillsSummary?: string;
  onEditRole(role: EditRole): void;
  onEditData(): void;
  onEditRerank(): void;
  onEditWeb(): void;
  onEditAgent(): void;
  onEditSkills(): void;
}) {
  const down = !health;
  const roles: EditRole[] = ["chat", "embed"];
  const [exportOpen, setExportOpen] = useState(false);
  const [offlineModelsOpen, setOfflineModelsOpen] = useState(false);
  const [usageOpen, setUsageOpen] = useState(false);
  const [appearanceOpen, setAppearanceOpen] = useState(false);
  // 条目 23：翻译行副标题的 has_key。settings prop 由 App 在进设置页时拉好（GET /api/settings 含 translate）；
  // 弹窗里保存成功后先本地覆盖，副标题立即跟上，不必等下一次刷新。
  const [deepLOpen, setDeepLOpen] = useState(false);
  const [translateKeyOverride, setTranslateKeyOverride] = useState<boolean | null>(null);
  const translateHasKey =
    translateKeyOverride ??
    (typeof settings?.translate?.has_key === "boolean" ? settings.translate.has_key : null);
  // 外观行副标题：当前生效主题；在弹窗里切换时实时跟上
  const [appearanceSummary, setAppearanceSummary] = useState(() => themeSummary());
  useEffect(() => {
    const sync = () => setAppearanceSummary(themeSummary());
    window.addEventListener(THEME_CHANGE_EVENT, sync);
    return () => window.removeEventListener(THEME_CHANGE_EVENT, sync);
  }, []);

  return (
    <div className="mx-auto h-full w-full max-w-[720px] overflow-auto px-6 py-8 scrollbar-thin">
      <div className="mb-6 flex items-center gap-3">
        <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-brand-soft">
          <Waves size={18} className="text-brand" strokeWidth={1.75} />
        </span>
        <div>
          <h1 className="text-[20px] font-semibold text-ink">设置</h1>
          <p className="text-[12px] text-faint">知澜 · AI 知识库桌面客户端</p>
        </div>
      </div>

      <div className="mb-2 flex items-center justify-between px-1 text-[11.5px] text-faint">
        <span>模型配置</span>
        <span>点进去可加多条供应商，随时切换</span>
      </div>
      <div className="space-y-2">
        {settings === null ? (
          <div className="rounded-2xl bg-fog/[0.03] px-4 py-6 text-center text-[12.5px] text-faint">
            {down ? "后端未连接，无法读取模型配置" : "正在读取模型配置…"}
          </div>
        ) : (
          roles.map((role) => <RoleRow key={role} role={role} settings={settings} down={down} onEditRole={onEditRole} />)
        )}
        <button
          type="button"
          onClick={() => setOfflineModelsOpen(true)}
          aria-label="导入离线文档解析模型"
          className="flex w-full items-center gap-3.5 rounded-2xl bg-fog/[0.03] px-4 py-3.5 text-left transition-colors hover:bg-fog/[0.05]"
        >
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-fog/[0.05] text-mute">
            <Download size={16} strokeWidth={1.75} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-[14px] font-medium text-ink">离线文档解析模型</div>
            <div className="mt-0.5 truncate text-[12.5px] text-mute">导入本机模型包，用于扫描件和图片文字识别</div>
          </div>
          <ChevronRight size={16} className="shrink-0 text-faint" strokeWidth={1.75} />
        </button>
      </div>

      <div className="mb-2 mt-6 flex items-center justify-between px-1 text-[11.5px] text-faint">
        <span>检索增强</span>
        <span>重排序 / 联网默认关闭，按需开启</span>
      </div>
      <div className="space-y-2">
        <RerankRow settings={settings} down={down} onEditRerank={onEditRerank} />
        <WebRow settings={settings} down={down} onEditWeb={onEditWeb} />
      </div>

      <div className="mb-2 mt-6 flex items-center justify-between px-1 text-[11.5px] text-faint">
        <span>翻译</span>
        <span>DeepL · 中英互译</span>
      </div>
      <div className="space-y-2">
        <TranslateRow hasKey={translateHasKey} down={down} onOpen={() => setDeepLOpen(true)} />
      </div>

      <div className="mb-2 mt-6 flex items-center justify-between px-1 text-[11.5px] text-faint">
        <span>Agent</span>
        <span>默认直答，对话页可按次覆盖</span>
      </div>
      <div className="space-y-2">
        <AgentRow settings={settings} down={down} onEditAgent={onEditAgent} />
      </div>

      <div className="mb-2 mt-6 flex items-center justify-between px-1 text-[11.5px] text-faint">
        <span>技能 / MCP</span>
        <span>对话里输入 / 选用技能</span>
      </div>
      <div className="space-y-2">
        <SkillsRow down={down} summary={skillsSummary || ""} onEditSkills={onEditSkills} />
      </div>

      <div className="mb-2 mt-6 flex items-center justify-between px-1 text-[11.5px] text-faint">
        <span>外观</span>
        <span>浅色 / 深色 / 跟随系统</span>
      </div>
      <div className="space-y-2">
        <AppearanceRow summary={appearanceSummary} onOpen={() => setAppearanceOpen(true)} />
      </div>

      <div className="mb-2 mt-6 flex items-center justify-between px-1 text-[11.5px] text-faint">
        <span>数据服务</span>
        <span>点进去可改存放位置</span>
      </div>
      <div className="space-y-2">
        <DataRow health={health} settings={settings} down={down} onEditData={onEditData} />
        <UsageRow onOpen={() => setUsageOpen(true)} />
        <ExportRow onExport={() => setExportOpen(true)} />
      </div>

      {appearanceOpen ? <AppearanceDialog onClose={() => setAppearanceOpen(false)} /> : null}
      {deepLOpen ? (
        <DeepLDialog
          hasKey={translateHasKey === true}
          onSaved={(hasKey) => setTranslateKeyOverride(hasKey)}
          onClose={() => setDeepLOpen(false)}
        />
      ) : null}
      {exportOpen ? <ExportDialog onClose={() => setExportOpen(false)} /> : null}
      {offlineModelsOpen ? <OfflineModelsDialog onClose={() => setOfflineModelsOpen(false)} /> : null}
      {usageOpen ? <UsageDialog onClose={() => setUsageOpen(false)} /> : null}
    </div>
  );
}
