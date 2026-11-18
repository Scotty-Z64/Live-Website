"use client";
import { use, useState } from "react";
import { AlertOctagon, CalendarClock, Download, FileSignature, Loader2, RefreshCw, Send, Users } from "lucide-react";
import { Badge, statusTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { UploadButton } from "@/components/upload";
import { ActionDrawer } from "@/components/workspace/action-drawer";
import { AddendumDialog, BriefingDialog, SubmitDialog } from "@/components/workspace/dialogs";
import { CompliancePanel, PreflightPanel, PricingPanel, SbdPanel } from "@/components/workspace/panels";
import { useBid } from "@/components/workspace/use-bid";
import { ApiError, api, upload } from "@/lib/api";
import type { Bid, Issue, Tender } from "@/lib/types";
import { daysUntil, fmtDate } from "@/lib/utils";

export default function Workspace({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { tender, bid, setBid, setTender, refresh, live, error } = useBid(id);
  const [tab, setTab] = useState("preflight");
  const [briefingOpen, setBriefingOpen] = useState(false);
  const [submitOpen, setSubmitOpen] = useState(false);
  const [addendumId, setAddendumId] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<{ tone: "ok" | "bad"; text: string; blockers?: Issue[] } | null>(null);

  if (error) return <div className="p-6 text-bad">{error}</div>;
  if (!tender || !bid) return <div className="flex items-center gap-2 p-6 text-muted"><Loader2 className="size-4 animate-spin" /> Loading workspace…</div>;

  const blockers = bid.blocking_issues.filter((i) => i.severity === "BLOCKER");
  const locked = blockers.length > 0;
  const days = daysUntil(tender.closing_timestamp);
  const pendingAddenda = tender.addenda_history.filter((a) => !a.acknowledged);

  async function run(label: string, fn: () => Promise<void>) {
    setBusy(label); setToast(null);
    try { await fn(); } catch (e) {
      const err = e as ApiError;
      const detail = err.detail as { message?: string; blockers?: Issue[] } | string;
      setToast({ tone: "bad", text: typeof detail === "string" ? detail : detail?.message ?? err.message, blockers: typeof detail === "object" ? detail?.blockers : undefined });
    } finally { setBusy(null); }
  }
  const uploadDoc = async (category: string, files: File[]) => {
    for (const f of files) {
      const fd = new FormData(); fd.append("file", f); fd.append("doc_category", category);
      await upload("/api/vault/upload", fd);
    }
    await refresh();
  };
  const uploadCert = async (files: File[]) => {
    const fd = new FormData(); fd.append("file", files[0]);
    const r = await upload<{ bid: Bid; warnings: string[] }>(`/api/bids/${bid.id}/briefing/certificate`, fd);
    setBid(r.bid);
    setToast({ tone: "ok", text: `Attendance verified — briefing gate unlocked.${r.warnings.length ? " Note: " + r.warnings.join(" ") : ""}` });
  };

  return (
    <div className="flex min-h-screen flex-col">
      {/* Header */}
      <div className="border-b border-line bg-white px-4 py-3 md:px-6">
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
          <span className="font-mono">{tender.tender_reference}</span>·<span>{tender.issuing_entity ?? tender.source_portal}</span>
          {tender.submission_mode && <Badge tone="info">{tender.submission_mode.replace("_", " ")}</Badge>}
          <Badge tone={statusTone(bid.status)} data-testid="bid-status">{bid.status}</Badge>
        </div>
        <h1 className="mt-1 text-lg font-bold leading-tight">{tender.title}</h1>
        <div className={`mt-1 flex items-center gap-1 text-sm ${days != null && days < 7 ? "text-bad" : "text-muted"}`}>
          <CalendarClock className="size-4" /> Closes {fmtDate(tender.closing_timestamp, true)}{days != null && ` · ${days} day${days === 1 ? "" : "s"} left`}
        </div>
        {/* Top Action Bar */}
        <div className="mt-3 flex flex-wrap gap-2" data-testid="action-bar">
          <Button variant="outline" disabled={!!busy} onClick={() => run("audit", async () => {
            const r = await api<{ bid: Bid; tender: Tender }>(`/api/bids/${bid.id}/audit`, { method: "POST" }); setBid(r.bid); setTender(r.tender);
            setToast({ tone: "ok", text: "Pre-flight and compliance audit re-run." });
          })}>{busy === "audit" ? <Loader2 className="animate-spin" /> : <RefreshCw />} Re-Run Pre-Flight & Compliance Audit</Button>
          <Button variant="outline" disabled={!!busy} onClick={() => run("sbd", async () => {
            const r = await api<{ bid: Bid }>(`/api/bids/${bid.id}/sbd/autofill`, { method: "POST" }); setBid(r.bid); setTab("sbd");
          })}>{busy === "sbd" ? <Loader2 className="animate-spin" /> : <FileSignature />} Auto-Fill All SBDs</Button>
          <Button disabled={!!busy || locked} title={locked ? "Resolve blockers in the Action Drawer first" : undefined} onClick={() => run("binder", async () => {
            const r = await api<{ zip_url: string; total_pages: number; bid: Bid; spine: { spine_width_mm: number } }>(`/api/bids/${bid.id}/binder`, { method: "POST" });
            setBid(r.bid);
            setToast({ tone: "ok", text: `Binder compiled: ${r.total_pages} pages, ${r.spine.spine_width_mm} mm lever-arch spine.` });
            window.location.href = r.zip_url;
          })} data-testid="download-binder">{busy === "binder" ? <Loader2 className="animate-spin" /> : <Download />} Download Hardcopy Binder (ZIP)</Button>
          <Button variant="accent" disabled={!!busy || (locked && bid.status !== "AWAITING_OTP" && bid.status !== "SUBMITTED") || tender.submission_mode === "MANUAL_HARDCOPY"}
            onClick={() => setSubmitOpen(true)} data-testid="submit-digital"><Send /> Submit Digitally to Portal</Button>
        </div>
        {toast && (
          <div className={`mt-2 rounded border p-2 text-sm ${toast.tone === "ok" ? "border-ok/30 bg-ok/5 text-ok" : "border-bad/30 bg-bad/5 text-bad"}`} data-testid="toast">
            {toast.text}{toast.blockers && <ul className="ml-4 list-disc text-xs">{toast.blockers.map((b) => <li key={b.id}>{b.message}</li>)}</ul>}
          </div>
        )}
      </div>

      {/* Banners */}
      {tender.has_compulsory_briefing && bid.briefing_gate_status !== "VERIFIED_ATTENDED" && (
        <div className="flex flex-wrap items-center gap-3 border-b border-warn/40 bg-warn/10 px-4 py-3 text-sm md:px-6" data-testid="briefing-banner">
          <Users className="size-5 text-warn" />
          <div className="min-w-0 flex-1">
            <b>Compulsory briefing</b> — {fmtDate(tender.briefing_timestamp, true)} · {tender.briefing_venue_or_link}
            <div className="text-xs">Gate: <b data-testid="gate-status">{bid.briefing_gate_status.replaceAll("_", " ")}</b>. Non-attendance disqualifies the bid. Automated registration is prohibited.</div>
          </div>
          {bid.briefing_gate_status === "PENDING_REGISTRATION"
            ? <Button size="sm" variant="accent" onClick={() => setBriefingOpen(true)}>Prepare Briefing Pack</Button>
            : <div className="flex gap-2">
                {bid.letter_of_authority_url && <Button size="sm" variant="outline" asChild><a href={bid.letter_of_authority_url} target="_blank">Letter of Authority</a></Button>}
                <UploadButton size="sm" variant="accent" onFiles={(f) => run("cert", () => uploadCert(f))} testId="banner-attendance">Upload Attendance Cert</UploadButton>
              </div>}
        </div>
      )}
      {pendingAddenda.length > 0 && (
        <div className="flex items-center gap-3 border-b border-bad/40 bg-bad/10 px-4 py-3 text-sm md:px-6" data-testid="addendum-banner">
          <AlertOctagon className="size-5 text-bad" />
          <div className="flex-1"><b>SILENT_ADDENDUM_DETECTED</b> — {pendingAddenda.length} new/changed tender file(s). Export and submission are hard-locked until you sign off.</div>
          <Button size="sm" variant="danger" onClick={() => setAddendumId(pendingAddenda[0].id)}>Review addendum</Button>
        </div>
      )}

      {/* Split screen 65 / 35 */}
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <section className="min-w-0 p-4 md:p-6 lg:w-[65%]">
          <Tabs value={tab} onValueChange={setTab}>
            <div className="overflow-x-auto">
              <TabsList>
                <TabsTrigger value="preflight">Pre-Flight RFP Breakdown</TabsTrigger>
                <TabsTrigger value="compliance">Compliance Matrix</TabsTrigger>
                <TabsTrigger value="pricing">BoQ & Catalog Pricing</TabsTrigger>
                <TabsTrigger value="sbd">Auto-Filled SBD Preview</TabsTrigger>
              </TabsList>
            </div>
            <TabsContent value="preflight"><PreflightPanel tender={tender} /></TabsContent>
            <TabsContent value="compliance"><CompliancePanel bidId={bid.id} bid={bid} /></TabsContent>
            <TabsContent value="pricing"><PricingPanel key={bid.pricing_summary?.lines ? "priced" : "raw"} bid={bid} tender={tender} onSaved={setBid} /></TabsContent>
            <TabsContent value="sbd"><SbdPanel bid={bid} onAutofill={() => run("sbd", async () => {
              const r = await api<{ bid: Bid }>(`/api/bids/${bid.id}/sbd/autofill`, { method: "POST" }); setBid(r.bid);
            })} /></TabsContent>
          </Tabs>
        </section>
        <aside className="border-t border-line bg-white lg:sticky lg:top-0 lg:h-screen lg:w-[35%] lg:border-l lg:border-t-0">
          <ActionDrawer bid={bid} live={live}
            onUpload={uploadDoc}
            onBriefingPack={() => setBriefingOpen(true)}
            onAttendanceCert={(f) => run("cert", () => uploadCert(f))}
            onAddendum={setAddendumId}
            onTab={setTab}
            onRerun={async () => { const r = await api<{ bid: Bid; tender: Tender }>(`/api/bids/${bid.id}/audit`, { method: "POST" }); setBid(r.bid); setTender(r.tender); }} />
        </aside>
      </div>

      <BriefingDialog open={briefingOpen} onOpenChange={setBriefingOpen} tender={tender} bid={bid} onDone={setBid} />
      <AddendumDialog addendum={tender.addenda_history.find((a) => a.id === addendumId) ?? null} bid={bid} onClose={() => setAddendumId(null)} onDone={refresh} />
      <SubmitDialog open={submitOpen} onOpenChange={setSubmitOpen} bid={bid} tender={tender} />
    </div>
  );
}
