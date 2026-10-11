import { memo, useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, Copy, Languages, Pencil, SlidersHorizontal, Sparkles, Square, Waves } from "lucide-react";
import { describeFilters, hasFilters, IMAGE_ONLY_PLACEHOLDER, resolveCitations, type ChatMessage } from "../api";
import Markdown from "./Markdown";
import CitationList from "./CitationList";
import ReasoningBlock from "./ReasoningBlock";
import AgentStepsBlock from "./AgentStepsBlock";
import ImageLightbox from "./ImageLightbox";
import { cx } from "../utils";

/** skill id → 短名：local:code-review → code-review；builtin:knowledge → knowledge */
function skillShortName(id: string) {
  const slug = id.includes(":") ? id.split(":").slice(1).join(":") : id;
  return slug || id;
}

/**
 * 条目 3.6：角落来源标签——「**这整条回答从哪来**」的答案级标记，与句子级引用
 * （正文 `[n]` + 下方引用卡片）并列的第二层（`优化/对话机制/设计说明` §0.4）。
 * 光看引用卡片，「没有卡片」分不清「查了库没命中」和「压根没查库」，补的就是这个判断。
 *
 * 取值与文案逐字定稿于阶段 1《正常对话规格》§5.2，**与后端同源**——后端唯一控制点是
 * `backend/app/rag/schemas.py` 的 `ANSWER_SOURCE_BY_KIND`（分路结果 → `messages.answer_source`）：
 *
 * | `answer_source` | 角落文案（逐字） | 配色 | 引用区 |
 * |---|---|---|---|
 * | `kb` | 依据知识库 | 绿 / 中性（`text-brand` 这类中性强调色） | 显示（卡片 + `[n]`） |
 * | `general` | 库外回答 · 模型自身知识 | 琥珀提示色（`text-warn`） | 不显示 |
 * | `empty_scope` | 未找到库内资料 | 中性 / 灰（`text-faint`） | 不显示 |
 * | `web` | 依据联网资料 | 绿 / 中性（`text-brand`） | 显示（卡片 + `[n]`） |
 * | `clarify` | 不渲染标签（追问不是回答） | — | 不显示 |
 * | `null` / 缺省 | 不渲染标签（历史存量消息、停止轮 `kind="stopped"`） | — | 按现状（有 refs 就显示） |
 *
 * 两条硬约束（阶段 1 规格 §5.2「渲染规则」2 / 3）：
 * ① **只读后端字段**：不用「有没有 refs」反推来源、不让模型在正文里自述来源；
 * ② **文案不得改写**：含 `general` 里的中点「·」与它两侧各一个空格。
 * 未知取值按「不渲染」处理——宁可暂时缺标记，也不猜错来源。
 * 🔴 `web`（第五值）随 02 v1.0.11 新增（`回归修复`，2026-09-27 拍板①取 B）：
 * 后端唯一控制点 = `source_map.answer_source_for_materials`（Agent 收口装配覆写，
 * 混合轮一票即走）；本行只加映射，不反推、不改既有四行文案。
 */
const ANSWER_SOURCE_TAG: Record<string, { text: string; hint: string; tone: string } | undefined> = {
  kb: { text: "依据知识库", hint: "本条回答依据知识库资料", tone: "bg-brand/10 text-brand" },
  general: {
    text: "库外回答 · 模型自身知识",
    hint: "库内没有命中，本次由模型自身知识回答",
    tone: "bg-warn/10 text-warn",
  },
  empty_scope: { text: "未找到库内资料", hint: "指定范围内没有资料", tone: "bg-fog/[0.06] text-faint" },
  web: { text: "依据联网资料", hint: "本条回答引用了联网检索的资料", tone: "bg-brand/10 text-brand" },
  // `clarify`（澄清追问）与 `null` / 缺省（历史存量消息、停止轮）刻意不在表里——一律不渲染标签。
};

function MessageItem({
  message,
  onOpenRef,
  onTranslate,
  translateEnabled = false,
  /** 条目 48：仅当前会话「最新一条用户消息」为真（更早的用户消息不渲染操作区，
   *  因条目 49 已让任意文字可拖选复制，历史消息不缺复制路径，也避免每个气泡都挂一排按钮） */
  latestUser = false,
  /** 条目 48：把这条用户消息原文交还给上层，由 App 装回输入框（重新编辑） */
  onEditUser,
  /** 条目 48：复制失败等可读提示（复用上层 TopBanner） */
  onNotify,
}: {
  message: ChatMessage;
  onOpenRef(type: string, id?: number | null): void;
  /** 条目 23：点「翻译」把这条回答交给上层共享的 TranslateDialog（仅 assistant 消息有此按钮） */
  onTranslate?(text: string): void;
  /** DeepL Key 已配置；未配置时翻译按钮置灰并提示去设置 */
  translateEnabled?: boolean;
  latestUser?: boolean;
  onEditUser?(text: string): void;
  onNotify?(message: string): void;
}) {
  const isUser = message.role === "user";
  // 条目 3.3：与后端 `cited_refs` 同规则解析引用，并拿到 mode——fallback 时
  // CitationList 要显式说明「模型未标注编号，以下是本次检索到的全部资料」，不许静默。
  // 用户消息没有引用，恒为空（mode 固定 none，不会渲染 fallback 说明）。
  const { items: refs, mode: citationMode } = isUser
    ? { items: [], mode: "none" as const }
    : resolveCitations(message.content, message.refs);
  const kind = message.kind || "normal";
  const generationStatus = isUser ? undefined : (
    message.generation_status ?? message.thinking?.generation_status ??
    (kind === "stopped" ? "stopped" : undefined)
  );
  const finishReason = message.finish_reason ?? message.thinking?.finish_reason;
  const generationLabel = generationStatus === "completed" ? "已完成"
    : generationStatus === "incomplete" ? "回答已截断"
    : generationStatus === "stopped" ? "已停止"
    : generationStatus === "failed" ? "生成失败" : "";
  const reasonLabel = finishReason ? ({
    stop: "正常结束",
    length: "达到输出长度上限",
    aborted: "用户停止",
    stopped: "用户停止",
    user_stop: "用户停止",
    local_abort: "本地停止，部分内容未保存",
    error: "生成出错",
    content_filter: "服务方内容限制",
  } as Record<string, string>)[finishReason] ?? finishReason : "";
  // 条目 53：这条用户消息随消息发出的图（助理消息与历史存量消息恒为空）
  const images = message.images ?? [];
  const rawText = message.content ?? "";
  // 条目 53：纯图消息在库里 content 落的是占位串「（图片）」，带图时不再把它当正文画出来
  const text = images.length > 0 && rawText === IMAGE_ONLY_PLACEHOLDER ? "" : rawText;
  const hasText = Boolean(text.trim());
  // 条目 53：当前点开看大图的是第几张（null = 没开预览）
  const [preview, setPreview] = useState<number | null>(null);
  // 回归检查：历史消息回显当次检索范围，而不是只留下答案
  const scoped = hasFilters(message.filters) ? describeFilters(message.filters) : "";
  const skillIds = message.skill_ids || [];
  const label = isUser ? "我" : kind === "clarify" ? "知澜 · 追问" : kind === "refuse" ? "知澜 · 资料不足" : "知澜";
  // 条目 3.6：角落来源标签**只读**后端字段 `answer_source`（`null` / 缺省 / `clarify` → 无标签）。
  // 流式回答在 `done` 帧到达前这个字段还是空的 ⟹ 自然不渲染，不会先闪一个错值。
  const sourceTag = isUser ? undefined : ANSWER_SOURCE_TAG[message.answer_source ?? ""];
  // 条目 48：复制反馈（图标/文案在 1.6 秒内由「复制」切换为「已复制」）
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<number | null>(null);
  const copyMounted = useRef(true);
  useEffect(() => {
    copyMounted.current = true;
    return () => {
      copyMounted.current = false;
      if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
      copyTimer.current = null;
    };
  }, []);

  /** 条目 48：把这条用户消息纯文本写入剪贴板；
   *  优先 navigator.clipboard，非安全上下文/权限被拒再兜底 execCommand，再失败提示手动复制。
   *  条目 53：复制的口径跟着「看得见的正文」走——纯图消息没有正文，不把库里的
   *  「（图片）」占位串当内容复制走，改为提示去看大图。 */
  function copyText() {
    const copySource = message.content;
    if (!hasText) {
      onNotify?.("这条消息只有图片，没有文字可复制；点图片可以看大图");
      return;
    }
    const ok = () => {
      if (!copyMounted.current) return;
      setCopied(true);
      if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => {
        setCopied(false);
        copyTimer.current = null;
      }, 1600);
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard
        .writeText(copySource)
        .then(ok)
        .catch(() => fallbackCopy(copySource, ok));
    } else {
      fallbackCopy(copySource, ok);
    }
  }

  function fallbackCopy(text: string, ok: () => void) {
    if (!copyMounted.current) return;
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const done = document.execCommand("copy");
      document.body.removeChild(ta);
      if (done) {
        ok();
        return;
      }
    } catch {
      /* 兜底也失败则落到提示 */
    }
    onNotify?.("复制失败，请手动选中文字后复制");
  }

  return (
    <div className={cx("group mb-8 flex animate-fade-up", isUser ? "justify-end" : "justify-start")}>
      {/* 我的消息靠右（气泡），知澜的靠左（正文排版） */}
      <div className={cx("flex min-w-0 flex-col", isUser ? "max-w-[80%] items-end" : "w-full items-stretch")}>
        <div className="mb-2 flex select-none items-center gap-1.5 text-[12px] text-mute">
          {isUser ? null : <Waves size={12} className="text-brand" strokeWidth={2} />}
          {label}
        </div>
        {isUser ? (
          <>
            {/* 条目 53：随消息发出的图先显示（多张横排、放不下换行；点一张看一张）。
                定死一种排布：图在上、文在下（对齐 Codex / 常见聊天界面），
                纯图消息连气泡都不画，整条消息只由缩略图表达。 */}
            {images.length ? (
              <div className="mb-2 flex flex-wrap justify-end gap-2">
                {images.map((src, index) => (
                  <button
                    key={`${index}-${src.length}`}
                    type="button"
                    onClick={() => setPreview(index)}
                    aria-label={`查看第 ${index + 1} 张图片的大图`}
                    title="点击看大图"
                    className="overflow-hidden rounded-xl border border-hairline transition-opacity hover:opacity-90"
                  >
                    <img
                      src={src}
                      alt=""
                      draggable={false}
                      className="h-32 w-auto max-w-[280px] object-cover"
                    />
                  </button>
                ))}
              </div>
            ) : null}
            {hasText ? (
              /* select-text：对抗 pywebview 可能注入的全局 user-select:none（客户端也在改，这里双保险） */
              <div className="select-text whitespace-pre-wrap break-words rounded-2xl bg-raise px-3.5 py-2.5 text-[15px] leading-7 text-ink">
                {text}
              </div>
            ) : null}
            {/* 条目 48：仅最新一条用户消息露出「复制 / 重新编辑」；装饰不参与选中，hover/键盘可达时显示 */}
            {latestUser ? (
              <div className="mt-1 flex select-none items-center justify-end gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
                <button
                  type="button"
                  onClick={copyText}
                  aria-label="复制这条消息"
                  className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11.5px] text-mute transition-colors hover:bg-fog/[0.06] hover:text-ink"
                >
                  {copied ? <Check size={12} strokeWidth={1.75} /> : <Copy size={12} strokeWidth={1.75} />}
                  {copied ? "已复制" : "复制"}
                </button>
                {onEditUser ? (
                  <button
                    type="button"
                    onClick={() => onEditUser(message.content)}
                    aria-label="重新编辑这条消息"
                    className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11.5px] text-mute transition-colors hover:bg-fog/[0.06] hover:text-ink"
                  >
                    <Pencil size={12} strokeWidth={1.75} />
                    重新编辑
                  </button>
                ) : null}
              </div>
            ) : null}
          </>
        ) : (
          <>
            {/* 条目 19：厂商给的思考过程放在答案上方，可折叠；追问/资料不足这类没有思考内容 */}
            <ReasoningBlock
              reasoning={message.reasoning}
              thinking={message.thinking}
              hasAnswer={Boolean(message.content.trim())}
            />
            {/* 条目 18：Agent 过程轨迹放在思考过程之后、答案之前；非 Agent 回答不渲染 */}
            <AgentStepsBlock
              trace={message.agent_trace}
              hasAnswer={Boolean(message.content.trim())}
            />
            <Markdown content={message.content} />
            {/* 条目 3.6 / 条目 42 / 条目 23：正文右下角**同一条信息行**——
                来源标签固定在气泡角落（右端），左端留给「已停止」角标，右端还有翻译入口。
                标签不占引用卡片的位置：引用区是下一块（`CitationList`），库内回答两者同时出现。 */}
            {generationLabel || sourceTag || message.recovered || (onTranslate && message.content.trim()) ? (
              <div className="mt-1 flex select-none flex-wrap items-center justify-between gap-2">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  {/* 条目 42：被用户停止的回答在截断处标明「已停止」，不装作完整答案 */}
                  {generationLabel ? (
                    <div
                      data-generation-status={generationStatus}
                      className={cx("flex min-w-0 items-center gap-1 break-words text-[12px]",
                        generationStatus === "incomplete" || generationStatus === "failed" ? "text-warn" : "text-faint")}
                    >
                      {generationStatus === "stopped" ? <Square size={10} className="shrink-0" strokeWidth={2.5} fill="currentColor" />
                        : generationStatus === "completed" ? <Check size={12} className="shrink-0" strokeWidth={2} />
                        : <AlertTriangle size={12} className="shrink-0" strokeWidth={2} />}
                      <span className="min-w-0 break-words">{generationLabel}{reasonLabel ? ` · ${reasonLabel}` : ""}</span>
                    </div>
                  ) : null}
                  {/* 阶段 4 条目 4.3：这一轮发生过崩溃恢复（工具已经开始但没留下结果）。
                      判据是**后端字段**（`recovery/synthesized` 事件现算），不是前端内存，
                      所以刷新 / 重进会话后照样在；也不靠「有没有引用」反推来源。 */}
                  {message.recovered ? (
                    <div
                      data-recovered="true"
                      className="flex items-center gap-1 text-[12px] text-amber-500/90"
                      title="这一轮有一次工具调用已经开始但没留下结果，结论未知；如需重试，只重试只读或幂等操作"
                    >
                      <AlertTriangle size={10} strokeWidth={2.5} />
                      上次工具结果不明
                    </div>
                  ) : null}
                </div>
                <div className="flex items-center gap-2">
                  {/* 条目 3.6：角落来源标签（文案 / 配色见文件头 ANSWER_SOURCE_TAG 的定稿表）；
                      悬停给一行说明，值本身不在这里做任何推算或兜底改写 */}
                  {sourceTag ? (
                    <span
                      data-answer-source={message.answer_source ?? ""}
                      className={cx("shrink-0 rounded-full px-2 py-0.5 text-[11px]", sourceTag.tone)}
                      title={sourceTag.hint}
                    >
                      {sourceTag.text}
                    </span>
                  ) : null}
                  {/* 条目 23：DeepL 翻译入口（hover 显隐；Key 未配置时置灰，title 说明去设置） */}
                  {onTranslate && message.content.trim() ? (
                    <button
                      type="button"
                      onClick={() => onTranslate(message.content)}
                      disabled={!translateEnabled}
                      title={translateEnabled ? "翻译这条回答（DeepL 中英互译）" : "未配置 DeepL Key，请到设置 → 翻译"}
                      className="hidden items-center gap-1 rounded-md px-1.5 py-0.5 text-[11.5px] text-mute transition-colors hover:bg-fog/[0.06] hover:text-ink group-hover:flex disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent"
                    >
                      <Languages size={12} strokeWidth={1.75} />
                      翻译
                    </button>
                  ) : null}
                </div>
              </div>
            ) : null}
          </>
        )}
        {scoped ? (
          <div className="mt-2 flex select-none items-center gap-1.5 text-[11.5px] text-faint">
            <SlidersHorizontal size={11} strokeWidth={1.75} />
            检索范围：{scoped}
          </div>
        ) : null}
        {skillIds.length ? (
          <div className="mt-2 flex select-none flex-wrap items-center gap-1.5 text-[11.5px] text-faint">
            <Sparkles size={11} strokeWidth={1.75} />
            技能
            {skillIds.map((id) => (
              <span key={id} className="rounded-full bg-brand/10 px-1.5 py-0.5 text-[10.5px] text-brand">
                {skillShortName(id)}
              </span>
            ))}
          </div>
        ) : null}
        {/* 条目 31：引用资料默认收起成一行摘要，展开后卡片与跳转行为不变；
            条目 3.3：补 mode——fallback 模式在列表上方给一行可读说明 */}
        <CitationList items={refs} mode={citationMode} onOpen={onOpenRef} />
      </div>
      {/* 条目 53：大图预览挂在消息自己身上（fixed 定位，不参与气泡布局）；
          关掉即回到原处，翻页只在同一条消息带的这几张里走 */}
      {preview !== null && images.length ? (
        <ImageLightbox images={images} startIndex={preview} onClose={() => setPreview(null)} />
      ) : null}
    </div>
  );
}

export default memo(MessageItem);
