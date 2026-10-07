// Run: node frontend/scripts/check_u4_citations.mjs [--source-root <frontend-copy>]
// Executes the actual resolver and CitationList with installed TypeScript and Node stdlib.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(path.join(frontend, "package.json"));
const ts = require("typescript");
const arg = process.argv.indexOf("--source-root");
const root = arg < 0 ? frontend : path.resolve(process.argv[arg + 1]);
const react = {
  createElement: (type, props, ...children) => ({ type, props: { ...props, children } }),
  useState: value => [value, () => {}],
  useRef: value => ({ current: value }),
  useEffect: () => {},
};
function load(file) {
  const module = { exports: {} };
  const js = ts.transpileModule(fs.readFileSync(path.join(root, file), "utf8"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React },
  }).outputText;
  vm.runInNewContext(js, {
    module, exports: module.exports, React: react,
    require: name => name === "react" ? react : { cx: (...values) => values.filter(Boolean).join(" ") },
  });
  return module.exports;
}
const { resolveCitations, citeMarkers, validCiteSet } = load("src/citation.ts");
const CitationList = load("src/components/CitationList.tsx").default;
const ref = (title, ordinal) => ({ type: "document", title, snippet: title, score: 1, ordinal });
const sparse = [ref("second", 2), ref("fourth", 4)];
const plain = value => JSON.parse(JSON.stringify(value));
const check = (content, refs, mode, titles) => {
  const result = resolveCitations(content, refs);
  assert.equal(result.mode, mode, content);
  assert.deepEqual(plain(result.items.map(item => item.title)), titles, content);
  assert.ok(result.items.every(item => item.citation_mode === mode));
  return result;
};
check("[4] [2] [4]", sparse, "cited", ["second", "fourth"]);
check("[4]", sparse, "cited", ["fourth"]);
check("[1]", sparse, "fallback", ["second", "fourth"]);
check("[1] [4]", sparse, "cited", ["fourth"]);
check("[01]", [ref("legacy"), ref("other")], "cited", ["legacy"]);
check("[2]", [ref("legacy", 0), ref("other", null)], "cited", ["other"]);
check("[1]", [ref("legacy", -1)], "cited", ["legacy"]);
check("[1]", [ref("legacy", 1.5)], "cited", ["legacy"]);
check("[2]", [ref("explicit", 2), ref("legacy")], "cited", ["explicit", "legacy"]);
check("[02] [٠٤] [４] [𝟚] [۰۲] [२]", sparse, "cited", ["second", "fourth"]);
check(`[${"9".repeat(5000)}] [4]`, sparse, "cited", ["fourth"]);
check(`[${"٠".repeat(5000)}٤]`, sparse, "cited", ["fourth"]);
for (const content of ["no marker", "[0]", "[99]", "［2］", "[-2]", "[ 2 ]", "[2.0]"]) {
  check(content, sparse, "fallback", ["second", "fourth"]);
}
check("[2]", [], "none", []);
check("[2]", null, "none", []);
check("[2]", [{ title: null }, sparse[0]], "cited", ["second"]);
assert.deepEqual(plain(citeMarkers("[٠٢] [４] [𝟚] [२] [01]")), [2, 4, 2, 2, 1]);
assert.deepEqual([...validCiteSet("[1] [01] [3]", 2)], [1]);
assert.ok(sparse.every(item => item.citation_mode === undefined), "resolver must not mutate stored refs");

function text(tree) {
  if (tree == null || tree === false) return "";
  if (typeof tree !== "object") return String(tree);
  if (Array.isArray(tree)) return tree.map(text).join("");
  return text(tree.props.children);
}
function find(tree, type) {
  if (!tree || typeof tree !== "object") return;
  if (tree.type === type) return tree;
  for (const child of tree.props?.children ?? []) {
    const match = find(child, type);
    if (match) return match;
  }
}
const fallback = CitationList({ items: sparse, mode: "fallback", onOpen: () => {} });
assert.ok(text(fallback).includes("候选资料 2 条"));
assert.ok(text(fallback).includes("尚未确认回答使用了这些资料，也未确认它们支持回答"));
assert.equal(find(fallback, "button").props.title, "展开候选资料");
assert.ok(!text(fallback).includes("引用 2 条资料"));
const cited = CitationList({ items: sparse, mode: "cited", onOpen: () => {} });
assert.ok(text(cited).includes("引用 2 条资料"));
assert.ok(!text(cited).includes("候选资料"));
assert.equal(find(cited, "button").props.title, "展开引用资料");
assert.equal(CitationList({ items: [], mode: "none", onOpen: () => {} }), null);
console.log("U4 frontend PASS: sparse ordinals, legacy refs, Unicode/huge markers, candidate labels and unchanged cited labels");
