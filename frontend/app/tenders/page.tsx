"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AlertOctagon, CalendarClock, RefreshCw, Users } from "lucide-react";
import { Badge, statusTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { UploadButton } from "@/components/upload";
import { api, upload } from "@/lib/api";
import type { TenderBrief } from "@/lib/types";
import { daysUntil, fmtDate } from "@/lib/utils";

const TIERS: [string, string][] = [["", "All sources"], ["NATIONAL_PROVINCIAL", "eTenders & Provinces"], ["MUNICIPAL", "Municipalities"],
  ["SOE", "SOEs"], ["SETA", "SETAs"], ["PRIVATE", "Private (Sasol, mining, retail)"], ["MANUAL", "Manual uploads"]];

export default function TendersPage() {
  const router = useRouter();
  const [tier, setTier] = useState("");
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<TenderBrief[] | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const load = useCallback(() => {
    const p = new URLSearchParams(); if (tier) p.set("tier", tier); if (q) p.set("q", q);
    return api<{ tenders: TenderBrief[] }>(`/api/tenders?${p}`).then((r) => setRows(r.tenders));
  }, [tier, q]);
  useEffect(() => { load(); }, [load]);

  async function sync() {
    await api("/api/tenders/sync", { method: "POST" });
    setNote("Harvest queued across eTenders, provincial treasuries, metros, SOEs, SETAs and private supplier portals. New tenders appear as each portal completes.");
    setTimeout(load, 4000);
  }
  async function manual(files: File[]) {
    const fd = new FormData(); fd.append("file", files[0]);
    try {
      const r = await upload<{ tender: { id: string } }>("/api/tenders/manual", fd);
      router.push(`/tenders/${r.tender.id}/workspace`);
    } catch (e) { setNote((e as Error).message); }
  }
  async function start(t: TenderBrief) {
    if (!t.bid) await api(`/api/tenders/${t.id}/bid`, { method: "POST" });
    router.push(`/tenders/${t.id}/workspace`);
  }

  return (
    <div className="mx-auto max-w-7xl p-4 md:p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Tender Feed</h1>
          <p className="text-sm text-muted">Harvested every 4 hours · addenda hash-watched every 6 hours.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={sync}><RefreshCw /> Sync All Portals Now</Button>
          <UploadButton accept=".zip,.pdf" onFiles={manual} testId="manual-rfp-input">Upload Manual RFP Pack</UploadButton>
        </div>
      </div>
      {note && <p className="mt-3 rounded border border-line bg-white p-2 text-sm text-muted" data-testid="feed-note">{note}</p>}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        {TIERS.map(([k, v]) => (
          <button key={k} onClick={() => setTier(k)} className={`rounded-full border px-3 py-1 text-xs ${tier === k ? "border-brand bg-brand text-white" : "border-line bg-white"}`}>{v}</button>
        ))}
        <Input className="ml-auto max-w-xs" placeholder="Search reference, entity, description…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="mt-4 space-y-2">
        {rows === null && <p className="text-sm text-muted">Loading…</p>}
        {rows?.length === 0 && <Card className="p-6 text-center text-sm text-muted">No open tenders yet. Sync the portals or upload an RFP pack.</Card>}
        {rows?.map((t) => {
          const d = daysUntil(t.closing_timestamp);
          return (
            <Card key={t.id} className="flex flex-col gap-3 p-4 md:flex-row md:items-center" data-testid="tender-row">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs text-muted">{t.tender_reference}</span>
                  <Badge tone="muted">{t.source_portal}</Badge>
                  {t.submission_mode && <Badge tone="info">{t.submission_mode.replace("_", " ")}</Badge>}
                  {t.has_compulsory_briefing && <Badge tone="warn"><Users className="size-3" /> Compulsory briefing {fmtDate(t.briefing_timestamp)}</Badge>}
                  {t.unacknowledged_addenda > 0 && <Badge tone="bad"><AlertOctagon className="size-3" /> {t.unacknowledged_addenda} new addenda</Badge>}
                </div>
                <div className="mt-1 truncate font-semibold">{t.title || "Untitled tender"}</div>
                <div className="text-xs text-muted">{t.issuing_entity}</div>
              </div>
              <div className="flex items-center gap-4 md:w-80">
                <div className="text-xs">
                  <div className="flex items-center gap-1 text-muted"><CalendarClock className="size-3" /> Closes</div>
                  <div className={`font-semibold ${d != null && d < 7 ? "text-bad" : ""}`}>{fmtDate(t.closing_timestamp, true)}</div>
                </div>
                {t.bid ? (
                  <div className="flex-1">
                    <div className="mb-1 flex justify-between text-xs"><Badge tone={statusTone(t.bid.status)}>{t.bid.status}</Badge><span>{t.bid.readiness_score_pct}%</span></div>
                    <Progress value={t.bid.readiness_score_pct} />
                  </div>
                ) : <div className="flex-1" />}
                <Button size="sm" onClick={() => start(t)}>{t.bid ? "Open workspace" : "Start bid"}</Button>
              </div>
            </Card>
          );
        })}
      </div>
      <p className="mt-6 text-xs text-muted">Portal coverage: <Link className="underline" href="/api/portals" target="_blank">view adapter registry</Link></p>
    </div>
  );
}
