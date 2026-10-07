// Run from repository root: node frontend/scripts/check_opt5_citations.mjs
// --source-root selects an isolated frontend; --fixture selects the same frozen JSON used by pytest.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import crypto from "node:crypto";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(path.join(frontend, "package.json"));
const ts = require("typescript");
const option = (name, fallback) => {
  const index = process.argv.indexOf(name);
  return index < 0 ? fallback : path.resolve(process.argv[index + 1]);
};
const root = option("--source-root", frontend);
const fixturePath = option("--fixture", path.join(root, "../backend/tests/fixtures/opt5_citations.json"));
const fixtureBytes = fs.readFileSync(fixturePath);
const fixture = JSON.parse(fixtureBytes);
const source = fs.readFileSync(path.join(root, "src/citation.ts"), "utf8");
const module = { exports: {} };
const js = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
vm.runInNewContext(js, { module, exports: module.exports });
const { resolveCitations } = module.exports;
const results = [];
for (const test of fixture.cases) {
  const refs = structuredClone(test.refs);
  const actual = resolveCitations(test.content, refs);
  assert.equal(actual.mode, test.mode, test.id);
  assert.deepEqual(Array.from(actual.items, item => item.title), test.titles, test.id);
  assert.ok(actual.items.every(item => item.citation_mode === actual.mode), test.id);
  assert.deepEqual(refs, test.refs, test.id);
  results.push({ id: test.id, mode: actual.mode, titles: Array.from(actual.items, item => item.title), verdict: "PASS" });
}
const sha = bytes => crypto.createHash("sha256").update(bytes).digest("hex");
console.log(JSON.stringify({
  schema: fixture.schema, scope: fixture.scope, fixture_sha256: sha(fixtureBytes),
  citation_source_sha256: sha(source), cases: results, verdict: "PASS",
}));
