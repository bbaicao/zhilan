// Run: node frontend/scripts/check_u5_graph.mjs [--baseline-ref <git-ref>] [--output <json>] [--source-root <frontend-copy>]
// Executes GraphView's real effects / handlers and layout step. Canvas calls are mocked:
// these are Node scheduling / algorithm measurements, not browser rendering or CPU measurements.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import os from "node:os";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { performance } from "node:perf_hooks";
import { fileURLToPath } from "node:url";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(path.join(frontend, "package.json"));
const ts = require("typescript");
const file = "src/components/GraphView.tsx";
const option = name => { const i = process.argv.indexOf(name); return i < 0 ? null : process.argv[i + 1]; };
const sourceRoot = option("--source-root") ? path.resolve(option("--source-root")) : frontend;
const source = fs.readFileSync(path.join(sourceRoot, file), "utf8");
const baselineRef = option("--baseline-ref");
const baseline = baselineRef
  ? execFileSync("git", ["show", `${baselineRef}:frontend/${file}`], { cwd: frontend, encoding: "utf8" })
  : null;
const plain = value => JSON.parse(JSON.stringify(value));
const compile = text => ts.transpileModule(text, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React },
}).outputText;

function harness(text, graph) {
  const hooks = [], effects = [], events = new Map(), windowEvents = new Map(), queue = new Map();
  const storage = new Map(), labels = [];
  let cursor = 0, nextId = 1, frames = 0, draws = 0, tree, resize;
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
      if (changed(hooks[i]?.deps, deps)) {
        effects.push(() => { hooks[i]?.cleanup?.(); hooks[i] = { deps, cleanup: fn() }; });
      }
    },
  };
  const ctx = new Proxy({
    clearRect: () => { draws += 1; },
    fillText: label => labels.push(label),
  }, { get: (target, key) => key in target ? target[key] : () => {} });
  const wrap = { clientWidth: 1000, clientHeight: 720 };
  const canvas = {
    style: {}, getContext: () => ctx,
    getBoundingClientRect: () => ({ left: 0, top: 0 }),
    addEventListener: (name, fn) => events.set(name, fn),
    removeEventListener: (name, fn) => { if (events.get(name) === fn) events.delete(name); },
    setPointerCapture: () => {}, releasePointerCapture: () => {},
  };
  const module = { exports: {} };
  const context = vm.createContext({
    console, module, exports: module.exports, React: react,
    require: name => name === "react" ? react : name === "../api"
      ? { getGraph: () => Promise.resolve(graph), ApiError: Error }
      : name === "../theme" ? { THEME_CHANGE_EVENT: "theme" } : {},
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    },
    document: { documentElement: { dataset: { theme: "dark" } } },
    window: {
      devicePixelRatio: 1,
      addEventListener: (name, fn) => windowEvents.set(name, fn),
      removeEventListener: (name, fn) => { if (windowEvents.get(name) === fn) windowEvents.delete(name); },
    },
    ResizeObserver: class { constructor(fn) { resize = fn; } observe() {} disconnect() {} },
    requestAnimationFrame: fn => { const id = nextId++; queue.set(id, fn); return id; },
    cancelAnimationFrame: id => queue.delete(id),
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
      focus: null, onOpenNote: () => {}, onOpenDocument: () => {}, onNotify: () => {}, onClearFocus: () => {},
    });
    walk(tree, node => { if (node.props.ref) node.props.ref.current = node.type === "canvas" ? canvas : wrap; });
    for (const effect of effects.splice(0)) effect();
  };
  const ref = name => {
    const ast = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    let index = -1, found;
    function visit(node) {
      if (ts.isVariableDeclaration(node) && node.initializer && ts.isCallExpression(node.initializer)
        && /^use(Ref|State|Memo|Callback)$/.test(node.initializer.expression.getText())) {
        index += 1;
        if (node.name.getText() === name) found = hooks[index];
      }
      if (ts.isCallExpression(node) && node.expression.getText() === "useEffect") index += 1;
      ts.forEachChild(node, visit);
    }
    visit(ast);
    assert.ok(found, `missing hook ${name}`);
    return found;
  };
  return {
    render, ref, storage, labels, context,
    get frames() { return frames; }, get draws() { return draws; }, get pending() { return queue.size; },
    flush(max = 2000) {
      let count = 0;
      while (queue.size && count < max) {
        const [id, fn] = queue.entries().next().value;
        queue.delete(id);
        fn(count * (1000 / 60));
        frames += 1; count += 1;
      }
      return count;
    },
    event: (name, extra = {}) => {
      assert.ok(events.has(name), `missing ${name} listener`);
      events.get(name)({ clientX: 500, clientY: 360, pointerId: 1, deltaY: -1, preventDefault() {}, ...extra });
    },
    change(label, checked) {
      let match;
      const content = node => typeof node === "string" ? node : node?.props?.children?.map(content).join("") ?? "";
      walk(tree, node => {
        if (node.type === "label" && content(node).trim() === label) {
          walk(node, child => { if (child.type === "input") match = child; });
        }
      });
      assert.ok(match, label); match.props.onChange({ target: { checked } });
    },
    click(label) {
      let match;
      const content = node => typeof node === "string" ? node : node?.props?.children?.map(content).join("") ?? "";
      walk(tree, node => { if (node.type === "button" && content(node).includes(label)) match = node; });
      assert.ok(match, label); match.props.onClick();
    },
    theme() { context.document.documentElement.dataset.theme = "light"; windowEvents.get("theme")(); },
    resize: () => resize(),
    cleanup: () => { for (const hook of hooks) hook?.cleanup?.(); },
  };
}

const graphOf = (count, links = 1, spread = 120) => {
  const nodes = Array.from({ length: count }, (_, i) => ({
    id: `note:${i}`, type: "note", source_id: i, title: `中文节点${i}`,
    category: "测试", tags: [], degree: links * 2,
    x: (i % Math.ceil(Math.sqrt(count))) * spread,
    y: Math.floor(i / Math.ceil(Math.sqrt(count))) * spread, vx: 0, vy: 0, pinned: false,
  }));
  const edges = nodes.flatMap((node, i) => Array.from({ length: links }, (_, j) => ({
    source: node.id, target: nodes[(i + j + 1) % count].id, kind: "wiki",
  })));
  return { nodes, edges, stats: { notes: count, documents: 0, ghosts: 0, edges: edges.length, isolated: 0 } };
};
async function mount(text, graph) {
  const h = harness(text, graph);
  h.render();
  await Promise.resolve(); await Promise.resolve();
  h.render();
  return h;
}
const h = await mount(source, graphOf(12));
const interactionChecks = [];
// Pinning all nodes makes zero layout work an exact control, independent of convergence.
for (const node of h.ref("dataRef").current.nodes) node.pinned = true;
h.ref("energyRef").current = 0;
h.flush(240);
const stationary = { scheduled_callbacks: h.pending, callbacks_after_240_budget: h.frames, draws: h.draws };
if (!option("--measure-only")) {
  assert.equal(h.pending, 0, "a stationary graph must leave no RAF task");
  const before = h.draws;
  for (const [name, wake] of [
    ["theme", () => h.theme()], ["resize", () => h.resize()], ["fit", () => h.click("显示全貌")],
    ["zoom", () => h.event("wheel")],
    ["pan", () => { h.event("pointerdown", { clientX: -100, clientY: -100 }); h.event("pointermove", { clientX: -90, clientY: -80 }); h.event("pointerup", { clientX: -90, clientY: -80 }); }],
  ]) {
    wake(); assert.ok(h.pending > 0, "an interaction must wake the canvas");
    h.flush(); assert.equal(h.pending, 0, "interaction must settle back to zero tasks");
    interactionChecks.push(name);
  }
  assert.ok(h.draws > before);
  const data = h.ref("dataRef").current, node = data.nodes[0], view = h.ref("viewRef").current;
  const x = node.x * view.k + view.x, y = node.y * view.k + view.y;
  h.event("pointerdown", { clientX: x, clientY: y });
  h.event("pointermove", { clientX: x + 30, clientY: y + 20 });
  h.event("pointerup", { clientX: x + 30, clientY: y + 20 });
  h.render(); h.flush();
  assert.ok(node.pinned);
  assert.ok(h.storage.get("zhilan-graph-coords-v1")?.includes(node.id));
  assert.ok(h.labels.some(label => label.includes("中文")));
  interactionChecks.push("drag", "pin_and_persist", "chinese_label");
  h.change("笔记", false); h.render(); h.flush();
  assert.equal(h.ref("visibleIdsRef").current.size, 0);
  assert.equal(h.pending, 0);
  h.change("笔记", true); h.render(); h.flush();
  assert.equal(h.ref("visibleIdsRef").current.size, 12);
  assert.equal(h.pending, 0);
  interactionChecks.push("filter_off_and_on");
  h.ref("energyRef").current = 0;
  h.click("重置布局"); h.flush();
  assert.ok(data !== h.ref("dataRef").current, "reset must replace graph data");
  assert.ok(h.frames > stationary.callbacks_after_240_budget);
  assert.equal(h.pending, 0, "unfixed reset graph must naturally settle");
  interactionChecks.push("data_replacement", "reset_and_natural_settle");
  h.cleanup(); assert.equal(h.pending, 0, "unmount cancels outstanding frames");
}
h.cleanup();

function stepFactory(text) {
  const ast = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let step;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText() === "step") step = node.initializer.getText();
    ts.forEachChild(node, visit);
  }
  visit(ast); assert.ok(step, "real source layout step");
  const prefix = text.slice(0, text.indexOf("export default function"));
  const module = { exports: {} };
  let randomState = 12345;
  const math = Object.create(Math);
  math.random = () => ((randomState = (randomState * 1664525 + 1013904223) >>> 0) / 2 ** 32);
  vm.runInNewContext(compile(prefix + `
    export function makeStep(dataRef, energyRef, dragRef, visibleIds) { return ${step}; }
    export { makeStore };
  `), {
    module, exports: module.exports, require: () => ({}), Math: math,
  });
  return module.exports;
}
const percentile = (samples, p) => [...samples].sort((a, b) => a - b)[Math.ceil(samples.length * p) - 1];
function resetGeometry(nodes, count) {
  nodes.forEach((node, i) => {
    node.x = (i % Math.ceil(Math.sqrt(count))) * 120;
    node.y = Math.floor(i / Math.ceil(Math.sqrt(count))) * 120;
    node.vx = node.vy = 0; node.pinned = false;
  });
}
async function measurePair(nextText, oldText, fullFrame = false) {
  const versions = oldText ? [nextText, oldText] : [nextText];
  const factories = fullFrame ? null : versions.map(stepFactory);
  const results = versions.map(() => []);
  for (const count of [100, 500, 1500, 3000]) for (const links of [1, 8]) {
    const cases = [];
    for (let i = 0; i < versions.length; i += 1) {
      if (fullFrame) {
        const h = await mount(versions[i], graphOf(count, links));
        cases.push({
          samples: [], nodes: h.ref("dataRef").current.nodes, h,
          prepare: () => { h.ref("energyRef").current = 1; if (!h.pending) h.resize(); },
          run: () => h.flush(1),
        });
      } else {
        const graph = graphOf(count, links), energy = { current: 1 };
        const { makeStep, makeStore } = factories[i];
        cases.push({
          samples: [], nodes: graph.nodes, prepare: () => { energy.current = 1; },
          run: makeStep({ current: makeStore(graph.nodes, graph.edges) }, energy,
            { current: { mode: "none", id: null } }, new Set(graph.nodes.map(node => node.id))),
        });
      }
    }
    // Alternate version order on each sample; always restore equal input geometry.
    for (let round = 0; round < 34; round += 1) {
      const order = round % 2 ? [...cases.keys()].reverse() : [...cases.keys()];
      for (const i of order) {
        const item = cases[i]; resetGeometry(item.nodes, count); item.prepare();
        const start = performance.now(); item.run(); const ms = performance.now() - start;
        if (round >= 4) item.samples.push(ms);
      }
    }
    cases.forEach((item, i) => {
      results[i].push({ nodes: count, edges: count * links, samples: item.samples.length,
        p50_ms: percentile(item.samples, .5), p95_ms: percentile(item.samples, .95) });
      item.h?.cleanup();
    });
  }
  return results;
}
function forceEquivalence(oldText, nextText) {
  const cases = [];
  for (const kind of ["negative_and_boundary", "exact_cutoff", "overlap_across_boundary", "dense_cluster"]) {
    const graph = graphOf(1200, 2);
    graph.nodes.forEach((node, i) => {
      node.x = i % 20 * 120 - 720; node.y = Math.floor(i / 20) * 120 - 720;
      node.pinned = i % 13 === 0; node.vx = (i % 3 - 1) * .1; node.vy = (i % 5 - 2) * .1;
    });
    if (kind === "exact_cutoff") {
      Object.assign(graph.nodes[1], { x: -640, y: 40 });
      Object.assign(graph.nodes[2], { x: 0, y: 40 });
      Object.assign(graph.nodes[3], { x: 640.001, y: 40 });
    }
    if (kind === "overlap_across_boundary") {
      for (let i = 1; i <= 8; i += 1) Object.assign(graph.nodes[i], { x: i % 2 ? 639.9 : 640.1, y: i % 3 ? -640.1 : -639.9 });
    }
    if (kind === "dense_cluster") {
      graph.nodes.forEach((node, i) => { node.x = i % 20 * .15; node.y = Math.floor(i / 20) * .15; });
    }
    const run = text => {
      const { makeStore, makeStep } = stepFactory(text), copy = plain(graph);
      const energy = { current: 1 };
      makeStep({ current: makeStore(copy.nodes, copy.edges) }, energy, { current: { mode: "node", id: "note:2" } },
        new Set(copy.nodes.filter((_, i) => i % 11 !== 0).map(node => node.id)))();
      return { nodes: copy.nodes, energy: energy.current };
    };
    const old = run(oldText), next = run(nextText);
    let maxDelta = 0;
    old.nodes.forEach((node, i) => {
      for (const key of ["x", "y", "vx", "vy"]) {
        const delta = Math.abs(node[key] - next.nodes[i][key]);
        maxDelta = Math.max(maxDelta, delta);
        assert.ok(delta <= 1e-8 * Math.max(1, Math.abs(node[key])), `${kind} ${node.id}.${key} delta=${delta}`);
      }
      if (node.pinned) assert.equal(node.x, graph.nodes[i].x, "fixed nodes never move");
    });
    assert.ok(Math.abs(old.energy - next.energy) <= 1e-10);
    cases.push({ case: kind, nodes: graph.nodes.length, passed: true, max_absolute_delta: maxDelta });
  }
  return cases;
}
let baselineStationary = null;
if (baseline) {
  const old = await mount(baseline, graphOf(12));
  for (const node of old.ref("dataRef").current.nodes) node.pinned = true;
  old.ref("energyRef").current = 0; old.flush(240);
  baselineStationary = { scheduled_callbacks: old.pending, callbacks_after_240_budget: old.frames, draws: old.draws };
  old.cleanup();
}
const [layout, baselineLayout = null] = await measurePair(source, baseline);
const [mockFrame, baselineMockFrame = null] = await measurePair(source, baseline, true);
const result = {
  generated_at: new Date().toISOString(), node: process.version, platform: process.platform,
  cpu: os.cpus()[0]?.model, logical_cpus: os.cpus().length,
  method: "Actual GraphView TypeScript executed in Node VM; RAF and Canvas mocked. 4 warmup + 30 reset-geometry samples per load and version, alternating version order. Layout excludes drawing; mock_frame includes real draw callback plus mocked Canvas calls and excludes browser paint / real idle CPU.",
  browser_rendering_measured: false, stationary_cpu_measured: false,
  scheduler_check: option("--measure-only") ? "measurement_only" : "pass",
  interaction_checks: interactionChecks,
  force_equivalence: baseline ? forceEquivalence(baseline, source) : null,
  baseline_ref: baselineRef, stationary, baseline_stationary: baselineStationary,
  layout, baseline_layout: baselineLayout, mock_frame: mockFrame, baseline_mock_frame: baselineMockFrame,
};
if (option("--output")) {
  const output = path.resolve(option("--output"));
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, JSON.stringify(result, null, 2) + "\n");
}
console.log(JSON.stringify(result, null, 2));
