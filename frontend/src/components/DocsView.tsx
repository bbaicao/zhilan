import { useEffect, useRef, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  CloudUpload,
  FileSearch,
  FileText,
  FolderInput,
  Library,
  Link2,
  Loader2,
  NotebookPen,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
import { createKnowledgeBase, listKnowledgeBases, type DocumentItem, type KnowledgeBaseItem } from "../api";
import { cx, fileSuffix, formatTime, statusLabel } from "../utils";
import CleanReportView from "./CleanReportView";

type DocsViewProps = {
  docs: DocumentItem[];
  dragging: boolean;
  uploading: boolean;
  reindexing: boolean;
  section: DocsSection;
  cleanHint?: string;
  /** 外部受控入口（笔记预览里的 [[文档链接]]）：非空时直接进入该文档的清洗报告详情 */
  initialDocId?: number | null;
  /** 外部入口的「返回」：清掉 App 侧受控状态，回到文档列表 */
  onInitialDocClosed?(): void;
  onDragState(dragging: boolean): void;
  /** opts.generateNotes = 上传区勾了「上传后用 AI 生成笔记」 */
  onFiles(files: FileList | File[], opts?: { generateNotes?: boolean }): void;
  /** 行内「生成笔记」：打开 AI 生成笔记弹窗（App 未传则不显示入口） */
  onGenerateNote?(doc: { id: number; filename: string }): void;
  /** 条目 39：打开「导入文件夹」弹窗 */
  onImportFolder(): void;
  onDelete(id: number, filename: string): void;
  /** 条目 41：批量删除。resolve(true)=确认并执行完（含部分失败），false=用户取消确认 */
  onBatchDelete(ids: number[], names: string[]): Promise<boolean>;
  /** 批量删除进行中：按钮禁用防重复触发 */
  batchDeleting?: boolean;
  onReindex(): void;
  onSection(section: DocsSection): void;
  /** 条目 16：在图谱中查看该文档 */
  onShowInGraph?(id: number): void;
};

export type DocsSection = "home" | "local" | "import" | "external";

/** 提示文案里的格式清单（**43 token = 44 个后缀 − `.markdown`**）。
 *
 *  为什么与 `accept=` 分开：`accept=` 是**逐字后缀**（含别名 `.markdown`），
 *  而用户可见文案历来只写扩展名**主形态**（`.md`）—— 与后端 `SUPPORTED_FORMATS_HINT` 同一口径。
 *  ⚠️ 三段与后端 `SUPPORTED_FORMATS_HINT` **必须逐字一致**，由
 *  `backend/tests/pipeline/test_frontend_whitelist_t3_2.py` 钉住（A13b 文案腿）。
 */
const DOC_FORMAT_HINT =
  "支持 txt / md / pdf / docx / xlsx / xls / pptx / csv / jpg / jpeg / png / bmp / tiff / webp" +
  " / py / js / ts / java / go / rs / c / h / cpp / cs / rb / php / sh / sql / kt / swift" +
  " / json / yaml / yml / toml / ini / cfg / xml" +
  " / jsx / tsx / vue / css / scss / html";

// 条目 36：文件类型着色直接引用主题变量，浅色下自动换成加深版
const TYPE_COLOR: Record<string, string> = {
  ".txt": "var(--zh-info)",
  ".md": "var(--zh-brand)",
  ".markdown": "var(--zh-brand)",
  ".pdf": "var(--zh-rose)",
  ".docx": "var(--zh-azure)",
  ".xlsx": "var(--zh-moss)",
  ".xls": "var(--zh-moss)",
  ".pptx": "var(--zh-clay)",
  ".csv": "var(--zh-olive)",
};

function StatusBadge({ doc }: { doc: DocumentItem }) {
  if (doc.status === "processing") {
    return (
      <span className="flex items-center gap-1 text-warn">
        <Loader2 size={11} className="animate-spin" strokeWidth={2} />
        处理中
      </span>
    );
  }
  if (doc.status === "failed") {
    return (
      <span className="flex items-center gap-1.5 text-rose" title={doc.message || "解析失败"}>
        <span className="h-1.5 w-1.5 rounded-full bg-rose" />
        失败
      </span>
    );
  }
  if (!doc.vectorized) {
    return (
      <span
        className="flex items-center gap-1.5 text-warn"
        title={doc.vector_error || "该文档只有关键词索引，向量缺失，检索会漏召"}
      >
        <span className="h-1.5 w-1.5 rounded-full bg-warn" />
        未向量化
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1.5 text-ok">
      <span className="h-1.5 w-1.5 rounded-full bg-ok" />
      {statusLabel(doc.status)}
    </span>
  );
}

function KnowledgeBaseRow({
  icon,
  title,
  meta,
  onClick,
}: {
  icon: React.ReactNode;
  title: string;
  meta: string;
  onClick(): void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="mb-1.5 flex w-full items-center gap-3 rounded-xl bg-fog/[0.03] px-3 py-2.5 text-left transition-colors hover:bg-fog/[0.06]"
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-fog/[0.05]">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13.5px] text-ink">{title}</div>
        <div className="mt-0.5 text-[11.5px] text-mute">{meta}</div>
      </div>
      <ChevronRight size={16} className="shrink-0 text-faint" strokeWidth={1.75} />
    </button>
  );
}

const PENDING: Record<
  "import" | "external",
  { title: string; icon: typeof CloudUpload; iconClass: string; headline: string; body: string }
> = {
  import: {
    title: "外部数据库",
    icon: CloudUpload,
    iconClass: "text-brand",
    headline: "还不能导入",
    body: "以后可以从本机文件夹，或另一份知澜知识库整份引进来。导入后会出现在这里，也能用来提问和引用。现在请先用本机数据库。",
  },
  external: {
    title: "在线数据库",
    icon: Link2,
    iconClass: "text-iris",
    headline: "还不能连接",
    body: "以后可以填写网址或本机路径，连上后只检索、只引用，不会改对方文件。现在请先用本机数据库。",
  },
};

function PendingSource({
  section,
  items,
  onBack,
  onLocal,
  onCreated,
}: {
  section: "import" | "external";
  items: KnowledgeBaseItem[];
  onBack(): void;
  onLocal(): void;
  onCreated(item: KnowledgeBaseItem): void;
}) {
  const spec = PENDING[section];
  const kind = section === "import" ? "imported" : "external";
  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function onSubmit() {
    const trimmedName = name.trim();
    const trimmedTarget = target.trim();
    if (!trimmedName || !trimmedTarget) {
      setError("名称和地址都要填");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const created = await createKnowledgeBase({
        name: trimmedName,
        kind,
        path_or_url: trimmedTarget,
      });
      setName("");
      setTarget("");
      onCreated(created);
    } catch (err) {
      setError(err instanceof Error ? err.message : "登记失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={onBack}
        className="mb-4 flex items-center gap-0.5 text-[18px] font-semibold text-ink"
      >
        <ChevronLeft size={20} strokeWidth={1.75} />
        {spec.title}
      </button>
      <div className="min-h-0 flex-1 overflow-auto scrollbar-thin">
        {items.length === 0 ? (
          <p className="mb-4 text-[13px] leading-6 text-mute">{spec.body}</p>
        ) : (
          items.map((item) => (
            <div key={item.id} className="mb-1.5 rounded-xl bg-fog/[0.03] px-3 py-2.5">
              <div className="truncate text-[13.5px] text-ink">{item.name}</div>
              <div className="mt-0.5 truncate text-[11.5px] text-mute">{item.path_or_url || "已登记"}</div>
            </div>
          ))
        )}
        <div className="mt-4 rounded-2xl bg-fog/[0.03] p-3">
          <p className="text-[12.5px] text-mute">{section === "import" ? "登记一份导入库（本机路径或集合名）" : "登记一个只读地址，提问时会一起检索"}</p>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="名称"
            className="mt-3 w-full rounded-lg bg-fog/[0.04] px-3 py-2 text-[13px] text-ink outline-none"
          />
          <input
            value={target}
            onChange={(event) => setTarget(event.target.value)}
            placeholder={section === "import" ? "路径或集合" : "https://.../search"}
            className="mt-2 w-full rounded-lg bg-fog/[0.04] px-3 py-2 text-[13px] text-ink outline-none"
          />
          {error ? <p className="mt-2 text-[12px] text-rose">{error}</p> : null}
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              disabled={saving}
              onClick={() => void onSubmit()}
              className="rounded-full bg-send px-4 py-2 text-[13px] font-medium text-canvas disabled:opacity-60"
            >
              {saving ? "正在登记…" : "登记"}
            </button>
            <button type="button" onClick={onLocal} className="rounded-full bg-fog/[0.06] px-4 py-2 text-[13px] text-ink">
              去本机数据库
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

export default function DocsView(props: DocsViewProps) {
  const {
    docs,
    dragging,
    uploading,
    reindexing,
    section,
    cleanHint,
    initialDocId,
    onInitialDocClosed,
    onDragState,
    onFiles,
    onGenerateNote,
    onImportFolder,
    onDelete,
    onBatchDelete,
    batchDeleting,
    onReindex,
    onSection,
    onShowInGraph,
  } = props;
  const fileRef = useRef<HTMLInputElement>(null);
  const [reportDoc, setReportDoc] = useState<{ id: number; filename: string } | null>(null);
  /** 上传区复选框：本次上传成功的文档依次进入 AI 生成笔记弹窗 */
  const [autoNote, setAutoNote] = useState(false);
  const [kbs, setKbs] = useState<KnowledgeBaseItem[]>([]);
  /** 条目 41：批量删除的行多选。列表刷新后自动清掉已不存在的 id，避免计数失真 */
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const missing = docs.filter((doc) => doc.status === "ready" && !doc.vectorized).length;

  useEffect(() => {
    void listKnowledgeBases()
      .then(setKbs)
      .catch(() => setKbs([]));
  }, []);

  useEffect(() => {
    setSelected((prev) => {
      const alive = new Set(docs.map((doc) => doc.id));
      const next = new Set([...prev].filter((id) => alive.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [docs]);

  function toggleSelected(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const allSelected = docs.length > 0 && docs.every((doc) => selected.has(doc.id));

  function toggleAll() {
    setSelected(allSelected ? new Set() : new Set(docs.map((doc) => doc.id)));
  }

  /** 确认与执行都在 App 侧（与单篇删除同一处理人）；返回 true 才清空选择，取消则保留 */
  async function runBatchDelete() {
    if (selected.size === 0 || batchDeleting) return;
    const ids = [...selected];
    const names = docs.filter((doc) => selected.has(doc.id)).map((doc) => doc.filename);
    const ok = await onBatchDelete(ids, names);
    if (ok) setSelected(new Set());
  }

  /** 外部受控入口：与内部点行进入的 reportDoc 互不覆盖（内部优先）。文档还没载入时也先进入详情。 */
  const externalDoc =
    typeof initialDocId === "number" && !reportDoc
      ? { id: initialDocId, filename: docs.find((item) => item.id === initialDocId)?.filename ?? "" }
      : null;
  const activeReport = reportDoc ?? externalDoc;

  if (activeReport) {
    return (
      <CleanReportView
        documentId={activeReport.id}
        filename={activeReport.filename}
        onBack={() => {
          if (reportDoc) setReportDoc(null);
          else onInitialDocClosed?.();
        }}
        onGenerateNote={
          // 外部受控入口（externalDoc）可能是外部库文档，只读，不提供生成入口
          reportDoc && onGenerateNote
            ? () => onGenerateNote({ id: activeReport.id, filename: activeReport.filename })
            : undefined
        }
        onShowInGraph={onShowInGraph}
      />
    );
  }

  return (
    <div className="mx-auto flex h-full min-h-0 w-full max-w-[880px] flex-col px-6 py-6">
      <input
        ref={fileRef}
        type="file"
        accept=".txt,.md,.markdown,.pdf,.docx,.xlsx,.xls,.pptx,.csv,.jpg,.jpeg,.png,.bmp,.tiff,.webp,.py,.js,.ts,.java,.go,.rs,.c,.h,.cpp,.cs,.rb,.php,.sh,.sql,.kt,.swift,.json,.yaml,.yml,.toml,.ini,.cfg,.xml,.jsx,.tsx,.vue,.css,.scss,.html"
        multiple
        className="hidden"
        onChange={(event) => {
          onFiles(event.target.files || [], { generateNotes: autoNote });
          event.target.value = "";
        }}
      />

      {section === "local" ? (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => onSection("home")}
              className="mr-2 flex items-center gap-0.5 text-[18px] font-semibold text-ink"
            >
              <ChevronLeft size={20} strokeWidth={1.75} />
              本机数据库
            </button>
            <button
              type="button"
              disabled={uploading}
              onClick={() => fileRef.current?.click()}
              className="flex items-center gap-1.5 rounded-full bg-send px-4 py-2 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover disabled:opacity-60"
            >
              {uploading ? <Loader2 size={14} className="animate-spin" strokeWidth={2} /> : <Upload size={14} strokeWidth={1.75} />}
              {uploading ? "正在上传…" : "上传文件"}
            </button>
            <button
              type="button"
              onClick={onImportFolder}
              className="flex items-center gap-1.5 rounded-full bg-send px-4 py-2 text-[13px] font-medium text-canvas transition-colors hover:bg-send-hover disabled:opacity-60"
            >
              <FolderInput size={14} strokeWidth={1.75} />
              导入文件夹
            </button>
            <label
              title="勾选后，本次上传成功的每个文档会依次打开 AI 生成笔记弹窗"
              className="flex cursor-pointer select-none items-center gap-1.5 rounded-full bg-fog/[0.04] px-3 py-2 text-[12.5px] text-body transition-colors hover:bg-fog/[0.08]"
            >
              <input
                type="checkbox"
                checked={autoNote}
                onChange={(event) => setAutoNote(event.target.checked)}
                className="h-3.5 w-3.5 accent-brand"
              />
              上传后用 AI 生成笔记
            </label>
            <button
              type="button"
              disabled={reindexing || uploading}
              onClick={onReindex}
              title="重新生成向量、补齐漏写的向量并清理孤儿向量"
              className="flex items-center gap-1.5 rounded-full bg-fog/[0.06] px-4 py-2 text-[13px] font-medium text-ink transition-colors hover:bg-fog/[0.12] disabled:opacity-60"
            >
              {reindexing ? (
                <Loader2 size={14} className="animate-spin" strokeWidth={2} />
              ) : (
                <RefreshCw size={14} strokeWidth={1.75} />
              )}
              {reindexing ? "正在重建…" : "重建索引"}
            </button>
            {missing > 0 ? (
              <span className="rounded-full bg-warn/15 px-2.5 py-1 text-[11.5px] text-warn">
                {missing} 篇未向量化
              </span>
            ) : null}
          </div>

          {cleanHint ? (
            <div className="mb-3 flex items-start gap-2 rounded-xl bg-warn/10 px-3 py-2.5">
              <span className="mt-0.5 h-1.5 w-1.5 shrink-0 rounded-full bg-warn" />
              <p className="text-[12.5px] leading-5 text-warn">
                {cleanHint}
                <span className="text-mute">
                  {" "}
                  点「重建索引」会按当前规则重新清洗这些文档的正文，并重算向量。
                </span>
              </p>
            </div>
          ) : null}

          <div className="min-h-0 flex-1 overflow-auto scrollbar-thin">
            <div
              onDragOver={(event) => {
                event.preventDefault();
                onDragState(true);
              }}
              onDragLeave={() => onDragState(false)}
              onDrop={(event) => {
                event.preventDefault();
                onDragState(false);
                onFiles(event.dataTransfer.files, { generateNotes: autoNote });
              }}
              className={cx(
                "mb-3 flex items-center justify-center gap-3 rounded-2xl border border-dashed px-4 py-5 transition-colors",
                dragging ? "border-brand bg-brand-soft" : "border-line bg-fog/[0.02]",
              )}
            >
              <CloudUpload size={20} className={dragging ? "text-brand" : "text-faint"} strokeWidth={1.25} />
              <div>
                <p className="text-[13px] text-ink">把文档拖到这里，即入本机数据库</p>
                <p className="mt-1 text-[11.5px] text-faint">{DOC_FORMAT_HINT}，单文件 ≤ 20MB</p>
                <p className="mt-0.5 text-[11.5px] text-mute">单个文件不超过 20MB</p>
              </div>
            </div>

            {docs.length > 0 ? (
              <div className="mb-1.5 flex items-center gap-3 rounded-xl bg-fog/[0.02] px-3 py-1.5">
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={toggleAll}
                  aria-label="全选文档"
                  className="h-3.5 w-3.5 accent-brand"
                />
                <span className="text-[11.5px] text-faint">全选</span>
                {selected.size > 0 ? (
                  <>
                    <span className="text-[12px] text-body">已选 {selected.size} 篇</span>
                    <div className="flex-1" />
                    <button
                      type="button"
                      disabled={batchDeleting}
                      onClick={() => void runBatchDelete()}
                      className="rounded-full px-3 py-1 text-[12px] text-rose transition-colors hover:bg-fog/[0.06] disabled:opacity-60"
                    >
                      {batchDeleting ? "正在删除…" : "批量删除"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setSelected(new Set())}
                      className="rounded-full px-2 py-1 text-[12px] text-mute transition-colors hover:text-ink"
                    >
                      清除选择
                    </button>
                  </>
                ) : (
                  <div className="flex-1" />
                )}
              </div>
            ) : null}

            {docs.length === 0 ? (
              <p className="py-4 text-center text-[12.5px] text-faint">还没有文档，上传后 AI 就能基于它回答。</p>
            ) : (
              docs.map((doc) => {
                const color = TYPE_COLOR[fileSuffix(doc.filename)] || "var(--zh-mute)";
                return (
                  <div
                    key={doc.id}
                    className="group mb-1.5 flex items-center gap-3 rounded-xl bg-fog/[0.03] px-3 py-2.5 transition-colors hover:bg-fog/[0.06]"
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(doc.id)}
                      onChange={() => toggleSelected(doc.id)}
                      onClick={(event) => event.stopPropagation()}
                      aria-label={`选中 ${doc.filename}`}
                      className="h-3.5 w-3.5 shrink-0 accent-brand"
                    />
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-fog/[0.05]">
                      <FileText size={15} color={color} strokeWidth={1.75} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[13.5px] text-ink">{doc.filename}</div>
                      <div className="mt-0.5 flex items-center gap-2 text-[11.5px] text-mute">
                        <StatusBadge doc={doc} />
                        <span className="text-faint">·</span>
                        <span>{doc.chunk_count} 段</span>
                        <span className="text-faint">·</span>
                        <span>{formatTime(doc.created_at)}</span>
                      </div>
                    </div>
                    {onGenerateNote ? (
                      <button
                        type="button"
                        onClick={() => onGenerateNote({ id: doc.id, filename: doc.filename })}
                        title="用 AI 生成笔记"
                        aria-label="生成笔记"
                        className="hidden rounded-md p-1.5 text-mute hover:text-brand group-hover:block"
                      >
                        <NotebookPen size={14} strokeWidth={1.75} />
                      </button>
                    ) : null}
                    <button
                      type="button"
                      onClick={() => setReportDoc({ id: doc.id, filename: doc.filename })}
                      title="查看原文与清洗报告"
                      aria-label="查看清洗报告"
                      className="hidden rounded-md p-1.5 text-mute hover:text-info group-hover:block"
                    >
                      <FileSearch size={14} strokeWidth={1.75} />
                    </button>
                    <button
                      type="button"
                      onClick={() => onDelete(doc.id, doc.filename)}
                      aria-label="删除文档"
                      className="hidden rounded-md p-1.5 text-mute hover:text-rose group-hover:block"
                    >
                      <Trash2 size={14} strokeWidth={1.75} />
                    </button>
                  </div>
                );
              })
            )}
          </div>
        </>
      ) : section === "import" || section === "external" ? (
        <PendingSource
          section={section}
          items={kbs.filter((item) => item.kind === (section === "import" ? "imported" : "external"))}
          onBack={() => onSection("home")}
          onLocal={() => onSection("local")}
          onCreated={(item) => setKbs((current) => [...current, item])}
        />
      ) : (
        <>
          <div className="mb-4 flex items-center justify-between gap-3">
            <h1 className="text-[18px] font-semibold text-ink">知识库管理</h1>
          </div>
          <div className="min-h-0 flex-1 overflow-auto scrollbar-thin">
            <KnowledgeBaseRow
              icon={<Library size={15} className="text-info" strokeWidth={1.75} />}
              title="本机数据库"
              meta={`${docs.length} 个文件`}
              onClick={() => onSection("local")}
            />
            <KnowledgeBaseRow
              icon={<CloudUpload size={15} className="text-brand" strokeWidth={1.75} />}
              title="外部数据库"
              meta={
                kbs.filter((item) => item.kind === "imported").length
                  ? `${kbs.filter((item) => item.kind === "imported").length} 个已登记`
                  : "未导入"
              }
              onClick={() => onSection("import")}
            />
            <KnowledgeBaseRow
              icon={<Link2 size={15} className="text-iris" strokeWidth={1.75} />}
              title="在线数据库"
              meta={
                kbs.filter((item) => item.kind === "external").length
                  ? `${kbs.filter((item) => item.kind === "external").length} 个已连接`
                  : "未连接"
              }
              onClick={() => onSection("external")}
            />
          </div>
        </>
      )}
    </div>
  );
}
