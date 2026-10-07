// Run: node frontend/scripts/check_chat_stream_opt2.mjs [--output <json>] [--source-file <api.ts>]
// Calls the real streamChat with native Node streams; browser/remote cancellation is a separate check.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { createRequire } from "node:module";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ts = createRequire(path.join(frontend, "package.json"))("typescript");
const option = name => {
  const index = process.argv.indexOf(name);
  return index < 0 ? undefined : path.resolve(process.argv[index + 1]);
};
const file = option("--source-file") ?? path.join(frontend, "src/api.ts");
const source = fs.readFileSync(file, "utf8");
const ast = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
function declaration(name) {
  const node = ast.statements.find(item => item.name?.getText(ast) === name ||
    (ts.isVariableStatement(item) && item.declarationList.declarations.some(item => item.name.getText(ast) === name)));
  assert.ok(node, `missing real declaration: ${name}`);
  return node.getText(ast).replace(/^export\s+/, "");
}
const js = ts.transpileModule(
  `${["ApiError", "OFFLINE", "parse", "jsonBody", "streamChat"].map(declaration).join("\n")}\nglobalThis.streamChat = streamChat;`,
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } },
).outputText;
const token = { type: "token", text: "answer" };
const done = { type: "done", chat_id: 7, refs: [], answer_source: "general" };
const stopped = { type: "stopped", chat_id: 7 };
const error = { type: "error", detail: "synthetic server failure" };
const normal = [
  { type: "meta", chat_id: 7 },
  { type: "reasoning", text: "reason" },
  { type: "agent", step: { kind: "tool", title: "synthetic" } },
  token, done,
];
const cases = [
  { id: "done_eof", events: normal, close: true, expected: ["meta", "reasoning", "agent", "token", "done"] },
  { id: "done_open_stream", events: [token, done], expected: ["token", "done"] },
  { id: "duplicate_terminal", events: [done, done, stopped, token, error], close: true, expected: ["done"] },
  { id: "stopped_partial", events: [token, stopped, done], expected: ["token", "stopped"] },
  { id: "stopped_empty", events: [stopped], expected: ["stopped"] },
  { id: "error_frame", events: [error], expected: [], message: error.detail },
  { id: "natural_eof", events: [token], close: true, expected: ["token"], eof: true },
  { id: "read_throws", readError: Error("synthetic reader failure"), expected: [] },
  { id: "abort_read", abort: true, events: [token], expected: ["token"] },
  { id: "token_callback_throws", events: [token], callback: "token", expected: ["token"] },
  { id: "done_callback_throws", events: [done], callback: "done", expected: ["done"] },
  { id: "cancel_rejects", events: [error], cancelError: Error("synthetic cleanup failure"), expected: [], message: error.detail },
];
const results = [];
for (const spec of cases) {
  const counts = { read: 0, cancel: 0, releaseLock: 0, underlyingCancel: 0 };
  const controller = new AbortController();
  const abortError = new DOMException("synthetic cancellation", "AbortError");
  const callbackError = Error("synthetic callback failure");
  const body = new ReadableStream({
    start(stream) {
      controller.signal.addEventListener("abort", () => stream.error(abortError), { once: true });
      if (spec.readError) stream.error(spec.readError);
      else {
        const bytes = new TextEncoder().encode(spec.events.map(ev => `data: ${JSON.stringify(ev)}\n\n`).join(""));
        stream.enqueue(bytes.subarray(0, 11));
        stream.enqueue(bytes.subarray(11));
        if (spec.close) stream.close();
      }
    },
    cancel() {
      counts.underlyingCancel++;
      if (spec.cancelError) return Promise.reject(spec.cancelError);
    },
  });
  const getReader = body.getReader.bind(body);
  body.getReader = (...args) => {
    const reader = getReader(...args);
    for (const method of ["read", "cancel", "releaseLock"]) {
      const native = reader[method].bind(reader);
      reader[method] = (...values) => { counts[method]++; return native(...values); };
    }
    return reader;
  };
  const observed = [];
  const context = vm.createContext({
    TextDecoder, DOMException,
    fetch: async (url, init) => {
      assert.equal(url, "/api/chat/stream");
      assert.equal(init.signal, controller.signal);
      assert.equal(JSON.parse(init.body).message, "synthetic question");
      return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
    },
  });
  vm.runInContext(js, context);
  const callback = type => () => {
    observed.push(type);
    if (spec.callback === type) throw callbackError;
    if (spec.abort && type === "token") controller.abort();
  };
  let thrown;
  try {
    await context.streamChat({ message: "synthetic question", request_id: spec.id }, {
      onMeta: callback("meta"), onReasoning: callback("reasoning"), onAgent: callback("agent"),
      onToken: callback("token"), onDone: callback("done"), onStopped: callback("stopped"),
    }, { signal: controller.signal });
  } catch (cause) { thrown = cause; }
  assert.deepEqual(observed, spec.expected, `${spec.id}: callbacks and terminal event isolation`);
  if (spec.readError || spec.callback || spec.abort) {
    assert.equal(thrown, spec.readError ?? (spec.abort ? abortError : callbackError), `${spec.id}: preserve original exception`);
  } else if (spec.message) {
    assert.equal(thrown?.name, "ApiError");
    assert.equal(thrown?.message, spec.message, `${spec.id}: preserve original API error`);
  } else assert.equal(thrown, undefined, spec.id);
  assert.equal(body.locked, false, `${spec.id}: release native reader lock`);
  assert.equal(counts.releaseLock, 1, `${spec.id}: release exactly once`);
  assert.equal(counts.cancel, spec.eof ? 0 : 1, `${spec.id}: cancel only when leaving before natural EOF`);
  if (!spec.eof && !spec.close && !spec.readError && !spec.abort) {
    assert.equal(counts.underlyingCancel, 1, `${spec.id}: native stream source cancelled`);
  }
  results.push({
    id: spec.id, verdict: "PASS", callbacks: observed, body_locked: body.locked, counters: counts,
    exception: thrown ? { name: thrown.name, message: thrown.message } : null,
  });
}
const report = {
  schema: "OPT2.frontend-reader.v1", created_at: new Date().toISOString(), source_file: file,
  source_sha256: createHash("sha256").update(source).digest("hex"),
  scope: "Real source-extracted streamChat; native Node ReadableStream; synthetic HTTP/SSE; no DOM or remote backend",
  cases: results, passed: results.length, verdict: "PASS",
};
const output = option("--output");
if (output) fs.writeFileSync(output, `${JSON.stringify(report, null, 2)}\n`, "utf8");
console.log(JSON.stringify(report));
