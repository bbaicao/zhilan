// Run: node frontend/scripts/check_s7_frontend.mjs [--source-root <frontend-copy>] [--mutations]
// Executes functions/handlers extracted from real TS/TSX with installed TypeScript and Node stdlib.
// These checks do not substitute for real browser rendering, IME or downloaded-file evidence.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(frontend, 'package.json'));
const ts = require('typescript');
const arg = process.argv.indexOf('--source-root');
const root = arg < 0 ? frontend : path.resolve(process.argv[arg + 1]);
const files = ['src/App.tsx', 'src/api.ts', 'src/download.ts', 'src/components/Composer.tsx',
  'src/components/ChatView.tsx', 'src/components/MessageItem.tsx', 'src/components/Markdown.tsx', 'index.html'];
const original = Object.fromEntries(files.map(file => [file, fs.readFileSync(path.join(root, file), 'utf8')]));
let source = original;
const APP = 'src/App.tsx', API = 'src/api.ts', COMPOSER = 'src/components/Composer.tsx';
const CHAT = 'src/components/ChatView.tsx', MESSAGE = 'src/components/MessageItem.tsx', MARKDOWN = 'src/components/Markdown.tsx';
const noop = () => {};
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
function ast(file) { return ts.createSourceFile(file, source[file], ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX); }
function find(file, predicate) {
  let found;
  const visit = node => { if (!found && predicate(node)) found = node; if (!found) ts.forEachChild(node, visit); };
  visit(ast(file));
  assert.ok(found, `missing executable source node in ${file}`);
  return found;
}
function text(file, node) { return node.getText(ast(file)); }
function expression(file, name) {
  const node = find(file, node => (ts.isFunctionDeclaration(node) || ts.isVariableDeclaration(node)) && node.name?.getText() === name);
  return ts.isVariableDeclaration(node) ? text(file, node.initializer) : text(file, node).replace(/^export\s+(default\s+)?/, '');
}
function run(code, env = {}) {
  const js = ts.transpileModule(`globalThis.result = (${code});`, { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React, module: ts.ModuleKind.None } }).outputText;
  const context = vm.createContext({ AbortController, DOMException, TextEncoder, TextDecoder, Response, Blob, URL, console, setTimeout, clearTimeout, useCallback: fn => fn, ...env });
  vm.runInContext(js, context);
  return context.result;
}
const fn = (file, name, env) => run(expression(file, name), env);
function appEnv(extra = {}) {
  const state = { chatId: 1, messages: [{ id: 10, role: 'assistant', content: 'A' }], flashes: [], notices: [{ chatId: 1 }, { chatId: 2 }], sends: [] };
  const env = {
    state, chatId: 1, chatInput: 'question', images: [], chatAgentMode: '', settingsData: null, chatFilters: {}, chatWebEnabled: false,
    activeViewRef: { current: { chatId: 1, token: 1 } }, viewTokenSeq: { current: 1 }, viewRef: { current: 'chat' },
    deletionEpoch: { current: 0 }, deletedChatVersions: { current: new Map() }, streamsRef: { current: [] }, queueRef: { current: [] }, recoveredFetchSeq: { current: new Map() },
    navStack: { current: { current: { view: 'chat', chatId: 1 }, past: [{ view: 'chat', chatId: 1 }], future: [{ view: 'chat', chatId: 1 }] } },
    window: { confirm: () => true }, ApiError: class ApiError extends Error {},
    flash: value => state.flashes.push(value), setView: noop, setViewToken: noop, setNavTick: noop, pushNav: noop,
    setChatId: id => { state.chatId = id; }, setMessages: next => { state.messages = typeof next === 'function' ? next(state.messages) : next; },
    setNotices: next => { state.notices = next(state.notices); }, setChatInput: noop, setImages: noop, setBanner: noop, setComposerFocusTick: noop,
    getChat: async id => ({ id, messages: [{ id: id * 10, role: 'assistant', content: `chat${id}` }] }), deleteChat: async () => ({}), refreshChats: async () => [], stopChatStream: async () => ({ stopped: true }),
    hasFilters: () => false, newRequestId: () => String(Math.random()),
    pushNotice: (chatId, kind, text) => state.notices.push({ chatId, kind, text }),
    ...extra,
  };
  env.setQueueItems = next => { env.queueRef.current = next; };
  env.setStreamItems = next => { env.streamsRef.current = next; };
  env.streamOnView = fn(APP, 'streamOnView');
  env.partialFromStream = fn(APP, 'partialFromStream');
  env.watchingOnScreen = rec => env.streamOnView(env.activeViewRef.current, rec) && env.viewRef.current === 'chat';
  return env;
}
function record(id = 1) { return { key: 'r', chatId: id, freshToken: 1, controller: new AbortController(), stopRequested: false, pendingSend: [{ text: 'pending' }], invalidated: false, deletionEpoch: 0, botId: -2, userMsg: { id: -1, role: 'user', content: 'question' }, mode: 'off', buf: { content: 'partial', reasoning: '', steps: [] } }; }

const cases = [];
const check = (id, name, test) => cases.push({ id, name, test });
check('172', 'pending/failed/reversed navigation stays consistent; running snapshot retained', async () => {
  const b = deferred(), c = deferred();
  const env = appEnv({ getChat: id => id === 2 ? b.promise : c.promise });
  env.streamsRef.current = [record(1), record(3)];
  const open = fn(APP, 'openChat', env);
  const oldMessages = env.state.messages;
  const waitB = open(2);
  assert.equal(env.activeViewRef.current.chatId, 1, 'B loading must keep A identity');
  assert.equal(env.state.messages, oldMessages);
  const waitC = open(3);
  c.resolve({ id: 3, messages: [] }); await waitC;
  assert.equal(env.activeViewRef.current.chatId, 3);
  assert.equal(env.state.messages.at(-1).content, 'partial');
  assert.equal(env.state.messages[0].content, 'question');
  b.resolve({ id: 2, messages: [] }); await waitB;
  assert.equal(env.activeViewRef.current.chatId, 3, 'late B cannot overwrite C');
  const bad = appEnv({ getChat: async () => { throw Error('offline'); } });
  await fn(APP, 'openChat', bad)(2);
  assert.equal(bad.activeViewRef.current.chatId, 1);
  assert.equal(bad.state.messages[0].content, 'A');
  await fn(APP, 'openChat', bad)(null);
  assert.equal(bad.activeViewRef.current.chatId, null);
  assert.equal(bad.state.messages.length, 0);
  for (const navigation of ['pushNav', 'applyLocation']) {
    const stale = deferred();
    const away = appEnv({ getChat: () => stale.promise, sameLocation: (a, b) => a.view === b.view });
    const request = fn(APP, 'openChat', away)(2);
    fn(APP, navigation, away)({ view: 'settings' });
    away.viewRef.current = 'chat'; // Returning to chat cannot revive the already abandoned GET.
    stale.resolve({ id: 2, messages: [] }); await request;
    assert.equal(away.activeViewRef.current.chatId, 1, `${navigation} must cancel pending navigation`);
    assert.equal(away.state.messages[0].content, 'A');
  }
});
check('173', 'false stop releases intent; true stops; hanging request/body times out; edit does not lie', async () => {
  for (const stopped of [false, true, 'error']) {
    const env = appEnv({ stopChatStream: async () => { if (stopped === 'error') throw Error('offline'); return { stopped }; } });
    const rec = record(); env.streamsRef.current = [rec];
    const result = await fn(APP, 'stopChat', env)();
    assert.equal(result, stopped === 'error' ? null : stopped);
    assert.equal(rec.stopRequested, stopped !== false);
    assert.equal(rec.controller.signal.aborted, stopped === 'error');
  }
  const env = appEnv({ stopChat: async () => false, viewBusy: () => true });
  env.streamsRef.current = [record()];
  fn(APP, 'editUserMessage', env)('edited'); await settle();
  assert.ok(!env.state.flashes.some(item => item.includes('已停止当前生成')));
  for (const stopped of [false, true, 'error']) for (const immediate of [false, true]) {
    const stream = deferred(), stop = deferred(), sent = []; let streamHandlers;
    const race = appEnv({ stopChatStream: () => stop.promise, streamChat: async (request, handlers) => { sent.push(request.message); if (sent.length === 1) { streamHandlers = handlers; await stream.promise; } } });
    const submission = fn(APP, 'submitChat', race)(); await settle();
    race.queueRef.current = [{ text: 'queued' }];
    if (immediate) race.streamsRef.current[0].pendingSend = [{ text: 'immediate' }];
    const stopping = fn(APP, 'stopChat', race)();
    await streamHandlers.onDone({ chat_id: 1 });
    stream.resolve(); await settle();
    assert.equal(sent.length, 1, 'finalization must wait for the stop response');
    if (stopped === 'error') stop.reject(Error('offline')); else stop.resolve({ stopped });
    await stopping; await submission; await settle();
    assert.equal(sent[1], immediate ? 'immediate' : stopped === false ? 'queued' : undefined);
    if (immediate) assert.equal(sent[2], 'queued', 'immediate preserves later queue order');
    else assert.equal(race.queueRef.current.length, stopped === false ? 0 : 1);
  }
  function stopApi(fetch) {
    const timers = new Map();
    const env = {
      ApiError: class ApiError extends Error {}, OFFLINE: 'offline', fetch,
      setTimeout: (callback, delay) => { const id = timers.size + 1; timers.set(id, { callback, delay }); return id; },
      clearTimeout: id => timers.delete(id),
    };
    for (const name of ['parse', 'jsonBody', 'request']) env[name] = fn(API, name, env);
    return { stop: fn(API, 'stopChatStream', env), timers };
  }
  for (const stopped of [false, true, 'error']) {
    const api = stopApi(async (url, init) => {
      assert.equal(url, '/api/chat/stop'); assert.equal(init.method, 'POST');
      assert.equal(JSON.parse(init.body).request_id, 'r');
      if (stopped === 'error') throw Error('offline');
      return new Response(JSON.stringify({ stopped }));
    });
    if (stopped === 'error') await assert.rejects(api.stop('r'), /offline/);
    else assert.equal((await api.stop('r')).stopped, stopped);
    assert.equal(api.timers.size, 0, 'completed stops must cancel their timer');
  }
  for (const phase of ['headers', 'body']) for (const ended of [false, true]) for (const immediate of [false, true]) {
    const reply = deferred(), stream = deferred(), sent = []; let stopSignal, streamHandlers;
    const api = stopApi(async (_url, init) => {
      stopSignal = init.signal;
      stopSignal?.addEventListener('abort', () => reply.reject(stopSignal.reason), { once: true });
      return phase === 'headers' ? reply.promise : { ok: true, status: 200, json: () => reply.promise };
    });
    const race = appEnv({
      stopChatStream: api.stop,
      streamChat: async (request, handlers, options) => {
        sent.push(request.message);
        if (sent.length === 1) {
          streamHandlers = handlers; handlers.onToken('partial');
          options.signal.addEventListener('abort', () => stream.reject(options.signal.reason), { once: true });
          await stream.promise;
        }
      },
    });
    const submission = fn(APP, 'submitChat', race)(); let finished = false;
    submission.then(() => { finished = true; }); await settle();
    const rec = race.streamsRef.current[0];
    race.queueRef.current = [{ text: 'queued' }];
    if (immediate) rec.pendingSend = [{ text: 'immediate' }];
    const stop = fn(APP, 'stopChat', race);
    let stopResult = 'pending'; stop().then(result => { stopResult = result; });
    void stop(); assert.equal(api.timers.size, 1, 'repeated stop shares one bounded request');
    if (ended) {
      await streamHandlers.onDone({ chat_id: 1 }); stream.resolve(); await settle();
      assert.equal(finished, false, 'terminal SSE still waits for a bounded stop decision');
      await fn(APP, 'submitChat', race)('blocked');
      assert.equal(sent.length, 1, 'waiting stop must not overlap same-chat generation');
    }
    const timer = api.timers.values().next().value;
    assert.equal(timer?.delay, 10000, 'stop deadline is ten seconds');
    timer.callback(); await settle(); await settle();
    assert.equal(stopResult, null, 'timeout falls back without claiming server stopped');
    assert.equal(stopSignal.aborted, true); assert.equal(rec.controller.signal.aborted, true);
    assert.equal(finished, true, 'SSE finalization cannot remain blocked after stop timeout');
    await submission; await settle();
    assert.equal(rec.stopPending, undefined); assert.equal(api.timers.size, 0);
    assert.equal(race.streamsRef.current.includes(rec), false, 'timed-out stop must release the original viewBusy record');
    assert.equal(sent[1], immediate ? 'immediate' : undefined);
    assert.equal(race.queueRef.current.length, immediate ? 0 : 1, 'manual timeout preserves queue; immediate preserves its continuation');
    reply.resolve(new Response(JSON.stringify({ stopped: false }))); await settle();
    assert.equal(sent.length, immediate ? 3 : 1, 'late stop response cannot change continuation');
    await fn(APP, 'submitChat', race)('after-timeout'); await settle();
    assert.ok(sent.includes('after-timeout'), 'user can send again after the bounded fallback');
  }
});
check('174', 'note SSE EOF is error; first terminal wins; AbortError preserved', async () => {
  const ApiError = class ApiError extends Error {};
  const frame = value => `data: ${JSON.stringify(value)}\n\n`;
  for (const data of ['', frame({ type: 'token', text: 'partial' })]) {
    const generate = fn(API, 'generateNoteStream', { ApiError, fetch: async () => new Response(data), OFFLINE: 'offline' });
    await assert.rejects(generate(4, {}), /笔记生成流程异常结束，请重试。/);
  }
  let done = 0, tokens = '';
  const data = frame({ type: 'done', title: 'ok' }) + frame({ type: 'done', title: 'duplicate' }) + frame({ type: 'token', text: 'late' });
  await fn(API, 'generateNoteStream', { ApiError, fetch: async () => new Response(data) })(4, { onDone: () => done++, onToken: text => { tokens += text; } });
  assert.equal(done, 1); assert.equal(tokens, '');
  const error = new DOMException('cancelled', 'AbortError');
  await assert.rejects(fn(API, 'generateNoteStream', { ApiError, fetch: async () => { throw error; } })(4, {}), value => value === error);
  await assert.rejects(fn(API, 'generateNoteStream', { ApiError, fetch: async () => new Response(frame({ type: 'error', detail: 'specific' })) })(4, {}), /specific/);
});
check('175', 'IME candidate keys untouched; normal submit/direction/Shift preserved', () => {
  for (const slashOpen of [false, true]) for (const key of ['Enter', 'Tab', 'ArrowUp', 'ArrowDown', 'Escape']) {
    let actions = 0;
    const env = { slashOpen, slashHits: [1], slashActive: 0, loading: true, setSlashIndex: () => actions++, insertSkill: () => actions++, onChange: () => actions++, onQueueDirection: () => actions++, onSubmit: () => actions++ };
    fn(COMPOSER, 'onKeyDown', env)({ key, ctrlKey: true, nativeEvent: { isComposing: true }, preventDefault: () => actions++ });
    assert.equal(actions, 0, `IME ${key} slash=${slashOpen}`);
  }
  let sent = 0, directed = 0;
  const handler = fn(COMPOSER, 'onKeyDown', { slashOpen: false, loading: true, onSubmit: () => sent++, onQueueDirection: () => directed++ });
  handler({ key: 'Enter', nativeEvent: {}, preventDefault: noop });
  handler({ key: 'Enter', ctrlKey: true, nativeEvent: {}, preventDefault: noop });
  handler({ key: 'Enter', shiftKey: true, nativeEvent: {}, preventDefault: noop });
  assert.equal(sent, 1); assert.equal(directed, 1);
});
check('176', 'new conversation resets scroll and follow state by view identity', () => {
  const call = find(CHAT, node => ts.isCallExpression(node) && node.expression.getText() === 'useLayoutEffect');
  const box = { scrollTop: 40, scrollHeight: 900 };
  let atBottom = false;
  run(text(CHAT, call.arguments[0]), { scrollRef: { current: box }, setAtBottom: value => { atBottom = value; } })();
  assert.equal(box.scrollTop, 900); assert.equal(atBottom, true);
  const deps = run(text(CHAT, call.arguments[1]), { props: { viewToken: 23 } });
  assert.equal(deps.length, 1); assert.equal(deps[0], 23);
});
check('195', 'memo export and stable message callback props; changed message object compares unequal', () => {
  const sf = ast(MESSAGE);
  const exported = sf.statements.find(node => ts.isExportAssignment(node));
  assert.ok(exported && ts.isCallExpression(exported.expression), 'MessageItem must export memo');
  const component = () => {};
  const memoized = run(text(MESSAGE, exported.expression), { MessageItem: component, memo: require('react').memo });
  assert.equal(memoized.type, component);
  assert.equal(memoized.compare, null, 'default shallow compare must include all fields');
  const node = find(CHAT, node => ts.isJsxSelfClosingElement(node) && node.tagName.getText() === 'MessageItem');
  const item = { id: 1, content: 'old' }, setTranslateSource = noop, setSavedBanner = noop;
  const env = { React: { createElement: (type, props) => props }, MessageItem: component, item, onOpenRef: noop, translateHasKey: true, latestUserId: 5, setTranslateSource, setSavedBanner, props: { onEditUser: noop } };
  const a = run(text(CHAT, node), env), b = run(text(CHAT, node), env);
  for (const key of Object.keys(a)) assert.equal(a[key], b[key], `unstable message prop ${key}`);
  const changed = run(text(CHAT, node), { ...env, item: { ...item, reasoning: 'new' } });
  assert.notEqual(changed.message, a.message);
  for (const name of ['flash', 'stopChat', 'editUserMessage', 'openChatRef']) {
    const init = find(APP, node => ts.isVariableDeclaration(node) && node.name.getText() === name).initializer;
    assert.ok(ts.isCallExpression(init) && init.expression.getText() === 'useCallback', `${name} must be stable across token renders`);
  }
});
check('196', 'delete success invalidates target only; failure preserves stream/queue; late callbacks suppressed', async () => {
  const env = appEnv(); const a = record(), b = record(2); env.streamsRef.current = [a, b]; env.queueRef.current = [{ text: 'queued' }];
  await fn(APP, 'removeSession', env)(1);
  assert.equal(a.invalidated, true); assert.equal(a.controller.signal.aborted, true); assert.equal(a.pendingSend.length, 0);
  assert.equal(env.streamsRef.current.length, 1); assert.equal(env.streamsRef.current[0], b);
  assert.equal(b.controller.signal.aborted, false); assert.equal(env.state.notices.length, 1);
  assert.equal(env.activeViewRef.current.chatId, null); assert.equal(env.queueRef.current.length, 0);
  assert.equal(env.streamOnView({ chatId: 1, token: 1 }, a), false);
  const failed = appEnv({ deleteChat: async () => { throw Error('offline'); } });
  const keep = record(); failed.streamsRef.current = [keep]; failed.queueRef.current = [{ text: 'keep' }];
  await fn(APP, 'removeSession', failed)(1);
  assert.equal(keep.invalidated, false); assert.equal(keep.controller.signal.aborted, false); assert.equal(failed.queueRef.current.length, 1);
  const stream = deferred(); let handlers;
  const late = appEnv({ streamChat: async (_request, value) => { handlers = value; await stream.promise; } });
  const pending = fn(APP, 'submitChat', late)(); await settle();
  await fn(APP, 'removeSession', late)(1);
  handlers.onMeta(1); handlers.onToken('late'); handlers.onReasoning('late'); handlers.onAgent({ detail: 'late' });
  await handlers.onDone({ chat_id: 1, recovered: true }); handlers.onStopped();
  stream.resolve(); await pending;
  assert.equal(late.state.messages.length, 0); assert.equal(late.activeViewRef.current.chatId, null);
  assert.ok(!late.state.notices.some(notice => notice.chatId === 1));
  assert.equal(late.streamsRef.current.length, 0);
  const recovery = deferred(); let recoverDone;
  const detached = appEnv({ getChat: () => recovery.promise, streamChat: async (_request, value) => { recoverDone = value.onDone({ chat_id: 1, recovered: true }); } });
  await fn(APP, 'submitChat', detached)();
  assert.equal(detached.streamsRef.current.length, 0, 'async done callback outlives stream finally');
  await fn(APP, 'removeSession', detached)(1);
  recovery.resolve({ id: 1, messages: [{ id: 77, content: 'deleted' }] }); await recoverDone;
  assert.equal(detached.state.messages.length, 0);
  assert.ok(!detached.state.notices.some(notice => notice.chatId === 1), 'detached recovered callback cannot notify deleted chat');
  const deletion = deferred();
  const moved = appEnv({ deleteChat: () => deletion.promise }); moved.streamsRef.current = [record(1), record(2)];
  const deleting = fn(APP, 'removeSession', moved)(1);
  await fn(APP, 'openChat', moved)(2);
  const messagesB = moved.state.messages;
  deletion.resolve(); await deleting;
  assert.equal(moved.activeViewRef.current.chatId, 2); assert.equal(moved.state.messages, messagesB);
  assert.equal(moved.streamsRef.current[0].chatId, 2);
  // A restored chat may be opened and used again: deletion versions fence old streams only.
  await fn(APP, 'openChat', moved)(1);
  moved.streamChat = async (_request, value) => { value.onToken('restored'); };
  await fn(APP, 'submitChat', moved)();
  assert.equal(moved.state.messages.at(-1).content, 'restored');
  const delayedMeta = deferred(); let metaHandlers;
  const unknown = appEnv({ streamChat: async (_request, value) => { metaHandlers = value; await delayedMeta.promise; } });
  unknown.activeViewRef.current = { chatId: null, token: 1 };
  const unknownPending = fn(APP, 'submitChat', unknown)(); await settle();
  await fn(APP, 'removeSession', unknown)(1);
  metaHandlers.onMeta(1); metaHandlers.onToken('deleted'); delayedMeta.resolve(); await unknownPending;
  assert.equal(unknown.activeViewRef.current.chatId, null); assert.equal(unknown.streamsRef.current.length, 0);
});
check('197', 'both copy paths clear timer and ignore clipboard completion after unmount', async () => {
  for (const [file, name] of [[MESSAGE, 'copyText'], [MARKDOWN, 'copy']]) {
    const pending = deferred(), timers = new Map(); let next = 1, updates = 0, fallback = 0;
    const env = { copyMounted: { current: true }, copyTimer: { current: null }, setCopied: () => updates++, message: { content: 'text' }, hasText: true, children: 'text', textOf: value => value, fallbackCopy: () => fallback++, navigator: { clipboard: { writeText: () => pending.promise } }, window: { setTimeout: callback => { const id = next++; timers.set(id, callback); return id; }, clearTimeout: id => timers.delete(id) } };
    const copy = fn(file, name, env);
    const effect = find(file, node => ts.isCallExpression(node) && node.expression.getText() === 'useEffect' && node.arguments[0].getText().includes('copyMounted'));
    const cleanup = run(text(file, effect.arguments[0]), env)();
    copy(); cleanup(); pending.resolve(); await settle();
    assert.equal(updates, 0, `${file} late clipboard completion`); assert.equal(timers.size, 0);
    env.copyMounted.current = true;
    await copy(); await settle(); assert.equal(timers.size, 1);
    await copy(); await settle(); assert.equal(timers.size, 1, 'repeated copy has one timer');
    cleanup(); assert.equal(timers.size, 0);
    if (file === MESSAGE) {
      const fallbackFn = fn(file, 'fallbackCopy', { ...env, document: { createElement: () => { fallback++; throw Error('unmounted DOM use'); } } });
      fallbackFn('text', noop); assert.equal(fallback, 0);
    }
  }
});
check('198', 'menu backdrop is native button; click/Escape close; cleanup restores opener', () => {
  let closed = 0, focused = 0; const effects = [], listeners = {};
  class Element { isConnected = true; focus() { focused++; } }
  const React = { Fragment: 'fragment', createElement: (type, props, ...children) => ({ type, props, children }) };
  const tree = fn(COMPOSER, 'MenuShell', { React, useEffect: effect => effects.push(effect()), HTMLElement: Element, document: { activeElement: new Element() }, window: { addEventListener: (name, callback) => { listeners[name] = callback; }, removeEventListener: name => { delete listeners[name]; } }, cx: (...values) => values.join(' ') })({ children: 'menu', onClose: () => closed++ });
  const backdrop = tree.children[0]; assert.equal(backdrop.type, 'button'); assert.equal(backdrop.props.type, 'button'); assert.ok(backdrop.props['aria-label']);
  backdrop.props.onClick(); listeners.keydown({ key: 'Escape' }); assert.equal(closed, 2);
  effects.forEach(cleanup => cleanup?.()); assert.equal(focused, 1); assert.equal(Object.keys(listeners).length, 0);
});
check('199', 'CSV preserves bytes/name and delays revoke on success and click error', async () => {
  for (const clickError of [false, true]) {
    const events = []; let anchor, timer;
    const url = { createObjectURL: () => 'blob:s7', revokeObjectURL: value => events.push(['revoke', value]) };
    const env = { URL: url, DOWNLOAD_REVOKE_DELAY_MS: 60000, window: { setTimeout: (callback, delay) => { timer = callback; events.push(['timer', delay]); } }, document: { body: { appendChild: noop }, createElement: () => (anchor = { style: {}, click: () => { events.push(['click']); if (clickError) throw Error('click'); }, remove: () => events.push(['remove']) }) } };
    env.triggerBlobDownload = fn('src/download.ts', 'triggerBlobDownload', env);
    env.scheduleRevoke = fn('src/download.ts', 'scheduleRevoke', env);
    let endpoint; const blob = new Blob(['\uFEFFday,tokens\n2026-10-02,7\n']);
    env.fetch = async value => { endpoint = value; return { ok: true, blob: async () => blob }; };
    env.ApiError = Error;
    const exportCsv = fn(API, 'exportUsageCsv', env);
    if (clickError) await assert.rejects(exportCsv(90), /click/); else await exportCsv(1);
    assert.ok(endpoint.includes(`days=${clickError ? 90 : 1}`));
    assert.match(anchor.download, /^usage-\d{4}-\d{2}-\d{2}\.csv$/);
    assert.equal(events.some(event => event[0] === 'revoke'), false);
    assert.ok(events.some(event => event[0] === 'remove')); assert.ok(events.some(event => event[0] === 'timer' && event[1] === 60000));
    timer(); assert.equal(events.at(-1)[0], 'revoke');
  }
});
check('200', 'favicon URLs use mounted assets and copied SVG/ICO match original bytes', () => {
  for (const ext of ['svg', 'ico']) {
    assert.ok(source['index.html'].includes(`href="/assets/favicon.${ext}"`));
    assert.deepEqual(fs.readFileSync(path.join(root, 'public/assets', `favicon.${ext}`)), fs.readFileSync(path.join(root, 'public', `favicon.${ext}`)));
  }
});

let failures = 0;
for (const entry of cases) {
  try { await entry.test(); console.log(`PASS ${entry.id}: ${entry.name}`); }
  catch (error) { failures++; console.log(`FAIL ${entry.id}: ${entry.name}\n${error.stack}`); }
}
if (process.argv.includes('--mutations') && !failures) {
  const mutations = [
    ['172', APP, 'const detail = await getChat(id);', 'activeViewRef.current = { chatId: id, token }; const detail = await getChat(id);'],
    ['172-pushNav', APP, 'if (next.view !== "chat") viewTokenSeq.current += 1;', ''],
    ['172-applyLocation', APP, 'if (loc.view !== "chat") viewTokenSeq.current += 1;', ''],
    ['173', APP, 'if (!result.stopped)', 'if (false)'],
    ['173-doneRace', APP, 'async onDone(result) {', 'async onDone(result) { rec.stopRequested = false;'],
    ['173-stopTimeout', API, 'const timeout = setTimeout(() => controller.abort(), 10_000);', 'const timeout = 0;'],
    ['173-stopSignal', API, 'signal: controller.signal,', ''],
    ['174', API, 'if (!terminal) throw new ApiError("笔记生成流程异常结束，请重试。");', 'if (false) throw new ApiError("笔记生成流程异常结束，请重试。");'],
    ['175', COMPOSER, 'if (event.nativeEvent.isComposing) return;', 'if (false) return;'],
    ['176', CHAT, 'box.scrollTop = box.scrollHeight', 'box.scrollTop = 0'],
    ['195', CHAT, 'onTranslate={setTranslateSource}', 'onTranslate={(text) => setTranslateSource(text)}'],
    ['196', APP, 'rec.invalidated = true;', 'rec.invalidated = false;'],
    ['196-recovered', APP, '> rec.deletionEpoch', '> Infinity'],
    ['197', MARKDOWN, 'if (!copyMounted.current) return;', 'if (false) return;'],
    ['198', COMPOSER, '<button type="button" aria-label="关闭菜单"', '<div role="button" aria-label="关闭菜单"'],
    ['199', API, 'scheduleRevoke(url);', 'URL.revokeObjectURL(url);'],
    ['200', 'index.html', '/assets/favicon.svg', '/favicon.svg'],
  ];
  for (const [id, file, before, after] of mutations) {
    assert.ok(original[file].includes(before), `mutation anchor ${id}`);
    // Only the in-memory copy is mutated; product files and snapshot stay untouched.
    source = { ...original, [file]: id === '196-recovered' ? original[file].replaceAll(before, after) : original[file].replace(before, after) };
    try { await cases.find(entry => entry.id === id.split('-')[0]).test(); failures++; console.log(`MUTANT-SURVIVED ${id}`); }
    catch (error) { console.log(`MUTANT-KILLED ${id}: ${error.message.split('\n')[0]}`); }
  }
  source = original;
}
console.log(`S7 frontend direct checks: ${cases.length - failures} passed, ${failures} failed`);
process.exitCode = failures ? 1 : 0;
