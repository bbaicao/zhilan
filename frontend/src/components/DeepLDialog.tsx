import { useState } from "react";
import { CheckCircle2, CircleAlert, KeyRound, Loader2, X } from "lucide-react";
import { ApiError, updateSettings } from "../api";

/** 条目 23：设置页「翻译」→ 配置 DeepL Key。
 *  Key 只存本机 .env 的 DEEPL_API_KEY，接口不回显；保存走既有 PUT /api/settings（translate.api_key，空串=保留现值）。 */

const inputCls =
  "w-full rounded-lg border border-line bg-fog/[0.04] px-3 py-2 text-[13px] text-ink outline-none transition-colors placeholder:text-faint focus:border-edge";

type DeepLDialogProps = {
  /** 打开弹窗时的已配置状态（只影响状态说明文案） */
  hasKey: boolean;
  /** 保存成功后回传最新 has_key，让设置行的副标题立即跟上 */
  onSaved(hasKey: boolean): void;
  onClose(): void;
};

export default function DeepLDialog({ hasKey, onSaved, onClose }: DeepLDialogProps) {
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [savedNow, setSavedNow] = useState(false);

  function close() {
    if (saving) return;
    onClose();
  }

  async function save() {
    if (saving) return;
    setSaving(true);
    setError("");
    try {
      const next = await updateSettings({ translate: { api_key: apiKey.trim() } });
      const nowHasKey = Boolean(next.translate?.has_key);
      onSaved(nowHasKey);
      setSavedNow(true);
      setApiKey("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={saving ? undefined : close}>
      <div className="mx-auto mt-16 w-full max-w-lg px-4 pb-12" onClick={(event) => event.stopPropagation()}>
        <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="flex items-center gap-2 text-[15px] font-semibold text-ink">
                <KeyRound size={16} className="text-brand" strokeWidth={1.75} />
                DeepL 翻译
              </h2>
              <p className="mt-0.5 text-[11.5px] leading-4 text-faint">
                {hasKey ? "已配置（Key 仅存本机，界面不回显）" : "未配置 · 配置后可翻译选中文本与回答"}
              </p>
            </div>
            <button
              type="button"
              onClick={close}
              disabled={saving}
              aria-label="关闭"
              className="rounded-md p-1 text-mute hover:bg-fog/5 hover:text-ink disabled:opacity-50"
            >
              <X size={16} strokeWidth={1.75} />
            </button>
          </div>

          <label htmlFor="deepl-api-key" className="mb-1.5 block text-[12.5px] text-mute">
            DeepL API Key
          </label>
          <input
            id="deepl-api-key"
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder={hasKey ? "留空则保留已配置的 Key" : "粘贴 DeepL API Key（免费版以 :fx 结尾）"}
            autoComplete="off"
            spellCheck={false}
            className={inputCls}
          />

          {savedNow ? (
            <p className="mt-2.5 flex items-start gap-1.5 text-[12px] leading-4 text-ok">
              <CheckCircle2 size={13} className="mt-0.5 shrink-0" strokeWidth={1.75} />
              Key 已保存，笔记与对话里的翻译入口已可用。
            </p>
          ) : null}
          {error ? (
            <p className="mt-2.5 flex items-start gap-1.5 text-[11.5px] leading-4 text-rose">
              <CircleAlert size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
              {error}
            </p>
          ) : null}

          <div className="mt-3 space-y-1 rounded-xl bg-fog/[0.03] px-3.5 py-3 text-[11px] leading-4 text-faint">
            <p>· Key 只保存在本机 .env 的 DEEPL_API_KEY，不会上传到任何服务器。</p>
            <p>· 免费版 Key 以 :fx 结尾，保存后自动走 api-free 免费端点。</p>
            <p>· 仅支持中文 ⇄ English 互译。</p>
          </div>

          <div className="mt-5 flex items-center gap-2">
            <span className="flex-1 text-[11px] leading-4 text-faint">保存后立即生效。</span>
            <button
              type="button"
              onClick={close}
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
        </div>
      </div>
    </div>
  );
}
