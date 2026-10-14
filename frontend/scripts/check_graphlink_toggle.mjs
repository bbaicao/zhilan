// Run: node frontend/scripts/check_graphlink_toggle.mjs
// 专项-图谱自动连线 P3 判据载体：执行 GraphView 的**真实** hooks / effects / 事件处理器（React 与 canvas 为最小替身），
// 断言「相似边开关」作用于整条链路：store 边集与邻接表（力导向 / 邻域 / 绘制共用）、布局、隐藏孤立、局部图、
// 以及**显示的孤立数**（后端 stats.isolated 是服务端口径，必须被本地重算覆盖）。
// ⚠️ 这是组件树 / 处理器级断言，不是真实浏览器渲染，也不覆盖视觉密度。
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(path.join(frontend, "package.json"));
const ts = require("typescript");
const file = "src/components/GraphView.tsx";
const source = fs.readFileSync(path.join(frontend, file), "utf8");
const compile = text => ts.transpileModule(text, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React },
}).outputText;

/* ---- 最小 React 替身：hook 顺序必须与源码声明顺序一致（ref(name) 用 AST 数同一个顺序） ---- */
function harness(text, graph, focus = null) {
  const hooks = [], effects = [], storage = new Map();
  let cursor = 0, tree;
  const changed = (old, deps) => !old || deps.some((value, i) => !Object.is(value, old[i]));
  const react = {
    createElement: (type, props, ...children) => ({ type, props: { ...props, children } }),
    useRef: value => { const i = cursor++; return hooks[i] ||= { current: value }; },
    useState: value => {
      const i = cursor++;
      hooks[i] ||= { value };
      return [hooks[i].value, next => { hooks[i].value = typeof next === "function" ? next(hooks[i].value) : next; }];
    },
    useMemo: (fn, deps) => {
      const i = cursor++;
      if (changed(hooks[i]?.deps, deps)) hooks[i] = { value: fn(), deps };
      return hooks[i].value;
    },
    useCallback: (fn, deps) => react.useMemo(() => fn, deps),
    useEffect: (fn, deps) => {
      const i = cursor++;
      if (changed(hooks[i]?.deps, deps)) effects.push(() => { hooks[i]?.cleanup?.(); hooks[i] = { deps, cleanup: fn() }; });
    },
  };
  const ctx = new Proxy({}, { get: (target, key) => key in target ? target[key] : () => {} });
  const wrap = { clientWidth: 1000, clientHeight: 720 };
  const canvas = {
    style: {}, getContext: () => ctx, getBoundingClientRect: () => ({ left: 0, top: 0 }),
    addEventListener: () => {}, removeEventListener: () => {}, setPointerCapture: () => {}, releasePointerCapture: () => {},
  };
  const module = { exports: {} };
  const context = vm.createContext({
    console, module, exports: module.exports, React: react,
    require: name => name === "react" ? react
      : name === "../api" ? { getGraph: () => Promise.resolve(graph), ApiError: Error }
        : name === "../theme" ? { THEME_CHANGE_EVENT: "theme" } : {},
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    },
    document: { documentElement: { dataset: { theme: "dark" } } },
    window: { devicePixelRatio: 1, addEventListener: () => {}, removeEventListener: () => {} },
    ResizeObserver: class { constructor() {} observe() {} disconnect() {} },
    requestAnimationFrame: () => 1,
    cancelAnimationFrame: () => {},
  });
  vm.runInContext(compile(text), context);
  const walk = (node, visit) => {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) { for (const child of node) walk(child, visit); return; }
    visit(node);
    walk(node.props?.children, visit);
  };
  const render = () => {
    cursor = 0;
    tree = module.exports.default({
      focus, onOpenNote: () => {}, onOpenDocument: () => {}, onNotify: () => {}, onClearFocus: () => {},
    });
    walk(tree, node => { if (node.props?.ref) node.props.ref.current = node.type === "canvas" ? canvas : wrap; });
    for (const effect of effects.splice(0)) effect();
  };
  const ref = name => {
    const ast = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    let index = -1, found;
    const visit = node => {
      if (ts.isVariableDeclaration(node) && node.initializer && ts.isCallExpression(node.initializer)
        && /^use(Ref|State|Memo|Callback)$/.test(node.initializer.expression.getText())) {
        index += 1;
        if (node.name.getText() === name) found = hooks[index];
      }
      if (ts.isCallExpression(node) && node.expression.getText() === "useEffect") index += 1;
      ts.forEachChild(node, visit);
    };
    visit(ast);
    assert.ok(found, `missing hook ${name}`);
    return found;
  };
  const content = node => typeof node === "string" ? node
    : typeof node === "number" ? String(node)
      : node?.props?.children?.map(content).join("") ?? "";
  return {
    render, ref, context,
    text: () => content(tree),
    change(label, checked) {
      let match;
      walk(tree, node => {
        if (node.type === "label" && content(node).trim() === label) {
          walk(node, child => { if (child.type === "input") match = child; });
        }
      });
      assert.ok(match, `missing checkbox「${label}」`);
      match.props.onChange({ target: { checked } });
    },
    click(label) {
      let match;
      walk(tree, node => { if (node.type === "button" && content(node).includes(label)) match = node; });
      assert.ok(match, `missing button「${label}」`);
      match.props.onClick();
    },
    labels: () => {
      const out = [];
      walk(tree, node => { if (node.type === "label") out.push(content(node).trim()); });
      return out;
    },
  };
}

const mount = async (text, graph, focus = null) => {
  const h = harness(text, graph, focus);
  h.render();
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  h.render();
  // focus 的 effect 会 setLocalMode(true)（挂载后补一次 state），再渲一轮派生值才是稳态
  h.render();
  return h;
};

/* ---- mock payload：wiki 边连 doc0-doc1；**只有** similar 边连 doc2-doc3 ----
   stats 故意用服务端口径的可疑值（edges=77 / isolated=99）：页面必须显示本地重算值，
   否则「关掉相似边而孤立数不动」这条验收缺陷会照样通过。 */
const graph = {
  nodes: [0, 1, 2, 3].map(i => ({
    id: `document:${i}`, type: "document", source_id: i, title: `文档${i}`,
    category: "测试", tags: [], kb_id: 1, degree: i < 2 ? 1 : 1,
  })),
  edges: [
    { source: "document:0", target: "document:1", kind: "wiki" },
    { source: "document:2", target: "document:3", kind: "similar" },
  ],
  stats: { notes: 0, documents: 4, ghosts: 0, edges: 77, isolated: 99 },
};

const checks = [];
const h = await mount(source, graph);
const store = () => h.ref("dataRef").current;
const visible = () => h.ref("visibleIdsRef").current;

assert.deepEqual(h.labels().sort(), ["来源", "双链", "相似边", "笔记", "文档", "未创建", "隐藏孤立节点", "局部图"].sort());
assert.ok(h.text().includes("不是事实关系") && h.text().includes("内容接近"), "图例必须写明相似边不是事实关系");
checks.push("legend_has_similar_and_hint");

assert.equal(store().edges.length, 2, "默认（相似边开）必须画出全部边");
assert.equal(store().adjacency.get("document:2").size, 1, "similar 边必须进邻接表（力导向 / 邻域 / 绘制共用）");
assert.ok(h.text().includes("边 2"), "边数须按过滤后的边显示（后端 stats.edges=77 不得直接上屏）");
assert.ok(h.text().includes("孤立 0"), "孤立数须本地重算（后端 stats.isolated=99 不得直接上屏）");
checks.push("similar_on_store_and_local_stats");

h.change("相似边", false);
h.render();
assert.equal(store().edges.length, 1, "关掉相似边：store 边集必须同步收紧");
assert.equal(store().adjacency.get("document:2").size, 0, "关掉相似边：邻接表必须同步收紧");
assert.equal(store().adjacency.get("document:0").size, 1, "wiki 边不受影响");
assert.equal(visible().size, 4, "无隐藏孤立时节点全可见");
assert.ok(h.text().includes("边 1"));
assert.ok(h.text().includes("孤立 2"), "关掉相似边：**显示的**孤立数必须变化（doc2/doc3 变孤立）");
checks.push("similar_off_store_and_isolated");

h.change("隐藏孤立节点", true);
h.render();
assert.deepEqual([...visible()].sort(), ["document:0", "document:1"], "隐藏孤立按过滤后的边判定（doc2/doc3 被隐藏）");
h.change("相似边", true);
h.render();
assert.equal(visible().size, 4, "重新打开相似边：doc2/doc3 不再是孤立");
checks.push("similar_off_hide_isolated");

h.change("隐藏孤立节点", false);
h.change("相似边", false);
h.render();
h.click("重置布局");
assert.equal(store().edges.length, 1, "重置布局必须按过滤后的边重建 store");
assert.equal(store().adjacency.get("document:2").size, 0);
checks.push("reset_layout_uses_filtered");
h.change("相似边", true);
h.render();

// 局部图 / 邻域：以 doc2 为焦点，邻居集合只能来自过滤后的边
const hf = await mount(source, graph, { type: "document", id: 2 });
assert.equal(hf.ref("visibleIdsRef").current.size, 2, "局部图（相似边开）：doc2 + doc3");
hf.change("相似边", false);
hf.render();
assert.deepEqual([...hf.ref("visibleIdsRef").current].sort(), ["document:2"], "局部图（相似边关）：只剩 doc2");
checks.push("local_mode_neighborhood");

/* ---- 纯函数面：buildSimNodes 必须按传入的（过滤后的）边铺邻居 ----
   doc2 给一个记忆坐标（否则空 saved 下所有节点都走外环，邻居位分支根本不触发）；
   相似边开：doc3 落在 doc2 的邻居位（固定 70 偏移）；关：只能落到外环（半径 ≥ 80 + √4×18 = 116）。 */
const prefix = source.slice(0, source.indexOf("export default function"));
const pureModule = { exports: {} };
vm.runInNewContext(compile(`${prefix}\nexport { buildSimNodes, filterEdges };\n`), {
  module: pureModule, exports: pureModule.exports, require: () => ({}),
});
const { buildSimNodes, filterEdges } = pureModule.exports;
const kindsAll = { wiki: true, source: true, similar: true };
assert.equal(filterEdges([{ source: "a", target: "b", kind: "future_kind" }], kindsAll).length, 1,
  "未知 kind 必须保留（后端只加不改）");
const placed = edges => {
  const list = buildSimNodes(graph.nodes, filterEdges(edges, kindsAll), { "document:2": { x: 0, y: 0 } });
  const at = id => list.find(node => node.id === id);
  return { d23: Math.hypot(at("document:3").x - at("document:2").x, at("document:3").y - at("document:2").y), r3: Math.hypot(at("document:3").x, at("document:3").y) };
};
const on = placed(graph.edges);
const off = placed([graph.edges[0]]);
assert.ok(on.d23 <= 80, `相似边开：doc3 应铺在 doc2 邻居位（实测 ${on.d23.toFixed(1)}）`);
assert.ok(off.r3 >= 100, `相似边关：doc3 应落外环（实测半径 ${off.r3.toFixed(1)}）`);
checks.push("build_sim_nodes_uses_filtered", "unknown_kind_kept");

console.log(`graphlink-toggle: PASS (${checks.length})`);
console.log(checks.join(" · "));

/* ---- 真实 payload 模式（P4 复验）：
   node frontend/scripts/check_graphlink_toggle.mjs --payload <graph-payload.json> [--payload-off <graph-payload-OFF.json>]
   只读审计产物；期望值全部由 payload 自身独立重算（不引用后端 stats.isolated），开/关两态共用同一份 ON payload ⇒
   「关掉开关后显示的孤立数」若仍等于后端 stats.isolated 就会被当场抓住。 ---- */
const argAt = (name) => { const at = process.argv.indexOf(name); return at === -1 ? null : process.argv[at + 1]; };
const payloadPath = argAt("--payload");
if (payloadPath) {
  const data = JSON.parse(fs.readFileSync(payloadPath, "utf8"));
  const offPath = argAt("--payload-off");
  const similar = data.edges.filter((edge) => edge.kind === "similar");
  assert.ok(similar.length > 0, `payload 内没有 kind="similar" 的边：${payloadPath}`);
  const degree = new Map(data.nodes.map((node) => [node.id, 0]));
  for (const edge of similar) {
    assert.ok(degree.has(edge.source) && degree.has(edge.target), `边端点不在 nodes 内：${edge.source} -> ${edge.target}`);
    degree.set(edge.source, degree.get(edge.source) + 1);
    degree.set(edge.target, degree.get(edge.target) + 1);
  }
  const isolatedOn = [...degree.values()].filter((value) => value === 0).length;
  const [pivotId, pivotDegree] = [...degree.entries()].sort((a, b) => b[1] - a[1])[0];
  const pivotNode = data.nodes.find((node) => node.id === pivotId);
  const pivot = { type: pivotNode.type, id: pivotNode.source_id ?? Number(pivotId.slice(pivotId.lastIndexOf(":") + 1)) };
  const realChecks = [];
  const hr = await mount(source, { nodes: data.nodes, edges: data.edges, stats: data.stats });
  const storeR = () => hr.ref("dataRef").current;
  const visibleR = () => hr.ref("visibleIdsRef").current;
  const adjacencyTotal = () => [...storeR().adjacency.values()].reduce((sum, set) => sum + set.size, 0);
  const readings = { payload: payloadPath, nodes: data.nodes.length, similar_edges: similar.length, backend_stats: data.stats };

  assert.deepEqual(hr.labels().sort(), ["来源", "双链", "相似边", "笔记", "文档", "未创建", "隐藏孤立节点", "局部图"].sort());
  assert.ok(hr.text().includes("不是事实关系") && hr.text().includes("内容接近"), "图例必须写明相似边不是事实关系");
  realChecks.push("legend_similar_and_hint");

  // ① 开关开
  assert.equal(storeR().edges.length, similar.length, "开：store 边集 = 全部 similar 边");
  assert.equal(adjacencyTotal(), similar.length * 2, "开：邻接表覆盖每条边两端（力导向 / 邻域 / 绘制共用）");
  assert.equal(storeR().adjacency.get(pivotId).size, pivotDegree, `开：最大度节点 ${pivotId} 的邻接数`);
  assert.ok(hr.text().includes(`边 ${similar.length}`), "开：屏幕上的边数");
  assert.ok(hr.text().includes(`孤立 ${isolatedOn}`), `开：屏幕上的孤立数应 = 本地重算 ${isolatedOn}`);
  assert.equal(visibleR().size, data.nodes.length, "开：未隐藏孤立时节点全可见");
  readings.ON = { store_edges: storeR().edges.length, adjacency_total: adjacencyTotal(), isolated_shown: isolatedOn, visible: visibleR().size };
  realChecks.push("on_edges_and_isolated");

  // ② 开关关（同一份 payload，后端 stats.isolated 仍是开态值 ⇒ 只隐藏线的实现会在此露馅）
  hr.change("相似边", false);
  hr.render();
  assert.equal(storeR().edges.length, 0, "关：store 边集清空");
  assert.equal(adjacencyTotal(), 0, "关：邻接表清空");
  assert.ok(hr.text().includes("边 0"), "关：屏幕上的边数");
  assert.ok(hr.text().includes(`孤立 ${data.nodes.length}`), `关：屏幕上的孤立数应 = 全部 ${data.nodes.length}（后端 stats.isolated=${data.stats.isolated} 不得上屏）`);
  readings.OFF = { store_edges: storeR().edges.length, adjacency_total: adjacencyTotal(), isolated_shown: data.nodes.length, backend_stats_isolated: data.stats.isolated };
  realChecks.push("off_edges_and_isolated");

  // ③ 隐藏孤立 / 重置布局同样按过滤后的边
  hr.change("隐藏孤立节点", true);
  hr.render();
  assert.equal(visibleR().size, 0, "关 + 隐藏孤立：全部节点都是孤立");
  hr.change("相似边", true);
  hr.render();
  assert.equal(visibleR().size, data.nodes.length - isolatedOn, `开 + 隐藏孤立：只剩有边的 ${data.nodes.length - isolatedOn} 个节点`);
  hr.change("隐藏孤立节点", false);
  hr.change("相似边", false);
  hr.render();
  hr.click("重置布局");
  assert.equal(storeR().edges.length, 0, "关 + 重置布局：按过滤后的边重建 store");
  hr.change("相似边", true);
  hr.render();
  assert.equal(storeR().edges.length, similar.length, "开 + 重置布局：重建出全部 similar 边");
  realChecks.push("hide_isolated_and_reset_use_filtered");

  // ④ 局部图 / 邻域：焦点取 payload 里度最大的节点
  const hf = await mount(source, { nodes: data.nodes, edges: data.edges, stats: data.stats }, pivot);
  assert.equal(hf.ref("visibleIdsRef").current.size, 1 + pivotDegree, `局部图（开）：${pivotId} + ${pivotDegree} 个邻居`);
  hf.change("相似边", false);
  hf.render();
  assert.deepEqual([...hf.ref("visibleIdsRef").current], [pivotId], "局部图（关）：只剩焦点本身");
  readings.local_mode = { focus: pivotId, on: 1 + pivotDegree, off: 1 };
  realChecks.push("local_mode_neighborhood");

  // ⑤ 与审计另存的关态 payload 交叉核对（若提供）
  if (offPath) {
    const off = JSON.parse(fs.readFileSync(offPath, "utf8"));
    assert.equal(off.edges.length, 0, "OFF payload 应无边");
    assert.equal(off.nodes.length, data.nodes.length, "OFF payload 节点数应与 ON 一致");
    assert.equal(off.stats.isolated, off.nodes.length, "OFF payload 应全部孤立");
    assert.equal(readings.OFF.isolated_shown, off.stats.isolated, "页面关态的孤立数应与 OFF payload 一致");
    readings.off_payload = { path: offPath, edges: off.edges.length, isolated: off.stats.isolated };
    realChecks.push("cross_check_off_payload");
  }

  console.log(`graphlink-toggle-real: PASS (${realChecks.length})`);
  console.log(realChecks.join(" · "));
  console.log(`readings ${JSON.stringify(readings)}`);
}
