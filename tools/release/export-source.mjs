#!/usr/bin/env node
// Export current public bytes without changing Git's index or references.
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';

export const sha256 = body => createHash('sha256').update(body).digest('hex');
export function readTree(root) {
  const files = new Map();
  function walk(directory) {
    for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, item.name);
      if (item.isSymbolicLink()) throw new Error('Symbolic links are unsupported');
      if (item.isDirectory()) walk(absolute);
      else if (item.isFile()) files.set(path.relative(root, absolute).replaceAll('\\', '/'), fs.readFileSync(absolute));
    }
  }
  walk(root);
  return new Map([...files].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0));
}
export function inventory(files) {
  const digest = createHash('sha256');
  const entries = [...files].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([name, body]) => {
    digest.update(name).update('\0').update(body).update('\0');
    return { path: name, bytes: body.length, sha256: sha256(body) };
  });
  return { schema: 1, tree_hash: digest.digest('hex'), files: entries };
}
export function gitRead(root, args) {
  const result = spawnSync('git', ['--no-optional-locks', '-C', root, ...args], { encoding: 'utf8', windowsHide: true, maxBuffer: 32 * 1024 * 1024 });
  if (result.error || result.status !== 0) throw new Error(`Git read failed: ${args[0]}`);
  return result.stdout;
}
export function workingFiles(root) {
  const names = gitRead(root, ['ls-files', '--cached', '--others', '--exclude-standard', '-z']).split('\0').filter(Boolean);
  const files = new Map();
  for (const name of [...new Set(names)].sort()) {
    const absolute = path.join(root, name);
    if (!fs.existsSync(absolute)) continue; // Deleted tracked files are absent from the deliverable.
    const info = fs.lstatSync(absolute);
    if (info.isSymbolicLink() || !info.isFile()) throw new Error(`Unsupported public file: ${name}`);
    files.set(name.replaceAll('\\', '/'), fs.readFileSync(absolute));
  }
  return files;
}
export function frontendIdentity(files) {
  function hash(prefix, relativePrefix, fold) {
    const entries = [...files].filter(([name]) => name.startsWith(prefix)).map(([name, body]) => [name.slice(relativePrefix.length), body]);
    entries.sort(([a], [b]) => {
      if (!fold) {
        // The existing Python src checker sorts WindowsPath objects by
        // case-folded path components, not the raw slash-separated string.
        const left = a.toLowerCase().split('/'), right = b.toLowerCase().split('/');
        for (let index = 0; index < Math.min(left.length, right.length); index++) {
          if (left[index] !== right[index]) return left[index] < right[index] ? -1 : 1;
        }
        return left.length - right.length;
      }
      const x = a.toLowerCase(), y = b.toLowerCase();
      return x < y ? -1 : x > y ? 1 : 0;
    });
    const digest = createHash('sha256');
    for (const [name, body] of entries) digest.update(name).update('\0').update(body).update('\0');
    return { hash: digest.digest('hex'), count: entries.length };
  }
  const src = hash('frontend/src/', 'frontend/', false);
  const dist = hash('frontend/dist/', 'frontend/dist/', true);
  return { src_tree_hash: src.hash, src_file_count: src.count, dist_tree_hash: dist.hash, dist_file_count: dist.count };
}
export function zipOperation(operation, source, destination) {
  // Environment arguments preserve Unicode and do not become PowerShell code.
  const command = operation === 'create'
    ? '[IO.Compression.ZipFile]::CreateFromDirectory($env:ZHILAN_ZIP_SOURCE,$env:ZHILAN_ZIP_DESTINATION,[IO.Compression.CompressionLevel]::Optimal,$false)'
    : '$archive=[IO.Compression.ZipFile]::OpenRead($env:ZHILAN_ZIP_SOURCE); try { $seen=@{}; foreach($entry in $archive.Entries) { $name=$entry.FullName.Replace("\\","/"); if($name.StartsWith("/") -or $name -match "^[A-Za-z]:" -or $name.Split("/") -contains ".." -or $seen.ContainsKey($name.ToLowerInvariant())) { throw "Unsafe or duplicate ZIP member" }; $seen[$name.ToLowerInvariant()]=$true }; [IO.Compression.ZipFileExtensions]::ExtractToDirectory($archive,$env:ZHILAN_ZIP_DESTINATION) } finally { $archive.Dispose() }';
  const result = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', '$ErrorActionPreference="Stop"; Add-Type -AssemblyName System.IO.Compression.FileSystem; ' + command], {
    windowsHide: true, encoding: 'utf8', maxBuffer: 1024 * 1024,
    env: { ...process.env, ZHILAN_ZIP_SOURCE: source, ZHILAN_ZIP_DESTINATION: destination },
  });
  if (result.error || result.status !== 0) throw new Error(`ZIP ${operation} failed`);
}
export function exportSource(root, output) {
  if (fs.existsSync(output)) throw new Error('Export output must be a new directory');
  const files = workingFiles(root);
  const privateRoots = [String.fromCharCode(20219, 21153, 26641), String.fromCharCode(37325, 26500, 26041, 26696),
    String.fromCharCode(20219, 21153, 26641) + '-' + String.fromCharCode(25991, 26723, 35299, 26512, 21319, 32423)];
  const forbidden = /(?:^|\/)(?:\.git|\.venv|venv|node_modules|__pycache__|\.pytest_cache|AGENTS\.md|Atlas\.md)(?:\/|$)|^(?:data|models|build|release-assets|docs|dist)\/|^frontend\/build\/|(?:^|\/)\.env(?:\.(?!example$)[^/]*)?$|\.(?:pyc|log|exit|out|err|tsbuildinfo|exe|dll|pyd|zip|7z|onnx|gguf|safetensors|pth)$/i;
  for (const name of files.keys()) {
    if (forbidden.test(name) || privateRoots.some(item => name === item || name.startsWith(item + '/'))) files.delete(name);
  }
  for (const [name, body] of readTree(path.join(root, 'frontend/dist'))) files.set('frontend/dist/' + name, body);
  const expectedFrontend = JSON.parse(files.get('frontend/.build-manifest')?.toString('utf8') ?? '{}');
  const frontend = frontendIdentity(files);
  for (const [key, value] of Object.entries(frontend)) if (expectedFrontend[key] !== value || !value) throw new Error(`Frontend identity mismatch: ${key}`);
  const version = JSON.parse(files.get('frontend/package.json').toString('utf8')).version;
  if (!/^\d+\.\d+\.\d+(?:[-.][a-z0-9]+)*$/i.test(version)) throw new Error('Invalid source version');
  const listing = inventory(files);
  const licenseFiles = listing.files.filter(item => ['LICENSE', 'NOTICE'].includes(item.path) || item.path.startsWith('tools/packaging/licenses-vendor/'));
  if (!licenseFiles.some(item => item.path === 'LICENSE') || !licenseFiles.some(item => item.path === 'NOTICE')) throw new Error('Source license documents missing');
  fs.mkdirSync(output, { recursive: false });
  const source = path.join(output, 'source');
  fs.mkdirSync(source);
  for (const [name, body] of files) {
    const target = path.join(source, name);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, body);
  }
  const provenance = {
    schema: 1, version, git_head: gitRead(root, ['rev-parse', 'HEAD']).trim(),
    git_dirty: Boolean(gitRead(root, ['status', '--porcelain=v1', '-uall']).trim()),
    source_tree_hash: listing.tree_hash, source_file_count: listing.files.length,
    frontend, license_files: licenseFiles,
    boundaries: ['Local candidate only; publication is not authorized.', 'License provenance limitations in NOTICE remain applicable.'],
  };
  const archiveName = `zhilan-source-${version}-candidate.zip`;
  const archive = path.join(output, archiveName);
  zipOperation('create', source, archive);
  const archiveBody = fs.readFileSync(archive);
  const manifest = { schema: 1, version, artifacts: [{ path: archiveName, kind: 'source-zip', bytes: archiveBody.length, sha256: sha256(archiveBody) }] };
  for (const [name, data] of [['SOURCE.json', provenance], ['SOURCE-INVENTORY.json', listing], ['release-manifest.json', manifest]]) {
    fs.writeFileSync(path.join(output, name), JSON.stringify(data, null, 2) + '\n');
  }
  const extracted = path.join(output, 'archive-verification');
  zipOperation('extract', archive, extracted);
  const actual = inventory(readTree(extracted));
  if (JSON.stringify(actual) !== JSON.stringify(listing)) throw new Error('Source archive content mismatch');
  return { output, version, files: listing.files.length, tree_hash: listing.tree_hash, archive: manifest.artifacts[0] };
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    const option = name => { const index = args.indexOf(name); return index < 0 ? undefined : args[index + 1]; };
    if (!option('--root') || !option('--out')) throw new Error('Usage: export-source.mjs --root PUBLIC_ROOT --out NEW_DIRECTORY');
    console.log(JSON.stringify(exportSource(path.resolve(option('--root')), path.resolve(option('--out'))), null, 2));
  } catch (error) { console.error(`FAIL export: ${error.message}`); process.exitCode = 1; }
}
