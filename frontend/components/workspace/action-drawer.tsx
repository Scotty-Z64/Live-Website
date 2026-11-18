"use client";
import { AlertTriangle, CheckCircle2, Radio, ShieldAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { UploadButton } from "@/components/upload";
import type { Bid, Issue } from "@/lib/types";
import { CATEGORY_LABELS } from "@/lib/types";

export function ActionDrawer({ bid, live, onUpload, onBriefingPack, onAttendanceCert, onAddendum, onTab, onRerun }: {
  bid: Bid; live: boolean;
  onUpload: (category: string, files: File[]) => Promise<void>;
  onBriefingPack: () => void;
  onAttendanceCert: (files: File[]) => Promise<void>;
  onAddendum: (id: string) => void;
  onTab: (tab: string) => void;
  onRerun: () => Promise<void>;
}) {
  const blockers = bid.blocking_issues.filter((i) => i.severity === "BLOCKER");
  const warnings = bid.blocking_issues.filter((i) => i.severity !== "BLOCKER");
  const score = bid.readiness_score_pct ?? 0;
  return (
    <div className="flex h-full flex-col" data-testid="action-drawer">
      <div className="border-b border-line p-4">
        <div className="flex items-center justify-between">
          <h2 className="font-semibold">Action Drawer</h2>
          <span className={`flex items-center gap-1 text-[11px] ${live ? "text-ok" : "text-muted"}`}><Radio className="size-3" />{live ? "live" : "reconnecting"}</span>
        </div>
        <div className="mt-3 flex items-end justify-between">
          <div className="text-3xl font-bold" data-testid="readiness-score">{score.toFixed(0)}%</div>
          <Badge tone={blockers.length ? "bad" : "ok"} data-testid="blocker-count">{blockers.length ? `${blockers.length} blocker${blockers.length > 1 ? "s" : ""}` : "Ready to submit"}</Badge>
        </div>
        <Progress value={score} className="mt-2" />
      </div>
      <div className="scroll-thin flex-1 space-y-2 overflow-y-auto p-4">
        {!blockers.length && (
          <div className="flex items-start gap-2 rounded-md border border-ok/30 bg-ok/5 p-3 text-sm text-ok">
            <CheckCircle2 className="mt-0.5 size-4 shrink-0" /> All compliance checks pass. Export and submission are unlocked.
          </div>
        )}
        {[...blockers, ...warnings].map((i) => (
          <IssueCard key={i.id} issue={i} onUpload={onUpload} onBriefingPack={onBriefingPack} onAttendanceCert={onAttendanceCert}
            onAddendum={onAddendum} onTab={onTab} onRerun={onRerun} />
        ))}
      </div>
    </div>
  );
}

function IssueCard({ issue, onUpload, onBriefingPack, onAttendanceCert, onAddendum, onTab, onRerun }: {
  issue: Issue;
  onUpload: (category: string, files: File[]) => Promise<void>;
  onBriefingPack: () => void;
  onAttendanceCert: (files: File[]) => Promise<void>;
  onAddendum: (id: string) => void;
  onTab: (tab: string) => void;
  onRerun: () => Promise<void>;
}) {
  const a = issue.action;
  const blocker = issue.severity === "BLOCKER";
  return (
    <div className={`rounded-md border p-3 text-sm ${blocker ? "border-bad/30 bg-bad/5" : "border-warn/30 bg-warn/5"}`} data-testid={`issue-${issue.code}`}>
      <div className="flex items-start gap-2">
        {issue.code === "SILENT_ADDENDUM_DETECTED" || issue.code === "BRIEFING_GATE_LOCKED"
          ? <ShieldAlert className={`mt-0.5 size-4 shrink-0 ${blocker ? "text-bad" : "text-warn"}`} />
          : <AlertTriangle className={`mt-0.5 size-4 shrink-0 ${blocker ? "text-bad" : "text-warn"}`} />}
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-bold tracking-wide text-slate-500">{issue.code}</div>
          <div className="font-medium">{issue.message}</div>
          {issue.remediation && <div className="mt-1 text-xs text-slate-600">{issue.remediation}</div>}
          <div className="mt-2">
            {a?.type === "UPLOAD_DOCUMENT" && a.doc_category && (
              <UploadButton size="sm" variant="accent" multiple={a.doc_category === "DIRECTOR_ID"} testId={`fix-${a.doc_category}`}
                onFiles={(f) => onUpload(a.doc_category!, f)}>
                Upload Missing Document Now
              </UploadButton>
            )}
            {a?.type === "UPLOAD_DOCUMENT" && a.doc_category && <div className="mt-1 text-[11px] text-muted">→ {CATEGORY_LABELS[a.doc_category]}</div>}
            {a?.type === "BRIEFING_PACK" && <Button size="sm" variant="accent" onClick={onBriefingPack}>Prepare Briefing Pack</Button>}
            {a?.type === "UPLOAD_ATTENDANCE_CERT" && (
              <UploadButton size="sm" variant="accent" testId="fix-attendance" onFiles={onAttendanceCert}>Upload Attendance Cert</UploadButton>
            )}
            {a?.type === "ACK_ADDENDUM" && a.addendum_id && <Button size="sm" variant="accent" onClick={() => onAddendum(a.addendum_id!)}>Review Addendum</Button>}
            {a?.type === "OPEN_PRICING" && <Button size="sm" variant="outline" onClick={() => onTab("pricing")}>Open Pricing Grid</Button>}
            {a?.type === "OPEN_VAULT" && <Button size="sm" variant="outline" asChild><a href="/vault">Review documents</a></Button>}
            {a?.type === "RERUN_PREFLIGHT" && <Button size="sm" variant="outline" onClick={onRerun}>Re-Run Pre-Flight</Button>}
          </div>
        </div>
      </div>
    </div>
  );
}
