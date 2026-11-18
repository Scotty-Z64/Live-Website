import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold leading-4", {
  variants: {
    tone: {
      ok: "border-ok/30 bg-ok/10 text-ok",
      warn: "border-warn/30 bg-warn/10 text-warn",
      bad: "border-bad/30 bg-bad/10 text-bad",
      info: "border-brand/20 bg-brand/5 text-brand",
      muted: "border-line bg-panel text-muted",
    },
  },
  defaultVariants: { tone: "muted" },
});

export function Badge({ className, tone, ...props }: React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}

export function statusTone(s: string | null | undefined): "ok" | "warn" | "bad" | "info" | "muted" {
  if (!s) return "muted";
  if (["VALID", "VERIFIED_ATTENDED", "NOT_REQUIRED", "READY", "SUBMITTED"].includes(s)) return "ok";
  if (["QUEUED", "PROCESSING", "REGISTERED_AWAITING_CERT", "AWAITING_OTP", "SUBMITTING", "DRAFT"].includes(s)) return "info";
  if (["EXPIRED", "MISSING_STAMP", "MISMATCH_ERROR", "BLOCKED", "MISSING", "SUBMISSION_FAILED", "PENDING_REGISTRATION"].includes(s)) return "bad";
  if (["LOW_OCR_CONFIDENCE", "NEEDS_CATEGORY"].includes(s)) return "warn";
  return "muted";
}
