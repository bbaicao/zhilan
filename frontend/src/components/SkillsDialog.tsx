import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  Check,
  ChevronLeft,
  ChevronRight,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  X,
  Zap,
} from "lucide-react";
import {
  getSkills,
  probeMcp,
  putSkills,
  rescanSkills,
  type McpTool,
  type SkillPack,
  type SkillsOut,
} from "../api";
import { cx } from "../utils";

/**
 * 条目 35（修订）：技能 / MCP 配置弹窗，分类次级菜单。
 * 一级页 = 分类入口（内置能力 / 本机 Skill / MCP，带条数与启用数）+ 本机目录；
 * 次级页管理该类下的条目；MCP 再下钻到单个 server 的工具页做工具级启停。
 * 保存仍是一次事务写 .env（PUT /api/skills）；builtin 不进提交列表（后端硬编码）。
 */

const inputCls =
  "h-9 w-full rounded-lg border border-line bg-fog/[0.04] px-3 text-[13.5px] text-ink outline-none transition-colors placeholder:text-faint focus:border-edge";

// 与后端 skills.is_dangerous_tool 同一套动词规则（仅用于前端即时角标，真相源在后端）
const DANGEROUS_RE =
  /delete|remove|drop|write|update|create|insert|exec|shell|command|run|rm|destroy|modify|send|post|put|patch|upload/i;

type Pane =
  | { kind: "root" }
  | { kind: "builtin" }
  | { kind: "local" }
  | { kind: "mcp" }
  | { kind: "server"; id: string };

function statusText(item: SkillPack) {
  if (item.status === "ok") return item.enabled ? "可用" : "已停用";
  if (item.status === "disabled") return "已停用";
  if (item.status === "planned") return "规划中";
  if (item.status === "invalid") return "配置有误";
  return item.status_note || item.status || "";
}

function Toggle({ on, disabled, onClick, label }: { on: boolean; disabled?: boolean; onClick(): void; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className={cx(
        "relative h-[22px] w-[38px] shrink-0 rounded-full transition-colors",
        on ? "bg-brand" : "bg-fog/[0.14]",
        disabled && "cursor-not-allowed opacity-40",
      )}
    >
      <span
        className={cx(
          "absolute h-4 w-4 rounded-full bg-canvas transition-all",
          on ? "left-[18px]" : "left-[3px]",
        )}
        style={{ top: 3 }}
      />
    </button>
  );
}

function BackBar({ onBack, title, desc }: { onBack(): void; title: string; desc: string }) {
  return (
    <div className="mb-3">
      <button
        type="button"
        onClick={onBack}
        className="-ml-1 mb-1 flex items-center gap-0.5 rounded-md px-1 py-0.5 text-[12px] text-mute hover:bg-fog/5 hover:text-ink"
      >
        <ChevronLeft size={14} strokeWidth={2} />
        返回
      </button>
      <div className="text-[13.5px] font-medium text-ink">{title}</div>
      <div className="mt-0.5 text-[11.5px] text-faint">{desc}</div>
    </div>
  );
}

function CategoryRow({
  title,
  desc,
  count,
  enabled,
  onClick,
}: {
  title: string;
  desc: string;
  count: number;
  enabled: number;
  onClick(): void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full items-center gap-3 rounded-xl border border-line bg-fog/[0.02] px-3 py-3 text-left transition-colors hover:bg-fog/[0.05]"
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="text-[13.5px] font-medium text-ink">{title}</span>
          <span className="text-[11.5px] text-faint">
            {count} 项 · 启用 {enabled}
          </span>
        </div>
        <div className="mt-0.5 text-[12px] text-mute">{desc}</div>
      </div>
      <ChevronRight size={16} strokeWidth={1.75} className="shrink-0 text-faint" />
    </button>
  );
}

export default function SkillsDialog({ onClose }: { onClose(): void }) {
  const [data, setData] = useState<SkillsOut | null>(null);
  const [pane, setPane] = useState<Pane>({ kind: "root" });
  const [draftDir, setDraftDir] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [probeBusy, setProbeBusy] = useState(false);
  const [newPack, setNewPack] = useState({
    name: "",
    description: "",
    transport: "stdio" as "stdio" | "http",
    command: "",
    url: "",
    tools: "",
  });

  const load = useCallback(async () => {
    setError("");
    try {
      const out = await getSkills();
      setData(out);
      setDraftDir(out.skill_dir);
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取技能列表失败");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const items = data?.items ?? [];
  const localItems = items.filter((item) => item.type === "local");
  const mcpItems = items.filter((item) => item.type === "mcp");
  const builtinItems = items.filter((item) => item.type === "builtin");

  function patchPack(id: string, patch: Partial<SkillPack>) {
    setData((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        items: prev.items.map((item) => (item.id === id ? { ...item, ...patch } : item)),
      };
    });
  }

  function patchTool(packId: string, toolName: string, patch: Partial<McpTool>) {
    setData((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        items: prev.items.map((item) =>
          item.id === packId
            ? { ...item, tools: (item.tools || []).map((tool) => (tool.name === toolName ? { ...tool, ...patch } : tool)) }
            : item,
        ),
      };
    });
  }

  function removeTool(packId: string, toolName: string) {
    setData((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        items: prev.items.map((item) =>
          item.id === packId
            ? { ...item, tools: (item.tools || []).filter((tool) => tool.name !== toolName) }
            : item,
        ),
      };
    });
  }

  function removePack(id: string) {
    if (editingId === id) {
      setFormOpen(false);
      setEditingId(null);
    }
    setData((prev) => {
      if (!prev) return prev;
      return { ...prev, items: prev.items.filter((item) => item.id !== id) };
    });
  }

  async function saveAll() {
    if (!data) return;
    setSaving(true);
    setError("");
    try {
      const packs = data.items
        .filter((item) => item.type !== "builtin")
        .map((item) =>
          item.type === "local"
            // 本机 skill 只需启停/名称；transport 等扫描字段不必回传（GET 里 transport 是空串）
            ? ({
                id: item.id,
                type: "local",
                name: item.name || "",
                description: item.description || "",
                enabled: item.enabled !== false,
              } as SkillPack)
            : {
                ...item,
                command: item.command || [],
                tools: (item.tools || []).map((tool) => ({ ...tool, enabled: tool.enabled !== false })),
              },
        );
      const out = await putSkills({
        packs: packs as SkillsOut["items"],
        skill_dir: draftDir.trim() || null,
      });
      setData(out);
      setNote("已保存");
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function rescan() {
    setScanning(true);
    try {
      const out = await rescanSkills();
      setData(out);
      setNote("已重新扫描本机 skill 目录");
    } catch (err) {
      setError(err instanceof Error ? err.message : "扫描失败");
    } finally {
      setScanning(false);
    }
  }

  async function doProbe() {
    const command = newPack.command.trim().split(/\s+/).filter(Boolean);
    const url = newPack.url.trim();
    setProbeBusy(true);
    setError("");
    try {
      const out = await probeMcp({
        transport: newPack.transport,
        command: newPack.transport === "stdio" ? command : [],
        url: newPack.transport === "http" ? url : "",
      });
      if (!out.ok) {
        setError(out.error || "连接失败");
        return;
      }
      setNote(`已列出 ${out.tools.length} 个工具；高危工具默认不会被自动执行。`);
      // 预填工具名，便于用户勾选
      const names = out.tools.map((t: McpTool) => t.name).join(", ");
      setNewPack((prev) => ({ ...prev, tools: names }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "连接失败");
    } finally {
      setProbeBusy(false);
    }
  }

  /** server 工具页里的连接探测：刷新工具清单，保留同名工具的启停状态，新增的默认启用。 */
  async function probeServer(pack: SkillPack) {
    setProbeBusy(true);
    setError("");
    try {
      const out = await probeMcp({
        transport: pack.transport === "http" ? "http" : "stdio",
        command: pack.transport === "http" ? [] : pack.command || [],
        url: pack.transport === "http" ? pack.url : "",
      });
      if (!out.ok) {
        setError(out.error || "连接失败");
        return;
      }
      const known = new Set((pack.tools || []).map((tool) => tool.name));
      const fresh = out.tools.filter((tool) => !known.has(tool.name)).length;
      setData((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          items: prev.items.map((item) => {
            if (item.id !== pack.id) return item;
            const old = new Map((item.tools || []).map((tool) => [tool.name, tool]));
            const merged = out.tools.map((tool) => {
              const prevTool = old.get(tool.name);
              return prevTool ? { ...tool, enabled: prevTool.enabled !== false } : { ...tool, enabled: true };
            });
            return { ...item, tools: merged };
          }),
        };
      });
      setNote(`已列出 ${out.tools.length} 个工具${fresh ? `，新增 ${fresh} 个` : ""}；已有工具的启停状态已保留，点「保存」写入。`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "连接失败");
    } finally {
      setProbeBusy(false);
    }
  }

  function openAdd() {
    setEditingId(null);
    setNewPack({ name: "", description: "", transport: "stdio", command: "", url: "", tools: "" });
    setFormOpen(true);
  }

  function openEdit(pack: SkillPack) {
    setEditingId(pack.id);
    setNewPack({
      name: pack.name,
      description: pack.description,
      transport: pack.transport === "http" ? "http" : "stdio",
      command: (pack.command || []).join(" "),
      url: pack.url || "",
      tools: "",
    });
    setFormOpen(true);
  }

  function submitPack() {
    if (!data) return;
    const name = newPack.name.trim() || "未命名 MCP";
    const command = newPack.command.trim().split(/\s+/).filter(Boolean);
    const url = newPack.url.trim();
    if (editingId) {
      // 编辑只改 server 本身；工具清单在工具页里管理（表单里那个逗号输入框不参与，避免误清工具状态）
      setData((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          items: prev.items.map((item) =>
            item.id === editingId
              ? {
                  ...item,
                  name,
                  description: newPack.description.trim() || "MCP server",
                  transport: newPack.transport,
                  command,
                  url,
                  available: newPack.transport === "stdio" ? command.length > 0 : Boolean(url),
                }
              : item,
          ),
        };
      });
      setNote("已修改，点「保存」写入 .env");
    } else {
      const id = `mcp:${name.toLowerCase().replace(/[^a-z0-9_-]+/g, "-") || "server"}-${Date.now().toString(36).slice(-4)}`;
      const tools = newPack.tools
        .split(/[,，\s]+/)
        .map((t) => t.trim())
        .filter(Boolean)
        .map((toolName) => ({
          name: toolName,
          description: "",
          dangerous: DANGEROUS_RE.test(toolName),
          enabled: true,
        }));
      const pack: SkillPack = {
        id,
        slug: name.toLowerCase().replace(/[^a-z0-9_-]+/g, "-") || "server",
        name,
        type: "mcp",
        description: newPack.description.trim() || "MCP server",
        enabled: true,
        locked: false,
        available: newPack.transport === "stdio" ? command.length > 0 : Boolean(url),
        status: "ok",
        status_note: "",
        tools,
        path: "",
        command,
        url,
        transport: newPack.transport,
      };
      setData({ ...data, items: [...data.items, pack] });
      setNote("已加入列表，点「保存」写入 .env");
    }
    setFormOpen(false);
    setEditingId(null);
    setNewPack({ name: "", description: "", transport: "stdio", command: "", url: "", tools: "" });
  }

  if (!data) {
    return (
      <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={onClose}>
        <div className="mx-auto mt-16 w-full max-w-2xl px-4 pb-12" onClick={(e) => e.stopPropagation()}>
          <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
            <div className="flex items-center gap-2 text-[13px] text-mute">
              <Loader2 size={14} className="animate-spin" /> 正在读取技能…
            </div>
          </div>
        </div>
      </div>
    );
  }

  const serverPack = pane.kind === "server" ? items.find((item) => item.id === pane.id) : undefined;
  const paneMeta: Record<Pane["kind"], { title: string; desc: string }> = {
    root: { title: "技能 / MCP", desc: "对话里输入 / 可选用技能；本机 skill 只读说明注入提示词，不会自动执行代码" },
    builtin: { title: "内置能力", desc: "随应用内置的固定能力；启停由各自的设置管理，这里只看不删" },
    local: { title: "本机 Skill", desc: `扫描自本机 skill 目录；停用的技能不进对话 / 菜单（共 ${localItems.length} 项）` },
    mcp: { title: "MCP", desc: `登记 MCP server；点进单个 server 可管理工具级启停（共 ${mcpItems.length} 个）` },
    server: { title: "MCP 工具", desc: "工具级启停：停用的工具不进提示词目录，也不进 Agent 工具清单" },
  };

  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={onClose}>
      <div className="mx-auto mt-10 w-full max-w-2xl px-4 pb-12" onClick={(e) => e.stopPropagation()}>
        <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-semibold text-ink">{paneMeta[pane.kind].title}</h2>
              <p className="mt-0.5 text-[12px] text-faint">{paneMeta[pane.kind].desc}</p>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="rounded-md p-1.5 text-mute hover:bg-fog/5 hover:text-ink"
            >
              <X size={16} strokeWidth={1.75} />
            </button>
          </div>

          {/* ---------- 一级页：分类入口 + 本机目录 ---------- */}
          {pane.kind === "root" ? (
            <>
              <div className="space-y-2">
                <CategoryRow
                  title="内置能力"
                  desc="知识库问答、联网检索（联网随设置页开关）"
                  count={builtinItems.length}
                  enabled={builtinItems.filter((item) => item.enabled).length}
                  onClick={() => setPane({ kind: "builtin" })}
                />
                <CategoryRow
                  title="本机 Skill"
                  desc="SKILL.md / skill.json 只读注入提示词；可启停、可重新扫描"
                  count={localItems.length}
                  enabled={localItems.filter((item) => item.enabled).length}
                  onClick={() => setPane({ kind: "local" })}
                />
                <CategoryRow
                  title="MCP"
                  desc="server 增删改与连接探测；工具级启停、高危角标"
                  count={mcpItems.length}
                  enabled={mcpItems.filter((item) => item.enabled).length}
                  onClick={() => setPane({ kind: "mcp" })}
                />
              </div>

              <div className="mt-4 mb-1 space-y-2 rounded-xl border border-line bg-fog/[0.02] p-3">
                <div className="flex items-center gap-2">
                  <label className="w-28 shrink-0 text-[12.5px] text-mute">本机 skill 目录</label>
                  <input
                    className={inputCls}
                    value={draftDir}
                    onChange={(e) => setDraftDir(e.target.value)}
                    placeholder="skills（相对仓库根，或绝对路径）"
                  />
                </div>
                <div className="flex items-center gap-2 pl-28 text-[11.5px] text-faint">
                  <span className={cx("h-1.5 w-1.5 shrink-0 rounded-full", data.skill_dir_exists ? "bg-ok" : "bg-warn")} />
                  <span className="min-w-0 flex-1 truncate">
                    {data.skill_dir_exists ? data.skill_dir : `目录不存在：${data.skill_dir}（可手填或创建）`}
                  </span>
                  <button
                    type="button"
                    onClick={() => void rescan()}
                    disabled={scanning}
                    className="flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-mute hover:bg-fog/5 hover:text-ink"
                  >
                    {scanning ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                    重新扫描
                  </button>
                </div>
              </div>
            </>
          ) : null}

          {/* ---------- 内置能力 ---------- */}
          {pane.kind === "builtin" ? (
            <div className="max-h-[420px] space-y-2 overflow-auto scrollbar-thin">
              <BackBar
                onBack={() => setPane({ kind: "root" })}
                title="内置能力"
                desc="locked 项不可在技能列表里开关：knowledge 是默认问答路径，web 随「联网检索」设置"
              />
              {builtinItems.map((item) => (
                <div key={item.id} className="rounded-xl border border-line bg-fog/[0.02] px-3 py-2.5">
                  <div className="flex items-start gap-2.5">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="text-[13.5px] font-medium text-ink">{item.name}</span>
                        <code className="rounded bg-fog/[0.06] px-1 py-0.5 text-[10.5px] text-mute">/{item.slug}</code>
                        <span
                          className={cx(
                            "text-[11px]",
                            item.status === "ok" && item.enabled ? "text-ok" : item.status === "planned" ? "text-faint" : "text-warn",
                          )}
                        >
                          {statusText(item)}
                        </span>
                      </div>
                      <div className="mt-0.5 text-[12px] text-mute">{item.description}</div>
                      {item.status_note ? <div className="mt-0.5 text-[11.5px] text-warn">{item.status_note}</div> : null}
                      {item.tools.length ? (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {item.tools.map((tool) => (
                            <span
                              key={tool.name}
                              title={tool.dangerous ? "高危工具：默认不自动执行，需用户确认" : tool.description}
                              className={cx(
                                "rounded-md px-1.5 py-0.5 text-[10.5px]",
                                tool.dangerous ? "bg-rose/15 text-rose" : "bg-fog/[0.06] text-faint",
                              )}
                            >
                              {tool.name}
                              {tool.dangerous ? " !" : ""}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </div>
                    <Toggle
                      on={item.enabled}
                      disabled={item.locked || item.status === "planned"}
                      onClick={() => patchPack(item.id, { enabled: !item.enabled })}
                      label={`启用 ${item.name}`}
                    />
                  </div>
                </div>
              ))}
            </div>
          ) : null}

          {/* ---------- 本机 Skill ---------- */}
          {pane.kind === "local" ? (
            <div className="max-h-[420px] space-y-2 overflow-auto scrollbar-thin">
              <BackBar
                onBack={() => setPane({ kind: "root" })}
                title="本机 Skill"
                desc="子目录含 SKILL.md 或 skill.json 即算一个 skill；停用后对话 / 菜单不再出现"
              />
              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={() => void rescan()}
                  disabled={scanning}
                  className="flex items-center gap-1 rounded-lg px-2 py-1 text-[12.5px] text-mute hover:bg-fog/5 hover:text-ink"
                >
                  {scanning ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                  重新扫描
                </button>
              </div>
              {localItems.length === 0 ? (
                <div className="rounded-xl border border-dashed border-line px-3 py-6 text-center text-[12px] text-faint">
                  目录里没有发现 skill（子目录需含 SKILL.md 或 skill.json）
                </div>
              ) : null}
              {localItems.map((item) => (
                <div
                  key={item.id}
                  className={cx("rounded-xl border border-line bg-fog/[0.02] px-3 py-2.5", !item.available && "opacity-70")}
                >
                  <div className="flex items-start gap-2.5">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="text-[13.5px] font-medium text-ink">{item.name}</span>
                        <code className="rounded bg-fog/[0.06] px-1 py-0.5 text-[10.5px] text-mute">/{item.slug}</code>
                        <span
                          className={cx("text-[11px]", item.status === "ok" && item.enabled ? "text-ok" : "text-warn")}
                        >
                          {statusText(item)}
                        </span>
                      </div>
                      <div className="mt-0.5 text-[12px] text-mute">{item.description}</div>
                      {item.status_note ? <div className="mt-0.5 text-[11.5px] text-warn">{item.status_note}</div> : null}
                      {item.path ? <div className="mt-1 truncate text-[11px] text-faint">{item.path}</div> : null}
                    </div>
                    <Toggle
                      on={item.enabled}
                      onClick={() => patchPack(item.id, { enabled: !item.enabled })}
                      label={`启用 ${item.name}`}
                    />
                  </div>
                </div>
              ))}
            </div>
          ) : null}

          {/* ---------- MCP server 列表 ---------- */}
          {pane.kind === "mcp" ? (
            <div className="max-h-[420px] space-y-2 overflow-auto scrollbar-thin">
              <BackBar
                onBack={() => setPane({ kind: "root" })}
                title="MCP"
                desc="stdio / http server；点进单个 server 管理工具级启停"
              />
              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={openAdd}
                  className="flex items-center gap-1 rounded-lg px-2 py-1 text-[12.5px] text-brand hover:bg-fog/5"
                >
                  <Plus size={14} strokeWidth={2} />
                  添加 MCP
                </button>
              </div>
              {formOpen ? (
                <div className="mb-3 space-y-2 rounded-xl border border-line bg-fog/[0.02] p-3">
                  <div className="text-[12px] text-mute">{editingId ? "编辑 MCP server（工具在工具页管理）" : "添加 MCP server"}</div>
                  <input
                    className={inputCls}
                    placeholder="名称"
                    value={newPack.name}
                    onChange={(e) => setNewPack({ ...newPack, name: e.target.value })}
                  />
                  <input
                    className={inputCls}
                    placeholder="一句话描述"
                    value={newPack.description}
                    onChange={(e) => setNewPack({ ...newPack, description: e.target.value })}
                  />
                  <div className="flex gap-2">
                    {(["stdio", "http"] as const).map((t) => (
                      <button
                        key={t}
                        type="button"
                        onClick={() => setNewPack({ ...newPack, transport: t })}
                        className={cx(
                          "rounded-md px-2.5 py-1 text-[12px]",
                          newPack.transport === t ? "bg-brand/15 text-brand" : "bg-fog/[0.06] text-body",
                        )}
                      >
                        {t}
                      </button>
                    ))}
                  </div>
                  {newPack.transport === "stdio" ? (
                    <input
                      className={inputCls}
                      placeholder="启动命令，如 npx -y @modelcontextprotocol/server-filesystem"
                      value={newPack.command}
                      onChange={(e) => setNewPack({ ...newPack, command: e.target.value })}
                    />
                  ) : (
                    <input
                      className={inputCls}
                      placeholder="接口地址 http(s)://…"
                      value={newPack.url}
                      onChange={(e) => setNewPack({ ...newPack, url: e.target.value })}
                    />
                  )}
                  {!editingId ? (
                    <input
                      className={inputCls}
                      placeholder="工具名（逗号分隔；可先点「连接探测」自动填入）"
                      value={newPack.tools}
                      onChange={(e) => setNewPack({ ...newPack, tools: e.target.value })}
                    />
                  ) : null}
                  <div className="flex items-center gap-2 pt-1">
                    <button
                      type="button"
                      onClick={() => void doProbe()}
                      disabled={probeBusy}
                      className="flex items-center gap-1.5 rounded-lg bg-fog/[0.06] px-3 py-1.5 text-[12.5px] text-ink hover:bg-fog/[0.1]"
                    >
                      {probeBusy ? <Loader2 size={13} className="animate-spin" /> : <Zap size={13} />}
                      连接探测
                    </button>
                    <button
                      type="button"
                      onClick={submitPack}
                      className="rounded-lg bg-brand px-3 py-1.5 text-[12.5px] text-canvas hover:bg-send-hover"
                    >
                      {editingId ? "应用修改" : "加入列表"}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setFormOpen(false);
                        setEditingId(null);
                      }}
                      className="rounded-lg px-2 py-1.5 text-[12.5px] text-mute hover:bg-fog/5"
                    >
                      取消
                    </button>
                  </div>
                </div>
              ) : null}
              {mcpItems.length === 0 && !formOpen ? (
                <div className="rounded-xl border border-dashed border-line px-3 py-6 text-center text-[12px] text-faint">
                  还没有登记 MCP server；点「添加 MCP」开始
                </div>
              ) : null}
              {mcpItems.map((item) => {
                const tools = item.tools || [];
                const enabledTools = tools.filter((tool) => tool.enabled !== false).length;
                const dangerousTools = tools.filter((tool) => tool.dangerous).length;
                return (
                  <div
                    key={item.id}
                    className={cx("rounded-xl border border-line bg-fog/[0.02] px-3 py-2.5", !item.available && "opacity-70")}
                  >
                    <div className="flex items-start gap-2.5">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <button
                            type="button"
                            onClick={() => setPane({ kind: "server", id: item.id })}
                            className="text-[13.5px] font-medium text-ink hover:text-brand"
                          >
                            {item.name}
                          </button>
                          <span
                            className={cx("text-[11px]", item.status === "ok" && item.enabled ? "text-ok" : "text-warn")}
                          >
                            {statusText(item)}
                          </span>
                        </div>
                        <div className="mt-0.5 text-[12px] text-mute">{item.description}</div>
                        <div className="mt-0.5 text-[11px] text-faint">
                          {item.transport === "http" ? item.url : (item.command || []).join(" ")}
                          {" · "}
                          工具 {enabledTools}/{tools.length} 已启用
                          {dangerousTools ? ` · 高危 ${dangerousTools}` : ""}
                        </div>
                        {item.status_note ? <div className="mt-0.5 text-[11.5px] text-warn">{item.status_note}</div> : null}
                      </div>
                      <div className="flex shrink-0 items-center gap-1">
                        <Toggle on={item.enabled} onClick={() => patchPack(item.id, { enabled: !item.enabled })} label={`启用 ${item.name}`} />
                        <button
                          type="button"
                          onClick={() => openEdit(item)}
                          aria-label={`编辑 ${item.name}`}
                          className="rounded-md p-1.5 text-mute hover:bg-fog/5 hover:text-ink"
                        >
                          <Pencil size={14} strokeWidth={1.75} />
                        </button>
                        <button
                          type="button"
                          onClick={() => removePack(item.id)}
                          aria-label={`删除 ${item.name}`}
                          className="rounded-md p-1.5 text-mute hover:bg-rose/10 hover:text-rose"
                        >
                          <Trash2 size={14} strokeWidth={1.75} />
                        </button>
                        <button
                          type="button"
                          onClick={() => setPane({ kind: "server", id: item.id })}
                          aria-label={`管理 ${item.name} 的工具`}
                          className="rounded-md p-1.5 text-mute hover:bg-fog/5 hover:text-ink"
                        >
                          <ChevronRight size={14} strokeWidth={1.75} />
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : null}

          {/* ---------- 单个 server 的工具页 ---------- */}
          {pane.kind === "server" ? (
            <div className="max-h-[420px] space-y-2 overflow-auto scrollbar-thin">
              <BackBar onBack={() => setPane({ kind: "mcp" })} title={serverPack ? serverPack.name : "server 不存在"} desc={paneMeta.server.desc} />
              {serverPack ? (
                <>
                  <div className="flex items-center gap-2 rounded-xl border border-line bg-fog/[0.02] px-3 py-2 text-[11.5px] text-faint">
                    <span className="min-w-0 flex-1 truncate">
                      {serverPack.transport === "http" ? serverPack.url : (serverPack.command || []).join(" ")}
                    </span>
                    <button
                      type="button"
                      onClick={() => void probeServer(serverPack)}
                      disabled={probeBusy}
                      className="flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-mute hover:bg-fog/5 hover:text-ink"
                    >
                      {probeBusy ? <Loader2 size={12} className="animate-spin" /> : <Zap size={12} />}
                      连接探测
                    </button>
                  </div>
                  {(serverPack.tools || []).length === 0 ? (
                    <div className="rounded-xl border border-dashed border-line px-3 py-6 text-center text-[12px] text-faint">
                      还没有工具清单；点「连接探测」自动列出，或在表单里手填工具名
                    </div>
                  ) : null}
                  {(serverPack.tools || []).map((tool) => (
                    <div
                      key={tool.name}
                      className={cx(
                        "rounded-xl border border-line bg-fog/[0.02] px-3 py-2.5",
                        tool.enabled === false && "opacity-60",
                      )}
                    >
                      <div className="flex items-start gap-2.5">
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="text-[13px] font-medium text-ink">{tool.name}</span>
                            {tool.dangerous ? (
                              <span
                                title="高危工具：无论开关与否都不会被 Agent 自动执行"
                                className="rounded-md bg-rose/15 px-1.5 py-0.5 text-[10.5px] text-rose"
                              >
                                高危 !
                              </span>
                            ) : null}
                            <span className={cx("text-[11px]", tool.enabled !== false ? "text-ok" : "text-warn")}>
                              {tool.enabled !== false ? "已启用" : "已停用"}
                            </span>
                          </div>
                          {tool.description ? <div className="mt-0.5 text-[12px] text-mute">{tool.description}</div> : null}
                        </div>
                        <div className="flex shrink-0 items-center gap-1">
                          <Toggle
                            on={tool.enabled !== false}
                            onClick={() => patchTool(serverPack.id, tool.name, { enabled: tool.enabled === false })}
                            label={`启用工具 ${tool.name}`}
                          />
                          <button
                            type="button"
                            onClick={() => removeTool(serverPack.id, tool.name)}
                            aria-label={`移除工具 ${tool.name}`}
                            className="rounded-md p-1.5 text-mute hover:bg-rose/10 hover:text-rose"
                          >
                            <Trash2 size={13} strokeWidth={1.75} />
                          </button>
                        </div>
                      </div>
                    </div>
                  ))}
                  <div className="px-1 text-[11px] text-faint">
                    停用的工具不进对话提示词目录，也不进 Agent 工具清单；高危工具无论开关都不会被自动执行。
                  </div>
                </>
              ) : (
                <div className="rounded-xl border border-dashed border-line px-3 py-6 text-center text-[12px] text-faint">
                  该 server 已被删除；点「返回」回到 MCP 列表
                </div>
              )}
            </div>
          ) : null}

          {(error || note) && (
            <div
              className={cx(
                "mt-3 flex items-start gap-2 rounded-lg px-3 py-2 text-[12.5px] leading-5",
                error ? "bg-rose/10 text-rose" : "bg-fog/[0.04] text-mute",
              )}
            >
              {error ? <AlertCircle size={14} className="mt-0.5 shrink-0" /> : <Check size={14} className="mt-0.5 shrink-0" />}
              <span className="min-w-0 flex-1">{error || note}</span>
            </div>
          )}

          <div className="mt-4 flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg px-3 py-1.5 text-[13px] text-mute hover:bg-fog/5 hover:text-ink"
            >
              关闭
            </button>
            <button
              type="button"
              onClick={() => void saveAll()}
              disabled={saving}
              className="flex items-center gap-1.5 rounded-lg bg-brand px-3.5 py-1.5 text-[13px] text-canvas transition-colors hover:bg-send-hover disabled:opacity-50"
            >
              {saving ? <Loader2 size={13} className="animate-spin" /> : null}
              保存
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
