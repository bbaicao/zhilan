// Run: node frontend/scripts/check_u1_links.mjs [--source-root <frontend-copy>]
// Executes the actual Markdown plugin, NotesView resolver and click handlers with installed parser / TypeScript.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(path.join(frontend, "package.json"));
const ts = require("typescript");
const option = process.argv.indexOf("--source-root");
const root = option < 0 ? frontend : path.resolve(process.argv[option + 1]);
const markdown = "src/components/Markdown.tsx", notes = "src/components/NotesView.tsx";
const source = Object.fromEntries([markdown, notes].map(file => [file, fs.readFileSync(path.join(root, file), "utf8")]));
function find(file, predicate) {
  const ast = ts.createSourceFile(file, source[file], ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let found;
  const visit = node => { if (!found && predicate(node)) found = node; if (!found) ts.forEachChild(node, visit); };
  visit(ast);
  assert.ok(found, `missing executable source in ${file}`);
  return found;
}
function expression(file, name) {
  const node = find(file, node => (ts.isFunctionDeclaration(node) || ts.isVariableDeclaration(node)) && node.name?.getText() === name);
  return ts.isVariableDeclaration(node) ? node.initializer.getText() : node.getText().replace(/^export\s+/, "");
}
function run(code, env = {}) {
  const js = ts.transpileModule(`globalThis.result = (${code});`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React, module: ts.ModuleKind.None },
  }).outputText;
  const context = vm.createContext({ console, useMemo: fn => fn(), useCallback: fn => fn, ...env });
  vm.runInContext(js, context);
  return context.result;
}
const fn = (file, name, env) => run(expression(file, name), env);
const plain = value => JSON.parse(JSON.stringify(value));
const normalizeWikiTitle = fn(markdown, "normalizeWikiTitle");
const stripExt = fn(notes, "stripExt");
const prefix = "😀".repeat(255);
assert.equal(normalizeWikiTitle("\ufeff\u0085 名称 #锚 "), "名称");
assert.equal(normalizeWikiTitle(`${prefix}甲#锚`), prefix);
assert.equal(Array.from(normalizeWikiTitle(`${prefix}乙`)).length, 255);
assert.equal(normalizeWikiTitle("#只有锚"), "");
const boundary = normalizeWikiTitle("A".repeat(254) + " B");
assert.equal(boundary, "A".repeat(254));
assert.equal(normalizeWikiTitle(boundary), boundary);
assert.equal(stripExt(".md"), "");

const { unified } = await import(pathToFileURL(require.resolve("unified")).href);
const { default: remarkParse } = await import(pathToFileURL(require.resolve("remark-parse")).href);
const { default: remarkGfm } = await import(pathToFileURL(require.resolve("remark-gfm")).href);
const transform = fn(markdown, "transformWikiLinks", {
  normalizeWikiTitle,
  WIKI_PATTERN: run(expression(markdown, "WIKI_PATTERN")),
  wikiCarrier: fn(markdown, "wikiCarrier"),
});
const body = "[[正文#锚]] [[正文]] ![[嵌入]]\n"
  + "`[[行内]]` ``含 ` [[多反引号]]`` [[其他]]\n"
  + "`多行\n[[跨行行内]]`\n\n"
  + "```python\n[[围栏]]\n```\n"
  + "~~~~md\n[[波浪]]\n~~~\n[[短闭合仍代码]]\n~~~~\n\n"
  + "    [[缩进]]\n\t[[制表代码]]\n\n"
  + "> [[引用]]\n> ```md\n> [[引用代码]]\n> ```\n\n"
  + "- ```md\n  [[列表代码]]\n  ```\n\n"
  + "| [[表格]] |\n| --- |\n\n"
  + "\\` [[转义反引号]]\n\n"
  + "\\\\`[[偶数转义代码]]`\n\n"
  + "`[[未闭合]]";
const tree = unified().use(remarkParse).use(remarkGfm).parse(body);
transform(tree);
const extracted = [];
function collect(node) {
  const title = node.data?.hProperties?.["data-wiki-title"];
  if (title != null) extracted.push(normalizeWikiTitle(title));
  for (const child of node.children ?? []) collect(child);
}
collect(tree);
assert.deepEqual([...new Set(extracted)], ["正文", "其他", "引用", "表格", "转义反引号", "未闭合"]);

const node = (type, id, title, kb) => ({ type, source_id: id, title, kb_id: kb });
const nodes = [
  node("note", 10, "来源", 2), node("note", 20, "同名", 1),
  node("document", 30, "同名.md", 2), node("note", 40, "歧义", 2),
  node("document", 50, "歧义.md", 2), node("ghost", null, "未创建", 2),
  node("document", 60, `${prefix}甲.md`, 2), node("document", 70, `${prefix}乙.md`, 2),
  node("note", 80, "唯一笔记", 2),
];
const pool = graph => fn(notes, "wikiPool", { savedNoteId: 10, wikiGraph: graph, stripExt, normalizeWikiTitle });
const wikiPool = pool({ sourceId: 10, nodes });
assert.deepEqual(plain(wikiPool), [
  { kind: "note", id: 10, label: "来源" },
  { kind: "doc", id: 30, label: "同名" },
  { kind: "note", id: 80, label: "唯一笔记" },
]);
assert.deepEqual(plain(pool({ sourceId: 20, nodes })), []);
assert.deepEqual(plain(pool({ sourceId: 10, nodes: [] })), []);
const resolveWiki = fn(notes, "resolveWiki", { wikiPool, normalizeWikiTitle });
assert.deepEqual(plain(resolveWiki(" 同名 #段落")), { kind: "doc", id: 30 });
for (const title of ["歧义", "未创建", `${prefix}甲`]) assert.equal(resolveWiki(title), null);
const notices = [], opened = [];
const onOpenWiki = fn(notes, "handleOpenWiki", {
  onJumpNote: id => opened.push(["note", id]),
  onOpenDocument: id => opened.push(["document", id]),
});
const WikiLink = fn(markdown, "WikiLink", {
  normalizeWikiTitle,
  React: { createElement: (type, props, ...children) => ({ type, props: { ...props, children } }) },
});
for (const title of [" 同名 #段落", "唯一笔记", "歧义", "未创建"]) {
  WikiLink({ title, resolveWiki, onOpenWiki, onMissingWiki: title => notices.push(title) }).props.onClick();
}
assert.deepEqual(opened, [["document", 30], ["note", 80]]);
assert.deepEqual(notices, ["歧义", "未创建"]);

// Execute the real getGraph effect: failed / cancelled loads never enable a guessed target.
const graphEffect = find(notes, node => ts.isCallExpression(node)
  && node.expression.getText() === "useEffect" && node.arguments[0].getText().includes("getGraph()"));
let state, rejectLoad;
const pending = new Promise((_, reject) => { rejectLoad = reject; });
const effect = run(graphEffect.arguments[0].getText(), {
  savedNoteId: 10, getGraph: () => pending,
  setWikiGraph: next => { state = next; },
  console: { error: () => {} },
});
const cleanup = effect();
assert.deepEqual(plain(state), { sourceId: 10, nodes: [] });
rejectLoad(new Error("unavailable graph"));
await Promise.resolve();
await Promise.resolve();
assert.deepEqual(plain(state), { sourceId: 10, nodes: [] });
cleanup();
let resolveLoad;
const late = new Promise(resolve => { resolveLoad = resolve; });
const cancel = run(graphEffect.arguments[0].getText(), {
  savedNoteId: 10, getGraph: () => late, setWikiGraph: next => { state = next; },
})();
cancel();
resolveLoad({ nodes });
await Promise.resolve();
assert.deepEqual(plain(state), { sourceId: 10, nodes: [] });
console.log("U1.8 frontend PASS: Unicode identity, parsed Markdown code, scoped unique targets, clicks and unavailable graph");
