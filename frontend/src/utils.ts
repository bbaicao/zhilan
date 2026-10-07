export type View = "chat" | "notes" | "docs" | "graph" | "trash" | "settings";

export const PROVIDER_LABEL: Record<string, string> = {
  dashscope: "阿里云百炼",
  openai: "OpenAI",
  ollama: "Ollama",
  deepseek: "DeepSeek",
  kimi: "Kimi",
  moonshot: "Kimi",
  zhipu: "智谱",
  glm: "智谱",
  doubao: "豆包",
  mimo: "小米",
  xiaomi: "小米",
  claude: "Claude",
  anthropic: "Claude",
  gemini: "Gemini",
  google: "Gemini",
  siliconflow: "硅基流动",
};

export function labelProvider(value: string | null | undefined) {
  if (!value) return "";
  return PROVIDER_LABEL[value] || value;
}

export function formatTime(iso: string) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function statusLabel(status: string) {
  if (status === "ready") return "已就绪";
  if (status === "processing") return "处理中";
  if (status === "failed") return "失败";
  return status;
}

export function fileSuffix(name: string) {
  const idx = name.lastIndexOf(".");
  return idx >= 0 ? name.slice(idx).toLowerCase() : "";
}

export function cx(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(" ");
}
