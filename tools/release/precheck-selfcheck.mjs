#!/usr/bin/env node
// Isolated controls; no synthetic commits or mutations to the public index.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { exportSource, frontendIdentity, readTree, inventory, sha256, zipOperation } from './export-source.mjs';

const scanner = path.join(path.dirname(fileURLToPath(import.meta.url)), 'precheck.mjs');
const base = fs.mkdtempSync(path.join(os.tmpdir(), 'zhilan-release-selfcheck-'));
let controls = 0;
function write(root, name, content) {
  const destination = path.join(root, name);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.writeFileSync(destination, content);
}
function git(root, ...args) {
  const result = spawnSync('git', ['-C', root, ...args], { encoding: 'utf8', windowsHide: true });
  assert.equal(result.status, 0, `Git ${args[0]} failed`);
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
[架构](ARCHITECTURE.md#architecture)
`;
function source(name) {
  const root = path.join(base, name);
  fs.mkdirSync(root);
  write(root, 'README.md', readme);
  write(root, 'ARCHITECTURE.md', '# Architecture\nLocal storage and question answering.\n');
  write(root, 'LICENSE', 'Apache License\nVersion 2.0\n');
  write(root, 'NOTICE', 'Synthetic scanner control license notice.\n');
  write(root, 'tools/packaging/zhilan-desktop.env.template', 'WEB_SSRF_ALLOW_ANY_HOST=false\nWEB_SEARCH_BUDGET_SECONDS=12\n');
  return root;
}
function check(root, label, group, expected = 1, extra = []) {
  const mode = extra.includes('--working-tree') ? ['--root', root] : ['--source-dir', root];
  const result = spawnSync(process.execPath, [scanner, ...mode, ...extra], { encoding: 'utf8', windowsHide: true });
  assert.equal(result.status, expected, `${label}: unexpected exit status\n${result.stdout}\n${result.stderr}`);
  if (group) assert.match(result.stdout, new RegExp(`FAIL ${group}(?: |\\r?\\n)`), `${label}: missing failure group`);
  controls++;
  console.log(`PASS ${label}`);
  return result;
}
const clean = source('clean');
write(clean, 'safe.txt', [
  'pass' + 'word=changeme', 'api_' + 'key="${API_KEY}"', 'pass' + 'word="${PASSWORD:-changeme}"',
  'MYSQL_PASSWORD: ${MYSQL_PASSWORD:-changeme}', 'token="dummy-token"',
  'key="sk-' + 'x'.repeat(32) + '"', 'Authorization: Bear' + 'er ' + 'a'.repeat(24),
  'postgres://user:changeme@localhost/db',
].join('\n'));
check(clean, 'placeholders', null, 0);
const minified = source('minified-expressions');
write(minified, 'frontend/dist/assets/app.js',
  'const pass' + 'word=!0;const payload={token:$};const result=flag?job.token:job.source===current.source;');
write(minified, 'frontend/src/app.ts', 'const value = 1;');
write(minified, 'frontend/.build-manifest', JSON.stringify(frontendIdentity(readTree(minified))));
check(minified, 'minified-runtime-expressions', null, 0);
for (const [label, filename, value] of [
  ['minified-quoted-bool', 'frontend/dist/assets/app.js', '"!0"'],
  ['minified-quoted-variable', 'frontend/dist/assets/app.js', '"$"'],
  ['minified-quoted-equality', 'frontend/dist/assets/app.js', '"job.source===current.source"'],
  ['minified-actual-secret', 'frontend/dist/assets/app.js', '"realCredential923"'],
  ['deployment-bool-syntax', '.env.example', '!0'],
  ['deployment-variable-syntax', '.env.example', '$'],
  ['deployment-equality-syntax', '.env.example', 'job.source===current.source'],
]) {
  const root = source(label);
  write(root, filename, 'token=' + value);
  check(root, label, 'credentials');
}
const knownFixture = source('known-fixture');
write(knownFixture, 'backend/tests/mock.py', 'api_' + 'key="sk-' + 'chat-secret0001"');
check(knownFixture, 'reviewed-test-fixture', null, 0);
write(knownFixture, 'backend/tests/mock.py', 'api_' + 'key="ProductionSecret123"');
assert.ok(!check(knownFixture, 'same-test-file-new-secret', 'credentials').stdout.includes('ProductionSecret123'), 'redact secret values');
for (const [label, filename, values] of [
  ['web-llm', 'backend/tests/agent/test_web_llm.py', ['private-test-key', 'other-provider-key']],
  ['web-settings', 'backend/tests/api/test_web_settings_delivery.py', ['search-secret', 'mimo-secret', 'private-mimo-key']],
  ['release-runner', 'backend/scripts/check_release_ready.py', ['offline-placeholder']],
  ['s2-settings', 'backend/tests/api/test_installed_repair_s2_settings.py', ['s2-flat-dummy']],
  ['s2-models', 'backend/tests/generation/test_installed_repair_s2_models.py', ['s2-offline-only']],
]) {
  const body = values.map(value => 'api_' + 'key="' + value + '"').join('\n');
  const root = source(label);
  write(root, filename, body);
  check(root, `${label}-exact-fixtures`, null, 0);
  write(root, filename, body + '\napi_' + 'key="newUnreviewedCredential923"');
  check(root, `${label}-new-secret`, 'credentials');
  for (const [suffix, target] of [['other-file', 'backend/tests/unreviewed.py'], ['deployment', '.env.example']]) {
    const other = source(label + suffix);
    write(other, target, body);
    check(other, label + suffix, 'credentials');
  }
}
for (const [label, name, text] of [
  ['env-example', '.env.example', 'MYSQL_' + 'PASS' + 'WORD=sampleSecret987'],
  ['yaml', 'compose.yml', 'MYSQL_' + 'PASS' + 'WORD: ProductionSecret123'],
  ['deployment-fixture', '.env.example', 'pass' + 'word=pw'],
  ['api-key', 'test.txt', 'key="sk-' + 'AbC9dE8fG7hJ6kL5mN4pQ3rS2tU1vW0"'],
  ['private-key', 'test.txt', '-----BEGIN RSA PRIVATE' + ' KEY-----'],
  ['bearer', 'test.txt', 'Authorization: Bear' + 'er V7qR9nB3tP5kL8aC2dF6gH1jK4mN0sU'],
  ['dsn', 'test.txt', 'postgres://user:' + 'realCredential923' + '@localhost/db'],
  ['quoted', 'test.txt', 'pass' + 'word="realCredential923"'],
  ['unquoted', 'test.txt', 'pass' + 'word=realCredential923'],
  ['quoted-api-key', 'test.txt', 'api_' + 'key="realCredential923"'],
]) {
  const root = source(label); write(root, name, text); check(root, label, 'credentials');
}
for (const [label, group, text] of [
  ['windows-path', 'local-paths', 'E:' + '\\' + String.fromCharCode(30693, 28572) + '\\backend'],
  ['escaped-path', 'local-paths', 'E:' + '\\\\' + 'R' + 'AG\\\\backend'],
  ['slash-path', 'local-paths', 'E:/' + String.fromCharCode(30693, 28572) + '/backend'],
  ['user-path', 'local-paths', 'C:' + '\\\\' + 'Us' + 'ers\\\\someone'],
  ['git-bash-path', 'local-paths', '/e/' + 'G' + 'it/usr/bin'],
  ['internal-id', 'internal-references', 'P-' + 'V2-' + 'BUG-123'],
  ['parse-id', 'internal-references', 'DOC-' + 'PARSE-' + 'S2'],
  ['stage-id', 'internal-references', 'V2' + 'S12'],
  ['internal-link', 'internal-references', '../' + String.fromCharCode(20219, 21153, 26641) + '/file.md'],
]) {
  const root = source(label); write(root, 'sample.txt', text); check(root, label, group);
}
for (const [label, filename, group] of [
  ['env', '.env', 'environment-files'], ['env-local', '.env.local', 'environment-files'],
  ['internal-file', 'AG' + 'ENTS.md', 'history'], ['generated-file', 'dist/file.txt', 'history'],
  ['model-file', 'models/weights.onnx', 'history'], ['database', 'personal.sqlite', 'history'],
]) {
  const root = source(label); write(root, filename, 'nothing secret'); check(root, label, group);
}
for (const [label, size] of [['over-1MiB', 1024 * 1024 + 1], ['over-100MiB', 100 * 1024 * 1024 + 1]]) {
  const root = source(label); write(root, 'oversize.bin', Buffer.alloc(size)); check(root, label, 'large-files');
}
for (const [label, suffix] of [
  ['missing-link', '[Missing](missing.md)'], ['missing-inline', '`backend/missing.py`'],
  ['missing-anchor', '[Missing](ARCHITECTURE.md#absent)'], ['missing-self-anchor', '[Missing](#absent)'],
  ['malformed-link', '[Bad](%zz.md)'], ['outside-root', '[Bad](../outside.md)'],
]) {
  const root = source(label); write(root, 'README.md', readme + '\n' + suffix + '\n'); check(root, label, 'document-links');
}
for (const [index, target] of ['data/zhilan.db', 'models/', 'models/MinerU-4_models_onnx', '.mineru/models/'].entries()) {
  const root = source(`runtime-${index}`);
  write(root, 'README.md', readme + `\n运行时生成的路径，不是仓库文件链接：\`${target}\`。\n`);
  check(root, `runtime-${index}-declared`, null, 0);
  write(root, 'README.md', readme + `\n\`${target}\`\n`);
  check(root, `runtime-${index}-undeclared`, 'document-links');
  write(root, 'README.md', readme + `\n运行时生成的路径，不是仓库文件链接。\n[Bad](${target})\n`);
  check(root, `runtime-${index}-link`, 'document-links');
}
const architecture = source('architecture-reference');
write(architecture, 'ARCHITECTURE.md', 'Inspired by RAG' + 'Flow.');
check(architecture, 'architecture-reference-project', 'public-documents');
const missingLicense = source('missing-license');
fs.unlinkSync(path.join(missingLicense, 'LICENSE'));
check(missingLicense, 'missing-license', 'licenses');
const vendor = source('vendor-license');
write(vendor, 'tools/packaging/licenses-vendor/demo/LICENSE', 'Example license');
write(vendor, 'tools/packaging/licenses-vendor/demo/SOURCE.json', JSON.stringify({
  name: 'demo', version: '1.0', license: 'MIT', status: 'version-bound',
  material: { path: 'LICENSE', bytes: 15, sha256: sha256(Buffer.from('Example license')) },
}));
check(vendor, 'vendor-license-bytes', null, 0);
write(vendor, 'tools/packaging/licenses-vendor/demo/LICENSE', 'Tampered license');
check(vendor, 'vendor-license-tamper', 'licenses');
const working = source('working');
git(working, 'init', '--initial-branch=main'); git(working, 'add', '--all');
check(working, 'working-tree-clean-no-commit', null, 0, ['--working-tree']);
const indexPath = path.join(working, '.git/index');
const indexBefore = sha256(fs.readFileSync(indexPath));
write(working, 'README.md', readme + '\npass' + 'word="unstagedCredential923"\n');
check(working, 'unstaged-secret', 'credentials', 1, ['--working-tree']);
write(working, 'README.md', readme); write(working, 'new.txt', 'pass' + 'word="newCredential923"');
check(working, 'untracked-secret', 'credentials', 1, ['--working-tree']);
write(working, 'new.txt', 'ordinary'); write(working, '.env', 'ordinary');
check(working, 'untracked-env', 'environment-files', 1, ['--working-tree']);
fs.unlinkSync(path.join(working, '.env'));
write(working, 'README.md', readme + '\n[New](new.md)\n');
check(working, 'unstaged-missing-link', 'document-links', 1, ['--working-tree']);
write(working, 'new.md', '# New\n');
check(working, 'new-doc-link', null, 0, ['--working-tree']);
assert.equal(sha256(fs.readFileSync(indexPath)), indexBefore, 'working-tree scan must not mutate index');
controls++; console.log('PASS index-unchanged');

// Existing public objects supply an existing HEAD. No new commit is created.
const publicRoot = path.resolve(path.dirname(scanner), '../..');
const exporting = path.join(base, 'existing-public-history');
const clone = spawnSync('git', ['clone', '--shared', '--no-checkout', publicRoot, exporting], { windowsHide: true, encoding: 'utf8' });
assert.equal(clone.status, 0, 'Existing-history clone failed');
const fixture = source('export-fixture');
for (const [name, body] of readTree(fixture)) write(exporting, name, body);
write(exporting, 'frontend/package.json', JSON.stringify({ version: '0.1.4' }));
write(exporting, 'frontend/src/app.tsx', 'export const value = 1;\n');
write(exporting, 'frontend/dist/index.html', '<script src="assets/app.js"></script>');
write(exporting, 'frontend/dist/assets/app.js', 'const value = 1;\n');
const frontendFiles = new Map([
  ['frontend/src/app.tsx', Buffer.from('export const value = 1;\n')],
  ['frontend/dist/index.html', Buffer.from('<script src="assets/app.js"></script>')],
  ['frontend/dist/assets/app.js', Buffer.from('const value = 1;\n')],
]);
const mixedFiles = new Map([
  ['frontend/src/App.tsx', Buffer.from('upper')],
  ['frontend/src/api.ts', Buffer.from('lower')],
  ['frontend/dist/Z.js', Buffer.from('upper')],
  ['frontend/dist/a.js', Buffer.from('lower')],
]);
const expectedSrc = createHash('sha256').update('src/api.ts\0lower\0src/App.tsx\0upper\0').digest('hex');
const expectedDist = createHash('sha256').update('a.js\0lower\0Z.js\0upper\0').digest('hex');
assert.equal(frontendIdentity(mixedFiles).src_tree_hash, expectedSrc);
assert.equal(frontendIdentity(mixedFiles).dist_tree_hash, expectedDist);
controls++; console.log('PASS frontend-existing-windows-sort-contract');
write(exporting, 'frontend/.build-manifest', JSON.stringify(frontendIdentity(frontendFiles)));
write(exporting, 'backend/app/models/config.py', 'class Model:\n    pass\n');
write(exporting, '.gitignore', '.env\nfrontend/dist/\n');
git(exporting, 'read-tree', '--empty'); git(exporting, 'add', '--all');
write(exporting, '.env', 'pass' + 'word="excludedCredential923"');
const output = path.join(base, 'export');
exportSource(exporting, output);
const exportRoot = path.join(output, 'source');
assert.ok(!fs.existsSync(path.join(exportRoot, '.env')));
assert.ok(fs.existsSync(path.join(exportRoot, 'backend/app/models/config.py')), 'application model source must survive root runtime-model exclusion');
controls++; console.log('PASS export-retains-application-model-source');
check(exportRoot, 'artifact-clean', null, 0, ['--artifact-dir', output]);
write(exportRoot, 'frontend/src/app.tsx', 'export const value = 2;\n');
check(exportRoot, 'frontend-src-changed', 'frontend-identity');
write(exportRoot, 'frontend/src/app.tsx', 'export const value = 1;\n');
write(exportRoot, 'frontend/dist/assets/app.js', 'const value = 2;\n');
check(exportRoot, 'frontend-dist-changed', 'frontend-identity');
write(exportRoot, 'frontend/dist/assets/app.js', 'const value = 1;\n');
const manifestFile = path.join(output, 'release-manifest.json');
const manifest = JSON.parse(fs.readFileSync(manifestFile, 'utf8'));
const payloadRoot = path.join(base, 'synthetic-payload');
write(payloadRoot, 'zhilan.exe', 'MZsynthetic-control');
write(payloadRoot, '_internal/runtime.dll', 'synthetic-runtime');
write(payloadRoot, 'licenses/NOTICE', 'synthetic-license-control');
const payloadName = 'zhilan-windows-0.1.4-candidate.zip';
zipOperation('create', payloadRoot, path.join(output, payloadName));
const payloadInventoryName = 'WINDOWS-PAYLOAD-INVENTORY.json';
const payloadInventory = inventory(readTree(payloadRoot));
write(output, payloadInventoryName, JSON.stringify(payloadInventory));
manifest.artifacts.push({ path: payloadName, kind: 'windows-payload-zip',
  bytes: fs.statSync(path.join(output, payloadName)).size,
  sha256: sha256(fs.readFileSync(path.join(output, payloadName))),
  inventory: payloadInventoryName, main_executable: 'zhilan.exe' });
fs.writeFileSync(manifestFile, JSON.stringify(manifest));
check(exportRoot, 'windows-payload-clean', null, 0, ['--artifact-dir', output]);
payloadInventory.files[0].bytes++;
write(output, payloadInventoryName, JSON.stringify(payloadInventory));
check(exportRoot, 'windows-payload-inventory-tamper', 'artifact-identity', 1, ['--artifact-dir', output]);
write(output, payloadInventoryName, JSON.stringify(inventory(readTree(payloadRoot))));
manifest.artifacts[1].main_executable = 'missing.exe';
fs.writeFileSync(manifestFile, JSON.stringify(manifest));
check(exportRoot, 'windows-payload-main-missing', 'artifact-identity', 1, ['--artifact-dir', output]);
manifest.artifacts[1].main_executable = 'zhilan.exe';
fs.writeFileSync(manifestFile, JSON.stringify(manifest));
const originalManifest = JSON.stringify(manifest);
manifest.artifacts[0].sha256 = '0'.repeat(64); fs.writeFileSync(manifestFile, JSON.stringify(manifest));
check(exportRoot, 'artifact-hash-tamper', 'artifact-identity', 1, ['--artifact-dir', output]);
fs.writeFileSync(manifestFile, originalManifest);
const staleSource = source('stale-source');
for (const [name, body] of readTree(exportRoot)) write(staleSource, name, body);
write(staleSource, 'backend/app/models/config.py', 'class Model:\n    changed = True\n');
check(staleSource, 'archive-current-source-mismatch', 'artifact-identity', 1, ['--artifact-dir', output]);
manifest.artifacts[0].path = manifest.artifacts[0].path.replace('0.1.4', '0.1.3');
fs.copyFileSync(path.join(output, JSON.parse(originalManifest).artifacts[0].path), path.join(output, manifest.artifacts[0].path));
fs.writeFileSync(manifestFile, JSON.stringify(manifest));
check(exportRoot, 'archive-version-filename-mismatch', 'artifact-identity', 1, ['--artifact-dir', output]);
fs.unlinkSync(path.join(output, manifest.artifacts[0].path));
manifest.artifacts[0].path = JSON.parse(originalManifest).artifacts[0].path;
fs.writeFileSync(manifestFile, originalManifest);
const listingFile = path.join(output, 'SOURCE-INVENTORY.json');
const listing = JSON.parse(fs.readFileSync(listingFile, 'utf8'));
listing.files[0].bytes++; fs.writeFileSync(listingFile, JSON.stringify(listing));
check(exportRoot, 'inventory-tamper', 'artifact-identity', 1, ['--artifact-dir', output]);
fs.writeFileSync(listingFile, JSON.stringify(inventory(readTree(exportRoot))));
const provenanceFile = path.join(output, 'SOURCE.json');
const provenance = JSON.parse(fs.readFileSync(provenanceFile, 'utf8'));
const originalProvenance = JSON.stringify(provenance);
provenance.license_files[0].sha256 = '0'.repeat(64); fs.writeFileSync(provenanceFile, JSON.stringify(provenance));
check(exportRoot, 'source-license-tamper', 'artifact-identity', 1, ['--artifact-dir', output]);
fs.writeFileSync(provenanceFile, originalProvenance);
check(exporting, 'artifact-current-git-clean', null, 0, ['--working-tree', '--artifact-dir', output]);
provenance.git_head = '0'.repeat(40); fs.writeFileSync(provenanceFile, JSON.stringify(provenance));
check(exporting, 'artifact-current-git-head-tamper', 'artifact-identity', 1, ['--working-tree', '--artifact-dir', output]);
fs.writeFileSync(provenanceFile, originalProvenance);
provenance.git_head = JSON.parse(originalProvenance).git_head;
provenance.git_dirty = false; fs.writeFileSync(provenanceFile, JSON.stringify(provenance));
check(exporting, 'artifact-current-git-dirty-tamper', 'artifact-identity', 1, ['--working-tree', '--artifact-dir', output]);
fs.writeFileSync(provenanceFile, originalProvenance);
write(exporting, 'frontend/dist/assets/app.js', 'to' + 'ken="realCredential923";\n');
check(exporting, 'working-tree-dist-secret', 'credentials', 1, ['--working-tree']);
write(exporting, 'frontend/dist/assets/app.js', 'const value = 1;\n');
write(output, 'unlisted.exe', 'synthetic attachment');
check(exportRoot, 'unlisted-attachment', 'artifact-identity', 1, ['--artifact-dir', output]);
fs.unlinkSync(path.join(output, 'unlisted.exe'));
const archive = path.join(output, manifest.artifacts[0].path);
fs.unlinkSync(archive); write(exportRoot, 'archive-extra.txt', 'unexpected file');
zipOperation('create', exportRoot, archive);
manifest.artifacts[0].bytes = fs.statSync(archive).size; manifest.artifacts[0].sha256 = sha256(fs.readFileSync(archive));
fs.writeFileSync(manifestFile, JSON.stringify(manifest));
check(exportRoot, 'archive-extra-valid-outer-hash', 'artifact-identity', 1, ['--artifact-dir', output]);
const supportOutput = path.join(base, 'support-export');
exportSource(exporting, supportOutput);
const supportRoot = path.join(supportOutput, 'source');
const supportManifest = JSON.parse(fs.readFileSync(path.join(supportOutput, 'release-manifest.json'), 'utf8'));
for (const name of ['README.md', 'LICENSE', 'NOTICE']) fs.copyFileSync(path.join(supportRoot, name), path.join(supportOutput, name));
write(supportOutput, 'INSTALLATION-TERMS.txt', 'Synthetic collected license terms.\n');
function refreshSupportManifest() {
  supportManifest.supporting_files = ['README.md', 'LICENSE', 'NOTICE', 'INSTALLATION-TERMS.txt'].map(name => {
    const body = fs.readFileSync(path.join(supportOutput, name));
    return { path: name, bytes: body.length, sha256: sha256(body) };
  });
  write(supportOutput, 'release-manifest.json', JSON.stringify(supportManifest));
}
function refreshChecksums() {
  const lines = fs.readdirSync(supportOutput, { withFileTypes: true }).filter(item => item.isFile() && item.name !== 'SHA256SUMS.txt')
    .map(item => sha256(fs.readFileSync(path.join(supportOutput, item.name))) + '  ' + item.name);
  write(supportOutput, 'SHA256SUMS.txt', lines.join('\n') + '\n');
}
function supportCheck(label, expected = 1) {
  refreshChecksums();
  return check(supportRoot, label, expected ? 'artifact-identity' : null, expected, ['--artifact-dir', supportOutput]);
}
refreshSupportManifest();
supportCheck('supporting-files-clean', 0);
write(supportOutput, 'INSTALLATION-TERMS.txt', 'changed collected terms');
supportCheck('supporting-file-hash-tamper');
write(supportOutput, 'INSTALLATION-TERMS.txt', 'Synthetic collected license terms.\n');
write(supportOutput, 'LICENSE', 'Apache License\nchanged');
refreshSupportManifest(); supportCheck('supporting-root-license-source-tamper');
fs.copyFileSync(path.join(supportRoot, 'LICENSE'), path.join(supportOutput, 'LICENSE'));
write(supportOutput, 'README.md', readme + '\n[Missing](missing.md)\n');
refreshSupportManifest(); supportCheck('supporting-markdown-source-tamper');
fs.copyFileSync(path.join(supportRoot, 'README.md'), path.join(supportOutput, 'README.md'));
write(supportOutput, 'INSTALLATION-TERMS.txt', 'to' + 'ken="realCredential923"');
refreshSupportManifest(); refreshChecksums();
check(supportRoot, 'supporting-text-secret', 'credentials', 1, ['--artifact-dir', supportOutput]);
write(supportOutput, 'INSTALLATION-TERMS.txt', 'Synthetic collected license terms.\n');
refreshSupportManifest();
const cleanSupportManifest = JSON.stringify(supportManifest);
supportManifest.supporting_files.push({ ...supportManifest.supporting_files[1], path: 'license' });
write(supportOutput, 'release-manifest.json', JSON.stringify(supportManifest));
supportCheck('supporting-duplicate-casefold');
write(supportOutput, 'release-manifest.json', cleanSupportManifest);
supportManifest.supporting_files = JSON.parse(cleanSupportManifest).supporting_files;
supportManifest.supporting_files.push({ path: '../LICENSE', bytes: 1, sha256: '0'.repeat(64) });
write(supportOutput, 'release-manifest.json', JSON.stringify(supportManifest));
supportCheck('supporting-unsafe-path');
write(supportOutput, 'release-manifest.json', cleanSupportManifest);
write(supportOutput, 'unknown.txt', 'ordinary');
supportCheck('supporting-unknown-file-with-valid-checksum');
fs.unlinkSync(path.join(supportOutput, 'unknown.txt'));
refreshChecksums();
const cleanChecksums = fs.readFileSync(path.join(supportOutput, 'SHA256SUMS.txt'), 'utf8');
write(supportOutput, 'SHA256SUMS.txt', cleanChecksums.replace(/^[a-f0-9]{64}/, '0'.repeat(64)));
check(supportRoot, 'checksum-wrong-hash', 'artifact-identity', 1, ['--artifact-dir', supportOutput]);
write(supportOutput, 'SHA256SUMS.txt', cleanChecksums.split('\n').slice(1).join('\n'));
check(supportRoot, 'checksum-missing-member', 'artifact-identity', 1, ['--artifact-dir', supportOutput]);
write(supportOutput, 'SHA256SUMS.txt', cleanChecksums + cleanChecksums.split('\n')[0] + '\n');
check(supportRoot, 'checksum-duplicate-member', 'artifact-identity', 1, ['--artifact-dir', supportOutput]);
write(supportOutput, 'SHA256SUMS.txt', cleanChecksums.replace('  LICENSE', '  ../LICENSE'));
check(supportRoot, 'checksum-unsafe-member', 'artifact-identity', 1, ['--artifact-dir', supportOutput]);
write(supportOutput, 'SHA256SUMS.txt', cleanChecksums + '0'.repeat(64) + '  SHA256SUMS.txt\n');
check(supportRoot, 'checksum-self-reference', 'artifact-identity', 1, ['--artifact-dir', supportOutput]);
fs.unlinkSync(path.join(supportOutput, 'SHA256SUMS.txt'));
check(supportRoot, 'checksum-required-for-support', 'artifact-identity', 1, ['--artifact-dir', supportOutput]);
console.log(`${controls} current-byte release controls passed. Temporary evidence: ${base}`);
console.log('Historical negative controls were not recreated: no new commits; allrefs/index production scan remains enabled.');
