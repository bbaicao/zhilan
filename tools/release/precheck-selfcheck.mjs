#!/usr/bin/env node
// Negative controls live in temporary isolated Git repositories. They are kept
// for inspection; this script never recursively removes user files.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const scanner = path.join(path.dirname(fileURLToPath(import.meta.url)), 'precheck.mjs');
const base = fs.mkdtempSync(path.join(os.tmpdir(), 'zhilan-release-selfcheck-'));
function git(root, ...args) {
  const result = spawnSync('git', ['-C', root, ...args], { encoding: 'utf8', windowsHide: true });
  assert.equal(result.status, 0, `Git ${args[0]} failed`);
}
function write(root, name, content) {
  const destination = path.join(root, name);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.writeFileSync(destination, content);
}
const readme = `# Zhilan
Windows desktop. Apache-2.0.
运行前提：配置 API Key 或 Ollama。未签名安装包可能显示 SmartScreen 未知发布者。
MCP 失败时界面看不到原因，请看后端日志：
未许可：去设置添加许可。
http 未在 egress：去设置补 egress。
逐包发现失败：检查命令与地址。
schema 超子集：检查包返回的 schema。
WEB_SSRF_ALLOW_ANY_HOST 默认关。开启后界面看不出来，只见 WARNING。
WEB_SEARCH_BUDGET_SECONDS 控制抓取预算。
修改 .env 后重启。
[架构](ARCHITECTURE.md)
`;
let controls = 0;
function repo(name) {
  const root = path.join(base, name);
  fs.mkdirSync(root);
  git(root, 'init', '--initial-branch=main');
  git(root, 'config', 'user.name', 'Release selfcheck');
  git(root, 'config', 'user.email', 'selfcheck@example.invalid');
  write(root, 'README.md', readme);
  write(root, 'ARCHITECTURE.md', '# Architecture\nLocal storage and question answering.\n');
  write(root, 'tools/packaging/zhilan-desktop.env.template',
    '# Network access disabled by default; restart after changes.\nWEB_SSRF_ALLOW_ANY_HOST=false\n# Fetch time budget; restart after changes.\nWEB_SEARCH_BUDGET_SECONDS=12\n');
  git(root, 'add', '--all');
  return root;
}
function check(root, label, group, expected = 1) {
  const result = spawnSync(process.execPath, [scanner, '--root', root], { encoding: 'utf8', windowsHide: true });
  assert.equal(result.status, expected, `${label}: unexpected exit status\n${result.stdout}\n${result.stderr}`);
  if (group) assert.match(result.stdout, new RegExp(`FAIL ${group}(?: |\\r?\\n)`), `${label}: missing failure group`);
  controls++;
  console.log(`PASS ${label}`);
}
const clean = repo('clean');
write(clean, 'safe.txt', [
  'pass' + 'word=changeme', 'api_' + 'key="${API_KEY}"', 'pass' + 'word="${PASSWORD:-changeme}"',
  'MYSQL_PASSWORD: ${MYSQL_PASSWORD:-changeme}',
  'token="dummy-token"', 'key="sk-' + 'x'.repeat(32) + '"',
  'Authorization: Bear' + 'er ' + 'a'.repeat(24),
  'postgres://user:changeme@localhost/db',
].join('\n'));
write(clean, 'backend/app/models/config.py', 'class Model:\n    pass\n');
git(clean, 'add', '--all');
git(clean, 'commit', '-m', 'Clean snapshot');
check(clean, 'placeholders-and-clean-history', null, 0);
const unquotedEnv = repo('unquoted-env-example');
write(unquotedEnv, '.env.example', 'MYSQL_' + 'PASS' + 'WORD=' + 'sampleSecret987');
git(unquotedEnv, 'add', '--all');
check(unquotedEnv, 'unquoted-env-example-password', 'credentials');
const unquotedYaml = repo('unquoted-yaml-password');
write(unquotedYaml, 'compose.yml', 'MYSQL_' + 'PASS' + 'WORD: ' + 'ProductionSecret123');
git(unquotedYaml, 'add', '--all');
check(unquotedYaml, 'unquoted-yaml-password', 'credentials');
const knownFixture = repo('known-fixture-exact-values');
write(knownFixture, 'backend/tests/mock.py', 'api_' + 'key="sk-' + 'chat-secret0001"');
git(knownFixture, 'add', '--all');
check(knownFixture, 'reviewed-test-fixture-value', null, 0);
write(knownFixture, 'backend/tests/mock.py', 'api_' + 'key="ProductionSecret123"');
git(knownFixture, 'add', '--all');
check(knownFixture, 'same-test-file-new-secret-fails', 'credentials');
const deployFixture = repo('fixture-in-deployment-config');
write(deployFixture, '.env.example', 'pass' + 'word=pw');
git(deployFixture, 'add', '--all');
check(deployFixture, 'fixture-value-in-config-fails', 'credentials');
const runtime = repo('declared-runtime-path');
write(runtime, 'README.md', readme + '\n下列是运行时生成的路径，不是仓库文件链接：`data/zhilan.db`、`models/`。\n');
git(runtime, 'add', '--all');
check(runtime, 'declared-runtime-inline-path', null, 0);
write(runtime, 'README.md', readme + '\n下列是运行时生成的路径，不是仓库文件链接。\n[Bad](data/missing)\n');
git(runtime, 'add', '--all');
check(runtime, 'runtime-markdown-link-still-fails', 'document-links');
for (const [index, target] of ['models/MinerU-4_models_onnx', '.mineru/models/'].entries()) {
  const modelRuntime = repo(`model-runtime-${index}`);
  write(modelRuntime, 'README.md', readme + `\n运行时生成的路径，不是仓库文件链接：\`${target}\`。\n`);
  git(modelRuntime, 'add', '--all');
  check(modelRuntime, `model-runtime-${index}-declared-inline`, null, 0);
  write(modelRuntime, 'README.md', readme + `\n\`${target}\`\n`);
  git(modelRuntime, 'add', '--all');
  check(modelRuntime, `model-runtime-${index}-undeclared-inline`, 'document-links');
  write(modelRuntime, 'README.md', readme + `\n运行时生成的路径，不是仓库文件链接。\n[Bad](${target})\n`);
  git(modelRuntime, 'add', '--all');
  check(modelRuntime, `model-runtime-${index}-markdown-link`, 'document-links');
}
const cases = [
  ['api-key', 'credentials', 'key="sk-' + 'AbC9dE8fG7hJ6kL5mN4pQ3rS2tU1vW0"'],
  ['private-key', 'credentials', '-----BEGIN RSA PRIVATE' + ' KEY-----'],
  ['bearer-token', 'credentials', 'Authorization: Bear' + 'er V7qR9nB3tP5kL8aC2dF6gH1jK4mN0sU'],
  ['dsn-password', 'credentials', 'postgres://user:' + 'realCredential923' + '@localhost/db'],
  ['quoted-password', 'credentials', 'pass' + 'word="' + 'realCredential923' + '"'],
  ['unquoted-password', 'credentials', 'pass' + 'word=' + 'realCredential923'],
  ['quoted-api-key', 'credentials', 'api_' + 'key="' + 'realCredential923' + '"'],
  ['windows-path', 'local-paths', 'E:' + '\\' + String.fromCharCode(30693, 28572) + '\\backend'],
  ['escaped-windows-path', 'local-paths', 'E:' + '\\\\' + 'R' + 'AG\\\\backend'],
  ['slash-windows-path', 'local-paths', 'E:/' + String.fromCharCode(30693, 28572) + '/backend'],
  ['user-path', 'local-paths', 'C:' + '\\\\' + 'Us' + 'ers\\\\someone'],
  ['git-bash-path', 'local-paths', '/e/' + 'G' + 'it/usr/bin'],
  ['internal-id', 'internal-references', 'P-' + 'V2-' + 'BUG-123'],
  ['parse-id', 'internal-references', 'DOC-' + 'PARSE-' + 'S2'],
  ['stage-id', 'internal-references', 'V2' + 'S12'],
  ['internal-link', 'internal-references', '../' + String.fromCharCode(20219, 21153, 26641) + '/file.md'],
];
for (const [label, group, text] of cases) {
  const root = repo(label);
  write(root, 'backend/tests/fixture.txt', text);
  git(root, 'add', '--all');
  check(root, label, group);
}
for (const [label, name, group] of [
  ['environment-file', '.env', 'environment-files'],
  ['private-environment-file', '.env.local', 'environment-files'],
  ['internal-file', 'AG' + 'ENTS.md', 'history'],
  ['generated-file', 'dist/file.txt', 'history'],
  ['model-file', 'models/weights.onnx', 'history'],
]) {
  const root = repo(label);
  write(root, name, 'nothing secret');
  git(root, 'add', '--all');
  check(root, label, group);
}
const big = repo('large-file');
write(big, 'oversize.txt', Buffer.alloc(1024 * 1024 + 1, 97));
git(big, 'add', '--all');
check(big, 'over-1MiB', 'large-files');
const githubLimit = repo('github-size-limit');
write(githubLimit, 'oversize.bin', Buffer.alloc(100 * 1024 * 1024 + 1));
git(githubLimit, 'add', '--all');
check(githubLimit, 'over-100MiB', 'large-files');
const link = repo('broken-link');
write(link, 'README.md', readme + '\n[Missing](missing.md)\n');
git(link, 'add', '--all');
check(link, 'readme-broken-link', 'document-links');
const inline = repo('broken-inline-path');
write(inline, 'README.md', readme + '\n`backend/missing.py`\n');
git(inline, 'add', '--all');
check(inline, 'readme-broken-inline-path', 'document-links');
const architecture = repo('architecture-reference');
write(architecture, 'ARCHITECTURE.md', 'Inspired by RAG' + 'Flow.');
git(architecture, 'add', '--all');
check(architecture, 'architecture-reference-project', 'public-documents');
const history = repo('history-deleted-leak');
write(history, 'old.txt', 'pass' + 'word="' + 'historicalCredential923' + '"');
git(history, 'add', '--all');
git(history, 'commit', '-m', 'Historical secret');
fs.unlinkSync(path.join(history, 'old.txt'));
git(history, 'add', '--all');
git(history, 'commit', '-m', 'Remove old file');
check(history, 'deleted-historical-secret', 'credentials');
const orphan = repo('orphan-ref-leak');
git(orphan, 'commit', '-m', 'Clean snapshot');
git(orphan, 'checkout', '--orphan', 'other');
write(orphan, '.env', 'pass' + 'word="' + 'orphanCredential923' + '"');
git(orphan, 'add', '--all');
git(orphan, 'commit', '-m', 'Orphan secret');
git(orphan, 'checkout', 'main');
check(orphan, 'other-ref-historical-secret', 'credentials');
const direct = repo('direct-blob-ref');
git(direct, 'commit', '-m', 'Clean snapshot');
write(direct, 'untracked.txt', 'pass' + 'word="directRefCredential923"');
const blob = spawnSync('git', ['-C', direct, 'hash-object', '-w', 'untracked.txt'], { encoding: 'utf8', windowsHide: true });
assert.equal(blob.status, 0);
git(direct, 'update-ref', 'refs/tags/direct-blob', blob.stdout.trim());
check(direct, 'direct-blob-ref-secret', 'credentials');
const historicalBig = repo('historical-large');
write(historicalBig, 'old-big.txt', Buffer.alloc(1024 * 1024 + 1, 97));
git(historicalBig, 'add', '--all');
git(historicalBig, 'commit', '-m', 'Large historical blob');
fs.unlinkSync(path.join(historicalBig, 'old-big.txt'));
git(historicalBig, 'add', '--all');
git(historicalBig, 'commit', '-m', 'Remove large blob');
check(historicalBig, 'deleted-historical-large-file', 'large-files');
console.log(`${controls} release controls passed. Temporary evidence: ${base}`);
