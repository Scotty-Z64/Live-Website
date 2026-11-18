"use client";
import { useState } from "react";
import { ExternalLink } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import type { Addendum, Bid, Tender } from "@/lib/types";
import { fmtDate } from "@/lib/utils";

export function BriefingDialog({ open, onOpenChange, tender, bid, onDone }: {
  open: boolean; onOpenChange: (o: boolean) => void; tender: Tender; bid: Bid; onDone: (b: Bid) => void;
}) {
  const [rep, setRep] = useState({ full_name: "", id_number: "", position: "", phone: "", email: "" });
  const [sig, setSig] = useState({ full_name: "", capacity: "Director" });
  const [result, setResult] = useState<{ letter_url: string; registration_url: string | null } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  async function prepare(e: React.FormEvent) {
    e.preventDefault(); setErr(null);
    // Open the registration tab synchronously (inside the click) so pop-up blockers allow it.
    const regTab = tender.briefing_registration_url ? window.open("about:blank", "_blank") : null;
    try {
      const r = await api<{ letter_url: string; registration_url: string | null; bid: Bid }>(`/api/bids/${bid.id}/briefing/prepare`, {
        method: "POST", json: { representative: rep, signatory: sig } });
      setResult(r); onDone(r.bid);
      if (regTab && r.registration_url) regTab.location.href = r.registration_url; else regTab?.close();
    } catch (e) { regTab?.close(); setErr((e as Error).message); }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) setResult(null); }}>
      <DialogContent title="Prepare Briefing Pack" description="TenderSync never registers for briefings on your behalf — a named representative must attend and sign the register.">
        <div className="mb-4 rounded-md border border-warn/30 bg-warn/5 p-3 text-sm">
          <div><b>When:</b> {fmtDate(tender.briefing_timestamp, true)}</div>
          <div><b>Where:</b> {tender.briefing_venue_or_link ?? "See tender notice"}</div>
          {tender.briefing_registration_url && <div className="truncate"><b>Register:</b> <a className="text-brand underline" target="_blank" href={tender.briefing_registration_url}>{tender.briefing_registration_url}</a></div>}
        </div>
        {result ? (
          <div className="space-y-3 text-sm" data-testid="briefing-result">
            <p>Your pre-filled <b>Letter of Authority / Delegation</b> is ready. Print it on letterhead, sign it, and give it to your representative.</p>
            <div className="flex flex-wrap gap-2">
              <Button asChild><a href={result.letter_url} target="_blank">Download Letter of Authority</a></Button>
              {result.registration_url && <Button variant="outline" asChild><a href={result.registration_url} target="_blank"><ExternalLink /> Open registration page</a></Button>}
            </div>
            <p className="text-muted">The bid stays locked in <b>REGISTERED_AWAITING_CERT</b> until you upload the signed/stamped Certificate of Attendance.</p>
          </div>
        ) : (
          <form onSubmit={prepare} className="grid gap-3 sm:grid-cols-2">
            <div className="text-xs font-semibold sm:col-span-2">Attending representative</div>
            <Field label="Full name *"><Input name="rep_name" required value={rep.full_name} onChange={(e) => setRep({ ...rep, full_name: e.target.value })} /></Field>
            <Field label="ID / passport number *"><Input name="rep_id" required value={rep.id_number} onChange={(e) => setRep({ ...rep, id_number: e.target.value })} /></Field>
            <Field label="Position"><Input name="rep_position" value={rep.position} onChange={(e) => setRep({ ...rep, position: e.target.value })} /></Field>
            <Field label="Cellphone"><Input name="rep_phone" value={rep.phone} onChange={(e) => setRep({ ...rep, phone: e.target.value })} /></Field>
            <div className="text-xs font-semibold sm:col-span-2">Authorising signatory</div>
            <Field label="Full name *"><Input name="sig_name" required value={sig.full_name} onChange={(e) => setSig({ ...sig, full_name: e.target.value })} /></Field>
            <Field label="Capacity"><Input name="sig_capacity" value={sig.capacity} onChange={(e) => setSig({ ...sig, capacity: e.target.value })} /></Field>
            {err && <p className="text-sm text-bad sm:col-span-2">{err}</p>}
            <div className="sm:col-span-2"><Button className="w-full" variant="accent">Generate Letter of Authority & open registration</Button></div>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function AddendumDialog({ addendum, bid, onClose, onDone }: { addendum: Addendum | null; bid: Bid; onClose: () => void; onDone: () => void }) {
  const [signer, setSigner] = useState("");
  const [busy, setBusy] = useState(false);
  if (!addendum) return null;
  const changed = addendum.diff.changed_fields;
  return (
    <Dialog open={!!addendum} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title="Silent addendum detected" description={`${addendum.filename} · detected ${fmtDate(addendum.detected_at, true)}`} className="max-w-2xl">
        <div className="space-y-3 text-sm">
          <div className="rounded border border-line bg-panel p-2 font-mono text-[11px]">
            {addendum.kind === "CHANGED_FILE" ? `SHA-256 ${addendum.old_sha256?.slice(0, 16)}… → ${addendum.new_sha256.slice(0, 16)}…` : `New file · SHA-256 ${addendum.new_sha256.slice(0, 16)}…`}
            <div className="truncate">{addendum.url}</div>
          </div>
          {Object.keys(changed).length ? (
            <div>
              <div className="font-semibold">Rules that changed</div>
              <ul className="mt-1 space-y-1">
                {Object.entries(changed).map(([k, v]) => (
                  <li key={k} className="rounded border border-bad/30 bg-bad/5 p-2"><b>{k}</b>: <code className="text-xs">{JSON.stringify(v).slice(0, 400)}</code></li>
                ))}
              </ul>
            </div>
          ) : <p className="text-muted">No rule-level changes detected; review the text diff below.</p>}
          <div className="grid gap-2 sm:grid-cols-2">
            <div><div className="text-xs font-semibold text-ok">+ {addendum.diff.lines_added} lines</div>{addendum.diff.sample_added.map((l, i) => <div key={i} className="truncate text-xs">{l}</div>)}</div>
            <div><div className="text-xs font-semibold text-bad">− {addendum.diff.lines_removed} lines</div>{addendum.diff.sample_removed.map((l, i) => <div key={i} className="truncate text-xs">{l}</div>)}</div>
          </div>
          <form className="flex flex-wrap items-end gap-2 border-t border-line pt-3" onSubmit={async (e) => {
            e.preventDefault(); setBusy(true);
            try { await api(`/api/bids/${bid.id}/addenda/${addendum.id}/acknowledge`, { method: "POST", json: { signed_by: signer } }); onDone(); onClose(); }
            finally { setBusy(false); }
          }}>
            <div className="flex-1"><Field label="Signed off by (full name) *"><Input required value={signer} onChange={(e) => setSigner(e.target.value)} name="signer" /></Field></div>
            <Button disabled={busy}>{busy ? "Re-running pre-flight…" : "I have reviewed — sign off"}</Button>
          </form>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function SubmitDialog({ open, onOpenChange, bid, tender }: { open: boolean; onOpenChange: (o: boolean) => void; bid: Bid; tender: Tender }) {
  const [creds, setCreds] = useState({ username: "", password: "" });
  const [otp, setOtp] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [queued, setQueued] = useState(false);
  const digital = bid.submission_receipt?.digital;
  const status = bid.status;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title="Submit Digitally to Portal" description={tender.portal_url ?? undefined}>
        {status === "SUBMITTED" && digital?.portal_reference ? (
          <div className="space-y-2 text-sm" data-testid="submission-receipt">
            <div className="rounded-md border border-ok/30 bg-ok/5 p-3"><b>Submitted.</b> Portal reference <b className="font-mono">{digital.portal_reference}</b></div>
            <div>Submitted at {fmtDate(digital.submitted_at, true)}</div>
            <div className="break-all font-mono text-[11px]">payload SHA-256 {digital.payload_sha256}</div>
          </div>
        ) : status === "AWAITING_OTP" ? (
          <form className="space-y-3 text-sm" onSubmit={async (e) => {
            e.preventDefault(); setErr(null);
            try { await api(`/api/bids/${bid.id}/submit/otp`, { method: "POST", json: { otp } }); } catch (e) { setErr((e as Error).message); }
          }}>
            <p>All schedules are attached ({(digital?.attached ?? []).join(", ")}). The portal is waiting for the one-time PIN sent to your registered cellphone/e-mail.</p>
            <Field label="OTP / 2FA code"><Input name="otp" autoFocus value={otp} onChange={(e) => setOtp(e.target.value)} /></Field>
            {err && <p className="text-bad">{err}</p>}
            <Button className="w-full">Confirm submission</Button>
          </form>
        ) : (
          <form className="space-y-3 text-sm" onSubmit={async (e) => {
            e.preventDefault(); setErr(null);
            try { await api(`/api/bids/${bid.id}/submit`, { method: "POST", json: creds }); setQueued(true); } catch (e) { setErr((e as Error).message); }
          }}>
            <p className="text-muted">Your portal credentials are encrypted for this run only and are never stored. The worker uploads each schedule and pauses at the OTP step for you.</p>
            <Field label="Portal username"><Input name="portal_user" required value={creds.username} onChange={(e) => setCreds({ ...creds, username: e.target.value })} /></Field>
            <Field label="Portal password"><Input name="portal_pass" type="password" required value={creds.password} onChange={(e) => setCreds({ ...creds, password: e.target.value })} /></Field>
            {err && <p className="text-bad" data-testid="submit-error">{err}</p>}
            {status === "SUBMISSION_FAILED" && <p className="text-bad">Last attempt failed: {bid.submission_receipt?.digital_error}</p>}
            <Button className="w-full" disabled={queued && !["SUBMISSION_FAILED"].includes(status)}>
              {queued || ["SUBMISSION_QUEUED", "SUBMITTING"].includes(status) ? "Uploading schedules…" : "Start digital submission"}
            </Button>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
