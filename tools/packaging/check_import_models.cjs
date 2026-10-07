/* Run with Node: check_import_models.cjs <verified install dir> <new output dir> [helper path].
 * Uses tiny ZIP controls and existing SHA-correct models; never runs the app or deletes files.
 */
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawnSync } = require("node:child_process");

const [installArg, outArg, helperArg] = process.argv.slice(2);
if (!installArg || !outArg || process.platform !== "win32") {
  throw new Error("Windows required: node check_import_models.cjs <verified install dir> <new output dir> [helper path]");
}
const install = path.resolve(installArg), out = path.resolve(outArg);
const helper = helperArg ? path.resolve(helperArg) : path.join(__dirname, "import-models.ps1");
const manifest = fs.readFileSync(path.join(__dirname, "models-manifest.txt"));
const models = manifest.toString("utf8").split(/\r?\n/).flatMap(line => {
  const match = /^([0-9a-fA-F]{64})  ([A-Za-z0-9_./-]+)  ([0-9]+)$/.exec(line);
  return match ? [{ name: "MinerU-4_models_onnx/" + match[2], relative: match[2],
    digest: match[1], size: Number(match[3]) }] : [];
});
fs.mkdirSync(out); // Existing output must fail; original evidence is preserved.

function crc(bytes) {
  let value = 0xffffffff;
  for (const byte of bytes) {
    value ^= byte;
    for (let i = 0; i < 8; i++) value = (value >>> 1) ^ ((value & 1) ? 0xedb88320 : 0);
  }
  return (value ^ 0xffffffff) >>> 0;
}
// ponytail: small stored ZIPs need only local headers/central directory; no ZIP64 or compression.
// Model entries declare official lengths but have no payload; verified existing files must be skipped.
function makeZip(entries) {
  let offset = 0;
  const local = [], central = [];
  for (const entry of entries) {
    const name = Buffer.from(entry.name), data = entry.data || Buffer.alloc(0);
    const size = entry.size ?? data.length, checksum = crc(data);
    const header = Buffer.alloc(30), index = Buffer.alloc(46);
    header.writeUInt32LE(0x04034b50, 0); header.writeUInt16LE(20, 4);
    header.writeUInt32LE(checksum, 14); header.writeUInt32LE(data.length, 18);
    header.writeUInt32LE(size, 22); header.writeUInt16LE(name.length, 26);
    index.writeUInt32LE(0x02014b50, 0); index.writeUInt16LE(0x0314, 4);
    index.writeUInt16LE(20, 6); index.writeUInt32LE(checksum, 16);
    index.writeUInt32LE(data.length, 20); index.writeUInt32LE(size, 24);
    index.writeUInt16LE(name.length, 28); index.writeUInt32LE((entry.attributes || 0) >>> 0, 38);
    index.writeUInt32LE(offset, 42);
    local.push(header, name, data); central.push(index, name);
    offset += header.length + name.length + data.length;
  }
  const directory = Buffer.concat(central), end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10); end.writeUInt32LE(directory.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...local, directory, end]);
}

const cases = [
  { name: "non-white-file", entries: [{ name: "unexpected.txt" }] },
  { name: "nested-license", entries: [{ name: "MODEL-LICENSES/nested/LICENSE.txt" }] },
  { name: "duplicate-case", entries: [{ name: "MODEL-README.md" }, { name: "model-readme.md" }] },
  { name: "path-traversal", entries: [{ name: "../escape.txt" }] },
  { name: "unix-symlink", entries: [{ name: "MODEL-LICENSES/SYMLINK.txt", attributes: 0xa1ff0000 }] },
  { name: "notice-one-size-cap", entries: [{ name: "MODEL-README.md", data: Buffer.alloc(262145) }] },
  { name: "notice-count-cap", entries: Array.from({ length: 33 }, (_, i) => ({ name: `MODEL-LICENSES/L${i}.txt` })) },
  { name: "notice-total-size-cap", entries: Array.from({ length: 9 }, (_, i) =>
    ({ name: `MODEL-LICENSES/L${i}.txt`, data: Buffer.alloc(262144) })) },
  { name: "dos-reparse-notice", entries: [...models, { name: "MODEL-LICENSES/REPARSE.txt", attributes: 0x400 }] },
  { name: "unix-device-notice", entries: [...models, { name: "MODEL-LICENSES/DEVICE.txt", attributes: 0x21a40000 }] },
  { name: "unknown-model-directory", entries: [...models, { name: "MinerU-4_models_onnx/extra/", attributes: 0x41ed0010 }] },
  { name: "valid-notices-with-existing-models", expectedExit: 0, entries: [
    ...models, { name: "MinerU-4_models_onnx/" }, { name: "MODEL-LICENSES/" },
    { name: "MODEL-LICENSES/LICENSE.txt", data: Buffer.from("copyright/permission control") },
  ] },
];
const small = [...models].sort((a, b) => a.size - b.size)[0];
const boundedInstall = path.join(out, "bounded-install");
fs.mkdirSync(boundedInstall);
fs.linkSync(path.join(install, "zhilan.exe"), path.join(boundedInstall, "zhilan.exe"));
for (const model of models.filter(model => model !== small)) {
  const target = path.join(boundedInstall, "models", "MinerU-4_models_onnx", model.relative);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.linkSync(path.join(install, "models", "MinerU-4_models_onnx", model.relative), target);
}
cases.push({ name: "actual-stream-size-cap", install: boundedInstall, bounded: small,
  manifest: Buffer.from([small, ...models.filter(model => model !== small)].map(model =>
    `${model.digest}  ${model.relative}  ${model.size}`).join("\n") + "\n"),
  entries: models.map(model => model === small ? { ...model, data: Buffer.alloc(model.size + 1) } : model) });

const engine = path.join(process.env.WINDIR || "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
const results = [];
for (const control of cases) {
  const dir = path.join(out, control.name);
  fs.mkdirSync(dir);
  fs.copyFileSync(helper, path.join(dir, "import-models.ps1"));
  fs.writeFileSync(path.join(dir, "models-manifest.txt"), control.manifest || manifest);
  fs.writeFileSync(path.join(dir, "zhilan-models-offline-20261007.zip"), makeZip(control.entries));
  const run = spawnSync(engine, ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
    path.join(dir, "import-models.ps1"), "-InstallDir", control.install || install], { windowsHide: true });
  if (run.error) throw run.error;
  fs.writeFileSync(path.join(dir, "stdout.bin"), run.stdout);
  fs.writeFileSync(path.join(dir, "stderr.bin"), run.stderr);
  const stdout = new TextDecoder("gbk").decode(run.stdout);
  fs.writeFileSync(path.join(dir, "stdout.txt"), stdout);
  const target = control.bounded && path.join(boundedInstall, "models", "MinerU-4_models_onnx", control.bounded.relative);
  const written = target && fs.existsSync(target) ? fs.statSync(target).size : 0;
  const expectedExit = control.expectedExit ?? 1;
  const pass = run.status === expectedExit && (!control.bounded ||
    (written <= control.bounded.size && stdout.includes("模型解压长度超过清单上限")));
  results.push({ name: control.name, expectedExit, actualExit: run.status, pass,
    ...(control.bounded && { maximumBytes: control.bounded.size, actualWrittenBytes: written }) });
  console.log(`${pass ? "PASS" : "FAIL"} ${control.name}: expected=${expectedExit}, actual=${run.status}${control.bounded ? `, written=${written}/${control.bounded.size}` : ""}`);
}
const failed = results.filter(result => !result.pass).length;
fs.writeFileSync(path.join(out, "result.json"), JSON.stringify({
  helperSHA256: crypto.createHash("sha256").update(fs.readFileSync(helper)).digest("hex"),
  cases: results, passed: results.length - failed, failed,
  note: "Synthetic model metadata only; source data are never overwritten. 12 bounded-install files are hardlinks to verified targets and must be skipped.",
}, null, 2) + "\n");
process.exitCode = failed ? 1 : 0;
