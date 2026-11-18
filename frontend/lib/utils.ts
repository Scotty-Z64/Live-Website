import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export const zar = (n: number | null | undefined) =>
  n == null ? "—" : new Intl.NumberFormat("en-ZA", { style: "currency", currency: "ZAR" }).format(n);

export const fmtDate = (s: string | null | undefined, withTime = false) => {
  if (!s) return "—";
  const d = new Date(s);
  // South African procurement deadlines are always expressed in SAST, whatever the viewer's zone.
  return d.toLocaleString("en-ZA", withTime
    ? { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Africa/Johannesburg" }
    : { day: "2-digit", month: "short", year: "numeric", timeZone: "Africa/Johannesburg" }) + (withTime ? " SAST" : "");
};

export const daysUntil = (s: string | null | undefined) =>
  s ? Math.ceil((new Date(s).getTime() - Date.now()) / 86_400_000) : null;
