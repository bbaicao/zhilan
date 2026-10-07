/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        /* 条目 36：颜色全部走 CSS 变量（rgb 三元组），深色默认值在 index.css 的 :root，
           浅色在 html[data-theme="light"] 覆盖；保留 <alpha-value> 让 ink/90、brand/15 这类透明度写法继续可用。 */
        canvas: "rgb(var(--zh-canvas) / <alpha-value>)",
        sidebar: "rgb(var(--zh-sidebar) / <alpha-value>)",
        panel: "rgb(var(--zh-panel) / <alpha-value>)",
        composer: "rgb(var(--zh-composer) / <alpha-value>)",
        raise: "rgb(var(--zh-raise) / <alpha-value>)",
        ink: "rgb(var(--zh-ink) / <alpha-value>)",
        body: "rgb(var(--zh-body) / <alpha-value>)",
        mute: "rgb(var(--zh-mute) / <alpha-value>)",
        faint: "rgb(var(--zh-faint) / <alpha-value>)",
        line: "rgb(var(--zh-line) / <alpha-value>)",
        hairline: "rgb(var(--zh-hairline) / <alpha-value>)",
        paper: "rgb(var(--zh-paper) / <alpha-value>)",
        mist: "rgb(var(--zh-mist) / <alpha-value>)",
        snow: "rgb(var(--zh-snow) / <alpha-value>)",
        brand: "rgb(var(--zh-brand) / <alpha-value>)",
        "brand-soft": "rgb(var(--zh-brand-soft) / <alpha-value>)",
        send: "rgb(var(--zh-send) / <alpha-value>)",
        "send-hover": "rgb(var(--zh-send-hover) / <alpha-value>)",
        rose: "rgb(var(--zh-rose) / <alpha-value>)",
        "rose-soft": "rgb(var(--zh-rose-soft) / <alpha-value>)",
        ok: "rgb(var(--zh-ok) / <alpha-value>)",
        warn: "rgb(var(--zh-warn) / <alpha-value>)",
        info: "rgb(var(--zh-info) / <alpha-value>)",
        /* fog：雾层——深色是白色透明层（bg-white/0.06 的替代），浅色换成近黑透明层，语义不变 */
        fog: "rgb(var(--zh-fog) / <alpha-value>)",
        /* edge：输入框聚焦描边 / 滚动条；edge-soft：卡片悬停描边 / 块引用分隔线 */
        edge: "rgb(var(--zh-edge) / <alpha-value>)",
        "edge-soft": "rgb(var(--zh-edge-soft) / <alpha-value>)",
        /* 辅助强调色（文件类型 / 欢迎页图标），浅色下加深保证可读 */
        iris: "rgb(var(--zh-iris) / <alpha-value>)",
        azure: "rgb(var(--zh-azure) / <alpha-value>)",
        moss: "rgb(var(--zh-moss) / <alpha-value>)",
        olive: "rgb(var(--zh-olive) / <alpha-value>)",
        clay: "rgb(var(--zh-clay) / <alpha-value>)",
        apricot: "rgb(var(--zh-apricot) / <alpha-value>)",
        /* 以下三个是带固定透明度的整色变量（值含 alpha，不支持 /N 修饰）：
           veil 弹窗遮罩、float 画布上悬浮小条、pit 弹窗内凹陷底 */
        veil: "var(--zh-veil)",
        float: "var(--zh-float)",
        pit: "var(--zh-pit)",
      },
      fontFamily: {
        sans: ['"Segoe UI"', '"Microsoft YaHei UI"', '"PingFang SC"', "sans-serif"],
        mono: ['"Cascadia Code"', '"JetBrains Mono"', "Consolas", "monospace"],
      },
      boxShadow: {
        pop: "0 12px 40px rgba(0, 0, 0, 0.45)",
        card: "0 1px 2px rgba(0, 0, 0, 0.25)",
      },
      keyframes: {
        fadeUp: {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        thinkDot: {
          "0%, 80%, 100%": { opacity: "0.25", transform: "translateY(0)" },
          "40%": { opacity: "1", transform: "translateY(-3px)" },
        },
      },
      animation: {
        "fade-up": "fadeUp 240ms ease-out both",
        "fade-in": "fadeIn 180ms ease-out both",
        "think-dot": "thinkDot 1.2s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
