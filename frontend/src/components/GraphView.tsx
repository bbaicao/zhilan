import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Crosshair,
  LocateFixed,
  Loader2,
  Network,
  RefreshCw,
  Search,
  Share2,
} from "lucide-react";
import { ApiError, getGraph, type GraphData, type GraphEdge, type GraphNode } from "../api";
import { THEME_CHANGE_EVENT } from "../theme";

export type GraphFocus = { type: "note" | "document"; id: number } | null;

type GraphViewProps = {
  /** 从笔记 / 文档详情「在图谱中查看」带过来的聚焦目标 */
  focus: GraphFocus;
  onOpenNote(id: number): void;
  onOpenDocument(id: number): void;
  onNotify(message: string): void;
  onClearFocus(): void;
};

type SimNode = GraphNode & {
  x: number;
  y: number;
  vx: number;
  vy: number;
  pinned: boolean;
};

/** 边类型开关：三种边各自可关；`similar` 默认开（内容相似边，非事实关系）。 */
type EdgeKinds = { wiki: boolean; source: boolean; similar: boolean };

const EDGE_KINDS_DEFAULT: EdgeKinds = { wiki: true, source: true, similar: true };

const EDGE_KIND_ITEMS: { key: keyof EdgeKinds; label: string }[] = [
  { key: "wiki", label: "双链" },
  { key: "source", label: "来源" },
  { key: "similar", label: "相似边" },
];

/** 未知 kind 一律保留：后端 kind 只加不改，前端不能把没见过的新边悄悄吃掉。 */
function filterEdges(edges: GraphEdge[], kinds: EdgeKinds): GraphEdge[] {
  return edges.filter((edge) => kinds[edge.kind] !== false);
}

function adjacencyOf(nodes: { id: string }[], edges: GraphEdge[]): Map<string, Set<string>> {
  const byId = new Set(nodes.map((item) => item.id));
  const adjacency = new Map(nodes.map((item) => [item.id, new Set<string>()]));
  for (const edge of edges) {
    if (!byId.has(edge.source) || !byId.has(edge.target)) continue;
    adjacency.get(edge.source)!.add(edge.target);
    adjacency.get(edge.target)!.add(edge.source);
  }
  return adjacency;
}

/** 图数据 + 预建索引：id → 节点。热循环里禁止 Array.find，一律走 Map。
 *  传入的 `edges` 必须是**图例开关过滤后**的边，邻接表跟着同一份边走。 */
type GraphStore = {
  nodes: SimNode[];
  byId: Map<string, SimNode>;
  edges: GraphEdge[];
  adjacency: Map<string, Set<string>>;
};

function makeStore(nodes: SimNode[], edges: GraphEdge[]): GraphStore {
  return {
    nodes,
    byId: new Map(nodes.map((item) => [item.id, item])),
    edges,
    adjacency: adjacencyOf(nodes, edges),
  };
}

type CoordMap = Record<string, { x: number; y: number }>;

const COORD_KEY = "zhilan-graph-coords-v1";
const REPEL = 2200;
const SPRING = 0.035;
const SPRING_LEN = 90;
const CENTER = 0.008;
const DAMPING = 0.86;
const MIN_ZOOM = 0.12;
const MAX_ZOOM = 3.2;
// 斥力截断距离平方：超过后 1/d² 斥力可忽略，直接跳过，省掉大半 O(n²) 计算
const REPEL_CUTOFF = 640;
const REPEL_CUTOFF2 = REPEL_CUTOFF * REPEL_CUTOFF;

/* 条目 36：画布是自绘的，吃不到 CSS 变量，按 data-theme 准备两套调色板。
   深色是历史现值（逐像素不变），浅色换成浅底可读版；切主题时监听
   zhilan-theme-change 换调色板并重绘。 */
type GraphSkin = {
  bg: string;
  edgeDim: string;
  edge: string;
  edgeSource: string;
  /** 相似边（内容接近，非事实关系）：虚线 + 独立颜色，便于与双链/来源区分。 */
  edgeSimilar: string;
  edgeActive: string;
  ghostStroke: string;
  label: string;
  labelDim: string;
  node: Record<string, string>;
  hit: string;
  selected: string;
};

const GRAPH_SKIN: Record<"dark" | "light", GraphSkin> = {
  dark: {
    bg: "#0E0E10",
    edgeDim: "rgba(140, 140, 148, 0.06)",
    edge: "rgba(140, 140, 148, 0.22)",
    edgeSource: "rgba(108, 182, 255, 0.28)",
    edgeSimilar: "rgba(178, 148, 255, 0.34)",
    edgeActive: "rgba(232, 160, 84, 0.85)",
    ghostStroke: "rgba(142,142,147,0.7)",
    label: "rgba(214,214,217,0.9)",
    labelDim: "rgba(214,214,217,0.25)",
    node: { note: "#E8A054", document: "#6CB6FF", ghost: "#8E8E93" },
    hit: "#4ADE80",
    selected: "#E8A054",
  },
  light: {
    bg: "#F7F6F3",
    edgeDim: "rgba(60, 60, 66, 0.07)",
    edge: "rgba(60, 60, 66, 0.2)",
    edgeSource: "rgba(29, 111, 197, 0.3)",
    edgeSimilar: "rgba(124, 88, 205, 0.36)",
    edgeActive: "rgba(168, 107, 37, 0.9)",
    ghostStroke: "rgba(110, 110, 118, 0.8)",
    label: "rgba(45, 44, 50, 0.92)",
    labelDim: "rgba(45, 44, 50, 0.28)",
    node: { note: "#BC7C1E", document: "#2F7CC7", ghost: "#8E8E93" },
    hit: "#1E9E5C",
    selected: "#BC7C1E",
  },
};

function currentSkin(): GraphSkin {
  return document.documentElement.dataset.theme === "light" ? GRAPH_SKIN.light : GRAPH_SKIN.dark;
}

const CATEGORY_PALETTE = [
  "#E8A054",
  "#6CB6FF",
  "#4ADE80",
  "#F87171",
  "#C084FC",
  "#FBBF24",
  "#2DD4BF",
  "#FB7185",
  "#94A3B8",
  "#A3E635",
];

function loadCoords(): CoordMap {
  try {
    const raw = localStorage.getItem(COORD_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as CoordMap;
    if (!parsed || typeof parsed !== "object") return {};
    const out: CoordMap = {};
    for (const [key, value] of Object.entries(parsed)) {
      if (value && typeof value === "object" && Number.isFinite(value.x) && Number.isFinite(value.y)) {
        out[key] = { x: value.x, y: value.y };
      }
    }
    return out;
  } catch {
    return {};
  }
}

function saveCoords(map: CoordMap) {
  try {
    localStorage.setItem(COORD_KEY, JSON.stringify(map));
  } catch {
    // 桌面单机单用户，配额满时静默；「重置布局」仍可清
  }
}

function hashHue(text: string) {
  let h = 0;
  for (let i = 0; i < text.length; i += 1) h = (h * 31 + text.charCodeAt(i)) >>> 0;
  return h;
}

function neighborhood(adjacency: Map<string, Set<string>>, root: string, hops: number): Set<string> {
  const keep = new Set<string>([root]);
  let frontier = new Set<string>([root]);
  for (let step = 0; step < hops; step += 1) {
    const next = new Set<string>();
    for (const item of frontier) {
      for (const n of adjacency.get(item) || []) {
        if (!keep.has(n)) {
          keep.add(n);
          next.add(n);
        }
      }
    }
    frontier = next;
  }
  return keep;
}

function buildSimNodes(nodes: GraphNode[], edges: GraphEdge[], saved: CoordMap): SimNode[] {
  const byId = new Map(nodes.map((item) => [item.id, item]));
  const adjacency = new Map<string, string[]>();
  for (const edge of edges) {
    if (!byId.has(edge.source) || !byId.has(edge.target)) continue;
    if (!adjacency.has(edge.source)) adjacency.set(edge.source, []);
    if (!adjacency.has(edge.target)) adjacency.set(edge.target, []);
    adjacency.get(edge.source)!.push(edge.target);
    adjacency.get(edge.target)!.push(edge.source);
  }

  // 先按邻居中心铺，无邻居的再落环
  const placed = new Map<string, { x: number; y: number }>();
  const ordered = [...nodes].sort((a, b) => b.degree - a.degree);
  for (const item of ordered) {
    const hist = saved[item.id];
    if (hist) {
      placed.set(item.id, { x: hist.x, y: hist.y });
      continue;
    }
    const nbrs = (adjacency.get(item.id) || []).map((id) => placed.get(id)).filter(Boolean) as {
      x: number;
      y: number;
    }[];
    if (nbrs.length) {
      const avgX = nbrs.reduce((sum, p) => sum + p.x, 0) / nbrs.length;
      const avgY = nbrs.reduce((sum, p) => sum + p.y, 0) / nbrs.length;
      const angle = (hashHue(item.id) % 360) * (Math.PI / 180);
      placed.set(item.id, {
        x: avgX + Math.cos(angle) * 70,
        y: avgY + Math.sin(angle) * 70,
      });
    }
  }

  const total = nodes.length;
  nodes.forEach((item, index) => {
    if (placed.has(item.id)) return;
    const angle = (index / Math.max(total, 1)) * Math.PI * 2;
    const radius = 80 + Math.sqrt(total) * 18 + (hashHue(item.id) % 40);
    placed.set(item.id, { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius });
  });

  return nodes.map((item) => {
    const pos = placed.get(item.id)!;
    return {
      ...item,
      x: pos.x,
      y: pos.y,
      vx: 0,
      vy: 0,
      pinned: Boolean(saved[item.id]),
    };
  });
}

function nodeRadius(node: SimNode, zoom: number) {
  const base = node.type === "ghost" ? 3.5 : node.type === "document" ? 5 : 6;
  const boost = Math.min(node.degree, 12) * 0.35;
  return (base + boost) / Math.max(Math.min(zoom, 1.6), 0.55);
}

export default function GraphView({ focus, onOpenNote, onOpenDocument, onNotify, onClearFocus }: GraphViewProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const dataRef = useRef<GraphStore | null>(null);
  const viewRef = useRef({ x: 0, y: 0, k: 1 });
  const hoverRef = useRef<string | null>(null);
  const dragRef = useRef<{ id: string | null; mode: "none" | "pan" | "node" }>({ id: null, mode: "none" });
  const pointerRef = useRef({ x: 0, y: 0 });
  // 按下时刻的指针位置：松手时判断「点击空白 vs 拖拽平移」必须用它，
  // 不能用 pointerRef（onMove 会把它覆盖成最新位置，导致拖完永远算点击空白）
  const downRef = useRef({ x: 0, y: 0 });
  // 滚轮缩放缓动：目标 zoom + 指针锚点（世界坐标），绘制循环里逐帧逼近
  const zoomAnimRef = useRef<{ mx: number; my: number; wx: number; wy: number; targetK: number } | null>(null);
  // 待执行「全貌 fit」标记：初始加载 / 重置布局 / 点「显示全貌」时置位
  const pendingFitRef = useRef(true);
  // 脏标记：力在跑 / 视图在动 / 悬停变化时才重绘，其余帧跳过
  const dirtyRef = useRef(true);
  const rafRef = useRef(0);
  const wakeRef = useRef<() => void>(() => {});
  const energyRef = useRef(0);
  const redraw = useCallback(() => {
    dirtyRef.current = true;
    wakeRef.current();
  }, []);
  // 条目 36：当前画布调色板（深色 / 浅色），切主题时换
  const skinRef = useRef<GraphSkin>(currentSkin());

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [stats, setStats] = useState<GraphData["stats"] | null>(null);
  const [types, setTypes] = useState<{ note: boolean; document: boolean; ghost: boolean }>({
    note: true,
    document: true,
    ghost: true,
  });
  const [edgeKinds, setEdgeKinds] = useState<EdgeKinds>(EDGE_KINDS_DEFAULT);
  const [rawEdges, setRawEdges] = useState<GraphEdge[]>([]);
  const [category, setCategory] = useState("全部");
  const [hideIsolated, setHideIsolated] = useState(false);
  const [colorMode, setColorMode] = useState<"type" | "category">("type");
  const [query, setQuery] = useState("");
  const [localMode, setLocalMode] = useState(false);
  const [hops, setHops] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [hoverTitle, setHoverTitle] = useState<string>("");

  /** 图例开关过滤后的边：store / 布局 / 局部图 / 绘制 / 孤立数全部只认这一份。 */
  const filteredEdges = useMemo(
    () => filterEdges(rawEdges, edgeKinds),
    [rawEdges, edgeKinds],
  );

  /** 过滤后的邻接表：局部图 / 邻域在**同一次渲染里**就用它，不等 store 重建。 */
  const filteredAdjacency = useMemo(
    () => adjacencyOf(dataRef.current?.nodes ?? [], filteredEdges),
    [filteredEdges, loading, stats],
  );

  /** 显示的孤立数本地重算：后端 `stats.isolated` 是服务端口径（含全部边、不含图例开关）。 */
  const isolatedCount = useMemo(() => {
    const all = dataRef.current;
    if (!all) return 0;
    const linked = new Set<string>();
    for (const edge of filteredEdges) {
      if (!all.byId.has(edge.source) || !all.byId.has(edge.target)) continue;
      linked.add(edge.source);
      linked.add(edge.target);
    }
    return all.nodes.reduce((sum, item) => (linked.has(item.id) ? sum : sum + 1), 0);
  }, [filteredEdges, loading, stats]);

  const categories = useMemo(() => {
    const nodes = dataRef.current?.nodes;
    if (!nodes) return [] as string[];
    const set = new Set<string>();
    for (const item of nodes) if (item.category) set.add(item.category);
    return Array.from(set).sort((a, b) => a.localeCompare(b, "zh-CN"));
  }, [loading, error, stats]);

  const categoryColor = useMemo(() => {
    const map = new Map<string, string>();
    categories.forEach((name, index) => {
      map.set(name, CATEGORY_PALETTE[index % CATEGORY_PALETTE.length]);
    });
    return map;
  }, [categories]);

  const visibleIds = useMemo(() => {
    const all = dataRef.current;
    if (!all) return new Set<string>();
    let ids = new Set(
      all.nodes
        .filter((item) => types[item.type])
        .filter((item) => category === "全部" || item.category === category)
        .map((item) => item.id),
    );
    if (hideIsolated) {
      const linked = new Set<string>();
      for (const edge of filteredEdges) {
        if (ids.has(edge.source) && ids.has(edge.target)) {
          linked.add(edge.source);
          linked.add(edge.target);
        }
      }
      ids = linked;
    }
    const root = selectedId || (focus ? `${focus.type}:${focus.id}` : null);
    if (localMode && root && ids.has(root)) {
      const keep = neighborhood(filteredAdjacency, root, hops);
      ids = new Set([...ids].filter((id) => keep.has(id)));
    }
    return ids;
  }, [loading, stats, types, category, hideIsolated, localMode, hops, selectedId, focus, filteredEdges, filteredAdjacency]);

  const hits = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return new Set<string>();
    const all = dataRef.current;
    if (!all) return new Set<string>();
    return new Set(
      all.nodes
        .filter((item) => item.title.toLowerCase().includes(q) || item.category.toLowerCase().includes(q) || item.tags.some((t) => t.toLowerCase().includes(q)))
        .map((item) => item.id),
    );
  }, [query, loading, stats]);

  const persistOne = useCallback((id: string, x: number, y: number) => {
    const map = loadCoords();
    map[id] = { x, y };
    saveCoords(map);
  }, []);

  const resetLayout = useCallback(() => {
    localStorage.removeItem(COORD_KEY);
    const all = dataRef.current;
    if (!all) return;
    const saved = loadCoords();
    const next = buildSimNodes(
      all.nodes.map(({ degree, ...rest }) => ({ ...rest, degree })),
      filteredEdges,
      saved,
    );
    for (const item of next) {
      item.pinned = false;
      item.vx = 0;
      item.vy = 0;
      // 随机打散，避免又叠回旧位置
      const angle = (hashHue(item.id) % 360) * (Math.PI / 180);
      const radius = 60 + Math.sqrt(all.nodes.length) * 20;
      item.x = Math.cos(angle) * radius + ((hashHue(item.id + "x") % 80) - 40);
      item.y = Math.sin(angle) * radius + ((hashHue(item.id + "y") % 80) - 40);
    }
    dataRef.current = makeStore(next, filteredEdges);
    energyRef.current = 1;
    pendingFitRef.current = true;
    redraw();
    onNotify("已重置布局，坐标记忆已清空");
  }, [filteredEdges, onNotify, redraw]);

  const focusNode = useCallback(
    (nodeId: string) => {
      const all = dataRef.current;
      const canvas = canvasRef.current;
      const wrap = wrapRef.current;
      if (!all || !canvas || !wrap) return;
      const node = all.byId.get(nodeId);
      if (!node) return;
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      const k = Math.max(viewRef.current.k, 1);
      viewRef.current = {
        k,
        x: w / 2 - node.x * k,
        y: h / 2 - node.y * k,
      };
      setSelectedId(nodeId);
      setSelected(node);
      redraw();
    },
    [redraw],
  );

  const openNode = useCallback(
    (node: GraphNode) => {
      if (node.type === "note" && node.source_id != null) {
        onOpenNote(node.source_id);
        return;
      }
      if (node.type === "document" && node.source_id != null) {
        onOpenDocument(node.source_id);
        return;
      }
      onNotify(`「${node.title}」尚未创建，无法打开`);
    },
    [onOpenDocument, onOpenNote, onNotify],
  );

  // 初始加载
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    getGraph()
      .then((data) => {
        if (!alive) return;
        const edges = filterEdges(data.edges, EDGE_KINDS_DEFAULT);
        setRawEdges(data.edges);
        dataRef.current = makeStore(buildSimNodes(data.nodes, edges, loadCoords()), edges);
        setStats(data.stats);
        setLoading(false);
        energyRef.current = 1;
        pendingFitRef.current = true; // 打开图谱先看全貌
        redraw();
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setError(err instanceof ApiError ? err.message : "图谱加载失败");
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [redraw]);

  // 图例开关：只换边集与邻接表（力导向 / 邻域 / 局部图 / 绘制都读 store），节点位置与钉住状态保留
  useEffect(() => {
    const all = dataRef.current;
    if (!all) return;
    dataRef.current = { ...all, edges: filteredEdges, adjacency: adjacencyOf(all.nodes, filteredEdges) };
    energyRef.current = 1;
    redraw();
  }, [filteredEdges, redraw]);

  // 条目 36：主题切换时换调色板并强制重绘一帧
  useEffect(() => {
    const onThemeChange = () => {
      skinRef.current = currentSkin();
      redraw();
    };
    window.addEventListener(THEME_CHANGE_EVENT, onThemeChange);
    return () => window.removeEventListener(THEME_CHANGE_EVENT, onThemeChange);
  }, [redraw]);

  // 外部聚焦（笔记 / 文档详情入口）：数据加载完成后再套用，否则 mount 时 dataRef 还是空
  useEffect(() => {
    if (loading || !focus || !dataRef.current) return;
    const id = `${focus.type}:${focus.id}`;
    if (dataRef.current.nodes.some((item) => item.id === id)) {
      pendingFitRef.current = false; // 带焦点进入时按焦点居中，不做全貌 fit
      focusNode(id);
      setLocalMode(true);
      onClearFocus();
    }
  }, [focus, focusNode, onClearFocus, loading]);

  // visibleIds 同步到 ref，供绘制循环与指针交互的闭包读取（声明需在绘制 effect 之前）
  const visibleIdsRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    visibleIdsRef.current = visibleIds;
    energyRef.current = 1;
    redraw();
  }, [visibleIds, redraw]);

  // 力导向 + 绘制循环
  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let stopped = false;
    dirtyRef.current = true; // effect（重）建后强制重绘一帧

    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      canvas.width = Math.max(1, Math.floor(w * dpr));
      canvas.height = Math.max(1, Math.floor(h * dpr));
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      redraw();
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);

    const step = () => {
      const all = dataRef.current;
      if (!all) return false;
      const nodes = all.nodes;
      // 未全部钉死时才跑力
      const free = nodes.filter((item) => !item.pinned && visibleIds.has(item.id));
      if (!free.length || energyRef.current <= 0.002) return false;
      let energy = 0;
      const indices = free.map((_, i) => i);
      // ponytail: 千点以下沿用成对循环；复用640精确截断，密集同格仍O(n²)，再大时评估四叉树。
      const cells = free.length > 1000 ? new Map<number, Map<number, number[]>>() : null;
      if (cells) {
        free.forEach((node, i) => {
          const cx = Math.floor(node.x / REPEL_CUTOFF), cy = Math.floor(node.y / REPEL_CUTOFF);
          let column = cells.get(cx);
          if (!column) { column = new Map(); cells.set(cx, column); }
          const bucket = column.get(cy);
          if (bucket) bucket.push(i);
          else column.set(cy, [i]);
        });
      }
      for (let i = 0; i < free.length; i += 1) {
        const a = free[i];
        let buckets = [indices];
        if (cells) {
          const cx = Math.floor(a.x / REPEL_CUTOFF), cy = Math.floor(a.y / REPEL_CUTOFF);
          buckets = [];
          let count = 0;
          for (let dx = -1; dx <= 1; dx += 1) {
            const column = cells.get(cx + dx);
            if (!column) continue;
            for (let dy = -1; dy <= 1; dy += 1) {
              const bucket = column.get(cy + dy);
              if (bucket) { buckets.push(bucket); count += bucket.length; }
            }
          }
          // 重叠扰动按旧索引顺序；只有离格界不足1的点可能跨格重叠。
          const rx = a.x - cx * REPEL_CUTOFF, ry = a.y - cy * REPEL_CUTOFF;
          if (count > free.length / 2) buckets = [indices];
          else if (rx < 1 || ry < 1 || rx > REPEL_CUTOFF - 1 || ry > REPEL_CUTOFF - 1) buckets = [buckets.flat().sort((x, y) => x - y)];
        }
        for (const nearby of buckets) for (let offset = nearby === indices ? i + 1 : 0; offset < nearby.length; offset += 1) {
          const j = nearby[offset];
          if (j <= i) continue;
          const b = free[j];
          let dx = b.x - a.x;
          let dy = b.y - a.y;
          let dist2 = dx * dx + dy * dy;
          if (dist2 > REPEL_CUTOFF2) continue; // 远处斥力可忽略，跳过省算
          if (dist2 < 1) {
            dx = (Math.random() - 0.5) * 2;
            dy = (Math.random() - 0.5) * 2;
            dist2 = dx * dx + dy * dy + 1;
          }
          const force = REPEL / dist2;
          const dist = Math.sqrt(dist2);
          const fx = (dx / dist) * force;
          const fy = (dy / dist) * force;
          a.vx -= fx;
          a.vy -= fy;
          b.vx += fx;
          b.vy += fy;
        }
      }
      for (const edge of all.edges) {
        if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target)) continue;
        const a = all.byId.get(edge.source);
        const b = all.byId.get(edge.target);
        if (!a || !b) continue;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const f = (dist - SPRING_LEN) * SPRING;
        const fx = (dx / dist) * f;
        const fy = (dy / dist) * f;
        if (!a.pinned) {
          a.vx += fx;
          a.vy += fy;
        }
        if (!b.pinned) {
          b.vx -= fx;
          b.vy -= fy;
        }
      }
      for (const node of free) {
        if (dragRef.current.mode === "node" && dragRef.current.id === node.id) continue;
        node.vx += -node.x * CENTER;
        node.vy += -node.y * CENTER;
        node.vx *= DAMPING;
        node.vy *= DAMPING;
        node.x += node.vx;
        node.y += node.vy;
        energy += node.vx * node.vx + node.vy * node.vy;
      }
      energyRef.current = energy > 2000 ? 1 : energy / 2000;
      return true;
    };

    // 把视野对齐全部可见节点（留边距），打开图谱 / 重置布局 / 点「显示全貌」时用
    const fitNow = () => {
      const all = dataRef.current;
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      if (!all || w <= 0 || h <= 0) return;
      const list = visibleIdsRef.current.size
        ? all.nodes.filter((item) => visibleIdsRef.current.has(item.id))
        : all.nodes;
      if (!list.length) return;
      let minX = Infinity;
      let minY = Infinity;
      let maxX = -Infinity;
      let maxY = -Infinity;
      for (const node of list) {
        const r = nodeRadius(node, 1);
        if (node.x - r < minX) minX = node.x - r;
        if (node.y - r < minY) minY = node.y - r;
        if (node.x + r > maxX) maxX = node.x + r;
        if (node.y + r > maxY) maxY = node.y + r;
      }
      const pad = 48;
      const bw = Math.max(maxX - minX, 1);
      const bh = Math.max(maxY - minY, 1);
      const k = Math.min(1.35, Math.max(0.1, Math.min((w - pad * 2) / bw, (h - pad * 2) / bh)));
      viewRef.current = {
        k,
        x: w / 2 - ((minX + maxX) / 2) * k,
        y: h / 2 - ((minY + maxY) / 2) * k,
      };
      zoomAnimRef.current = null;
    };

    const draw = () => {
      rafRef.current = 0;
      if (stopped) return;
      const all = dataRef.current;
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      if (!all) return;

      // 滚轮缩放缓动：逐帧逼近目标 zoom，锚点保持在指针位置
      const za = zoomAnimRef.current;
      if (za) {
        const view = viewRef.current;
        const diff = za.targetK - view.k;
        const done = Math.abs(diff) <= Math.max(0.0015, za.targetK * 0.002);
        const k = done ? za.targetK : view.k + diff * 0.28;
        if (done) zoomAnimRef.current = null;
        viewRef.current = { k, x: za.mx - za.wx * k, y: za.my - za.wy * k };
        dirtyRef.current = true;
      }

      // 首次拿到数据（或点了「显示全貌」）：视野对齐全图
      if (pendingFitRef.current) {
        pendingFitRef.current = false;
        fitNow();
        dirtyRef.current = true;
      }

      const moving = step();
      if (moving) dirtyRef.current = true;
      if (!dirtyRef.current) return;
      dirtyRef.current = false;

      ctx.clearRect(0, 0, w, h);
      const skin = skinRef.current;
      ctx.fillStyle = skin.bg;
      ctx.fillRect(0, 0, w, h);

      const { x: tx, y: ty, k } = viewRef.current;
      const focusRoot = selectedId || hoverRef.current || (focus ? `${focus.type}:${focus.id}` : null);
      const neigh = focusRoot && filteredEdges.length ? neighborhood(filteredAdjacency, focusRoot, 1) : null;

      ctx.save();
      ctx.translate(tx, ty);
      ctx.scale(k, k);

      // 边
      for (const edge of all.edges) {
        if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target)) continue;
        const a = all.byId.get(edge.source);
        const b = all.byId.get(edge.target);
        if (!a || !b) continue;
        const active =
          focusRoot != null && (edge.source === focusRoot || edge.target === focusRoot);
        const dim = neigh != null && !active;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.strokeStyle = active
          ? skin.edgeActive
          : dim
            ? skin.edgeDim
            : edge.kind === "source"
              ? skin.edgeSource
              : edge.kind === "similar"
                ? skin.edgeSimilar
                : skin.edge;
        ctx.lineWidth = (active ? 1.6 : edge.kind === "source" ? 1.1 : edge.kind === "similar" ? 0.9 : 0.8) / Math.max(k, 0.5);
        ctx.setLineDash(edge.kind === "similar" ? [4 / Math.max(k, 0.5), 3 / Math.max(k, 0.5)] : []);
        ctx.stroke();
      }
      ctx.setLineDash([]);

      // 节点
      for (const node of all.nodes) {
        if (!visibleIds.has(node.id)) continue;
        const r = nodeRadius(node, k);
        let fill =
          colorMode === "category" && node.category
            ? categoryColor.get(node.category) || skin.node[node.type]
            : skin.node[node.type] || skin.node.ghost;
        const inNeigh = neigh == null || neigh.has(node.id) || node.id === focusRoot;
        if (hits.size && hits.has(node.id)) fill = skin.hit;
        if (node.id === selectedId) fill = skin.selected;
        ctx.beginPath();
        ctx.arc(node.x, node.y, node.id === hoverRef.current || node.id === selectedId ? r * 1.35 : r, 0, Math.PI * 2);
        ctx.fillStyle = fill;
        ctx.globalAlpha = inNeigh ? 1 : 0.18;
        ctx.fill();
        if (node.type === "ghost") {
          ctx.strokeStyle = skin.ghostStroke;
          ctx.lineWidth = 1 / Math.max(k, 0.5);
          ctx.stroke();
        }
        ctx.globalAlpha = 1;

        // 标签：高 zoom 或命中 / 选中 / 悬停时画
        if (k > 0.85 || hits.has(node.id) || node.id === selectedId || node.id === hoverRef.current) {
          const label = node.title.length > 18 ? node.title.slice(0, 17) + "…" : node.title;
          ctx.font = `${11 / Math.max(k, 0.7)}px "Segoe UI", "Microsoft YaHei UI", sans-serif`;
          ctx.fillStyle = inNeigh ? skin.label : skin.labelDim;
          ctx.textAlign = "center";
          ctx.fillText(label, node.x, node.y + r + 12 / Math.max(k, 0.7));
        }
      }

      ctx.restore();
      if ((moving && energyRef.current > 0.002) || zoomAnimRef.current) wake();
    };

    const wake = () => {
      if (!stopped && !rafRef.current) rafRef.current = requestAnimationFrame(draw);
    };
    wakeRef.current = wake;
    wake();

    return () => {
      stopped = true;
      cancelAnimationFrame(rafRef.current);
      rafRef.current = 0;
      if (wakeRef.current === wake) wakeRef.current = () => {};
      ro.disconnect();
    };
  }, [visibleIds, colorMode, categoryColor, hits, selectedId, focus, redraw, loading, filteredEdges, filteredAdjacency, dataRef.current?.nodes?.length]);

  // 指针交互。loading 必须在依赖里：loading 期间 canvas 不在 DOM（提前 return），
  // 翻转后要靠这个依赖重跑一次 effect，监听器才挂得上——否则滚轮/拖拽会随机失灵，
  // 只能等 App 层恰好重渲染换掉回调引用时才被顺带修好。
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const mx = event.clientX - rect.left;
      const my = event.clientY - rect.top;
      const view = viewRef.current;
      // 连续滚动时以「目标 zoom」为基准累乘，手感更线性
      const baseK = zoomAnimRef.current ? zoomAnimRef.current.targetK : view.k;
      const factor = event.deltaY < 0 ? 1.1 : 1 / 1.1;
      const targetK = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, baseK * factor));
      const wx = (mx - view.x) / view.k;
      const wy = (my - view.y) / view.k;
      zoomAnimRef.current = { mx, my, wx, wy, targetK };
      redraw();
    };

    const onDown = (event: PointerEvent) => {
      canvas.setPointerCapture(event.pointerId);
      zoomAnimRef.current = null; // 拖拽优先，停掉缩放缓动
      downRef.current = { x: event.clientX, y: event.clientY };
      pointerRef.current = { x: event.clientX, y: event.clientY };
      const node = pickAt(event.clientX, event.clientY);
      if (node) {
        dragRef.current = { id: node.id, mode: "node" };
        const all = dataRef.current;
        if (all) {
          const target = all.byId.get(node.id);
          if (target) {
            target.pinned = true;
            target.vx = 0;
            target.vy = 0;
          }
        }
        energyRef.current = 1;
        redraw();
      } else {
        dragRef.current = { id: null, mode: "pan" };
        canvas.style.cursor = "grabbing";
      }
    };

    const onMove = (event: PointerEvent) => {
      const prev = pointerRef.current;
      pointerRef.current = { x: event.clientX, y: event.clientY };
      const dx = event.clientX - prev.x;
      const dy = event.clientY - prev.y;
      const drag = dragRef.current;
      if (drag.mode === "pan") {
        const view = viewRef.current;
        viewRef.current = { ...view, x: view.x + dx, y: view.y + dy };
        redraw();
        return;
      }
      if (drag.mode === "node" && drag.id) {
        const all = dataRef.current;
        if (!all) return;
        const node = all.byId.get(drag.id);
        if (!node) return;
        const view = viewRef.current;
        node.x += dx / view.k;
        node.y += dy / view.k;
        node.vx = 0;
        node.vy = 0;
        node.pinned = true;
        energyRef.current = 1;
        redraw();
        return;
      }
      const node = pickAt(event.clientX, event.clientY);
      const id = node?.id ?? null;
      if (hoverRef.current !== id) {
        hoverRef.current = id;
        setHoverTitle(node?.title ?? "");
        canvas.style.cursor = node ? "pointer" : "grab";
        redraw(); // 悬停高亮即时重绘
      }
    };

    const onUp = (event: PointerEvent) => {
      const drag = dragRef.current;
      canvas.style.cursor = "grab";
      if (drag.mode === "node" && drag.id) {
        const all = dataRef.current;
        const node = all?.byId.get(drag.id);
        if (node) {
          persistOne(node.id, node.x, node.y);
          setSelectedId(node.id);
          setSelected(node);
        }
      } else if (drag.mode === "pan") {
        // 从按下位置算总位移：之前误用被 onMove 覆盖的 pointerRef，
        // 拖拽平移松手也被当成「点击空白」，把选中 / 局部图误清掉
        const moved =
          Math.abs(event.clientX - downRef.current.x) + Math.abs(event.clientY - downRef.current.y);
        if (moved < 4) {
          setSelectedId(null);
          setSelected(null);
          setLocalMode(false);
        }
      }
      redraw();
      dragRef.current = { id: null, mode: "none" };
      try {
        canvas.releasePointerCapture(event.pointerId);
      } catch {
        /* ignore */
      }
    };

    const onDblClick = (event: MouseEvent) => {
      const node = pickAt(event.clientX, event.clientY);
      if (node) openNode(node);
    };

    function pickAt(clientX: number, clientY: number): SimNode | null {
      const all = dataRef.current;
      const canvasEl = canvasRef.current;
      if (!all || !canvasEl) return null;
      const rect = canvasEl.getBoundingClientRect();
      const view = viewRef.current;
      const wx = (clientX - rect.left - view.x) / view.k;
      const wy = (clientY - rect.top - view.y) / view.k;
      let best: SimNode | null = null;
      let bestDist = Infinity;
      for (const node of all.nodes) {
        if (!visibleIdsRef.current.has(node.id)) continue;
        const r = nodeRadius(node, view.k) + 5;
        const dx = node.x - wx;
        const dy = node.y - wy;
        const d = Math.sqrt(dx * dx + dy * dy);
        if (d < r && d < bestDist) {
          best = node;
          bestDist = d;
        }
      }
      return best;
    }

    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerup", onUp);
    canvas.addEventListener("dblclick", onDblClick);
    canvas.style.cursor = "grab";
    return () => {
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerup", onUp);
      canvas.removeEventListener("dblclick", onDblClick);
    };
  }, [openNode, persistOne, redraw, loading]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-mute">
        <Loader2 size={16} className="animate-spin" />
        正在加载图谱…
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-mute">
        <p className="text-[14px] text-body">{error}</p>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="rounded-lg bg-fog/[0.06] px-3 py-1.5 text-[13px] text-ink hover:bg-fog/10"
        >
          重试
        </button>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0">
      {/* 左侧工具条 */}
      <aside className="flex w-[220px] shrink-0 flex-col gap-3 border-r border-line bg-sidebar px-3 py-3">
        <div className="flex items-center gap-2 text-[13px] font-medium text-ink">
          <Share2 size={15} className="text-brand" strokeWidth={1.75} />
          关系图谱
        </div>
        <p className="text-[11.5px] leading-4 text-faint">
          笔记 · 文档 · 双链边。拖拽节点会记住坐标；双击打开详情。
        </p>

        {stats ? (
          <div className="rounded-lg bg-fog/[0.04] px-2.5 py-2 text-[11.5px] leading-5 text-mute">
            <div>笔记 {stats.notes} · 文档 {stats.documents}</div>
            <div>边 {filteredEdges.length} · 未创建 {stats.ghosts}</div>
            <div>孤立 {isolatedCount}</div>
          </div>
        ) : null}

        <div className="relative">
          <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索高亮节点"
            className="h-8 w-full rounded-lg border border-line bg-composer pl-8 pr-2 text-[12.5px] text-ink placeholder:text-faint focus:border-fog/20 focus:outline-none"
          />
        </div>

        <div className="space-y-1.5">
          <div className="text-[11px] text-faint">类型</div>
          {(
            [
              ["note", "笔记"],
              ["document", "文档"],
              ["ghost", "未创建"],
            ] as const
          ).map(([key, label]) => (
            <label key={key} className="flex cursor-pointer items-center gap-2 text-[12.5px] text-body">
              <input
                type="checkbox"
                checked={types[key]}
                onChange={(e) => setTypes((prev) => ({ ...prev, [key]: e.target.checked }))}
                className="accent-brand"
              />
              {label}
            </label>
          ))}
        </div>

        <div className="space-y-1.5">
          <div className="text-[11px] text-faint">边</div>
          {EDGE_KIND_ITEMS.map(({ key, label }) => (
            <label key={key} className="flex cursor-pointer items-center gap-2 text-[12.5px] text-body">
              <input
                type="checkbox"
                checked={edgeKinds[key]}
                onChange={(e) => setEdgeKinds((prev) => ({ ...prev, [key]: e.target.checked }))}
                className="accent-brand"
              />
              {label}
            </label>
          ))}
          <p className="text-[11px] leading-4 text-faint">相似边 = 内容接近（向量近邻），不是事实关系。</p>
        </div>

        <div className="space-y-1.5">
          <div className="text-[11px] text-faint">分类</div>
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="h-8 w-full rounded-lg border border-line bg-composer px-2 text-[12.5px] text-ink focus:outline-none"
          >
            <option value="全部">全部</option>
            {categories.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </div>

        <div className="space-y-1.5">
          <div className="text-[11px] text-faint">着色</div>
          <select
            value={colorMode}
            onChange={(e) => setColorMode(e.target.value as "type" | "category")}
            className="h-8 w-full rounded-lg border border-line bg-composer px-2 text-[12.5px] text-ink focus:outline-none"
          >
            <option value="type">按类型</option>
            <option value="category">按分类</option>
          </select>
        </div>

        <label className="flex cursor-pointer items-center gap-2 text-[12.5px] text-body">
          <input
            type="checkbox"
            checked={hideIsolated}
            onChange={(e) => setHideIsolated(e.target.checked)}
            className="accent-brand"
          />
          隐藏孤立节点
        </label>

        <div className="space-y-1.5">
          <label className="flex cursor-pointer items-center gap-2 text-[12.5px] text-body">
            <input
              type="checkbox"
              checked={localMode}
              onChange={(e) => setLocalMode(e.target.checked)}
              className="accent-brand"
            />
            局部图
          </label>
          {localMode ? (
            <select
              value={hops}
              onChange={(e) => setHops(Number(e.target.value) || 1)}
              className="h-8 w-full rounded-lg border border-line bg-composer px-2 text-[12.5px] text-ink focus:outline-none"
            >
              <option value={1}>一阶邻居</option>
              <option value={2}>二阶邻居</option>
            </select>
          ) : null}
        </div>

        <div className="mt-auto space-y-1.5">
          <button
            type="button"
            onClick={resetLayout}
            className="flex h-8 w-full items-center justify-center gap-1.5 rounded-lg bg-fog/[0.06] text-[12.5px] text-ink hover:bg-fog/10"
          >
            <RefreshCw size={13} strokeWidth={1.75} />
            重置布局
          </button>
          {selected ? (
            <div className="rounded-lg bg-fog/[0.04] px-2.5 py-2 text-[11.5px] leading-5 text-mute">
              <div className="truncate text-ink">{selected.title}</div>
              <div>
                {selected.type === "note" ? "笔记" : selected.type === "document" ? "文档" : "未创建"}
                {selected.category ? ` · ${selected.category}` : ""}
              </div>
              <div>度数 {selected.degree}</div>
              {selected.source_id != null ? (
                <button
                  type="button"
                  onClick={() => openNode(selected)}
                  className="mt-1 text-brand hover:underline"
                >
                  打开详情
                </button>
              ) : null}
            </div>
          ) : (
            <div className="flex items-center gap-1.5 text-[11px] text-faint">
              <LocateFixed size={12} />
              点选节点可看详情
            </div>
          )}
        </div>
      </aside>

      <div ref={wrapRef} className="relative min-h-0 min-w-0 flex-1">
        <canvas ref={canvasRef} className="absolute inset-0 h-full w-full" />
        <div className="pointer-events-none absolute left-3 top-3 flex items-center gap-2 rounded-lg bg-float px-2.5 py-1.5 text-[11.5px] text-mute backdrop-blur">
          <Network size={12} className="text-brand" />
          {hoverTitle || "滚轮缩放 · 拖拽平移 · 双击打开"}
        </div>
        <button
          type="button"
          onClick={() => {
            pendingFitRef.current = true;
            redraw();
          }}
          className="absolute right-3 top-3 flex items-center gap-1.5 rounded-lg bg-float px-2.5 py-1.5 text-[11.5px] text-mute backdrop-blur hover:text-ink"
        >
          <LocateFixed size={12} />
          显示全貌
        </button>
        <div className="pointer-events-none absolute bottom-3 right-3 flex items-center gap-1.5 rounded-lg bg-float px-2.5 py-1.5 text-[11px] text-faint">
          <Crosshair size={11} />
          坐标已本地记住
        </div>
      </div>
    </div>
  );
}
