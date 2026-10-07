/** 内存 blob → 本机文件的下载工具（条目 50）。
 *
 * 背景：条目 22 的 PPT 下载在真机上「完成页说已开始下载、磁盘上什么都没有」。
 * 根因是 `URL.revokeObjectURL(url)` 紧跟 `link.click()` **同步**执行——浏览器把 blob
 * 写进下载文件是**异步**的，URL 一回收，写盘就被静默取消（Edge / Chrome 的下载记录里
 * 连条目都留不下）。playwright 的 `download` 事件在更底层把下载截走，所以旧代码在
 * 自动化里照样「过」，缺陷只在真机暴露。
 *
 * 因此本模块的纪律：**点击与回收彻底分开**——`triggerBlobDownload` 只点击，
 * 回收一律交给 `scheduleRevoke`（延迟）或调用方在卸载时回收；任何地方都不许
 * 在 click 的同一拍 revoke。
 */

/** object URL 的默认回收延迟：留足浏览器写入下载文件的时间。 */
export const DOWNLOAD_REVOKE_DELAY_MS = 60_000;

/** 触发一次浏览器下载：`url` 必须是一个**还活着**的 object URL（见模块说明）。 */
export function triggerBlobDownload(url: string, filename: string): void {
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  link.style.display = "none";
  document.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
  }
}

/** 延迟回收 object URL（默认 60s）；立刻 revoke 会掐断浏览器尚未完成的写盘。 */
export function scheduleRevoke(url: string, delayMs: number = DOWNLOAD_REVOKE_DELAY_MS): void {
  if (!url) return;
  window.setTimeout(() => URL.revokeObjectURL(url), delayMs);
}
