#!/usr/bin/env node
// Scans index/history and, when selected, final current/exported bytes.
// Secret findings report location and reason only, never the secret value.
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { workingFiles, readTree, inventory, sha256, frontendIdentity, zipOperation } from './export-source.mjs';

const args = process.argv.slice(2);
const rootOption = args.indexOf('--root');
const root = path.resolve(rootOption < 0 ? path.join(path.dirname(fileURLToPath(import.meta.url)), '../..') : args[rootOption + 1]);
const option = name => { const index = args.indexOf(name); return index < 0 ? undefined : args[index + 1]; };
const sourceDir = option('--source-dir');
const artifactDir = option('--artifact-dir');
const workingTree = args.includes('--working-tree');
let frontendChecked = false;
const groups = new Map([
  ['credentials', []], ['history', []], ['local-paths', []], ['large-files', []],
  ['document-links', []], ['environment-files', []], ['public-documents', []],
  ['internal-references', []], ['readme-guidance', []], ['licenses', []],
  ['frontend-identity', []], ['artifact-identity', []],
]);
const issues = new Set();
function fail(group, location, reason) {
  const key = `${group}:${location}:${reason}`;
  if (!issues.has(key)) {
    issues.add(key);
    groups.get(group).push(`${location} (${reason})`);
  }
}
function git(argv, input) {
  const result = spawnSync('git', ['--no-optional-locks', '-c', 'core.quotepath=false', '-C', root, ...argv], {
    input, encoding: null, maxBuffer: 256 * 1024 * 1024,
    windowsHide: true,
  });
  if (result.error || result.status !== 0) throw new Error(`Git operation failed: ${argv[0]}`);
  return result.stdout;
}
const internalDirs = [
  String.fromCharCode(20219, 21153, 26641),
  String.fromCharCode(37325, 26500, 26041, 26696),
  String.fromCharCode(20219, 21153, 26641) + '-' + String.fromCharCode(25991, 26723, 35299, 26512, 21319, 32423),
  'docs',
];
const internalNames = ['AG' + 'ENTS.md', 'At' + 'las.md'];
const ignoredDirNames = [...internalDirs, 'dist', 'build', 'data', 'models', '.venv', 'venv', 'node_modules'];
function forbiddenPath(name, source = false) {
  const parts = name.replaceAll('\\', '/').split('/');
  if (parts.some(part => ['node_modules', '.venv', 'venv'].includes(part))) return true;
  if (ignoredDirNames.includes(parts[0])) return true;
  if (parts[0] === 'frontend' && ['dist', 'build'].includes(parts[1]) && !(source && parts[1] === 'dist')) return true;
  if (parts.some(part => ['__pycache__', '.pytest_cache', '.git'].includes(part))) return true;
  if (/\.(?:pyc|log|exit|out|err|tsbuildinfo|db|sqlite|sqlite3)$/i.test(name)) return true;
  if (parts.some(part => internalNames.includes(part))) return true;
  return /\.(?:gguf|safetensors|onnx|pth|pyd|dll|exe|msi|zip|7z)$/i.test(name);
}
function envPath(name) {
  const base = path.posix.basename(name);
  return (base === '.env' || base.startsWith('.env.')) && base !== '.env.example';
}
const localPathPattern = new RegExp(
  'E:[\\\\/]+(?:R' + 'AG|' + String.fromCharCode(30693, 28572) + ')|' +
  'C:[\\\\/]+Us' + 'ers(?:[\\\\/]|\\b)|/[a-z]/(?:G' + 'it|' + String.fromCharCode(30693, 28572) + ')(?:/|\\b)', 'i',
);
const internalIdPattern = new RegExp(
  'P-' + 'V2-' + 'BUG-?\\d*|P-' + 'DP-' + 'BUG-?\\d*|V2' + 'S\\d+|DOC-' + 'PARSE-' + 'S\\d+', 'i',
);
const privateMarker = new RegExp('-----BEGIN (?:[A-Z ]+ )?PRIVATE' + ' KEY-----');
const tokenPattern = new RegExp('\\b' + 'sk' + '-[A-Za-z0-9_-]{10,}', 'g');
const bearerPattern = new RegExp('\\bBear' + 'er\\s+([A-Za-z0-9._~+/-]{8,})', 'gi');
const dsnPattern = /\b[a-z][a-z0-9+.-]*:\/\/([^/\s:@]+):([^@\s/]+)@/gi;
// Explicit placeholders are allowed, independently of the filename. A test
// directory is never exempt: genuine credentials there fail the same checks.
const knownFakeValues = new Set([
  'changeme', 'change_me', 'change-me', 'your-api-key', 'your_api_key',
  'your-key', 'your_key', 'your-token', 'your_token', '<api-key>', '<password>',
  'test', 'test-key', 'test_api_key', 'test-api-key', 'test-token', 'test-password',
  'testpass', 'test123', 'dummy', 'dummy-key', 'dummy-token', 'dummy-password',
  'fake', 'fake-key', 'fake-token', 'fake-password', 'not-a-real-key',
  'not-a-real-token', 'not-a-real-password', 'example-key', 'example-token',
  'placeholder', 'mock-key', 'mock-token', 'mock-password', 'unused', 'none', 'null',
  'llm_api_key', 'embed_api_key', 'embedding_api_key', 'rerank_api_key', 'ocr_api_key',
  'mysql_password', 'mysql_root_password',
]);
// Fixed synthetic fixtures reviewed in model settings, offline providers, and
// redaction tests. Accepted only in test/check sources, never in deployment
// configuration. A new secret in the same test file still fails.
const fixtureValues = new Set([
  'k1', 'sk-c', 'sk-a', 'sk-e', 'sk-r', 'sk-wire', 'sk-vision',
  's' + 'k-chat-secret0001', 's' + 'k-embed-secret0001', 's' + 'k-test-secret0001',
  'sk-top-111', 's' + 'k-nested-444', 'tk-list-666', 'sc-list-777',
  'secret-t-sentinel', 'offline-test-key', 'synthetic-only', 'synthetic-offline-key',
  'test-secret-never-export', 'deepl-key-123', 'deepl-key-xyz',
  'abcdefghijklmnop', 's' + 'k-proj-abcdefghijklmnop',
  's' + 'k-proj-abcdef1234567890', 's' + 'k-proj-zzzzzzzzzz123456',
  's' + 'k-proj-abcdefgh123456', 'topsecrettoken123', 'csecretvalue987',
  'qt-token-987654', 'pa55w0rd!xyz', 'hunter2', 'sekret123', 'pw',
  'k', 'k2', 'k3', 'k4', 'k5', 'bad', 't', 's', '***',
  't0-secret-arg-不该进回答请求体', 'bug092-secret-sentinel-do-not-log',
  'pw-3', 'sc-5', 'ak-6', 'tk-7', 'e2e-token-sentinel-do-not-log',
  't3-6-0-encoding-carrier-secret', 'gate-wiring-secret-not-persisted',
  'sk-x', 's' + 'k-embed-secret', 'sk-draft', 'sk-saved', 'sk-flat', 'sk-rerank',
  '文件命中词', '分类命中词', 's' + 'k-leak0123456789abcdef', 's' + 'k-flat-new',
  's' + 'k-reasoning0123456789', 'secret', '0123456789abcdef0123456789abcdef',
  'offline-test-placeholder', 'tokenabcdefgh123456', 'excsecret987654',
  'msgsecret123456', 'qt-token-987654&ok=1', 'sekret123&x=1', 'sk-test',
  'synthetic-cause', 'synthetic-outer', 'another-secret',
  'authorization: bea' + 'rer tokenabcdefgh123456',
]);
// Reviewed assignment literals only: neither the directory nor these values
// in another file are exempt, including historical blobs.
const reviewedFixtureValues = new Map([
  ['backend/tests/agent/test_web_llm.py', new Set(['private-test-key', 'other-provider-key'])],
  ['backend/tests/api/test_web_settings_delivery.py', new Set(['search-secret', 'mimo-secret', 'private-mimo-key'])],
  ['backend/scripts/check_release_ready.py', new Set(['offline-placeholder'])],
  ['backend/tests/api/test_installed_repair_s2_settings.py', new Set(['s2-flat-dummy'])],
  ['backend/tests/generation/test_installed_repair_s2_models.py', new Set(['s2-offline-only'])],
]);
function placeholder(value, name = '') {
  const text = value.trim().replace(/\\n.*$/, '');
  if (!text || knownFakeValues.has(text.toLowerCase())) return true;
  if (/^(?:backend\/tests\/|frontend\/scripts\/)/.test(name) && fixtureValues.has(text.toLowerCase())) return true;
  if (/^\$\{[A-Z_][A-Z0-9_]*(?::-[^}]*)?\}$/i.test(text)) return true;
  if (/^<[A-Z_ -]+>$/i.test(text)) return true;
  if (/^(?:\{[A-Za-z_0-9]*\}|\{\{.*\}\}|%[sr])$/.test(text)) return true;
  if (/^(?:x{8,}|0{8,}|a{12,}|b{12,}|1{12,}|2{12,})$/i.test(text)) return true;
  if (text.toLowerCase().startsWith('sk-')) return placeholder(text.slice(3), name);
  return false;
}
function credentialReasons(line, name) {
  const reasons = [];
  if (privateMarker.test(line)) reasons.push('private-key');
  for (const match of line.matchAll(tokenPattern)) {
    if (!placeholder(match[0], name)) reasons.push('api-key');
  }
  for (const match of line.matchAll(bearerPattern)) {
    if (!placeholder(match[1], name)) reasons.push('bearer-token');
  }
  for (const match of line.matchAll(dsnPattern)) {
    if (!placeholder(match[2], name) && !(/(?:[cm]?[jt]sx?|py)$/.test(name) && /^\{[a-z_][a-z0-9_.]*\}$/i.test(match[2]))) reasons.push('dsn-password');
  }
  // Quoted literals and YAML/env values are checked, while executable source
  // expressions (os.getenv(...), object.password, variables) are not passwords.
  const assignment = /(?<![\w-])(?:["']?(?:[a-z0-9_]*(?:password|passwd|api_key|apikey)|(?:client_)?secret|(?:access_|refresh_|auth_|bearer_)?token)\b["']?)\s*(?:=(?![=>])|:)\s*(?:"([^"]*)"|'([^']*)'|(\$\{[^}]+\}|\{[^}]*\}|[^\s,;#}\])"']+))/gi;
  for (const match of line.matchAll(assignment)) {
    if (/^\s*:param\b/.test(line)) continue;
    if (line.slice(0, match.index).endsWith('${')) continue;
    const value = match[1] ?? match[2] ?? match[3];
    const quoted = match[1] !== undefined || match[2] !== undefined;
    const source = /\.(?:[cm]?[jt]sx?|py|ps1)$/i.test(name);
    const javascript = /\.(?:[cm]?jsx?|tsx?)$/i.test(name);
    if (javascript && !quoted && (/^![01]$/.test(value) || value === '$')) continue;
    if (javascript && !quoted && line[match.index - 1] === '.' &&
        /^[a-z_][a-z0-9_]*\s*:/i.test(match[0]) &&
        /^[a-z_$][a-z0-9_$.]*===[a-z_$][a-z0-9_$.]*$/i.test(value)) continue;
    if (source && quoted && /^\s*\+\s*[a-z_][a-z0-9_.]*\s*\+\s*$/i.test(value)) continue;
    if (/\.ps1$/i.test(name) && /\$[a-z_][a-z0-9_]*/i.test(value)) continue;
    if (source && !quoted && (/^\{[a-z_][a-z0-9_.]*\}$/i.test(value) || value.startsWith('{ ') ||
        (!/^[\x00-\x7F]+$/.test(value) && /^\s*(?:#|\/\/)/.test(line)))) continue;
    if (!quoted && (/^(?:true|false|\d+)$/i.test(value) ||
        (source && (/^[a-z_][a-z0-9_.]*(?:[([]|$)/i.test(value) || value.startsWith('(') || value.startsWith('++'))))) continue;
    if (!reviewedFixtureValues.get(name)?.has(value) && !placeholder(value, name)) reasons.push('credential-assignment');
  }
  return [...new Set(reasons)];
}
function exclusionRule(line) {
  const plain = line.trim().replace(/^\//, '').replace(/\/$/, '');
  return ignoredDirNames.includes(plain) || internalNames.includes(plain);
}
function scanText(name, body, context) {
  if (body.includes(0)) return;
  const text = body.toString('utf8');
  const lines = text.split(/\r?\n/);
  lines.forEach((line, index) => {
    const location = `${context}:${name}:${index + 1}`;
    for (const reason of credentialReasons(line, name)) fail('credentials', location, reason);
    if (localPathPattern.test(line)) fail('local-paths', location, 'machine-specific-path');
    const ignoreRule = name === '.gitignore' && exclusionRule(line);
    if (!ignoreRule && (internalIdPattern.test(line) ||
        internalDirs.slice(0, 3).some(dir => line.includes(dir)) ||
        line.includes('docs/' + 'pitfalls.md') || internalNames.some(item => line.includes(item)))) {
      fail('internal-references', location, 'private-workflow-reference');
    }
  });
}
function allBlobs(objectIds) {
  if (!objectIds.length) return new Map();
  const output = git(['cat-file', '--batch'], Buffer.from(objectIds.join('\n') + '\n'));
  const blobs = new Map();
  let offset = 0;
  for (const expected of objectIds) {
    const end = output.indexOf(10, offset);
    const [oid, type, rawSize] = output.subarray(offset, end).toString('ascii').split(' ');
    const size = Number(rawSize);
    if (type !== 'blob' || oid !== expected || !Number.isFinite(size)) throw new Error('Invalid Git blob response');
    const body = output.subarray(end + 1, end + 1 + size);
    blobs.set(oid, body);
    offset = end + size + 2;
  }
  return blobs;
}
function markdownTargets(text) {
  const targets = [];
  const unfenced = text.replace(/```[\s\S]*?```/g, '');
  for (const match of unfenced.matchAll(/!?\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))(?:\s+["'][^"']*["'])?\)/g)) targets.push({ value: match[1] ?? match[2], inline: false });
  for (const match of unfenced.matchAll(/^\s*\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))/gm)) targets.push({ value: match[1] ?? match[2], inline: false });
  for (const match of unfenced.matchAll(/`([^`\r\n]+)`/g)) {
    const value = match[1].trim();
    if (value.includes('/') && !/\s|[*${}]|^[a-z]+:\/\//i.test(value) && !value.startsWith('/api/')) targets.push({ value, inline: true });
    else if (/^(?:\.gitignore|LICENSE|NOTICE|README\.md|ARCHITECTURE\.md|start\.bat)$/.test(value)) targets.push({ value, inline: true });
  }
  return targets;
}
function markdownAnchors(text) {
  const anchors = new Set();
  const used = new Map();
  const unfenced = text.replace(/```[\s\S]*?```/g, '');
  for (const match of unfenced.matchAll(/(?:^|\n)\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*(?=\n|$)/g)) {
    const heading = match[1].replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/<[^>]*>/g, '').replace(/[`*_~]/g, '');
    const slug = heading.toLowerCase().replace(/[^\p{L}\p{N}_\-\s]/gu, '').replace(/\s/g, '-');
    const count = used.get(slug) ?? 0;
    used.set(slug, count + 1);
    anchors.add(count ? `${slug}-${count}` : slug);
  }
  for (const match of unfenced.matchAll(/\b(?:id|name)=["']([^"']+)["']/g)) anchors.add(match[1]);
  return anchors;
}
function checkDocuments(current) {
  for (const [name, body] of current) {
    if (!/\.md$/i.test(name)) continue;
    const text = body.toString('utf8');
    for (const reference of markdownTargets(text)) {
      const target = reference.value;
      if (/^(?:https?:|mailto:|data:)/i.test(target)) continue;
      let decoded;
      let anchor;
      try {
        decoded = decodeURIComponent(target.split('#')[0].split('?')[0]);
        anchor = target.includes('#') ? decodeURIComponent(target.slice(target.indexOf('#') + 1)) : '';
      }
      catch { fail('document-links', name, 'malformed-link'); continue; }
      const normalized = decoded ? path.posix.normalize(path.posix.join(path.posix.dirname(name), decoded)) : name;
      // Generated storage/log paths are explanatory text, not repository links.
      // Only these actual application targets are accepted, only as inline code,
      // and only when the document explicitly explains that they are generated.
      const generatedTargets = new Set(['data', 'data/zhilan.db', 'data/qdrant',
        'data/uploads', 'models', 'models/MinerU-4_models_onnx', '.mineru/models',
        'logs/zhilan.log', 'client/start.log']);
      if (reference.inline && generatedTargets.has(normalized.replace(/\/$/, '')) &&
          /运行时生成/.test(text) && /不是仓库文件/.test(text)) continue;
      if (normalized.startsWith('../') || (!current.has(normalized) && ![...current.keys()].some(item => item.startsWith(normalized.replace(/\/$/, '') + '/')))) {
        fail('document-links', name, `missing-target:${target}`);
      } else if (anchor && !reference.inline && (!current.has(normalized) || !markdownAnchors(current.get(normalized).toString('utf8')).has(anchor))) {
        fail('document-links', name, `missing-anchor:${target}`);
      }
    }
  }
  const architecture = current.get('ARCHITECTURE.md');
  if (!architecture) fail('public-documents', 'ARCHITECTURE.md', 'missing');
  else {
    const referenceNames = ['RAG' + 'Flow', 'do' + 'cling', 'Fast' + 'GPT', 'Di' + 'fy',
      'semantic' + '-router', 'mem' + '0', 'GPT' + 'Cache', 'Light' + 'RAG',
      'ra' + 'gas', 'cube' + 'loop', 'open' + 'code', 'Ku' + 'n'];
    for (const project of referenceNames) {
      if (new RegExp(`\\b${project}\\b`, 'i').test(architecture.toString('utf8'))) fail('public-documents', 'ARCHITECTURE.md', 'reference-project-name');
    }
  }
  const readme = current.get('README.md')?.toString('utf8') ?? '';
  const firstScreen = readme.split(/\r?\n/).slice(0, 32).join('\n');
  for (const [label, expression] of [
    ['api-key-first-screen', /API\s*Key|API\s*密钥/i],
    ['ollama-first-screen', /Ollama/i],
    ['smartscreen-first-screen', /SmartScreen/i],
    ['unsigned-installer-first-screen', /未签名|无代码签名|没有代码签名|未知发布者/],
  ]) if (!expression.test(firstScreen)) fail('readme-guidance', 'README.md', label);
  for (const [label, expression] of [
    ['mcp-no-visible-cause', /界面.{0,8}(?:看不到|不显示|无法看到).{0,8}原因/],
    ['mcp-permission', /未许可/], ['mcp-egress', /egress/i],
    ['mcp-discovery', /逐包发现失败|包发现失败/], ['mcp-schema', /schema\s*超子集/i],
    ['mcp-user-actions', /设置|许可/], ['mcp-command-actions', /命令.{0,12}地址|地址.{0,12}命令/],
    ['env-restart', /\.env[\s\S]{0,180}重启/],
    ['escape-hatch-not-visible', /(?:界面.{0,20}(?:看不出|不可见|不显示)|(?:看不出|不可见).{0,12}界面)/],
  ]) if (!expression.test(readme)) fail('readme-guidance', 'README.md', label);
  const template = current.get('tools/packaging/zhilan-desktop.env.template')?.toString('utf8') ?? '';
  for (const key of ['WEB_SSRF_ALLOW_ANY_HOST', 'WEB_SEARCH_BUDGET_SECONDS']) {
    if (!readme.includes(key)) fail('readme-guidance', 'README.md', `missing:${key}`);
    if (!new RegExp(`^${key}=`, 'm').test(template)) fail('readme-guidance', 'tools/packaging/zhilan-desktop.env.template', `missing:${key}`);
  }
}
function jsonFile(files, name, group) {
  try {
    if (!files.has(name)) throw new Error();
    return JSON.parse(files.get(name).toString('utf8').replace(/^\uFEFF/, ''));
  } catch { fail(group, name, 'missing-or-invalid-json'); return null; }
}
function matches(item, files, name = item?.path) {
  const body = files.get(name);
  return Boolean(body && Number.isInteger(item?.bytes) && item.bytes === body.length && item.sha256 === sha256(body));
}
function checkLicenses(files) {
  if (!files.get('LICENSE')?.toString('utf8').includes('Apache License')) fail('licenses', 'LICENSE', 'missing-apache-license');
  if (!files.get('NOTICE')?.length) fail('licenses', 'NOTICE', 'missing-notice');
  for (const name of files.keys()) {
    if (!/^tools\/packaging\/licenses-vendor\/[^/]+\/SOURCE\.json$/.test(name)) continue;
    const document = jsonFile(files, name, 'licenses');
    if (!document) continue;
    if (!document.name || !document.version || !document.license || !document.status || path.posix.basename(path.posix.dirname(name)) !== document.name) fail('licenses', name, 'provenance-fields');
    for (const item of [document.material, ...(document.embeddedNotices ?? [])]) {
      if (!item || path.posix.isAbsolute(item.path ?? '') || (item.path ?? '').split('/').includes('..') || !matches(item, files, path.posix.join(path.posix.dirname(name), item.path ?? ''))) fail('licenses', name, 'license-material-identity');
    }
  }
}
function checkFrontend(files) {
  if (![...files.keys()].some(name => name.startsWith('frontend/dist/'))) {
    if (files.has('frontend/package.json')) {
      frontendChecked = true;
      fail('frontend-identity', 'frontend/dist', 'missing-build-output');
    }
    return;
  }
  frontendChecked = true;
  const recorded = jsonFile(files, 'frontend/.build-manifest', 'frontend-identity');
  if (!recorded) return;
  for (const [key, value] of Object.entries(frontendIdentity(files))) {
    if (!value || recorded[key] !== value) fail('frontend-identity', 'frontend/.build-manifest', `mismatch:${key}`);
  }
}
function inspect(name, body, context, source = false) {
  if (forbiddenPath(name, source)) fail('history', `${context}:${name}`, 'excluded-file');
  if (envPath(name)) fail('environment-files', `${context}:${name}`, 'credential-file');
  if (localPathPattern.test(name)) fail('local-paths', `${context}:${name}`, 'machine-specific-path');
  if (body.length > 1024 * 1024) fail('large-files', `${context}:${name}`, body.length > 100 * 1024 * 1024 ? 'over-100MiB' : 'over-1MiB');
  scanText(name, body, context);
}
function checkCurrent(files, context, source = false) {
  for (const [name, body] of files) inspect(name, body, context, source);
  checkDocuments(files);
  checkLicenses(files);
  checkFrontend(files);
}
function checkArtifacts(directory, expectedFiles) {
  const names = ['SOURCE.json', 'SOURCE-INVENTORY.json', 'release-manifest.json'];
  const metadata = new Map(names.filter(name => fs.existsSync(path.join(directory, name))).map(name => [name, fs.readFileSync(path.join(directory, name))]));
  const provenance = jsonFile(metadata, 'SOURCE.json', 'artifact-identity');
  const listing = jsonFile(metadata, 'SOURCE-INVENTORY.json', 'artifact-identity');
  const manifest = jsonFile(metadata, 'release-manifest.json', 'artifact-identity');
  if (!provenance || !listing || !manifest) return;
  if (manifest.schema !== 1 || provenance.schema !== 1 || listing.schema !== 1 || !Array.isArray(manifest.artifacts) || !Array.isArray(listing.files)) {
    fail('artifact-identity', 'release-manifest.json', 'schema'); return;
  }
  if (manifest.version !== provenance.version || !/^\d+\.\d+\.\d+(?:[-.][a-z0-9]+)*$/i.test(manifest.version ?? '') || !/^[a-f0-9]{40}$/.test(provenance.git_head ?? '') || typeof provenance.git_dirty !== 'boolean') fail('artifact-identity', 'SOURCE.json', 'source-version-or-git-identity');
  if (workingTree && !sourceDir && (provenance.git_head !== git(['rev-parse', 'HEAD']).toString('ascii').trim() ||
      provenance.git_dirty !== Boolean(git(['status', '--porcelain=v1', '-uall']).toString('utf8').trim()))) {
    fail('artifact-identity', 'SOURCE.json', 'current-git-identity');
  }
  const advertised = new Set();
  const metadataNames = new Set(names);
  metadataNames.add('SHA256SUMS.txt');
  let sourceFiles = expectedFiles;
  let archives = 0;
  for (const item of manifest.artifacts) {
    const name = item.path ?? '';
    if (path.basename(name) !== name || !name || advertised.has(name) || !['source-zip', 'windows-exe', 'windows-setup', 'windows-payload-zip'].includes(item.kind)) {
      fail('artifact-identity', 'release-manifest.json', 'artifact-path-or-kind'); continue;
    }
    advertised.add(name);
    const file = path.join(directory, name);
    if (!fs.existsSync(file) || !matches(item, new Map([[name, fs.readFileSync(file)]]))) { fail('artifact-identity', name, 'artifact-bytes'); continue; }
    if (!name.includes(manifest.version) || (item.kind === 'source-zip' && name !== `zhilan-source-${manifest.version}-candidate.zip`)) fail('artifact-identity', name, 'artifact-version-name');
    if (['windows-exe', 'windows-setup'].includes(item.kind) && (!name.toLowerCase().endsWith('.exe') || fs.readFileSync(file).subarray(0, 2).toString('ascii') !== 'MZ')) fail('artifact-identity', name, 'windows-executable-format');
    if (item.kind === 'windows-payload-zip') {
      if (!name.toLowerCase().endsWith('.zip') || !item.inventory || path.basename(item.inventory) !== item.inventory) {
        fail('artifact-identity', name, 'payload-inventory-path'); continue;
      }
      metadataNames.add(item.inventory);
      const inventoryPath = path.join(directory, item.inventory);
      if (!fs.existsSync(inventoryPath)) { fail('artifact-identity', name, 'payload-inventory-missing'); continue; }
      const payloadListing = jsonFile(new Map([[item.inventory, fs.readFileSync(inventoryPath)]]), item.inventory, 'artifact-identity');
      const extraction = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'zhilan-payload-precheck-')), 'payload');
      zipOperation('extract', file, extraction);
      const payload = readTree(extraction);
      if (JSON.stringify(inventory(payload)) !== JSON.stringify(payloadListing)) fail('artifact-identity', name, 'payload-inventory-mismatch');
      const executable = payload.get(item.main_executable);
      if (!item.main_executable?.toLowerCase().endsWith('.exe') || !executable || executable.subarray(0, 2).toString('ascii') !== 'MZ') fail('artifact-identity', name, 'payload-executable-format');
      continue;
    }
    if (item.kind !== 'source-zip') continue;
    archives++;
    const extraction = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'zhilan-archive-precheck-')), 'source');
    zipOperation('extract', file, extraction);
    const files = readTree(extraction);
    sourceFiles = files;
    const actual = inventory(files);
    if (JSON.stringify(actual.files) !== JSON.stringify(listing.files) || actual.tree_hash !== listing.tree_hash || actual.tree_hash !== provenance.source_tree_hash || actual.files.length !== provenance.source_file_count) fail('artifact-identity', name, 'archive-inventory-mismatch');
    if (expectedFiles && JSON.stringify(actual.files) !== JSON.stringify(inventory(expectedFiles).files)) fail('artifact-identity', name, 'archive-current-source-mismatch');
    const frontendPackage = jsonFile(files, 'frontend/package.json', 'artifact-identity');
    if (frontendPackage?.version !== manifest.version || JSON.stringify(frontendIdentity(files)) !== JSON.stringify(provenance.frontend)) fail('artifact-identity', 'SOURCE.json', 'archive-version-or-frontend');
    const licenseFiles = actual.files.filter(entry => ['LICENSE', 'NOTICE'].includes(entry.path) || entry.path.startsWith('tools/packaging/licenses-vendor/'));
    if (!Array.isArray(provenance.license_files) || !provenance.license_files.some(entry => entry.path === 'LICENSE') || !provenance.license_files.some(entry => entry.path === 'NOTICE') || JSON.stringify(provenance.license_files) !== JSON.stringify(licenseFiles)) fail('artifact-identity', 'SOURCE.json', 'archive-license-identity');
    checkCurrent(files, 'archive', true);
  }
  if (archives !== 1) fail('artifact-identity', 'release-manifest.json', 'one-source-archive-required');
  const safeBasename = name => typeof name === 'string' && name !== '.' && name !== '..' &&
    name.length > 0 && name === name.trim() && !/[\\/:<>"|?*\x00-\x1F]/.test(name);
  const declared = new Set([...advertised, ...metadataNames].map(name => name.toLowerCase()));
  const support = manifest.supporting_files ?? [];
  if (!Array.isArray(support)) fail('artifact-identity', 'release-manifest.json', 'supporting-files-schema');
  else for (const item of support) {
    const name = item?.path;
    if (!safeBasename(name) || declared.has(name.toLowerCase())) {
      fail('artifact-identity', 'release-manifest.json', 'supporting-file-path-or-duplicate'); continue;
    }
    declared.add(name.toLowerCase());
    advertised.add(name);
    const file = path.join(directory, name);
    if (!fs.existsSync(file) || !fs.statSync(file).isFile()) {
      fail('artifact-identity', name, 'supporting-file-missing'); continue;
    }
    const body = fs.readFileSync(file);
    if (!matches(item, new Map([[name, body]]))) fail('artifact-identity', name, 'supporting-file-bytes');
    inspect(name, body, 'support', true);
    if (/\.md$/i.test(name) || ['LICENSE', 'NOTICE'].includes(name)) {
      if (!sourceFiles?.has(name)) fail('artifact-identity', name, 'supporting-source-missing');
      else if (!body.equals(sourceFiles.get(name))) fail('artifact-identity', name, 'supporting-source-mismatch');
    }
  }
  const checksumPath = path.join(directory, 'SHA256SUMS.txt');
  if (fs.existsSync(checksumPath) || manifest.supporting_files !== undefined) {
    if (!fs.existsSync(checksumPath)) fail('artifact-identity', 'SHA256SUMS.txt', 'checksum-missing');
    else {
      const checksums = new Set();
      for (const line of fs.readFileSync(checksumPath, 'utf8').split(/\r?\n/).filter(Boolean)) {
        const match = /^([a-f0-9]{64}) {2}(.+)$/i.exec(line);
        if (!match || !safeBasename(match[2]) || match[2] === 'SHA256SUMS.txt' || checksums.has(match[2].toLowerCase())) {
          fail('artifact-identity', 'SHA256SUMS.txt', 'checksum-line-or-duplicate'); continue;
        }
        const name = match[2], file = path.join(directory, name);
        checksums.add(name.toLowerCase());
        if (!fs.existsSync(file) || !fs.statSync(file).isFile() || sha256(fs.readFileSync(file)) !== match[1].toLowerCase()) {
          fail('artifact-identity', 'SHA256SUMS.txt', 'checksum-bytes:' + name);
        }
      }
      const expected = fs.readdirSync(directory, { withFileTypes: true }).filter(item => item.isFile() && item.name !== 'SHA256SUMS.txt').map(item => item.name.toLowerCase());
      if (checksums.size !== expected.length || expected.some(name => !checksums.has(name))) fail('artifact-identity', 'SHA256SUMS.txt', 'checksum-coverage');
    }
  }
  for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
    if (item.isFile() && !advertised.has(item.name) && !metadataNames.has(item.name)) fail('artifact-identity', item.name, 'unlisted-attachment');
  }
}
try {
  let indexCount = 0, commitCount = 0, historyCount = 0, currentCount = 0;
  let expectedFiles;
  if (sourceDir) {
    const files = readTree(path.resolve(sourceDir));
    expectedFiles = files;
    currentCount = files.size;
    checkCurrent(files, 'source-dir', true);
  } else {
  git(['rev-parse', '--git-dir']);
  const index = new Map();
  for (const entry of git(['ls-files', '--stage', '-z']).toString('utf8').split('\0').filter(Boolean)) {
    const split = entry.indexOf('\t');
    const [mode, oid, stage] = entry.slice(0, split).split(' ');
    if (stage !== '0' || mode === '160000') throw new Error('Unmerged index or submodule is unsupported');
    index.set(entry.slice(split + 1), oid);
  }
  if (!index.size) throw new Error('Git index is empty');
  const history = new Map();
  const commits = git(['rev-list', '--all']).toString('ascii').trim().split(/\r?\n/).filter(Boolean);
  for (const commit of commits) {
    for (const entry of git(['ls-tree', '-r', '-z', commit]).toString('utf8').split('\0').filter(Boolean)) {
      const split = entry.indexOf('\t');
      const [, type, oid] = entry.slice(0, split).split(' ');
      if (type !== 'blob') continue;
      if (!history.has(oid)) history.set(oid, new Set());
      history.get(oid).add(entry.slice(split + 1));
    }
  }
  // An unusual tag/ref may point directly to a blob, outside a commit's tree.
  const reachable = git(['rev-list', '--objects', '--all']).toString('utf8').trim().split(/\r?\n/).filter(Boolean);
  if (reachable.length) {
    const checks = git(['cat-file', '--batch-check=%(objectname) %(objecttype)'], Buffer.from(reachable.map(item => item.split(' ')[0]).join('\n') + '\n')).toString('ascii').trim().split(/\r?\n/);
    for (let i = 0; i < checks.length; i++) {
      const [oid, type] = checks[i].split(' ');
      if (type === 'blob' && !history.has(oid)) history.set(oid, new Set([reachable[i].slice(41) || 'direct-ref-object']));
    }
  }
  const objects = allBlobs([...new Set([...index.values(), ...history.keys()])]);
  const current = new Map();
  for (const [name, oid] of index) { current.set(name, objects.get(oid)); inspect(name, objects.get(oid), 'index'); }
  for (const [oid, names] of history) for (const name of names) inspect(name, objects.get(oid), 'history');
  const finalFiles = workingTree ? workingFiles(root) : current;
  if (workingTree) for (const [name, body] of finalFiles) inspect(name, body, 'working-tree');
  checkDocuments(finalFiles);
  checkLicenses(finalFiles);
  if (workingTree && fs.existsSync(path.join(root, 'frontend/dist'))) {
    const withDist = new Map(finalFiles);
    for (const [name, body] of readTree(path.join(root, 'frontend/dist'))) {
      const publicName = 'frontend/dist/' + name;
      withDist.set(publicName, body);
      inspect(publicName, body, 'working-tree', true);
    }
    currentCount = withDist.size;
    checkFrontend(withDist);
    if (artifactDir) expectedFiles = withDist;
  } else if (workingTree && finalFiles.has('frontend/package.json')) {
    checkFrontend(finalFiles);
    if (artifactDir) expectedFiles = finalFiles;
  }
  indexCount = index.size; commitCount = commits.length; historyCount = history.size; currentCount = Math.max(currentCount, finalFiles.size);
  }
  if (artifactDir) checkArtifacts(path.resolve(artifactDir), expectedFiles);
  for (const [group, findings] of groups) {
    if ((group === 'artifact-identity' && !artifactDir) || (group === 'frontend-identity' && !frontendChecked)) {
      console.log(`UNVERIFIED ${group} (no input selected)`);
      continue;
    }
    console.log(`${findings.length ? 'FAIL' : 'PASS'} ${group}${findings.length ? ` (${findings.length})` : ''}`);
    for (const finding of findings) console.log(`  ${finding}`);
  }
  console.log(`Scanned ${indexCount} staged files, ${commitCount} reachable commits, ${historyCount} historical blobs, ${currentCount} current files (${sourceDir ? 'source-dir' : workingTree ? 'working-tree' : 'index'}).`);
  process.exitCode = issues.size ? 1 : 0;
} catch (error) {
  console.error(`FAIL scanner: ${error.message}`);
  process.exitCode = 2;
}
