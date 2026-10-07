import { isValidElement, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Copy } from "lucide-react";

function textOf(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

function PreBlock({ children }: { children?: ReactNode }) {
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

  async function copy() {
    try {
      await navigator.clipboard.writeText(textOf(children));
      if (!copyMounted.current) return;
      setCopied(true);
      if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => {
        setCopied(false);
        copyTimer.current = null;
      }, 1600);
    } catch {
      /* 剪贴板不可用时静默 */
    }
  }

  return (
    <div className="group/code relative">
      <pre>{children}</pre>
      <button
        type="button"
        onClick={() => void copy()}
        aria-label="复制代码"
        className="absolute right-2 top-2 flex h-7 w-7 items-center justify-center rounded-md bg-fog/[0.06] text-mute opacity-0 transition-opacity hover:bg-fog/[0.12] hover:text-ink group-hover/code:opacity-100"
      >
        {copied ? <Check size={13} strokeWidth={2} /> : <Copy size={13} strokeWidth={1.75} />}
      </button>
    </div>
  );
}

/* ---- [[双向链接]]：可选能力，只有传入 resolveWiki（笔记预览）才开启，聊天里的 Markdown 不受影响 ---- */

export type WikiTarget = { kind: "note" | "doc"; id: number };

type WikiResolve = (title: string) => WikiTarget | null;

const WIKI_PATTERN = /(?<!\!)\[\[([^\[\]\n]+)\]\]/g;

/** 与后端 normalize_title 同边界，Array.from 按代码点截断，不拆 emoji 代理对。 */
export function normalizeWikiTitle(title: string): string {
  return Array.from(
    title.split("#", 1)[0].replace(/^[\s\u0085\u001c-\u001f]+|[\s\u0085\u001c-\u001f]+$/g, ""),
  ).slice(0, 255).join("").replace(/[\s\u0085\u001c-\u001f]+$/g, "");
}

type WikiNode = {
  type: string;
  value?: string;
  children?: WikiNode[];
  data?: Record<string, unknown>;
};

/** 借 emphasis 节点做载体：经 data.hName / hProperties / hChildren 渲染成带 data-wiki-title 的 span。 */
function wikiCarrier(title: string): WikiNode {
  return {
    type: "emphasis",
    children: [{ type: "text", value: title }],
    data: {
      hName: "span",
      hProperties: { "data-wiki-title": title },
      hChildren: [{ type: "text", value: title }],
    },
  };
}

/** 把文本节点里的 [[标题]] 拆成载体节点。
 *  code / inlineCode 的正文存在 value 里不走 children，天然不会被链接化；
 *  常规链接（[文本](url)）里的文字也不拆，避免嵌套可点元素。 */
function transformWikiLinks(node: WikiNode, insideLink = false): void {
  const children = node.children;
  if (!children) return;
  const isLink = node.type === "link";
  let changed = false;
  const next: WikiNode[] = [];
  for (const child of children) {
    if (
      !insideLink &&
      !isLink &&
      child.type === "text" &&
      typeof child.value === "string" &&
      child.value.includes("[[")
    ) {
      let last = 0;
      for (const match of child.value.matchAll(WIKI_PATTERN)) {
        const start = match.index ?? 0;
        // 与后端口径一致：[[标题#锚点]] 按「标题」解析；纯锚点 [[#锚点]] 不算链接
        const base = normalizeWikiTitle(match[1]);
        if (!base) continue;
        if (start > last) next.push({ type: "text", value: child.value.slice(last, start) });
        next.push(wikiCarrier(match[1].trim()));
        last = start + match[0].length;
        changed = true;
      }
      if (last < child.value.length) next.push({ type: "text", value: child.value.slice(last) });
    } else {
      transformWikiLinks(child, isLink);
      next.push(child);
    }
  }
  if (changed) node.children = next;
}

function remarkWikiLinks(): (tree: unknown) => void {
  return (tree) => transformWikiLinks(tree as WikiNode);
}

type WikiContext = {
  resolveWiki: WikiResolve;
  onOpenWiki(target: WikiTarget): void;
  onMissingWiki(title: string): void;
};

function WikiLink({ title, resolveWiki, onOpenWiki, onMissingWiki }: { title: string } & WikiContext) {
  // 与后端 extract_links 一致：# 后的锚不参与存在性判定，只按前段标题匹配
  const base = normalizeWikiTitle(title);
  const target = resolveWiki(base);
  if (target) {
    return (
      <button
        type="button"
        onClick={() => onOpenWiki(target)}
        className="cursor-pointer border-b border-info/40 text-info transition-colors hover:border-info"
      >
        {title}
      </button>
    );
  }
  return (
    <button
      type="button"
      title="未找到唯一目标：可能尚未创建、同名，或关系信息暂不可用"
      onClick={() => onMissingWiki(base)}
      className="cursor-pointer border-b border-dashed border-faint text-mute transition-colors hover:text-body"
    >
      {title}
      <span className="ml-1 rounded bg-fog/[0.06] px-1 py-px text-[10px] font-normal text-faint">未解析</span>
    </button>
  );
}

/** markdown 输出里所有 span 都会经过这里；只有带 data-wiki-title 的才是 [[链接]]，其余原样返回。 */
function WikiSpan({ props, ctx }: { props: unknown; ctx: WikiContext }) {
  const { children, ...rest } = (props ?? {}) as {
    children?: ReactNode;
    node?: unknown;
    "data-wiki-title"?: unknown;
  } & Record<string, unknown>;
  const title = rest["data-wiki-title"];
  if (typeof title !== "string" || !title.trim()) {
    return <span>{children}</span>;
  }
  return <WikiLink title={title} {...ctx} />;
}

type MarkdownProps = {
  content: string;
  /** 传入即开启 [[标题]] 双向链接渲染（笔记预览）；不传保持普通 Markdown（聊天页） */
  resolveWiki?: WikiResolve;
  /** 点击指向笔记 / 文档的 [[链接]] */
  onOpenWiki?: (target: WikiTarget) => void;
  /** 点击未创建的 [[链接]] 时的可读提示 */
  onMissingWiki?: (title: string) => void;
};

export default function Markdown({ content, resolveWiki, onOpenWiki, onMissingWiki }: MarkdownProps) {
  const remarkPlugins = useMemo(
    () => (resolveWiki ? [remarkGfm, remarkWikiLinks] : [remarkGfm]),
    [resolveWiki],
  );
  const components: Components = useMemo(() => {
    if (!resolveWiki) return { pre: PreBlock };
    const ctx: WikiContext = {
      resolveWiki,
      onOpenWiki: onOpenWiki ?? (() => undefined),
      onMissingWiki: onMissingWiki ?? (() => undefined),
    };
    return {
      pre: PreBlock,
      span: (spanProps) => <WikiSpan props={spanProps} ctx={ctx} />,
    };
  }, [resolveWiki, onOpenWiki, onMissingWiki]);

  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={remarkPlugins} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
