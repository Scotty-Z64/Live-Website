import { cn } from "@/lib/utils";

export function Progress({ value, className }: { value: number; className?: string }) {
  const tone = value >= 100 ? "bg-ok" : value >= 70 ? "bg-warn" : "bg-bad";
  return (
    <div className={cn("h-2 w-full overflow-hidden rounded-full bg-line", className)} role="progressbar" aria-valuenow={value} aria-valuemin={0} aria-valuemax={100}>
      <div className={cn("h-full transition-all", tone)} style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
    </div>
  );
}
