"use client";
import { useEffect, useMemo, useState } from "react";
import { FileCheck2, Quote } from "lucide-react";
import { Badge, statusTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import type { Bid, Citation, PricingLine, Tender } from "@/lib/types";
import { fmtDate, zar } from "@/lib/utils";

function Cite({ c }: { c?: Citation }) {
  if (!c) return null;
  return (
    <span className="ml-1 inline-flex cursor-help items-center text-muted" title={`${c.file}${c.page ? ` p.${c.page}` : ""}: “${c.snippet}”`}>
      <Quote className="size-3" />
    </span>
  );
}

export function PreflightPanel({ tender }: { tender: Tender }) {
  const f = tender.preflight?.fields ?? {};
  const cites = tender.preflight?.citations ?? {};
  const zones = tender.preflight?.zones ?? {};
  const rows: [string, React.ReactNode, string?][] = [
    ["Tender reference", tender.tender_reference, "tender_reference"],
    ["Closing", fmtDate(tender.closing_timestamp, true), "closing_timestamp"],
    ["Submission mode", tender.submission_mode?.replace("_", " ")],
    ["Preference system", tender.preference_system?.replace("_", "/"), "preference_system"],
    ["Minimum functionality", tender.functionality_threshold_pct != null ? `${tender.functionality_threshold_pct}%` : "—", "functionality_threshold_pct"],
    ["Local content threshold", f.local_content_threshold_pct != null ? `${f.local_content_threshold_pct}%` : "—", "local_content_threshold_pct"],
    ["CIDB minimum", f.cidb_min_grade ? `${f.cidb_min_grade.grade}${f.cidb_min_grade.class}` : "—", "cidb_min_grade"],
    ["Bid validity", f.validity_days ? `${f.validity_days} days` : "—", "validity_days"],
    ["Portal", tender.portal_url ? <a className="text-brand underline" href={tender.portal_url} target="_blank">{tender.portal_url}</a> : "—", "portal_url"],
    ["Tender box", tender.physical_tender_box_address ?? "—", "physical_tender_box_address"],
    ["Initial every page", f.initial_every_page ? "Yes" : "Not stated", "initial_every_page"],
  ];
  return (
    <div className="space-y-4" data-testid="preflight-panel">
      {!tender.preflight_done && <Card className="p-4 text-sm text-muted">Pre-flight has not run yet — the tender pack is still downloading or must be uploaded.</Card>}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Disqualifiers & rules</CardTitle>{tender.preflight?.llm_used && <Badge tone="info">LLM-verified quotes</Badge>}</CardHeader>
          <CardContent>
            <dl className="divide-y divide-line text-sm">
              {rows.map(([k, v, c]) => (
                <div key={k} className="flex justify-between gap-3 py-1.5"><dt className="text-muted">{k}</dt><dd className="text-right font-medium">{v}{c && <Cite c={cites[c]} />}</dd></div>
              ))}
            </dl>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Four-zone segmentation</CardTitle></CardHeader>
          <CardContent className="space-y-2 text-sm">
            {Object.entries(zones).map(([z, info]) => (
              <div key={z} className="rounded border border-line p-2">
                <div className="flex justify-between"><b>{z === "C1C2" ? "Part C1/C2" : `Part ${z}`}</b><Badge tone={info.spans.length ? "ok" : "warn"}>{info.spans.length ? "located" : "not found"}</Badge></div>
                <div className="text-xs text-muted">{info.label}</div>
                {info.spans.map((s, i) => <div key={i} className="text-xs">{s.file} · pp. {s.page_from}–{s.page_to}</div>)}
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Mandatory returnable schedules (T2)</CardTitle></CardHeader>
          <CardContent><ul className="space-y-1 text-sm">{tender.mandatory_schedules.map((s) => <li key={s.name} className="flex items-center gap-1"><FileCheck2 className="size-3 text-ok" />{s.name}<Cite c={s.citation} /></li>)}</ul></CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Sector plugins activated</CardTitle></CardHeader>
          <CardContent className="space-y-1 text-sm">
            {(f.sector_plugins ?? []).map((p: { plugin: string; standards?: string[]; citation: Citation }) => (
              <div key={p.plugin}><Badge tone="info">{p.plugin}</Badge> {p.standards?.join(", ")}<Cite c={p.citation} /></div>
            ))}
            {!(f.sector_plugins ?? []).length && <p className="text-muted">None</p>}
          </CardContent>
        </Card>
      </div>
      <Card>
        <CardHeader><CardTitle>Tender pack files</CardTitle></CardHeader>
        <CardContent className="text-sm">
          {(f.file_manifest ?? []).map((m: { name: string; kind: string; pages: number }) => (
            <div key={m.name} className="flex justify-between border-b border-line py-1 last:border-0"><span className="truncate">{m.name}</span><Badge tone={m.kind === "ADDENDUM" ? "warn" : "muted"}>{m.kind}</Badge></div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}

type Row = { doc_category: string; label: string; sector_plugin: boolean; status: string; expiry_date: string | null; errors: { code: string; message: string }[] };

export function CompliancePanel({ bidId, bid }: { bidId: string; bid: Bid }) {
  const [rows, setRows] = useState<Row[]>([]);
  useEffect(() => { api<{ rows: Row[] }>(`/api/bids/${bidId}/compliance`).then((r) => setRows(r.rows)); }, [bidId, bid.blocking_issues]);
  return (
    <Card className="overflow-x-auto" data-testid="compliance-matrix">
      <table className="w-full text-sm">
        <thead className="bg-panel text-left text-xs text-muted"><tr><th className="p-2">Requirement</th><th className="p-2">Status</th><th className="p-2">Expiry</th><th className="p-2">Detail</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.doc_category} className="border-t border-line">
              <td className="p-2">{r.label}{r.sector_plugin && <Badge tone="info" className="ml-1">sector</Badge>}</td>
              <td className="p-2"><Badge tone={statusTone(r.status)}>{r.status.replaceAll("_", " ")}</Badge></td>
              <td className="p-2">{fmtDate(r.expiry_date)}</td>
              <td className="p-2 text-xs text-muted">{r.errors.map((e) => e.message).join("; ")}</td>
            </tr>
          ))}
          <tr className="border-t border-line">
            <td className="p-2">Compulsory briefing attendance</td>
            <td className="p-2"><Badge tone={statusTone(bid.briefing_gate_status)}>{bid.briefing_gate_status.replaceAll("_", " ")}</Badge></td><td /><td />
          </tr>
        </tbody>
      </table>
    </Card>
  );
}

const money = (n: number) => Math.round(n * 100) / 100;

export function PricingPanel({ bid, tender, onSaved }: { bid: Bid; tender: Tender; onSaved: (b: Bid) => void }) {
  const p = bid.pricing_summary;
  const [margin, setMargin] = useState<number>(p.margin_pct ?? 15);
  const [overrides, setOverrides] = useState<Record<string, { unit_cost?: number; catalog_item_id?: string }>>(p.overrides ?? {});
  const [busy, setBusy] = useState(false);
  const lines: PricingLine[] = p.lines ?? tender.boq_line_items.map((l) => ({ ...l, unit_cost: null, unit_price: null, line_total: null, local_content_pct: null, match: null, candidates: [] }));

  const computed = useMemo(() => {
    let sub = 0;
    const rows = lines.map((l) => {
      const ov = overrides[l.line_no] ?? {};
      const cand = ov.catalog_item_id ? l.candidates.find((c) => c.catalog_item_id === ov.catalog_item_id) : null;
      const cost = ov.unit_cost ?? cand?.unit_price_excl_vat ?? l.unit_cost;
      const price = cost == null ? null : money(cost * (1 + margin / 100));
      const total = price == null ? null : money(price * l.quantity);
      if (total != null) sub += total;
      return { ...l, cost, price, total };
    });
    const vat = money(sub * 0.15);
    return { rows, sub: money(sub), vat, total: money(sub + vat), unpriced: rows.filter((r) => r.total == null).length };
  }, [lines, overrides, margin]);

  async function save() {
    setBusy(true);
    try {
      const r = await api<{ bid: Bid }>(`/api/bids/${bid.id}/pricing`, { method: "POST", json: { margin_pct: margin, overrides } });
      onSaved(r.bid);
    } finally { setBusy(false); }
  }

  if (!tender.boq_line_items.length) return <Card className="p-6 text-sm text-muted">No Bill of Quantities was found in the tender pack.</Card>;
  return (
    <div className="space-y-3" data-testid="pricing-grid">
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm">Margin %<Input type="number" className="w-24" value={margin} onChange={(e) => setMargin(Number(e.target.value))} data-testid="margin-input" /></label>
        <Button onClick={save} disabled={busy} data-testid="match-price">{busy ? "Matching…" : p.lines ? "Save pricing" : "Match catalog & price BoQ"}</Button>
        <span className="text-xs text-muted">Catalog matches use pgvector cosine similarity. Rates are excl. VAT.</span>
      </div>
      <Card className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-sm">
          <thead className="bg-panel text-left text-xs text-muted"><tr><th className="p-2">#</th><th className="p-2">BoQ description</th><th className="p-2">Qty</th><th className="p-2">Catalog match</th><th className="p-2 text-right">Cost</th><th className="p-2 text-right">Rate</th><th className="p-2 text-right">Amount</th></tr></thead>
          <tbody>
            {computed.rows.map((r) => (
              <tr key={r.line_no} className="border-t border-line align-top">
                <td className="p-2">{r.line_no}</td>
                <td className="p-2">{r.description}</td>
                <td className="p-2 whitespace-nowrap">{r.quantity} {r.unit}</td>
                <td className="p-2">
                  {r.candidates.length ? (
                    <select className="w-full rounded border border-line px-1 py-0.5 text-xs" value={overrides[r.line_no]?.catalog_item_id ?? r.match?.catalog_item_id ?? ""}
                      onChange={(e) => setOverrides({ ...overrides, [r.line_no]: { catalog_item_id: e.target.value } })}>
                      {!r.match && <option value="">— no confident match —</option>}
                      {r.candidates.map((c) => <option key={c.catalog_item_id} value={c.catalog_item_id}>{c.sku_code} · {c.item_name} ({Math.round(c.similarity * 100)}%)</option>)}
                    </select>
                  ) : <span className="text-xs text-muted">{p.lines ? "no catalog items" : "not matched yet"}</span>}
                </td>
                <td className="p-2 text-right">
                  <Input className="h-7 w-24 text-right text-xs" inputMode="decimal" value={overrides[r.line_no]?.unit_cost ?? r.cost ?? ""}
                    onChange={(e) => setOverrides({ ...overrides, [r.line_no]: { ...overrides[r.line_no], unit_cost: e.target.value === "" ? undefined : Number(e.target.value) } })} />
                </td>
                <td className="p-2 text-right">{r.price == null ? "—" : zar(r.price)}</td>
                <td className="p-2 text-right font-medium">{r.total == null ? <span className="text-bad">unpriced</span> : zar(r.total)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot className="border-t-2 border-line text-sm">
            <tr><td colSpan={6} className="p-2 text-right">Sub-total excl. VAT</td><td className="p-2 text-right" data-testid="subtotal">{zar(computed.sub)}</td></tr>
            <tr><td colSpan={6} className="p-2 text-right">VAT 15%</td><td className="p-2 text-right">{zar(computed.vat)}</td></tr>
            <tr><td colSpan={6} className="p-2 text-right font-bold">Total incl. VAT</td><td className="p-2 text-right font-bold" data-testid="total-incl">{zar(computed.total)}</td></tr>
          </tfoot>
        </table>
      </Card>
      {p.local_content_weighted_pct != null && <p className="text-xs text-muted">Weighted local content of priced lines: {p.local_content_weighted_pct}%</p>}
    </div>
  );
}

export function SbdPanel({ bid, onAutofill }: { bid: Bid; onAutofill: () => Promise<void> }) {
  const forms = Object.entries(bid.sbd_outputs);
  const [sel, setSel] = useState<string | null>(forms[0]?.[0] ?? null);
  useEffect(() => { if (!sel && forms.length) setSel(forms[0][0]); }, [forms, sel]);
  if (!forms.length) {
    return <Card className="flex flex-col items-center gap-3 p-8 text-sm text-muted">SBD forms have not been filled yet.<Button onClick={onAutofill}>Auto-Fill All SBDs</Button></Card>;
  }
  const cur = sel ? bid.sbd_outputs[sel] : null;
  return (
    <div className="grid gap-3 lg:grid-cols-[220px_1fr]" data-testid="sbd-preview">
      <div className="space-y-2">
        {forms.map(([f, o]) => (
          <button key={f} onClick={() => setSel(f)} className={`w-full rounded border p-2 text-left text-sm ${sel === f ? "border-brand bg-brand/5" : "border-line bg-white"}`}>
            <div className="flex items-center justify-between font-semibold">{f}<Badge tone={o.method === "acroform" ? "ok" : o.method === "overlay" ? "info" : "bad"}>{o.method}</Badge></div>
            <div className="text-xs text-muted">{o.filled.length} fields filled · {o.missing_values.length} left for wet ink</div>
            <div className="truncate text-[11px] text-muted">{o.source}</div>
          </button>
        ))}
        <p className="text-[11px] text-muted">Only verified vault/profile values are injected. YES/NO declarations and signatures are never auto-answered.</p>
      </div>
      {cur && <iframe title={sel ?? ""} src={cur.url} className="h-[70vh] w-full rounded border border-line bg-white" />}
    </div>
  );
}
