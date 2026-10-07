#!/usr/bin/env node
/**
 * 知澜 v2 · 前端唯一构建入口（缺陷清单-3 · S0 · 条目 171）
 * =================================================================
 * 为什么需要它：裸 `pnpm run build` 在本机不可用（ERR_PNPM_IGNORED_BUILDS: esbuild@0.25.12），
 * 且 pnpm 的 run/exec 会触发依赖状态检查并自动 install ⇒ 生成 frontend/pnpm-lock.yaml +
 * frontend/pnpm-workspace.yaml（已发生 83,000 B 工作区污染事故）。本脚本**完全不经过任何包管理器**，
 * 直接调用 frontend/node_modules/.bin 下已安装的 tsc / vite，故不可能产生锁文件。
 *
 * 用法（frontend 下一条命令）：
 *   cd frontend && node scripts/build.mjs          # 等价于 tsc -b --pretty false + vite build
 *
 * 行为与 package.json 原 scripts.build = "tsc -b && vite build" 等价（顺序、退出码均按步骤传播）。
 * · 任一阶段非 0 ⇒ 立即以该退出码结束（不吞错、不续跑）；
 * · stdio = inherit（不建管道：DSH 受限沙箱下 Node 管道会 EPERM，且便于实时看输出）；
 * · 不写任何仓库外产物；dist 由 vite 自己清空并重建（vite.config.ts 的 outDir/emptyOutDir）。
 * · ⛔ 本脚本不更新 frontend/.build-manifest（那是 backend/scripts/check_frontend_dist_freshness.py --write 的职责）。
 */
import { spawnSync } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const frontendRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const binDir = path.join(frontendRoot, "node_modules", ".bin");
const isWindows = process.platform === "win32";

const steps = [
  { name: "tsc -b --pretty false", bin: "tsc", args: ["-b", "--pretty", "false"] },
  { name: "vite build", bin: "vite", args: ["build"] },
];

function resolveBin(name) {
  const names = isWindows ? [name + ".cmd", name + ".exe", name] : [name];
  for (const candidate of names) {
    const full = path.join(binDir, candidate);
    if (existsSync(full)) return full;
  }
  return null;
}

function countFiles(dir) {
  let n = 0;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) n += countFiles(path.join(dir, entry.name));
    else if (entry.isFile()) n += 1;
  }
  return n;
}

console.log("[build] frontend: " + frontendRoot);

for (const step of steps) {
  const binPath = resolveBin(step.bin);
  if (!binPath) {
    console.error("[build] FAIL: 找不到 " + step.bin + "（node_modules 未安装？查找目录 " + binDir + "）");
    process.exit(1);
  }
  console.log("[build] step: " + step.name);
  console.log("[build]   bin: " + binPath);
  const useCmdShim = isWindows && /\.(cmd|bat)$/i.test(binPath);
  const file = useCmdShim ? (process.env.ComSpec || "cmd.exe") : binPath;
  const argv = useCmdShim ? ["/d", "/s", "/c", binPath, ...step.args] : step.args;
  const started = Date.now();
  const result = spawnSync(file, argv, { cwd: frontendRoot, stdio: "inherit" });
  if (result.error) {
    console.error("[build] FAIL: " + step.name + " 启动失败：" + result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    console.error("[build] FAIL: " + step.name + " 退出码 " + result.status + "（" + Math.round((Date.now() - started) / 1000) + "s）");
    process.exit(result.status === null ? 1 : result.status);
  }
  console.log("[build]   ok: " + step.name + "（" + Math.round((Date.now() - started) / 1000) + "s）");
}

const dist = path.join(frontendRoot, "dist");
const distCount = existsSync(dist) ? countFiles(dist) : 0;
console.log("[build] OK: tsc -b + vite build 成功；dist=" + dist + "（" + distCount + " 文件）");
console.log("[build] 提示：改了 frontend/src 后须重跑本命令；manifest 更新见 backend/scripts/check_frontend_dist_freshness.py --write");
