import { CircleAlert, X } from "lucide-react";

export default function TopBanner({ message, onClose }: { message: string; onClose(): void }) {
  return (
    <div className="flex items-center gap-3 border-b border-rose/20 bg-rose-soft px-4 py-2 text-[13px] text-rose animate-fade-in">
      <CircleAlert size={15} strokeWidth={1.75} className="shrink-0" />
      <span className="min-w-0 flex-1 leading-5">{message}</span>
      <button
        type="button"
        onClick={onClose}
        aria-label="关闭提示"
        className="shrink-0 rounded-md p-1 text-rose/80 hover:bg-fog/5 hover:text-rose"
      >
        <X size={14} strokeWidth={1.75} />
      </button>
    </div>
  );
}
