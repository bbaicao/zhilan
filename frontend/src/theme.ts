/** 条目 36：主题（浅色 / 深色）。
 *
 * 主题状态不进 React：直接把结果写到 <html> 的 data-theme 与 color-scheme 上，
 * 组件只消费 CSS 变量（Tailwind token / GraphView 画布），切换全局立即生效。
 * localStorage 键 zhilan-theme 只在用户显式选择时写（light / dark）；
 * 没有键 = 跟随系统，并监听系统深浅色变化实时跟随。
 */

export type ThemePref = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "zhilan-theme";
export const THEME_CHANGE_EVENT = "zhilan-theme-change";

function readPref(): ThemePref {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw === "light" || raw === "dark" ? raw : "system";
  } catch {
    return "system";
  }
}

function systemTheme(): ResolvedTheme {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function resolveTheme(pref: ThemePref): ResolvedTheme {
  return pref === "system" ? systemTheme() : pref;
}

function applyTheme(theme: ResolvedTheme) {
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
}

/** 应用启动时调用一次：恢复上次的主题选择（没有则跟随系统），并监听系统主题变化。 */
export function initTheme() {
  applyTheme(resolveTheme(readPref()));
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  media.addEventListener("change", () => {
    // 用户有显式选择时不跟系统走
    if (readPref() !== "system") return;
    const theme = systemTheme();
    applyTheme(theme);
    window.dispatchEvent(new CustomEvent(THEME_CHANGE_EVENT, { detail: { pref: "system", theme } }));
  });
}

/** 设置主题偏好并立即生效；"system" 表示清除显式选择、跟随系统。 */
export function setTheme(pref: ThemePref) {
  try {
    if (pref === "system") localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, pref);
  } catch {
    // 写不进 localStorage（隐私模式等）时静默：本次会话内仍然生效
  }
  const theme = resolveTheme(pref);
  applyTheme(theme);
  window.dispatchEvent(new CustomEvent(THEME_CHANGE_EVENT, { detail: { pref, theme } }));
}

export function getThemePref(): ThemePref {
  return readPref();
}

export function getResolvedTheme(): ResolvedTheme {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}
