/**
 * 引用解析契约（条目 3.3）：前端与后端**同一套规则**，逐行同构。
 *
 * 后端实现是 `backend/app/generation/citation.py` 的 `resolve_citations` / `cited_refs`
 * （同一处注释里写着这三条规则），改任何一侧都必须同步改另一侧。
 * 不变量：同一 `(content, refs)` 输入，两侧算出的集合与 mode 必须完全相同。
 * 两侧一致性的交叉用例由条目 3.4 的 `test_backend_frontend_parity` 守住。
 *
 *   规则 1 —— 解析正则：`\[(\d+)\]`（半角方括号 + 至少一位数字；`[10]` 解析为 10，
 *             不会被拆成 `[1]` + 残留 `0`；全角 `［1］` 不构成编号，`[01]` 解析为 1）。
 *   规则 2 —— 以每条来源的正整数 `ordinal` 为编号；旧数据没有有效编号时才用原列表位置。
 *             服务端过滤后编号可以不连续，例如 `[2]`、`[4]`，不得重排成 `[1]`、`[2]`。
 *   规则 3 —— 兜底政策与 mode 判定：`refs` 为空 → `mode="none"` 且返回空数组；
 *             有效编号集合非空 → 只保留被点到的来源（按原顺序），mode=`"cited"`；
 *             有效编号集合为空（完全没写编号，或只写了越界编号）→ 带出全部来源，
 *             mode=`"fallback"`（保留候选资料，不宣称回答使用了它们或它们支持回答；
 *             `CitationList` 会明确显示候选身份与未确认说明）。
 *
 * 本文件是**纯 TS、零运行时 import**（只有类型位置 import `ChatMessage`，编译后消失），
 * 可以被 esbuild 单独打包成 `.mjs` 交给 Node 跑两侧比对（见
 * `优化/对话机制/阶段3-检索固化与引用溯源/证据/S3-前端比对-README.md`）。
 * `RefItem` 的类型定义放这里、由 `api.ts` re-export，避免 api ⇄ citation 的循环依赖。
 */

import type { ChatMessage } from "./api";

export type CitationMode = "cited" | "fallback" | "none";

export type RefItem = {
  type: string;
  title: string;
  snippet: string;
  score: number;
  kb_name?: string | null;
  /** 条目 14：to_ref() 写入的来源 id；历史消息可能没有，回链/相关要用 title 回落。
   *  **语义不变**：回链与「相关笔记」用它，不允许拿它承载 chunk 身份。 */
  source_id?: number | null;
  /** 条目 26：联网检索结果的原始链接 */
  url?: string | null;
  /** 条目 3.3：检索命中的 chunk 身份（旧数据 / 旧后端为缺省或 null） */
  chunk_id?: string | null;
  /** 条目 3.3：本轮检索的快照 id（条目 3.1 的 retrieval_id） */
  retrieval_id?: string | null;
  /** 条目 3.3：模型看到的资料编号（从 1 起），与 build_context 渲染的 [n] 同源 */
  ordinal?: number | null;
  /** 条目 3.3：后端给出的解析模式（同 resolveCitations 的 mode；旧数据为 null） */
  citation_mode?: CitationMode | null;
};

/** 规则 1：与后端 `_CITE_RE` 同一个正则。`lastIndex` 无关紧要——只用 `matchAll` / `test` 的
 *  调用方各自持有自己的正则实例（`matchAll` 要求带 `g`，且会内部复制，不改本实例）。 */
const CITE_RE = /\[(\p{Decimal_Number}+)\]/gu;

/** 归一化后端/历史数据里的 refs：只认「有字符串 title」的对象，其余整条丢掉。 */
export function asRefs(
  refs: ChatMessage["refs"] | RefItem[] | null | undefined,
): RefItem[] {
  if (!Array.isArray(refs)) return [];
  return refs.filter((item): item is RefItem => {
    return Boolean(
      item &&
        typeof item === "object" &&
        "title" in item &&
        typeof (item as RefItem).title === "string",
    );
  });
}

/** 规则 1：正文里出现的全部编号（**未**做范围过滤，顺序按出现顺序，可重复）。 */
export function citeMarkers(content: string): number[] {
  return [...(content || "").matchAll(CITE_RE)].map((match) => {
    // Python 的 \d 包含 Unicode 十进制数字；JS Number 只认 ASCII，先取字符的十进制位值。
    // 连续的 Decimal_Number 字符组由若干组 0..9 构成，数学字体的相邻数字组也按模 10 处理。
    const digits = Array.from(match[1], (digit) => {
      const code = digit.codePointAt(0)!;
      let first = code;
      while (/\p{Decimal_Number}/u.test(String.fromCodePoint(first - 1))) first--;
      return (code - first) % 10;
    }).join("");
    return Number(digits); // 超长数字得到 Infinity，后续视为未命中，不会打断回答渲染。
  });
}

/** 旧调用兼容：连续编号集合 `{n | 1 <= n <= count}`；稀疏引用由 resolveCitations 按 ordinal 判定。 */
export function validCiteSet(content: string, count: number): Set<number> {
  const valid = new Set<number>();
  for (const marker of citeMarkers(content)) {
    if (marker >= 1 && marker <= count) valid.add(marker);
  }
  return valid;
}

/** 规则 3：与后端 `resolve_citations` 同规则地解析引用（含兜底与 mode 判定）。
 *
 * 与后端一样，返回的每条来源都带上 `citation_mode`（后端打在 `Ref` 上、这里打在
 * `RefItem` 上），保证两侧不仅集合与 mode 相同，逐条的字段也相同。
 */
export function resolveCitations(
  content: string,
  refs: ChatMessage["refs"] | RefItem[] | null | undefined,
): { items: RefItem[]; mode: CitationMode } {
  const items = asRefs(refs);
  if (!items.length) return { items: [], mode: "none" }; // refs 为空 → none
  const markers = new Set(citeMarkers(content));
  const cited = items.filter((item, index) =>
    markers.has(Number.isInteger(item.ordinal) && (item.ordinal ?? 0) > 0
      ? item.ordinal!
      : index + 1),
  );
  if (!cited.length) {
    // 无有效编号 → 兜底带出全部（保留兜底口径，但 mode 会让界面显式说明，不静默）
    return { items: items.map((item) => ({ ...item, citation_mode: "fallback" })), mode: "fallback" };
  }
  return {
    items: cited.map((item) => ({ ...item, citation_mode: "cited" })),
    mode: "cited",
  };
}

/** 与后端 `cited_refs(answer, refs)` 同规则、同返回类型（`RefItem[]`，含兜底）。
 *
 * 历史：这里以前**没有兜底**（正文没有 `[n]` 就返回空数组），而后端会带出全部来源，
 * 同一条消息「库里存 N 条、界面显示 0 条」且不留痕——`P3-回归修复`。
 * 现在它就是 `resolveCitations(...).items`，需要区分模式（cited / fallback / none）
 * 的调用点直接用 `resolveCitations`。`api.ts` 只做 re-export，签名保持不变。
 */
export function citedRefs(
  content: string,
  refs: ChatMessage["refs"] | RefItem[] | null | undefined,
): RefItem[] {
  return resolveCitations(content, refs).items;
}
