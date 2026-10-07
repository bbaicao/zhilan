import { useState } from "react";
import { AlertCircle, ArrowLeft, Check, Download, Loader2, Pencil, Plus, Trash2, X } from "lucide-react";
import {
  ApiError,
  ROLE_LABEL,
  probeModels,
  serviceInScope,
  type ModelRole,
  type RoleBinding,
  type ServiceIn,
  type SettingsIn,
  type SettingsOut,
} from "../api";
import { cx, labelProvider } from "../utils";

/** 设置页只开放这两个角色的配置入口；视觉模型并入对话配置。 */
export type EditRole = "chat" | "embed";

const ROLE_TITLE: Record<EditRole, string> = {
  chat: "对话模型配置",
  embed: "向量模型配置",
};

const ROLE_SUBTITLE: Record<EditRole, string> = {
  chat: "这里的供应商只在对话模型里出现；对话与视觉共用一条。",
  embed: "这里的供应商只在向量模型里出现；换一条随时切。",
};

/** 本对话框负责的角色绑定：对话这条顺带管视觉。 */
const MANAGED: Record<EditRole, ModelRole[]> = {
  chat: ["chat", "vision"],
  embed: ["embed"],
};

const ALL_ROLES: ModelRole[] = ["chat", "vision", "embed"];

/** 供应商归属：在哪个弹窗里配的，就只在哪个弹窗里出现（不进界面、不用用户选）。 */
const SCOPE: Record<EditRole, ModelRole[]> = {
  chat: ["chat", "vision"],
  embed: ["embed"],
};

const inputCls =
  "h-9 w-full rounded-lg border border-line bg-fog/[0.04] px-3 text-[13.5px] text-ink outline-none transition-colors placeholder:text-faint focus:border-edge";

/** chip 上的短名字：对话 / 视觉 / 向量 */
function shortLabel(role: ModelRole) {
  return ROLE_LABEL[role].replace("模型", "");
}

/** 回显里的角色名收窄成合法角色（后端已归一，这里只防旧数据 / 手改 .env）。 */
function isRole(value: string): value is ModelRole {
  return (ALL_ROLES as string[]).includes(value);
}

/** 本地草稿：api_key 为空表示「不改动已存的那把」。 */
type Draft = {
  id: string;
  name: string;
  provider: string;
  base_url: string;
  /** 勾选的型号：存进 .env，并出现在对应页面的模型菜单里 */
  models: string[];
  /** 拉取到 + 手填的全部候选，只在本次编辑里有效 */
  pool: string[];
  for_roles: ModelRole[];
  api_key: string;
  has_key: boolean;
  isNew: boolean;
};

/** 供应商归属：老数据没写归属时按「对话」算（与后端默认一致）。 */
function toDraft(service: SettingsOut["services"][number]): Draft {
  return {
    id: service.id,
    name: service.name,
    provider: service.provider,
    base_url: service.base_url,
    models: [...service.models],
    pool: [...service.models],
    for_roles: [...service.for_roles],
    api_key: "",
    has_key: service.has_key,
    isNew: false,
  };
}

/** 条目 46：型号用途标记 {serviceId: {型号: [角色...]}}。可多标、可两标、可都不标，与角色绑定相互独立。 */
type Marks = Record<string, Record<string, ModelRole[]>>;

/** 每条服务的「角色 → 默认型号」选择（绑定从已标型号里选，标记 ≠ 绑定）。 */
type Defaults = Record<string, Partial<Record<ModelRole, string>>>;

function toPayload(draft: Draft, marks: Marks): ServiceIn {
  const body: ServiceIn = {
    id: draft.id,
    name: draft.name,
    provider: draft.provider,
    base_url: draft.base_url,
    models: draft.models,
    for_roles: draft.for_roles,
  };
  // 标记只随勾选的型号走：挂在未勾选型号上的标记没有意义（后端也会静默丢弃）
  const own = marks[draft.id];
  const model_roles: Record<string, string[]> = {};
  if (own) {
    for (const model of draft.models) {
      const roles = own[model];
      if (roles && roles.length > 0) model_roles[model] = [...roles];
    }
  }
  body.model_roles = model_roles;
  if (draft.api_key) body.api_key = draft.api_key;
  return body;
}

/** 新供应商的 id 在本地生成，这样同一次保存里角色就能引用到它。 */
function nextServiceId(provider: string, taken: string[]) {
  const slug = provider.replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "custom";
  const base = `svc_${slug}`;
  if (!taken.includes(base)) return base;
  let index = 2;
  while (taken.includes(`${base}_${index}`)) index += 1;
  return `${base}_${index}`;
}

function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-[12px] text-mute">{label}</span>
      {children}
      {hint ? <span className="mt-1 block text-[11px] leading-4 text-faint">{hint}</span> : null}
    </label>
  );
}

export default function SettingsDialog({
  role,
  settings,
  saving,
  onSave,
  onClose,
}: {
  role: EditRole;
  settings: SettingsOut;
  saving: boolean;
  onSave(payload: SettingsIn): Promise<boolean>;
  onClose(): void;
}) {
  const managed = MANAGED[role];
  const primary = managed[0];
  const presetKeys = Object.keys(settings.providers);

  const [services, setServices] = useState<Draft[]>(() => settings.services.map(toDraft));
  const visible = services.filter((item) => serviceInScope(item, managed));
  const [selectedId, setSelectedId] = useState(() => {
    const bound = settings.roles[role]?.service_id ?? "";
    if (bound && visible.some((item) => item.id === bound)) return bound;
    return visible[0]?.id ?? "";
  });

  /** 条目 46：每条供应商的「型号 → 用途标记」；chip 是独立 toggle，不再互斥/转移。
   *
   * 初始化 = service.model_roles；旧配置没有任何标记时，给本弹窗管到的服务
   * 按 settings.roles 绑定派生首屏标记（升级后不至于一片空白；标记 ≠ 绑定）。
   */
  const [marks, setMarks] = useState<Marks>(() => {
    const init: Marks = {};
    for (const service of settings.services) {
      const stored = service.model_roles ?? {};
      const own: Record<string, ModelRole[]> = {};
      for (const model of service.models) {
        const tagged = (stored[model] ?? []).filter(isRole);
        if (tagged.length > 0) own[model] = tagged;
      }
      init[service.id] = own;
    }
    for (const service of settings.services) {
      if (!serviceInScope(service, managed)) continue;
      if (Object.keys(init[service.id]).length > 0) continue;
      const own: Record<string, ModelRole[]> = {};
      for (const target of managed) {
        const bound = settings.roles[target];
        if (bound && bound.service_id === service.id) {
          own[bound.model] = [...(own[bound.model] ?? []), target];
        }
      }
      init[service.id] = own;
    }
    return init;
  });

  /** 每条服务「各角色默认型号」的选择；初始 = 当前绑定指向本服务时的绑定型号。 */
  const [defaults, setDefaults] = useState<Defaults>(() => {
    const init: Defaults = {};
    for (const item of services) {
      const own: Partial<Record<ModelRole, string>> = {};
      for (const target of managed) {
        const bound = settings.roles[target];
        if (bound && bound.service_id === item.id) own[target] = bound.model;
      }
      init[item.id] = own;
    }
    return init;
  });

  /** 非空即处于「供应商表单」视图 */
  const [form, setForm] = useState<Draft | null>(null);
  const [modelInput, setModelInput] = useState("");
  const [probing, setProbing] = useState(false);
  const [formError, setFormError] = useState("");
  const [formNote, setFormNote] = useState("");
  const [saveError, setSaveError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState("");

  const selected = visible.find((item) => item.id === selectedId) ?? visible[0] ?? null;

  /** 该服务里标了某角色的已勾选型号（保持型号列表顺序）。 */
  function markedModels(service: Draft, target: ModelRole): string[] {
    const own = marks[service.id];
    if (!own) return [];
    return service.models.filter((model) => own[model]?.includes(target));
  }

  /** 默认选择器的选项：标了该角色的已勾选型号 ∪ 当前绑定型号（绑定指向本服务时，即使未标也保留，防绑定悄悄失效）。 */
  function defaultOptions(service: Draft, target: ModelRole): string[] {
    const options = markedModels(service, target);
    const bound = settings.roles[target];
    if (bound && bound.service_id === service.id && bound.model && !options.includes(bound.model)) {
      return [...options, bound.model];
    }
    return options;
  }

  function setDefault(serviceId: string, target: ModelRole, model: string) {
    setDefaults((prev) => ({ ...prev, [serviceId]: { ...(prev[serviceId] ?? {}), [target]: model } }));
  }

  function selectService(id: string) {
    setSelectedId(id);
    setConfirmDelete("");
    setSaveError("");
  }

  function openForm(draft: Draft) {
    setForm(draft);
    // 新建的服务还没有默认选择记录，补一条空的（老服务在挂载时就有了）
    setDefaults((prev) => (prev[draft.id] ? prev : { ...prev, [draft.id]: {} }));
    setModelInput("");
    setFormError("");
    setFormNote("");
  }

  function openNewForm() {
    openForm({
      id: nextServiceId("", services.map((item) => item.id)),
      name: "",
      provider: "",
      base_url: "",
      models: [],
      pool: [],
      for_roles: [...SCOPE[role]],
      api_key: "",
      has_key: false,
      isNew: true,
    });
  }

  function onProviderChange(next: string) {
    if (!form) return;
    const preset = settings.providers[next];
    setForm({ ...form, provider: next, base_url: preset?.base_url || form.base_url });
  }

  async function fetchModels() {
    if (!form) return;
    if (!form.provider) {
      setFormError("请先选择模型厂商");
      return;
    }
    setProbing(true);
    setFormError("");
    setFormNote("");
    try {
      const result = await probeModels({
        provider: form.provider,
        base_url: form.base_url.trim(),
        service_id: form.isNew ? undefined : form.id,
        api_key: form.api_key.trim() || undefined,
      });
      const pool = [...result.models];
      for (const model of form.models) if (!pool.includes(model)) pool.push(model);
      setForm((prev) => (prev ? { ...prev, pool } : prev));
      setFormNote(`拉到 ${result.models.length} 个模型，勾选要用的再保存`);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "拉取模型列表失败");
    } finally {
      setProbing(false);
    }
  }

  function addModel() {
    const model = modelInput.trim();
    if (!form || !model) return;
    const pool = form.pool.includes(model) ? form.pool : [...form.pool, model];
    const models = form.models.includes(model) ? form.models : [...form.models, model];
    setForm({ ...form, pool, models });
    setModelInput("");
  }

  function setModelChecked(model: string, checked: boolean) {
    if (!form) return;
    const id = form.id;
    setForm({ ...form, models: checked ? [...form.models, model] : form.models.filter((item) => item !== model) });
    if (checked) return;
    // 取消勾选的同时把它的标记清掉；被选中的默认落到该角色剩余已标的第一个，没有就清空
    setMarks((prev) => {
      const own = { ...(prev[id] ?? {}) };
      if (!own[model]) return prev;
      delete own[model];
      return { ...prev, [id]: own };
    });
    setDefaults((prev) => {
      const own = { ...(prev[id] ?? {}) };
      let touched = false;
      for (const target of managed) {
        if (own[target] !== model) continue;
        const rest = form.models.filter((item) => item !== model && (marks[id]?.[item] ?? []).includes(target));
        if (rest[0]) own[target] = rest[0];
        else delete own[target];
        touched = true;
      }
      return touched ? { ...prev, [id]: own } : prev;
    });
  }

  /** 全选 / 清空：与单条取消勾选同一口径，同步清掉不再勾选的型号的标记与默认。 */
  function setFormModels(models: string[]) {
    if (!form) return;
    const id = form.id;
    const removed = form.models.filter((model) => !models.includes(model));
    setForm({ ...form, models });
    if (removed.length === 0) return;
    setMarks((prev) => {
      const own = { ...(prev[id] ?? {}) };
      let touched = false;
      for (const model of removed) {
        if (own[model]) {
          delete own[model];
          touched = true;
        }
      }
      return touched ? { ...prev, [id]: own } : prev;
    });
    setDefaults((prev) => {
      const own = { ...(prev[id] ?? {}) };
      let touched = false;
      for (const target of managed) {
        if (own[target] === undefined || !removed.includes(own[target])) continue;
        const rest = models.filter((model) => (marks[id]?.[model] ?? []).includes(target));
        if (rest[0]) own[target] = rest[0];
        else delete own[target];
        touched = true;
      }
      return touched ? { ...prev, [id]: own } : prev;
    });
  }

  /** 点 chip 切换用途标记：同型号可同时标对话 + 视觉，多型号可同标，点已标的取消（条目 46）。 */
  function toggleMark(target: ModelRole, model: string) {
    if (!form) return;
    const id = form.id;
    const on = marks[id]?.[model]?.includes(target) ?? false;
    setMarks((prev) => {
      const own = { ...(prev[id] ?? {}) };
      const roles = new Set(own[model] ?? []);
      if (on) roles.delete(target);
      else roles.add(target);
      if (roles.size > 0) own[model] = [...roles];
      else delete own[model];
      return { ...prev, [id]: own };
    });
    if (!on) return;
    // 取消的是当前默认 → 落到该角色剩余已标型号的第一个，没有就清空
    setDefaults((prev) => {
      const own = { ...(prev[id] ?? {}) };
      if (own[target] !== model) return prev;
      const rest = form.models.filter((item) => item !== model && (marks[id]?.[item] ?? []).includes(target));
      if (rest[0]) own[target] = rest[0];
      else delete own[target];
      return { ...prev, [id]: own };
    });
  }

  function commitForm() {
    if (!form) return;
    const provider = form.provider.trim();
    if (!provider) {
      setFormError("请先选择模型厂商");
      return;
    }
    const preset = settings.providers[provider];
    const baseUrl = form.base_url.trim() || preset?.base_url || "";
    if (preset?.kind === "openai" && !baseUrl) {
      setFormError("该厂商需要填写接口地址");
      return;
    }
    const next: Draft = {
      ...form,
      provider,
      base_url: baseUrl,
      name: form.name.trim() || provider,
      has_key: form.has_key || Boolean(form.api_key.trim()),
      // 归属只加不减：新供应商归当前弹窗，老数据不会被顺手踢出另一个弹窗
      for_roles: Array.from(new Set([...form.for_roles, ...SCOPE[role]])),
    };
    setServices((list) => (form.isNew ? [...list, next] : list.map((item) => (item.id === next.id ? next : item))));
    if (form.isNew) setSelectedId(next.id);
    setSaveError("");
    setForm(null);
  }

  function removeService(id: string) {
    const left = services.filter((item) => item.id !== id);
    setServices(left);
    setMarks((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    setDefaults((prev) => {
      if (!prev[id]) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
    if (selectedId === id) setSelectedId(left.filter((item) => serviceInScope(item, managed))[0]?.id ?? "");
    setConfirmDelete("");
  }

  async function save() {
    const alive = new Set(services.map((item) => item.id));
    // 先沿用服务端已有绑定（丢掉指向已删供应商的），再用本对话框的选择覆盖
    const roles: Partial<Record<ModelRole, RoleBinding>> = {};
    for (const item of ALL_ROLES) {
      const bound = settings.roles[item];
      if (bound && alive.has(bound.service_id)) roles[item] = bound;
    }
    if (selected) {
      if (selected.models.length === 0) {
        setSaveError(`「${selected.name}」还没勾选型号：点右边的铅笔进去拉取模型，勾选要用的型号。`);
        return;
      }
      for (const target of managed) {
        const model = defaults[selected.id]?.[target] ?? "";
        if (model) {
          roles[target] = { service_id: selected.id, model };
          continue;
        }
        // 默认空但还有别的型号标着该角色 → 绑定保持不动（标记 ≠ 绑定）
        if (markedModels(selected, target).length > 0) continue;
        if (target === primary) {
          setSaveError(
            `「${selected.name}」还没有可用的${shortLabel(primary)}型号：请先给至少一个型号标「${shortLabel(primary)}」。`,
          );
          return;
        }
        // 非 primary（如视觉）：维持既有「可删除绑定」语义
        delete roles[target];
      }
    }
    setSaveError("");
    const ok = await onSave({ services: services.map((item) => toPayload(item, marks)), roles });
    if (ok) onClose();
  }

  const noKey = selected ? !selected.has_key : false;
  const draft = form;

  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={onClose}>
      <div className="mx-auto mt-16 w-full max-w-xl px-4 pb-12" onClick={(event) => event.stopPropagation()}>
        <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-semibold text-ink">
                {draft ? (draft.isNew ? "添加供应商" : "编辑供应商") : ROLE_TITLE[role]}
              </h2>
              <p className="mt-0.5 text-[11.5px] leading-4 text-faint">
                {draft ? "填好地址与 Key，拉一次模型列表并勾选要用的。" : ROLE_SUBTITLE[role]}
              </p>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="rounded-md p-1 text-mute hover:bg-fog/5 hover:text-ink"
            >
              <X size={16} strokeWidth={1.75} />
            </button>
          </div>

          {draft ? (
            <div className="space-y-3.5">
              <Field label="供应商名称" hint="用来区分多厂商 / 多账号，例如「小米主账号」「百炼-向量」。">
                <input
                  value={draft.name}
                  onChange={(event) => setForm({ ...draft, name: event.target.value })}
                  placeholder={draft.provider ? labelProvider(draft.provider) : "供应商名称"}
                  className={inputCls}
                />
              </Field>

              <div className="grid grid-cols-2 gap-3">
                <Field label="厂商">
                  <select
                    value={draft.provider}
                    onChange={(event) => onProviderChange(event.target.value)}
                    className={inputCls}
                  >
                    <option value="">未选择</option>
                    {presetKeys.map((key) => (
                      <option key={key} value={key}>
                        {labelProvider(key) || key}（{key}）
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="接口地址" hint="OpenAI 兼容接口的基础地址">
                  <input
                    value={draft.base_url}
                    onChange={(event) => setForm({ ...draft, base_url: event.target.value })}
                    placeholder="https://…/v1"
                    className={inputCls}
                  />
                </Field>
              </div>

              <Field
                label="API Key"
                hint={draft.has_key ? "已配置；Key 只写入本机 .env，界面不回显。" : "只写入本机 .env，界面不回显。"}
              >
                <input
                  type="password"
                  value={draft.api_key}
                  onChange={(event) => setForm({ ...draft, api_key: event.target.value })}
                  placeholder={draft.has_key ? "留空则不修改" : "sk-…"}
                  className={inputCls}
                />
              </Field>

              {/* 条目 46：默认绑定选择器——从已标型号里选默认；绑定指向本服务但未标的型号也保留为选项 */}
              <div className={cx("grid gap-3", managed.length > 1 && "grid-cols-2")}>
                {managed.map((target) => {
                  const options = defaultOptions(draft, target);
                  return (
                    <Field
                      key={target}
                      label={`${shortLabel(target)}默认`}
                      hint={options.length === 0 ? `先给至少一个型号标上「${shortLabel(target)}」` : undefined}
                    >
                      <select
                        value={defaults[draft.id]?.[target] ?? ""}
                        onChange={(event) => setDefault(draft.id, target, event.target.value)}
                        className={inputCls}
                      >
                        <option value="">未选择</option>
                        {options.map((model) => (
                          <option key={model} value={model}>
                            {model}
                            {marks[draft.id]?.[model]?.includes(target) ? "" : "（当前绑定，未标）"}
                          </option>
                        ))}
                      </select>
                    </Field>
                  );
                })}
              </div>

              <div className="rounded-xl bg-fog/[0.03] p-3.5">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-[12.5px] text-body">模型列表</div>
                    <div className="mt-0.5 truncate text-[11px] text-faint">
                      {draft.models.length > 0
                        ? `已勾选 ${draft.models.length} 个型号`
                        : "还没勾选型号：拉取后只勾要用的"}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    {draft.pool.length > 0 ? (
                      <>
                        <button
                          type="button"
                          onClick={() => setFormModels([...draft.pool])}
                          className="rounded-lg bg-fog/[0.06] px-2 py-1 text-[11.5px] text-mute transition-colors hover:bg-fog/[0.1] hover:text-ink"
                        >
                          全选
                        </button>
                        <button
                          type="button"
                          onClick={() => setFormModels([])}
                          className="rounded-lg bg-fog/[0.06] px-2 py-1 text-[11.5px] text-mute transition-colors hover:bg-fog/[0.1] hover:text-ink"
                        >
                          清空
                        </button>
                      </>
                    ) : null}
                    <button
                      type="button"
                      onClick={() => void fetchModels()}
                      disabled={probing}
                      className="flex h-7 items-center gap-1.5 rounded-lg bg-fog/[0.07] px-2.5 text-[12px] text-body transition-colors hover:bg-fog/[0.12] hover:text-ink disabled:opacity-60"
                    >
                      {probing ? (
                        <Loader2 size={12} className="animate-spin" strokeWidth={2} />
                      ) : (
                        <Download size={12} strokeWidth={1.75} />
                      )}
                      {probing ? "拉取中…" : "拉取该供应商的模型"}
                    </button>
                  </div>
                </div>

                {draft.pool.length > 0 ? (
                  <div className="mt-2.5 max-h-56 overflow-auto rounded-lg bg-pit p-1 scrollbar-thin">
                    {draft.pool.map((model) => {
                      const checked = draft.models.includes(model);
                      return (
                        <div
                          key={model}
                          className="flex items-center gap-2 rounded-md px-1.5 py-1 transition-colors hover:bg-fog/[0.05]"
                        >
                          <button
                            type="button"
                            onClick={() => setModelChecked(model, !checked)}
                            className="flex min-w-0 flex-1 items-center gap-2 py-0.5 text-left"
                          >
                            <span
                              className={cx(
                                "flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border",
                                checked ? "border-brand bg-brand text-canvas" : "border-line",
                              )}
                            >
                              {checked ? <Check size={10} strokeWidth={3} /> : null}
                            </span>
                            <span className={cx("truncate text-[12.5px]", checked ? "text-ink" : "text-mute")}>
                              {model}
                            </span>
                          </button>
                          {checked
                            ? managed.map((target) => {
                                const on = marks[draft.id]?.[model]?.includes(target) ?? false;
                                return (
                                  <button
                                    key={target}
                                    type="button"
                                    onClick={() => toggleMark(target, model)}
                                    title={
                                      on
                                        ? `取消「${shortLabel(target)}」标记`
                                        : `把它标成${shortLabel(target)}用途（可多选）`
                                    }
                                    className={cx(
                                      "shrink-0 rounded-full px-2 py-0.5 text-[10.5px] transition-colors",
                                      on
                                        ? "bg-brand/20 text-brand"
                                        : "bg-fog/[0.06] text-faint hover:text-body",
                                    )}
                                  >
                                    {shortLabel(target)}
                                  </button>
                                );
                              })
                            : null}
                        </div>
                      );
                    })}
                  </div>
                ) : null}

                <div className="mt-2.5 flex items-center gap-2">
                  <input
                    value={modelInput}
                    onChange={(event) => setModelInput(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        addModel();
                      }
                    }}
                    placeholder="型号不在上面的列表里？手打一个"
                    className={cx(inputCls, "h-8 text-[12.5px]")}
                  />
                  <button
                    type="button"
                    onClick={addModel}
                    disabled={!modelInput.trim()}
                    className="h-8 shrink-0 rounded-lg bg-fog/[0.06] px-2.5 text-[12px] text-body transition-colors hover:bg-fog/[0.12] hover:text-ink disabled:opacity-50"
                  >
                    添加
                  </button>
                </div>

                {formError ? (
                  <p className="mt-2 flex items-start gap-1.5 text-[11.5px] leading-4 text-danger">
                    <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                    {formError}
                  </p>
                ) : formNote ? (
                  <p className="mt-2 flex items-center gap-1.5 text-[11.5px] text-ok">
                    <Check size={12} strokeWidth={2} />
                    {formNote}
                  </p>
                ) : (
                  <p className="mt-2 text-[11px] leading-4 text-faint">
                    可给多个型号标用途：点型号右边的
                    {managed.map((target) => shortLabel(target)).join(" / ")}
                    随时标或取消；各角色的默认型号在列表上方选择。
                  </p>
                )}
              </div>

              <div className="flex items-center gap-2 pt-1">
                <button
                  type="button"
                  onClick={() => setForm(null)}
                  className="flex items-center gap-1.5 rounded-full bg-fog/[0.06] px-3 py-1.5 text-[12.5px] text-mute transition-colors hover:text-ink"
                >
                  <ArrowLeft size={12} strokeWidth={1.75} />
                  返回列表
                </button>
                <span className="flex-1" />
                <button
                  type="button"
                  onClick={commitForm}
                  className="rounded-full bg-send px-4 py-1.5 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover"
                >
                  保存供应商
                </button>
              </div>
            </div>
          ) : (
            <>
              <div className="rounded-xl bg-fog/[0.03] p-3">
                <div className="mb-2 flex items-center justify-between px-1">
                  <span className="text-[12px] text-mute">供应商</span>
                  <span className="text-[11px] text-faint">
                    选一条作为{shortLabel(role)}模型的来源，保存后生效
                  </span>
                </div>

                {visible.length === 0 ? (
                  <p className="px-1 py-3 text-[12px] leading-5 text-mute">
                    {services.length > 0
                      ? `还没有${shortLabel(role)}供应商：别的供应商只在另一个弹窗里出现。`
                      : "还没有供应商。添加一条、填好地址与 API Key，拉取模型后勾选要用的。"}
                  </p>
                ) : (
                  <div className="space-y-1.5">
                    {visible.map((service) => {
                      const active = service.id === selected?.id;
                      // 「当前」= 该角色真正生效的绑定（标记 ≠ 绑定，未绑定的服务不冒充当前）
                      let using = "";
                      if (active) {
                        const bound = settings.roles[primary];
                        if (bound && bound.service_id === service.id) using = bound.model;
                      }
                      const taggedCount = markedModels(service, primary).length;
                      const status = using
                        ? `当前：${using}`
                        : service.models.length === 0
                          ? "还没勾选型号"
                          : taggedCount > 0
                            ? `已标${shortLabel(primary)} ${taggedCount} 个`
                            : `还没标${shortLabel(primary)}型号`;
                      return (
                        <div
                          key={service.id}
                          className={cx(
                            "flex items-center gap-2.5 rounded-xl px-2.5 py-2 transition-colors",
                            active ? "bg-fog/[0.08]" : "hover:bg-fog/[0.05]",
                          )}
                        >
                          <button
                            type="button"
                            onClick={() => selectService(service.id)}
                            className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
                          >
                            <span
                              className={cx(
                                "flex h-4 w-4 shrink-0 items-center justify-center rounded-full border",
                                active ? "border-brand bg-brand" : "border-line",
                              )}
                            >
                              {active ? <span className="h-1.5 w-1.5 rounded-full bg-canvas" /> : null}
                            </span>
                            <span className="min-w-0 flex-1">
                              <span className="flex items-center gap-2">
                                <span className={cx("truncate text-[13px]", active ? "text-ink" : "text-body")}>
                                  {service.name}
                                </span>
                                {!service.has_key ? (
                                  <span className="shrink-0 rounded-md bg-warn/15 px-1.5 py-0.5 text-[10.5px] text-warn">
                                    无 Key
                                  </span>
                                ) : null}
                              </span>
                              <span className="mt-0.5 block truncate text-[11.5px] text-faint">
                                {labelProvider(service.provider) || service.provider} ·{" "}
                                {service.base_url || "未填接口地址"} ·{" "}
                                {service.models.length > 0 ? `已勾选 ${service.models.length} 个型号` : "未勾选型号"}
                              </span>
                              {active ? (
                                <span className="mt-0.5 block truncate text-[11.5px] text-brand">{status}</span>
                              ) : null}
                            </span>
                          </button>
                          <button
                            type="button"
                            onClick={() => openForm(service)}
                            aria-label={`编辑供应商 ${service.name}`}
                            className="rounded-md p-1.5 text-mute transition-colors hover:bg-fog/5 hover:text-ink"
                          >
                            <Pencil size={13} strokeWidth={1.75} />
                          </button>
                          <button
                            type="button"
                            onClick={() =>
                              confirmDelete === service.id ? removeService(service.id) : setConfirmDelete(service.id)
                            }
                            aria-label={`删除供应商 ${service.name}`}
                            className={cx(
                              "flex items-center gap-1 rounded-md p-1.5 transition-colors",
                              confirmDelete === service.id ? "bg-rose/20 text-rose" : "text-mute hover:text-rose",
                            )}
                          >
                            <Trash2 size={13} strokeWidth={1.75} />
                            {confirmDelete === service.id ? <span className="text-[11px]">确认</span> : null}
                          </button>
                        </div>
                      );
                    })}
                  </div>
                )}

                <button
                  type="button"
                  onClick={openNewForm}
                  className="mt-2 flex w-full items-center justify-center gap-1.5 rounded-xl border border-dashed border-line py-2 text-[12.5px] text-mute transition-colors hover:bg-fog/[0.04] hover:text-ink"
                >
                  <Plus size={13} strokeWidth={1.75} />
                  添加供应商
                </button>
              </div>

              <p className="mt-3 text-[11px] leading-4 text-faint">
                型号在「编辑供应商」里勾选并标用途（可给多个型号标，也能标多个用途）：勾中的才出现在
                {role === "chat" ? "对话页右下角的模型菜单" : "向量检索里"}；各角色的默认型号在表单上方的下拉里选。
                {role === "chat" ? "没绑「视觉」默认就不能提问带图。" : ""}
              </p>

              {noKey && selected ? (
                <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-warn">
                  <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                  「{selected.name}」还没有 API Key，线上厂商调用会失败；本机 Ollama 之类的服务可以不填。
                </p>
              ) : null}

              {saveError ? (
                <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-danger">
                  <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                  {saveError}
                </p>
              ) : null}

              <div className="mt-5 flex items-center gap-2">
                <span className="flex-1 text-[11px] leading-4 text-faint">保存后立即生效，并写入本机 .env。</span>
                <button
                  type="button"
                  onClick={onClose}
                  className="rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12]"
                >
                  取消
                </button>
                <button
                  type="button"
                  disabled={saving}
                  onClick={() => void save()}
                  className="flex items-center gap-1.5 rounded-full bg-send px-4 py-1.5 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover disabled:opacity-60"
                >
                  {saving ? <Loader2 size={13} className="animate-spin" strokeWidth={2} /> : null}
                  {saving ? "保存中…" : "保存"}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
