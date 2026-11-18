"use client";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { UploadButton } from "@/components/upload";
import { api, upload } from "@/lib/api";
import { zar } from "@/lib/utils";

type Item = { id: string; sku_code: string; item_name: string; technical_specs: string; unit_of_measure: string; unit_price_excl_vat: number; lead_time_days: number | null; local_content_pct: number | null };

export default function CatalogPage() {
  const [items, setItems] = useState<Item[]>([]);
  const [provider, setProvider] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const load = () => api<{ items: Item[]; embedding_provider: string }>("/api/catalog").then((r) => { setItems(r.items); setProvider(r.embedding_provider); });
  useEffect(() => { load(); }, []);
  return (
    <div className="mx-auto max-w-6xl p-4 md:p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Product & Rate Catalog</h1>
          <p className="text-sm text-muted">Indexed as 1536-dim vectors for automatic BoQ matching. <Badge tone="muted">{provider}</Badge></p>
        </div>
        <UploadButton accept=".csv" testId="catalog-input" onFiles={async (f) => {
          const fd = new FormData(); fd.append("file", f[0]);
          const r = await upload<{ imported: number; errors: string[] }>("/api/catalog/import", fd);
          setMsg(`${r.imported} items imported${r.errors.length ? `, ${r.errors.length} rows skipped: ${r.errors.join("; ")}` : ""}`); load();
        }}>Import CSV</UploadButton>
      </div>
      <p className="mt-2 text-xs text-muted">CSV columns: sku_code, item_name, technical_specs, unit_of_measure, unit_price_excl_vat, lead_time_days, local_content_pct</p>
      {msg && <p className="mt-2 text-sm" data-testid="catalog-msg">{msg}</p>}
      <Card className="mt-4 overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-panel text-left text-xs text-muted"><tr>{["SKU", "Item", "Unit", "Price excl. VAT", "Lead time", "Local content"].map((h) => <th key={h} className="p-2">{h}</th>)}</tr></thead>
          <tbody>
            {items.map((i) => (
              <tr key={i.id} className="border-t border-line">
                <td className="p-2 font-mono text-xs">{i.sku_code}</td><td className="p-2">{i.item_name}<div className="text-xs text-muted">{i.technical_specs}</div></td>
                <td className="p-2">{i.unit_of_measure}</td><td className="p-2 text-right">{zar(i.unit_price_excl_vat)}</td>
                <td className="p-2">{i.lead_time_days ?? "—"} d</td><td className="p-2">{i.local_content_pct ?? "—"}%</td>
              </tr>
            ))}
            {!items.length && <tr><td colSpan={6} className="p-6 text-center text-muted">No catalog items yet.</td></tr>}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
