// Run from repository root: node frontend/scripts/check_settings_bindings.mjs
// Executes the real dialog's save handler with TypeScript and Node stdlib; no API/model calls.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ts = createRequire(path.join(frontend, "package.json"))("typescript");
const source = fs.readFileSync(path.join(frontend, "src/components/SettingsDialog.tsx"), "utf8");
const ast = ts.createSourceFile("SettingsDialog.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = ["shortLabel", "toPayload", "markedModels", "defaultOptions", "save"];
const declarations = new Map();
function visit(node) {
  if (ts.isFunctionDeclaration(node) && names.includes(node.name?.text)) {
    declarations.set(node.name.text, node.getText(ast));
  }
  ts.forEachChild(node, visit);
}
visit(ast);
assert.equal(declarations.size, names.length, "real dialog handlers must be present");
const executable = ts.transpileModule(
  [...declarations.values(), "globalThis.saveDialog = save;"].join("\n"),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } },
).outputText;
const labels = { chat: "对话模型", embed: "向量模型", vision: "视觉模型" };
const service = (id, models) => ({
  id, name: id, provider: "mimo", base_url: "https://example.invalid/v1",
  models, for_roles: ["chat", "vision", "embed"], api_key: "", has_key: true,
});
const chat = service("chat-service", ["chat-model"]);
const embed = service("embed-service", ["embed-model"]);
const bound = (item, model = item.models[0]) => ({ service_id: item.id, model });
const copy = value => JSON.parse(JSON.stringify(value));
async function save({ role = "chat", selected = chat, services = [selected], roles = {}, marks = {}, defaults = {}, ok = true } = {}) {
  const result = { writes: [], errors: [], closes: 0 };
  const context = vm.createContext({
    services, selected, settings: { roles }, marks, defaults,
    managed: role === "chat" ? ["chat", "vision"] : ["embed"], primary: role,
    ALL_ROLES: ["chat", "vision", "embed"], ROLE_LABEL: labels,
    setSaveError: message => result.errors.push(message),
    onSave: async payload => { result.writes.push(copy(payload)); return ok; },
    onClose: () => { result.closes += 1; },
  });
  vm.runInContext(executable, context);
  await context.saveDialog();
  return result;
}
const cases = [
  ["one marked chat/vision model activates both on first save", async () => {
    const result = await save({ marks: { [chat.id]: { "chat-model": ["chat", "vision"] } } });
    assert.equal(result.writes.length, 1);
    assert.deepEqual(result.writes[0].roles, { chat: bound(chat), vision: bound(chat) });
    assert.equal(result.closes, 1);
  }],
  ["one marked embedding model activates on first save and keeps chat", async () => {
    const result = await save({
      role: "embed", selected: embed, services: [chat, embed], roles: { chat: bound(chat) },
      marks: { [embed.id]: { "embed-model": ["embed"] } },
    });
    assert.deepEqual(result.writes[0]?.roles, { chat: bound(chat), embed: bound(embed) });
  }],
  ["multiple marked chat models without a default cannot claim success", async () => {
    const selected = service("chat-service", ["first", "second"]);
    const result = await save({ selected, marks: { [selected.id]: { first: ["chat"], second: ["chat"] } } });
    assert.equal(result.writes.length, 0);
    assert.equal(result.closes, 0);
    assert.match(result.errors.at(-1), /默认.*对话|对话.*默认/);
  }],
  ["multiple marked embedding models require an explicit default", async () => {
    const selected = service("embed-service", ["first", "second"]);
    const result = await save({ role: "embed", selected, marks: { [selected.id]: { first: ["embed"], second: ["embed"] } } });
    assert.equal(result.writes.length, 0);
    assert.match(result.errors.at(-1), /默认.*向量|向量.*默认/);
  }],
  ["an explicit default wins among multiple marked models", async () => {
    const selected = service("chat-service", ["first", "second"]);
    const result = await save({
      selected, marks: { [selected.id]: { first: ["chat"], second: ["chat"] } },
      defaults: { [selected.id]: { chat: "second" } },
    });
    assert.deepEqual(result.writes[0]?.roles.chat, bound(selected, "second"));
  }],
  ["switching supplier with one marked model uses the selected supplier", async () => {
    const selected = service("new-service", ["new-model"]);
    const result = await save({
      selected, services: [chat, selected], roles: { chat: bound(chat) },
      marks: { [selected.id]: { "new-model": ["chat"] } },
    });
    assert.deepEqual(result.writes[0]?.roles.chat, bound(selected));
  }],
  ["switching supplier with multiple models cannot silently keep the old supplier", async () => {
    const selected = service("new-service", ["first", "second"]);
    const result = await save({
      selected, services: [chat, selected], roles: { chat: bound(chat) },
      marks: { [selected.id]: { first: ["chat"], second: ["chat"] } },
    });
    assert.equal(result.writes.length, 0);
  }],
  ["a model without a role mark is not automatically assigned", async () => {
    const result = await save();
    assert.equal(result.writes.length, 0);
    assert.match(result.errors.at(-1), /标「对话」/);
  }],
  ["an existing explicit binding remains usable without a role mark", async () => {
    const result = await save({ roles: { chat: bound(chat) }, defaults: { [chat.id]: { chat: "chat-model" } } });
    assert.deepEqual(result.writes[0]?.roles.chat, bound(chat));
  }],
  ["saving chat keeps the embedding binding", async () => {
    const result = await save({
      services: [chat, embed], roles: { embed: bound(embed) },
      marks: { [chat.id]: { "chat-model": ["chat"] } },
    });
    assert.deepEqual(result.writes[0]?.roles.embed, bound(embed));
  }],
  ["removing a supplier also removes its managed or unmanaged bindings", async () => {
    const result = await save({
      roles: { embed: bound(embed) }, marks: { [chat.id]: { "chat-model": ["chat"] } },
    });
    assert.equal(result.writes[0]?.roles.embed, undefined);
  }],
  ["an unmarked optional vision role can still be disabled", async () => {
    const result = await save({
      roles: { vision: bound(chat) }, marks: { [chat.id]: { "chat-model": ["chat"] } },
    });
    assert.equal(result.writes[0]?.roles.vision, undefined);
  }],
  ["multiple vision candidates without a default cannot silently stay unbound", async () => {
    const selected = service("chat-service", ["first", "second"]);
    const result = await save({
      selected, marks: { [selected.id]: { first: ["chat", "vision"], second: ["vision"] } },
    });
    assert.equal(result.writes.length, 0);
    assert.match(result.errors.at(-1), /默认.*视觉|视觉.*默认/);
  }],
  ["blank API Key is omitted to preserve the stored credential", async () => {
    const result = await save({ marks: { [chat.id]: { "chat-model": ["chat"] } } });
    assert.equal("api_key" in result.writes[0].services[0], false);
  }],
  ["a failed API save leaves the dialog open for retry", async () => {
    const result = await save({ marks: { [chat.id]: { "chat-model": ["chat"] } }, ok: false });
    assert.equal(result.writes.length, 1);
    assert.equal(result.closes, 0);
  }],
];
let failures = 0;
for (const [name, check] of cases) {
  try {
    await check();
    console.log(`PASS ${name}`);
  } catch (error) {
    failures += 1;
    console.error(`FAIL ${name}: ${error.message}`);
  }
}
console.log(`${cases.length - failures} passed, ${failures} failed`);
process.exitCode = failures ? 1 : 0;
