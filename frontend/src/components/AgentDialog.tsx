import { useState } from "react";
import { AlertCircle, Check, Loader2, X } from "lucide-react";
import { AGENT_MODE_LABELS, type SettingsIn, type SettingsOut } from "../api";
import { cx } from "../utils";

/** 条目 18：Agent 设置弹窗——默认模式、循环上限与审计。
 *
 * 三个模式是后端契约里定死的取值（off / react / plan_execute），界面文案共用一张表。
 * 老后端没回 agent 段时按「直答 · 3 轮 · 不审计」给草稿，保存后即以响应为准。
 */

const inputCls =
  "h-9 w-full rounded-lg border border-line bg-fog/[0.04] px-3 text-[13.5px] text-ink outline-none transition-colors placeholder:text-faint focus:border-edge";

const MODE_OPTIONS: Array<{ value: string; label: string; hint: string }> = [
  { value: "off", label: AGENT_MODE_LABELS.off, hint: "不走工具循环，按现有检索 + 回答流程直接作答" },
  { value: "react", label: AGENT_MODE_LABELS.react, hint: "按需调用工具、多轮迭代，凑齐材料后再回答" },
  { value: "plan_execute", label: AGENT_MODE_LABELS.plan_execute, hint: "先列出计划，再按计划逐步执行后回答" },
];

function Toggle({ on, onClick, label }: { on: boolean; onClick(): void; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      onClick={onClick}
      className={cx(
        "relative h-[22px] w-[38px] shrink-0 rounded-full transition-colors",
        on ? "bg-brand" : "bg-fog/[0.14]",
      )}
    >
      <span
        className={cx(
          "absolute top-[3px] h-4 w-4 rounded-full bg-canvas transition-all",
          on ? "left-[18px]" : "left-[3px]",
        )}
      />
    </button>
  );
}

export default function AgentDialog({
  agent,
  onSave,
  onClose,
}: {
  agent: SettingsOut["agent"] | undefined;
  onSave(payload: SettingsIn, onFail?: (message: string) => void): Promise<boolean>;
  onClose(): void;
}) {
  const [mode, setMode] = useState(agent?.mode ?? "off");
  const [maxRounds, setMaxRounds] = useState(String(agent?.max_rounds ?? 3));
  const [audit, setAudit] = useState(Boolean(agent?.audit));
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const changed =
    mode !== (agent?.mode ?? "off") ||
    Number(maxRounds) !== (agent?.max_rounds ?? 3) ||
    audit !== Boolean(agent?.audit);

  async function save() {
    setError("");
    setSaving(true);
    // 后端拒绝时（例如轮数越界）把可读原因留在弹窗里
    const ok = await onSave({ agent: { mode, max_rounds: Number(maxRounds), audit } }, setError);
    setSaving(false);
    if (ok) onClose();
  }

  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={onClose}>
      <div className="mx-auto mt-16 w-full max-w-xl px-4 pb-12" onClick={(event) => event.stopPropagation()}>
        <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-semibold text-ink">Agent</h2>
              <p className="mt-0.5 text-[11.5px] leading-4 text-faint">
                Agent 模式决定提问时是否先走工具循环再回答；对话页模型菜单里可按次覆盖。
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

          <div className="rounded-xl bg-fog/[0.03] p-3.5">
            <div className="mb-2 px-0.5 text-[12.5px] text-body">默认模式</div>
            <div className="space-y-2">
              {MODE_OPTIONS.map((option) => {
                const on = option.value === mode;
                return (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => {
                      setMode(option.value);
                      setError("");
                    }}
                    className={cx(
                      "flex w-full items-center gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors",
                      on ? "border-brand/60 bg-brand-soft" : "border-line hover:bg-fog/[0.05]",
                    )}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block text-[13px] text-ink">{option.label}</span>
                      <span className="mt-0.5 block text-[11.5px] leading-4 text-faint">{option.hint}</span>
                    </span>
                    {on ? <Check size={14} className="shrink-0 text-brand" strokeWidth={2} /> : null}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="mt-3 rounded-xl bg-fog/[0.03] p-3.5">
            <div className="grid gap-3.5">
              <div>
                <div className="mb-1.5 px-0.5 text-[12.5px] text-body">循环上限</div>
                <input
                  type="number"
                  inputMode="numeric"
                  min={1}
                  max={12}
                  value={maxRounds}
                  onChange={(event) => {
                    setMaxRounds(event.target.value);
                    setError("");
                  }}
                  spellCheck={false}
                  className={inputCls}
                />
                <p className="mt-1.5 px-0.5 text-[11px] leading-4 text-faint">
                  单次提问最多执行多少轮工具调用（1~12）。
                </p>
              </div>

              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-[13.5px] text-ink">审计</div>
                  <div className="mt-0.5 text-[11.5px] leading-4 text-faint">
                    {audit ? "已开启：Agent 的每一步都会留下过程记录。" : "已关闭：不记录 Agent 过程明细。"}
                  </div>
                </div>
                <Toggle on={audit} label="开启审计" onClick={() => setAudit((on) => !on)} />
              </div>
            </div>
          </div>

          {error ? (
            <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-rose">
              <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
              {error}
            </p>
          ) : null}

          <div className="mt-5 flex items-center gap-2">
            <span className="flex-1 text-[11px] leading-4 text-faint">
              {changed ? "保存后立即生效。" : "没有改动。"}
            </span>
            <button
              type="button"
              onClick={onClose}
              className="rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12]"
            >
              取消
            </button>
            <button
              type="button"
              disabled={saving || !changed}
              onClick={() => void save()}
              className="flex items-center gap-1.5 rounded-full bg-send px-4 py-1.5 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover disabled:opacity-60"
            >
              {saving ? <Loader2 size={13} className="animate-spin" strokeWidth={2} /> : null}
              {saving ? "保存中…" : "保存"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
