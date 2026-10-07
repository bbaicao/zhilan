import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { ChevronLeft, ChevronRight, X } from "lucide-react";

/** 条目 53：消息区图片的大图预览（Lightbox）。
 *
 * 一期口径：全屏遮罩 + 居中大图 + 右上角关闭；Esc / 点遮罩关闭；
 * 多张时点哪张开哪张，并可用 ← → 或左右按钮逐张翻看。
 * 配色一律走 --zh-* token（深浅两套自动跟随），不写死 hex。
 *
 * **必须 portal 到 body**：消息气泡挂着 `animate-fade-up`，而它的 fill-mode 是 `both`
 * ——动画结束后 `transform: translateY(0)` 仍留在元素上，于是那个气泡成了
 * `position: fixed` 后代的**包含块**，遮罩会缩进气泡那么一小块里而不是铺满窗口
 *（2026-09-14 无头实测踩到：截图里遮罩只盖住消息那一行）。挂在 body 上才与祖先无关。
 */
export default function ImageLightbox({
  images,
  startIndex = 0,
  onClose,
}: {
  /** 这条消息带的所有图（有序）——翻页在同一批里走 */
  images: string[];
  /** 点开的是第几张 */
  startIndex?: number;
  onClose(): void;
}) {
  const total = images.length;
  const [index, setIndex] = useState(() =>
    total ? Math.min(Math.max(startIndex, 0), total - 1) : 0,
  );
  const many = total > 1;

  // Esc 关闭 / 左右翻页；遮罩期间占着键盘，关掉即摘
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (!many) return;
      if (event.key === "ArrowLeft") {
        setIndex((prev) => (prev - 1 + total) % total);
      } else if (event.key === "ArrowRight") {
        setIndex((prev) => (prev + 1) % total);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [many, onClose, total]);

  if (!total) return null;
  const current = images[Math.min(index, total - 1)];

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label="图片预览"
      className="fixed inset-0 z-[60] flex animate-fade-in items-center justify-center bg-veil p-6 backdrop-blur-sm"
      onClick={onClose}
    >
      {/* 图上点击不关闭：只有点遮罩（含图外的空白）才关。
          尺寸口径：按原始尺寸显示，可用空间（92vw / 86vh）不够才缩小 —— 不做放大，
          避免把小图糊成一片；但要给一个 200px 的下限，保证「点开的大图」永远不小于
          气泡里的缩略图（缩略图固定 128px 高，小图反而可能比原图大）。 */}
      <img
        src={current}
        alt={total > 1 ? `图片 ${index + 1} / ${total}` : "图片预览"}
        onClick={(event) => event.stopPropagation()}
        className="min-w-[200px] max-h-[86vh] max-w-[92vw] select-none rounded-xl border border-line bg-panel object-contain shadow-pop"
      />

      {total > 1 ? (
        <>
          <button
            type="button"
            aria-label="上一张"
            onClick={(event) => {
              event.stopPropagation();
              setIndex((prev) => (prev - 1 + total) % total);
            }}
            className="absolute left-5 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full border border-line bg-panel/90 text-mute shadow-pop transition-colors hover:text-ink"
          >
            <ChevronLeft size={18} strokeWidth={2} />
          </button>
          <button
            type="button"
            aria-label="下一张"
            onClick={(event) => {
              event.stopPropagation();
              setIndex((prev) => (prev + 1) % total);
            }}
            className="absolute right-5 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full border border-line bg-panel/90 text-mute shadow-pop transition-colors hover:text-ink"
          >
            <ChevronRight size={18} strokeWidth={2} />
          </button>
        </>
      ) : null}

      <button
        type="button"
        aria-label="关闭预览"
        title="关闭（Esc）"
        onClick={(event) => {
          event.stopPropagation();
          onClose();
        }}
        className="absolute right-5 top-5 flex h-9 w-9 items-center justify-center rounded-full border border-line bg-panel/90 text-mute shadow-pop transition-colors hover:text-ink"
      >
        <X size={17} strokeWidth={2} />
      </button>

      {total > 1 ? (
        <div className="absolute bottom-6 left-1/2 -translate-x-1/2 rounded-full border border-line bg-panel/90 px-2.5 py-1 text-[12px] text-mute shadow-pop">
          {index + 1} / {total}
        </div>
      ) : null}
    </div>,
    document.body,
  );
}
