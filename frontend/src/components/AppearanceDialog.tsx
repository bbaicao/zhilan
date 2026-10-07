import { useEffect, useState } from "react";
import { Check, X } from "lucide-react";
import { cx } from "../utils";
import { getThemePref, setTheme, THEME_CHANGE_EVENT, type ThemePref } from "../theme";

/** 条目 36：外观设置弹窗——跟随系统 / 浅色 / 深色三选一。
 *  点选即生效即保存（没有保存按钮），弹窗开着就能看到切换效果。 */

const OPTIONS: Array<{ value: ThemePref; label: string; hint: string }> = [
  { value: "system", label: "跟随系统", hint: "没有自己的固定偏好，随 Windows 的深浅色设置自动切换" },
  { value: "light", label: "浅色", hint: "浅底深字，适合明亮环境" },
  { value: "dark", label: "深色", hint: "深底浅字，默认风格" },
];

export default function AppearanceDialog({ onClose }: { onClose(): void }) {
  const [pref, setPref] = useState<ThemePref>(getThemePref());

  // 主题可能在别处变化（如多窗口），保持选中态同步
  useEffect(() => {
    const sync = () => setPref(getThemePref());
    window.addEventListener(THEME_CHANGE_EVENT, sync);
    return () => window.removeEventListener(THEME_CHANGE_EVENT, sync);
  }, []);

  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={onClose}>
      <div className="mx-auto mt-16 w-full max-w-md px-4 pb-12" onClick={(event) => event.stopPropagation()}>
        <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-semibold text-ink">外观</h2>
              <p className="mt-0.5 text-[11.5px] leading-4 text-faint">
                选择界面配色，点选后立即生效并记住，刷新后保持。
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
            <div className="space-y-2">
              {OPTIONS.map((option) => {
                const on = option.value === pref;
                return (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => {
                      setPref(option.value);
                      setTheme(option.value);
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

          <p className="mt-3 px-0.5 text-[11px] leading-4 text-faint">
            「跟随系统」时切换 Windows 深浅色，知澜会实时跟上；选浅色 / 深色则固定使用该主题。
          </p>
        </div>
      </div>
    </div>
  );
}
