"use client";

const TOKEN_KEY = "ts_token";

export function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function setToken(t: string | null) {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* storage unavailable */ }
}

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "string" ? detail : (detail as { message?: string })?.message || `HTTP ${status}`);
  }
}

export async function api<T = unknown>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  const res = await fetch(path, { ...init, headers, body });
  if (res.status === 401 && typeof window !== "undefined" && !path.startsWith("/api/auth")) {
    setToken(null);
    window.location.href = "/login";
  }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) throw new ApiError(res.status, (data as { detail?: unknown })?.detail ?? data);
  return data as T;
}

export function upload<T = unknown>(path: string, form: FormData) {
  return api<T>(path, { method: "POST", body: form });
}

export function wsUrl(path: string) {
  const base = process.env.NEXT_PUBLIC_WS_ORIGIN || `${location.protocol === "https:" ? "wss" : "ws"}://${location.hostname}:8000`;
  return `${base}${path}`;
}
