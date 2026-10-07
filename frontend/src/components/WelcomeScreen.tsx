import { Waves } from "lucide-react";

export default function WelcomeScreen({ notice }: { notice?: string }) {
  return (
    <div className="flex min-h-full flex-col items-center justify-center px-6 pb-8 pt-10 animate-fade-up">
      <Waves size={44} className="text-faint" strokeWidth={1.25} />
      <h1 className="mt-8 text-center text-[28px] font-normal tracking-normal text-ink">
        你想让我们在{" "}
        <span className="underline decoration-fog/40 decoration-2 underline-offset-[6px]">知澜</span>{" "}
        中了解什么？
      </h1>
      {notice ? <p className="mt-4 text-center text-[13px] text-rose">{notice}</p> : null}
    </div>
  );
}
