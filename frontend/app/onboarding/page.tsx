"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/input";
import { api, setToken } from "@/lib/api";

const SECTORS = ["electrical", "security", "construction", "plumbing", "ict", "skills development", "general supply"];

export default function Onboarding() {
  const router = useRouter();
  const [f, setF] = useState<Record<string, string>>({});
  const [sectors, setSectors] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });
  const num = (k: string) => (f[k] ? Number(f[k].replace(/[^\d.]/g, "")) : null);

  async function submit(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setErr(null);
    try {
      const r = await api<{ token: string }>("/api/auth/register", { method: "POST", json: {
        legal_name: f.legal_name, trading_name: f.trading_name || null, email: f.email, password: f.password, full_name: f.full_name,
        vat_number: f.vat_number || null, annual_turnover_zar: num("turnover"), black_ownership_pct: num("black"),
        black_women_ownership_pct: num("black_women"), physical_address: f.address || null, contact_phone: f.phone || null, sectors,
      } });
      setToken(r.token); router.push("/vault?welcome=1");
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }
  return (
    <div className="min-h-screen bg-brand px-4 py-10">
      <Card className="mx-auto w-full max-w-2xl p-6">
        <h1 className="text-xl font-bold">Set up your supplier workspace</h1>
        <p className="mt-1 text-sm text-muted">Registration and CSD numbers are verified from your uploaded CIPC and CSD documents — you don’t type them here.</p>
        <form onSubmit={submit} className="mt-5 grid gap-3 sm:grid-cols-2">
          <Field label="Registered company name *"><Input name="legal_name" required onChange={set("legal_name")} /></Field>
          <Field label="Trading name"><Input name="trading_name" onChange={set("trading_name")} /></Field>
          <Field label="Your full name *"><Input name="full_name" required onChange={set("full_name")} /></Field>
          <Field label="Work e-mail *"><Input name="email" type="email" required onChange={set("email")} /></Field>
          <Field label="Password (min 8) *"><Input name="password" type="password" minLength={8} required onChange={set("password")} /></Field>
          <Field label="VAT number"><Input name="vat_number" onChange={set("vat_number")} /></Field>
          <Field label="Annual turnover (ZAR) *"><Input name="turnover" required inputMode="numeric" placeholder="4500000" onChange={set("turnover")} /></Field>
          <Field label="Black ownership % *"><Input name="black" required inputMode="decimal" placeholder="100" onChange={set("black")} /></Field>
          <Field label="Black women ownership %"><Input name="black_women" inputMode="decimal" onChange={set("black_women")} /></Field>
          <Field label="Telephone"><Input name="phone" onChange={set("phone")} /></Field>
          <div className="sm:col-span-2"><Field label="Physical address"><Input name="address" onChange={set("address")} /></Field></div>
          <div className="sm:col-span-2">
            <span className="mb-1 block text-xs font-medium">Industry verticals (filters the tender feed)</span>
            <div className="flex flex-wrap gap-2">
              {SECTORS.map((s) => (
                <button type="button" key={s} onClick={() => setSectors(sectors.includes(s) ? sectors.filter((x) => x !== s) : [...sectors, s])}
                  className={`rounded-full border px-3 py-1 text-xs ${sectors.includes(s) ? "border-brand bg-brand text-white" : "border-line"}`}>{s}</button>
              ))}
            </div>
          </div>
          {err && <p className="text-sm text-bad sm:col-span-2">{err}</p>}
          <div className="flex items-center justify-between sm:col-span-2">
            <Link href="/login" className="text-sm text-muted underline">I already have an account</Link>
            <Button disabled={busy}>{busy ? "Creating…" : "Create workspace"}</Button>
          </div>
        </form>
      </Card>
    </div>
  );
}
