"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { FileStack, Gavel, LogOut, Package, ShieldCheck } from "lucide-react";
import { api, getToken, setToken } from "@/lib/api";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/vault", label: "Compliance Vault", icon: ShieldCheck },
  { href: "/tenders", label: "Tender Feed", icon: Gavel },
  { href: "/catalog", label: "Product Catalog", icon: Package },
];
const PUBLIC = ["/login", "/onboarding"];

export function AppShell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const [tenant, setTenant] = useState<{ legal_name: string; bee_level: number | null } | null>(null);
  const isPublic = PUBLIC.some((p) => path.startsWith(p));

  useEffect(() => {
    if (isPublic) return;
    if (!getToken()) { router.replace("/login"); return; }
    api<{ legal_name: string; bee_level: number | null }>("/api/tenant").then(setTenant).catch(() => {});
  }, [isPublic, path, router]);

  if (isPublic) return <>{children}</>;
  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <aside className="flex shrink-0 flex-row items-center gap-2 bg-brand px-4 py-3 text-white md:w-60 md:flex-col md:items-stretch md:py-5">
        <Link href="/tenders" className="flex items-center gap-2 font-bold tracking-tight md:mb-6 md:px-2">
          <FileStack className="size-5" /> TenderSync <span className="text-orange-300">SA</span>
        </Link>
        <nav className="flex flex-1 gap-1 overflow-x-auto md:flex-col">
          {NAV.map(({ href, label, icon: Icon }) => (
            <Link key={href} href={href}
              className={cn("flex items-center gap-2 whitespace-nowrap rounded-md px-3 py-2 text-sm text-white/80 hover:bg-white/10",
                path.startsWith(href) && "bg-white/15 font-medium text-white")}>
              <Icon className="size-4" /><span className="hidden sm:inline">{label}</span>
            </Link>
          ))}
        </nav>
        <div className="hidden border-t border-white/15 pt-3 text-xs text-white/70 md:block">
          <div className="truncate font-medium text-white" data-testid="tenant-name">{tenant?.legal_name ?? "…"}</div>
          {tenant?.bee_level ? <div>B-BBEE Level {tenant.bee_level}</div> : null}
          <button className="mt-2 flex items-center gap-1 hover:text-white" onClick={() => { setToken(null); router.push("/login"); }}>
            <LogOut className="size-3" /> Sign out
          </button>
        </div>
      </aside>
      <main className="min-w-0 flex-1 bg-panel/40">{children}</main>
    </div>
  );
}
