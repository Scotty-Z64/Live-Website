"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/input";
import { api, setToken } from "@/lib/api";

export default function Login() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setErr(null);
    try {
      const r = await api<{ token: string }>("/api/auth/login", { method: "POST", json: { email, password } });
      setToken(r.token); router.push("/tenders");
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }
  return (
    <div className="flex min-h-screen items-center justify-center bg-brand px-4">
      <Card className="w-full max-w-sm p-6">
        <h1 className="text-xl font-bold">Sign in to TenderSync SA</h1>
        <form onSubmit={submit} className="mt-4 space-y-3">
          <Field label="E-mail"><Input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
          <Field label="Password"><Input type="password" required value={password} onChange={(e) => setPassword(e.target.value)} /></Field>
          {err && <p className="text-sm text-bad">{err}</p>}
          <Button className="w-full" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</Button>
        </form>
        <p className="mt-4 text-sm text-muted">New supplier? <Link className="text-brand underline" href="/onboarding">Create your company workspace</Link></p>
      </Card>
    </div>
  );
}
