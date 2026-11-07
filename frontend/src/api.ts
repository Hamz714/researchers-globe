import type { AssetDetail, EvalReport, Preset, QueryResult } from "./types";

const BASE = "/api"; // proxied to FastAPI by vite.config.ts

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(BASE + path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!r.ok) {
    const body = await r.text();
    throw new Error(`${r.status} ${r.statusText}: ${body.slice(0, 200)}`);
  }
  return (await r.json()) as T;
}

export const api = {
  query: (text: string) => call<QueryResult>("/query", { method: "POST", body: JSON.stringify({ text }) }),
  presets: () => call<Preset[]>("/presets"),
  asset: (id: string) => call<AssetDetail>(`/asset/${encodeURIComponent(id)}`),
  evalReport: () => call<EvalReport>("/eval"),
};
