import { useEffect, useMemo, useState } from "react";
import { AlertCircle, Check, FolderDown, FolderOpen, Loader2, X } from "lucide-react";
import {
  ApiError,
  exportMarkdown,
  listDocuments,
  listNotes,
  pickDirectory,
  type DocumentItem,
  type ExportMarkdownOut,
  type Note,
} from "../api";
import { cx } from "../utils";

/** 条目 17：全库 Markdown 导出弹窗——按分类把笔记与文档原文导出成本机文件夹里的 Markdown。
 *  追加（任务清单 17）：导出范围可选——全部 / 按分类 / 按标签 / 按日期 / 自选条目；
 *  范围字段不传 = 全库，与旧请求逐字一致。 */

const inputCls =
  "h-9 w-full rounded-lg border border-line bg-fog/[0.04] px-3 text-[13.5px] text-ink outline-none transition-colors placeholder:text-faint focus:border-edge";

/** 一个「包含 …」复选框：勾选块沿用数据服务弹窗的样式。 */
function IncludeCheck({
  checked,
  title,
  hint,
  onToggle,
}: {
  checked: boolean;
  title: string;
  hint: string;
  onToggle(): void;
}) {
  return (
    <button type="button" onClick={onToggle} className="flex flex-1 items-start gap-2 text-left">
      <span
        className={cx(
          "mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border",
          checked ? "border-brand bg-brand text-canvas" : "border-line",
        )}
      >
        {checked ? <Check size={10} strokeWidth={3} /> : null}
      </span>
      <span className="min-w-0">
        <span className="block text-[12.5px] text-body">{title}</span>
        <span className="mt-0.5 block text-[11px] leading-4 text-faint">{hint}</span>
      </span>
    </button>
  );
}

/** 范围选择里的一行复选（分类 / 标签）：勾选块与 IncludeCheck 同款。 */
function CheckRow({ checked, label, onToggle }: { checked: boolean; label: string; onToggle(): void }) {
  return (
    <button type="button" onClick={onToggle} className="flex min-w-0 items-center gap-2 text-left">
      <span
        className={cx(
          "flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border",
          checked ? "border-brand bg-brand text-canvas" : "border-line",
        )}
      >
        {checked ? <Check size={10} strokeWidth={3} /> : null}
      </span>
      <span className="min-w-0 truncate text-[12px] text-body">{label}</span>
    </button>
  );
}

type ScopeMode = "all" | "category" | "tag" | "date" | "pick";

const SCOPE_MODES: { value: ScopeMode; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "category", label: "按分类" },
  { value: "tag", label: "按标签" },
  { value: "date", label: "按日期" },
  { value: "pick", label: "自选条目" },
];

export default function ExportDialog({ onClose }: { onClose(): void }) {
  const [value, setValue] = useState("");
  const [includeNotes, setIncludeNotes] = useState(true);
  const [includeDocuments, setIncludeDocuments] = useState(true);
  const [picking, setPicking] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ExportMarkdownOut | null>(null);

  // 导出范围：模式与各模式的已选内容
  const [mode, setMode] = useState<ScopeMode>("all");
  const [catalog, setCatalog] = useState<{ notes: Note[]; documents: DocumentItem[] } | null>(null);
  const [catalogWarn, setCatalogWarn] = useState("");
  const [selCats, setSelCats] = useState<string[]>([]);
  const [selTags, setSelTags] = useState<string[]>([]);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [pickedNotes, setPickedNotes] = useState<ReadonlySet<number>>(new Set());
  const [pickedDocs, setPickedDocs] = useState<ReadonlySet<number>>(new Set());
  // 点击执行时定格的范围描述与包含标志：结果行不随后续改动变化
  const [scopeText, setScopeText] = useState("");
  const [ranIncludeNotes, setRanIncludeNotes] = useState(true);
  const [ranIncludeDocuments, setRanIncludeDocuments] = useState(true);

  // 弹窗打开时拉一次笔记 + 文档清单，供分类 / 标签 / 自选条目用；失败不拦「全部」与手填日期
  useEffect(() => {
    let alive = true;
    Promise.all([listNotes(), listDocuments()])
      .then(([notes, documents]) => {
        if (alive) setCatalog({ notes, documents });
      })
      .catch((err) => {
        if (!alive) return;
        const reason = err instanceof ApiError ? err.message : "请稍后重试";
        setCatalogWarn(`清单加载失败：${reason}；仍可导出全部或手填日期。`);
      });
    return () => {
      alive = false;
    };
  }, []);

  // 分类选项：笔记 + 文档 category 去重，空串归「未分类」；一个分类都没有也至少给「未分类」
  const categoryOptions = useMemo(() => {
    if (!catalog) return [];
    const set = new Set<string>();
    catalog.notes.forEach((item) => set.add(item.category || ""));
    catalog.documents.forEach((item) => set.add(item.category || ""));
    if (!set.size) set.add("");
    return Array.from(set)
      .map((cat) => ({ value: cat, label: cat || "未分类" }))
      .sort((a, b) => a.label.localeCompare(b.label, "zh"));
  }, [catalog]);

  // 标签选项：笔记 + 文档 tags 并集去重排序
  const tagOptions = useMemo(() => {
    if (!catalog) return [];
    const set = new Set<string>();
    catalog.notes.forEach((item) => (item.tags || []).forEach((tag) => tag && set.add(tag)));
    catalog.documents.forEach((item) => (item.tags || []).forEach((tag) => tag && set.add(tag)));
    return Array.from(set).sort((a, b) => a.localeCompare(b, "zh"));
  }, [catalog]);

  const target = value.trim();
  const scopeReady =
    mode === "all"
      ? includeNotes || includeDocuments
      : mode === "category"
        ? selCats.length > 0
        : mode === "tag"
          ? selTags.length > 0
          : mode === "date"
            ? Boolean(dateFrom || dateTo)
            : pickedNotes.size > 0 || pickedDocs.size > 0;
  const canRun = scopeReady && Boolean(target) && !running;

  async function choose() {
    setPicking(true);
    setNote("");
    setError("");
    try {
      const picked = await pickDirectory(target);
      if (!picked.available) {
        // 本机没有可用的图形对话框：如实说明，用户手填即可
        setNote(picked.detail || "打不开系统选择框，请手动填写路径。");
      } else if (picked.path) {
        setValue(picked.path);
      }
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "打不开系统选择框，请手动填写路径。");
    } finally {
      setPicking(false);
    }
  }

  function toggleCat(cat: string) {
    setSelCats((list) => (list.includes(cat) ? list.filter((item) => item !== cat) : [...list, cat]));
  }

  function toggleTag(tag: string) {
    setSelTags((list) => (list.includes(tag) ? list.filter((item) => item !== tag) : [...list, tag]));
  }

  function toggleNotePick(id: number) {
    setPickedNotes((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleDocPick(id: number) {
    setPickedDocs((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  /** 本次执行的范围描述（点击执行时定格存 state，结果行不再变化）。 */
  function describeScope(): string {
    if (mode === "category") {
      const labels = selCats.map((cat) => cat || "未分类").sort((a, b) => a.localeCompare(b, "zh"));
      return `分类 ${labels.join("、")}`;
    }
    if (mode === "tag") {
      return `标签 ${[...selTags].sort((a, b) => a.localeCompare(b, "zh")).join("、")}`;
    }
    if (mode === "date") {
      if (dateFrom && dateTo) return `日期 ${dateFrom} ~ ${dateTo}`;
      if (dateFrom) return `日期 ${dateFrom} 起`;
      return `日期 ${dateTo} 止`;
    }
    if (mode === "pick") {
      return `自选（笔记 ${pickedNotes.size} · 文档 ${pickedDocs.size}）`;
    }
    return "全部";
  }

  async function run() {
    if (!target) {
      setError("请填写导出位置；不知道填什么就点「选择文件夹」。");
      return;
    }
    if (!scopeReady) return;
    setError("");
    // 自选条目模式下「包含 …」由各组是否有勾选决定；其余模式沿用可见的两个勾选
    const includeNotesNow = mode === "pick" ? pickedNotes.size > 0 : includeNotes;
    const includeDocumentsNow = mode === "pick" ? pickedDocs.size > 0 : includeDocuments;
    setRanIncludeNotes(includeNotesNow);
    setRanIncludeDocuments(includeDocumentsNow);
    setScopeText(describeScope());
    setRunning(true);
    try {
      const body: Parameters<typeof exportMarkdown>[0] = {
        path: target,
        include_notes: includeNotesNow,
        include_documents: includeDocumentsNow,
      };
      if (mode === "category") {
        body.categories = [...selCats];
      } else if (mode === "tag") {
        body.tags = [...selTags];
      } else if (mode === "date") {
        if (dateFrom) body.date_from = dateFrom;
        if (dateTo) body.date_to = dateTo;
      } else if (mode === "pick") {
        if (pickedNotes.size) body.note_ids = [...pickedNotes];
        if (pickedDocs.size) body.document_ids = [...pickedDocs];
      }
      const data = await exportMarkdown(body);
      setResult(data);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "导出失败，请稍后重试。");
    } finally {
      setRunning(false);
    }
  }

  const showNotesCount = mode === "pick" ? ranIncludeNotes : includeNotes;
  const showDocsCount = mode === "pick" ? ranIncludeDocuments : includeDocuments;
  const countText = result
    ? [
        showNotesCount ? `笔记 ${result.notes_exported} 篇` : "",
        showDocsCount ? `文档 ${result.documents_exported} 篇` : "",
      ]
        .filter(Boolean)
        .join(" ｜ ")
    : "";

  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-veil animate-fade-in" onClick={onClose}>
      <div className="mx-auto mt-16 w-full max-w-xl px-4 pb-12" onClick={(event) => event.stopPropagation()}>
        <div className="rounded-2xl border border-line bg-panel p-5 shadow-pop animate-fade-up">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-semibold text-ink">导出 · Markdown</h2>
              <p className="mt-0.5 text-[11.5px] leading-4 text-faint">
                把笔记与文档原文按分类导出为本机文件夹里的 Markdown，便于备份与交接。
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
            <div className="mb-1.5 px-0.5">
              <span className="text-[12.5px] text-body">导出到</span>
            </div>
            <div className="flex items-center gap-2">
              <input
                value={value}
                onChange={(event) => setValue(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void run();
                }}
                placeholder="例如 D:\zhilan\backup"
                spellCheck={false}
                className={cx(inputCls, "font-mono text-[12.5px]")}
              />
              <button
                type="button"
                onClick={() => void choose()}
                disabled={picking}
                className="flex h-9 shrink-0 items-center gap-1.5 rounded-lg bg-fog/[0.07] px-3 text-[12.5px] text-body transition-colors hover:bg-fog/[0.12] hover:text-ink disabled:opacity-60"
              >
                {picking ? (
                  <Loader2 size={13} className="animate-spin" strokeWidth={2} />
                ) : (
                  <FolderOpen size={13} strokeWidth={1.75} />
                )}
                {picking ? "选择中…" : "选择文件夹…"}
              </button>
            </div>

            {mode !== "pick" ? (
              <div className="mt-3 flex items-start gap-3 border-t border-line pt-3">
                <IncludeCheck
                  checked={includeNotes}
                  title="包含笔记"
                  hint="标题做文件名，frontmatter 带分类 / 标签 / 日期"
                  onToggle={() => setIncludeNotes((on) => !on)}
                />
                <IncludeCheck
                  checked={includeDocuments}
                  title="包含文档"
                  hint="优先导出原文文本，frontmatter 记录来源文件"
                  onToggle={() => setIncludeDocuments((on) => !on)}
                />
              </div>
            ) : null}

            <div className="mt-3 border-t border-line pt-3">
              <div className="mb-2 px-0.5">
                <span className="text-[12.5px] text-body">导出范围</span>
              </div>
              <div className="flex flex-wrap items-center gap-1.5">
                {SCOPE_MODES.map((item) => (
                  <button
                    key={item.value}
                    type="button"
                    onClick={() => setMode(item.value)}
                    className={cx(
                      "rounded-full border px-3 py-1 text-[12px] transition-colors",
                      mode === item.value
                        ? "border-brand/40 bg-brand/10 text-brand"
                        : "border-line text-mute hover:bg-fog/5 hover:text-body",
                    )}
                  >
                    {item.label}
                  </button>
                ))}
              </div>

              {catalogWarn ? (
                <p className="mt-2 flex items-start gap-1.5 text-[11.5px] leading-4 text-warn">
                  <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                  {catalogWarn}
                </p>
              ) : null}

              {mode === "category" ? (
                <div className="mt-2.5 grid grid-cols-2 gap-x-4 gap-y-2">
                  {categoryOptions.map((opt) => (
                    <CheckRow
                      key={opt.value || "未分类"}
                      checked={selCats.includes(opt.value)}
                      label={opt.label}
                      onToggle={() => toggleCat(opt.value)}
                    />
                  ))}
                </div>
              ) : null}

              {mode === "tag" ? (
                !catalog ? null : tagOptions.length ? (
                  <div className="mt-2.5 flex flex-wrap gap-x-4 gap-y-2">
                    {tagOptions.map((tag) => (
                      <CheckRow key={tag} checked={selTags.includes(tag)} label={tag} onToggle={() => toggleTag(tag)} />
                    ))}
                  </div>
                ) : (
                  <p className="mt-2 px-0.5 text-[11.5px] text-faint">库内暂无标签</p>
                )
              ) : null}

              {mode === "date" ? (
                <div className="mt-2.5 flex items-center gap-2">
                  <span className="shrink-0 text-[11.5px] text-faint">从</span>
                  <input
                    type="date"
                    value={dateFrom}
                    onChange={(event) => setDateFrom(event.target.value)}
                    className={cx(inputCls, "flex-1")}
                  />
                  <span className="shrink-0 text-[11.5px] text-faint">到</span>
                  <input
                    type="date"
                    value={dateTo}
                    onChange={(event) => setDateTo(event.target.value)}
                    className={cx(inputCls, "flex-1")}
                  />
                </div>
              ) : null}

              {mode === "pick" && catalog ? (
                <div className="mt-2.5 space-y-3">
                  <div>
                    <div className="flex items-center justify-between px-0.5">
                      <span className="text-[12px] text-body">
                        笔记 · 已选 {pickedNotes.size}/{catalog.notes.length}
                      </span>
                      <span className="flex items-center gap-2.5">
                        <button
                          type="button"
                          onClick={() => setPickedNotes(new Set(catalog.notes.map((item) => item.id)))}
                          className="text-[11px] text-mute transition-colors hover:text-brand"
                        >
                          全选
                        </button>
                        <button
                          type="button"
                          onClick={() => setPickedNotes(new Set())}
                          className="text-[11px] text-mute transition-colors hover:text-brand"
                        >
                          清空
                        </button>
                      </span>
                    </div>
                    <div className="mt-1.5 max-h-40 overflow-auto rounded-lg border border-line">
                      {catalog.notes.map((item) => (
                        <button
                          key={item.id}
                          type="button"
                          onClick={() => toggleNotePick(item.id)}
                          className="flex w-full items-center gap-2 border-b border-line/60 px-2 py-1.5 text-left last:border-b-0 hover:bg-fog/5"
                        >
                          <span
                            className={cx(
                              "flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border",
                              pickedNotes.has(item.id) ? "border-brand bg-brand text-canvas" : "border-line",
                            )}
                          >
                            {pickedNotes.has(item.id) ? <Check size={10} strokeWidth={3} /> : null}
                          </span>
                          <span className="min-w-0 flex-1 truncate text-[12px] text-body">{item.title}</span>
                          <span className="shrink-0 text-[11px] text-faint">{item.category || "未分类"}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                  <div>
                    <div className="flex items-center justify-between px-0.5">
                      <span className="text-[12px] text-body">
                        文档 · 已选 {pickedDocs.size}/{catalog.documents.length}
                      </span>
                      <span className="flex items-center gap-2.5">
                        <button
                          type="button"
                          onClick={() => setPickedDocs(new Set(catalog.documents.map((item) => item.id)))}
                          className="text-[11px] text-mute transition-colors hover:text-brand"
                        >
                          全选
                        </button>
                        <button
                          type="button"
                          onClick={() => setPickedDocs(new Set())}
                          className="text-[11px] text-mute transition-colors hover:text-brand"
                        >
                          清空
                        </button>
                      </span>
                    </div>
                    <div className="mt-1.5 max-h-40 overflow-auto rounded-lg border border-line">
                      {catalog.documents.map((item) => (
                        <button
                          key={item.id}
                          type="button"
                          onClick={() => toggleDocPick(item.id)}
                          className="flex w-full items-center gap-2 border-b border-line/60 px-2 py-1.5 text-left last:border-b-0 hover:bg-fog/5"
                        >
                          <span
                            className={cx(
                              "flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border",
                              pickedDocs.has(item.id) ? "border-brand bg-brand text-canvas" : "border-line",
                            )}
                          >
                            {pickedDocs.has(item.id) ? <Check size={10} strokeWidth={3} /> : null}
                          </span>
                          <span className="min-w-0 flex-1 truncate text-[12px] text-body">{item.filename}</span>
                          <span className="shrink-0 text-[11px] text-faint">{item.category || "未分类"}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
              ) : null}
            </div>

            {note ? (
              <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-warn">
                <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                {note}
              </p>
            ) : null}

            {error ? (
              <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-4 text-rose">
                <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                {error}
              </p>
            ) : null}

            {result ? (
              <div className="mt-3 border-t border-line pt-3">
                <p className="flex items-center gap-1.5 text-[12.5px] text-ok">
                  <Check size={13} strokeWidth={2} />
                  导出完成（{countText}）
                </p>
                {scopeText ? (
                  <p className="mt-1.5 px-0.5 text-[11.5px] leading-4 text-faint">范围：{scopeText}</p>
                ) : null}
                <p className="mt-1.5 break-all px-0.5 text-[11px] leading-4 text-faint">
                  输出目录：<span className="font-mono text-[11px] text-mute">{result.output_dir}</span>
                </p>
                {result.errors.length ? (
                  <div className="mt-2">
                    <p className="flex items-start gap-1.5 text-[11.5px] leading-4 text-warn">
                      <AlertCircle size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
                      有 {result.errors.length} 项没有导出成功：
                    </p>
                    <ul className="mt-1 space-y-1 pl-4">
                      {result.errors.map((item, index) => (
                        <li key={index} className="text-[11.5px] leading-4 text-warn">
                          {item}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>

          <p className="mt-3 flex items-start gap-1.5 text-[11px] leading-4 text-faint">
            <FolderDown size={12} className="mt-0.5 shrink-0" strokeWidth={1.75} />
            会在所选目录下新建「知澜导出-日期-时间」文件夹，不会改动原有文件。
          </p>

          <div className="mt-5 flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-full bg-fog/[0.08] px-4 py-1.5 text-[13px] text-body transition-colors hover:bg-fog/[0.12]"
            >
              {result ? "完成" : "取消"}
            </button>
            <button
              type="button"
              disabled={!canRun}
              onClick={() => void run()}
              className="flex items-center gap-1.5 rounded-full bg-send px-4 py-1.5 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover disabled:opacity-60"
            >
              {running ? <Loader2 size={13} className="animate-spin" strokeWidth={2} /> : null}
              {running ? "导出中…" : "执行导出"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
