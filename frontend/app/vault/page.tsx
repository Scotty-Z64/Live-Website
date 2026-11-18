"use client";
import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Download, FileText, Loader2 } from "lucide-react";
import { Badge, statusTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DropZone, UploadButton } from "@/components/upload";
import { api, upload } from "@/lib/api";
import { downloadAuthed } from "@/lib/download";
import { CATEGORY_LABELS, type VaultDoc } from "@/lib/types";
import { daysUntil, fmtDate } from "@/lib/utils";

type VaultResp = { slots: Record<string, VaultDoc[]>; unclassified: VaultDoc[] };

export default function VaultPage() {
  const [data, setData] = useState<VaultResp | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = useCallback(() => api<VaultResp>("/api/vault").then(setData), []);
  useEffect(() => { load(); }, [load]);
  const processing = data && [...Object.values(data.slots).flat(), ...data.unclassified].some((d) => ["QUEUED", "PROCESSING"].includes(d.processing_state));
  useEffect(() => { if (!processing) return; const t = setInterval(load, 2000); return () => clearInterval(t); }, [processing, load]);

  async function send(files: File[], category?: string) {
    setMsg(null);
    if (category) {
      for (const f of files) {
        const fd = new FormData(); fd.append("file", f); fd.append("doc_category", category);
        try { await upload("/api/vault/upload", fd); } catch (e) { setMsg(`${f.name}: ${(e as Error).message}`); }
      }
    } else {
      const fd = new FormData(); files.forEach((f) => fd.append("files", f));
      const r = await upload<{ results: { filename: string; ok: boolean; error?: string; duplicate?: boolean }[] }>("/api/vault/bulk", fd);
      const bad = r.results.filter((x) => !x.ok);
      setMsg(bad.length ? bad.map((b) => `${b.filename}: ${b.error}`).join(" · ") : `${r.results.length} document(s) scanned and queued for validation.`);
    }
    await load();
  }

  const slots = data ? Object.entries(data.slots) : [];
  const valid = slots.filter(([, docs]) => docs.some((d) => d.is_current && d.validation_status === "VALID")).length;

  return (
    <div className="mx-auto max-w-7xl p-4 md:p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Master Compliance Vault</h1>
          <p className="text-sm text-muted">Upload once. Every file is virus-scanned, SHA-256 de-duplicated, OCR’d and validated against SA statutory rules.</p>
        </div>
        <div className="flex items-center gap-2">
          <Badge tone={valid === slots.length ? "ok" : "info"}>{valid}/{slots.length} slots valid</Badge>
          <Button variant="outline" size="sm" onClick={() => downloadAuthed("/api/vault/templates/bbbee-affidavit", "BBBEE_Sworn_Affidavit_prefilled.pdf")}>
            <Download /> Pre-filled B-BBEE affidavit
          </Button>
        </div>
      </div>

      <div className="mt-4">
        <DropZone testId="bulk-drop-input" label="Bulk Drop Documents" hint="Drop CIPC, CSD, SARS, B-BBEE, COIDA, CIDB, PSIRA, IDs… — we classify each file automatically" onFiles={(f) => send(f)} />
        {msg && <p className="mt-2 text-sm text-muted" data-testid="vault-message">{msg}</p>}
      </div>

      {data?.unclassified.length ? (
        <Card className="mt-4 border-warn/40">
          <CardHeader><CardTitle>Classifying & awaiting a slot</CardTitle></CardHeader>
          <CardContent className="space-y-2 text-sm">
            {data.unclassified.map((d) => <UnclassifiedRow key={d.id} doc={d} onDone={load} />)}
          </CardContent>
        </Card>
      ) : null}

      <div className="mt-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {slots.map(([cat, docs]) => <Slot key={cat} cat={cat} docs={docs} onUpload={(f) => send(f, cat)} />)}
      </div>
    </div>
  );
}

function UnclassifiedRow({ doc, onDone }: { doc: VaultDoc; onDone: () => void }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <FileText className="size-4" /> <span className="font-medium">{doc.original_filename}</span>
      {["QUEUED", "PROCESSING"].includes(doc.processing_state) ? (
        <Badge tone="info"><Loader2 className="size-3 animate-spin" /> scanning & classifying</Badge>
      ) : (
      <select className="rounded border border-line px-2 py-1 text-sm" defaultValue=""
        onChange={async (e) => { await api(`/api/vault/${doc.id}/category`, { method: "POST", json: { doc_category: e.target.value } }); onDone(); }}>
        <option value="" disabled>Choose document type…</option>
        {Object.entries(CATEGORY_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
      </select>
      )}
    </div>
  );
}

function Slot({ cat, docs, onUpload }: { cat: string; docs: VaultDoc[]; onUpload: (f: File[]) => Promise<void> }) {
  const current = docs.filter((d) => d.is_current);
  const latest = current[0];
  const status = latest ? (["QUEUED", "PROCESSING"].includes(latest.processing_state) ? "PROCESSING" : latest.validation_status) : "MISSING";
  const days = daysUntil(latest?.expiry_date);
  return (
    <Card data-testid={`slot-${cat}`}>
      <CardHeader>
        <div>
          <CardTitle>{CATEGORY_LABELS[cat]}</CardTitle>
          <div className="mt-1 flex flex-wrap gap-1">
            <Badge tone={statusTone(status)} data-testid={`slot-status-${cat}`}>
              {status === "PROCESSING" ? <Loader2 className="size-3 animate-spin" /> : status === "VALID" ? <CheckCircle2 className="size-3" /> : null}
              {status?.replaceAll("_", " ")}
            </Badge>
            {latest?.ocr_confidence_score != null && (
              <Badge tone={latest.ocr_confidence_score >= 0.85 ? "muted" : "warn"}>OCR {(latest.ocr_confidence_score * 100).toFixed(0)}%</Badge>
            )}
            {latest?.expiry_date && <Badge tone={days! < 0 ? "bad" : days! < 30 ? "warn" : "muted"}>expires {fmtDate(latest.expiry_date)}</Badge>}
            {cat === "DIRECTOR_ID" && current.length > 1 && <Badge tone="info">{current.length} IDs</Badge>}
          </div>
        </div>
        <UploadButton size="sm" variant={latest ? "outline" : "default"} multiple={cat === "DIRECTOR_ID"} onFiles={onUpload} testId={`slot-input-${cat}`}>
          {latest ? "Replace / Upload" : "Upload"}
        </UploadButton>
      </CardHeader>
      <CardContent className="space-y-2 text-xs">
        {current.map((d) => (
          <div key={d.id} className="rounded border border-line bg-panel/60 p-2">
            <div className="flex items-center justify-between gap-2">
              <a href={d.url ?? "#"} target="_blank" className="truncate font-medium text-brand underline-offset-2 hover:underline">{d.original_filename}</a>
              <span className="shrink-0 text-muted">{fmtDate(d.uploaded_at)}</span>
            </div>
            {Object.entries(d.extracted_json).filter(([k, v]) => ["cipc_reg_number", "csd_maaa_number", "tcs_pin", "tier", "claimed_level", "grades", "account_number_masked", "full_name", "category"].includes(k) && v)
              .map(([k, v]) => <div key={k} className="text-muted">{k.replaceAll("_", " ")}: <span className="text-slate-800">{Array.isArray(v) ? v.join(", ") : String(v)}</span></div>)}
            {d.validation_errors.map((e, i) => (
              <div key={i} className={`mt-1 flex gap-1 ${e.severity === "BLOCKER" ? "text-bad" : "text-warn"}`}>
                <AlertTriangle className="mt-0.5 size-3 shrink-0" />
                <div><b>{e.code}</b> — {e.message}{e.remediation && <div className="text-slate-600">{e.remediation}</div>}</div>
              </div>
            ))}
          </div>
        ))}
        {!latest && <p className="text-muted">No document yet.</p>}
      </CardContent>
    </Card>
  );
}
