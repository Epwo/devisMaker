import type { Extraction, Pricing, Quote } from "./types";

async function check<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Erreur serveur (${r.status})`);
  }
  return r.json();
}

export const api = {
  health: () => fetch("/health").then((r) => check<{ status: string; vlm_available: boolean }>(r)),
  pricing: () => fetch("/pricing").then((r) => check<Pricing>(r)),
  samples: () => fetch("/samples").then((r) => check<string[]>(r)).catch(() => [] as string[]),
  sampleFile: async (name: string) => {
    const blob = await fetch(`/samples/${encodeURIComponent(name)}`).then((r) => r.blob());
    return new File([blob], name, { type: blob.type });
  },

  extract: (file: File, useAi: boolean, signal?: AbortSignal) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("use_vlm", String(useAi));
    fd.append("preview", "true");
    fd.append("include_ocr", "true");
    return fetch("/extract", { method: "POST", body: fd, signal }).then((r) => check<Extraction>(r));
  },

  quote: (values: Record<string, unknown>, quantity: number, overrides: Pricing, confidences: Record<string, number>) =>
    fetch("/quote/values", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values, quantity, pricing_overrides: overrides, confidences }),
    }).then((r) => check<Quote>(r)),

  /** Journal d'apprentissage : ce que l'utilisateur a fait d'une valeur proposée. Échec silencieux. */
  feedback: (body: { analysis_id: number; field: string; value: unknown; action: "confirmation" | "saisie" | "choix_sur_plan"; detail?: Record<string, unknown> }) =>
    fetch("/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), keepalive: true })
      .catch(() => undefined),

  /** Enregistre une nouvelle version des paramètres atelier (base de données). */
  savePricing: (params: Pricing, comment?: string) =>
    fetch("/pricing", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ params, comment }) })
      .then((r) => check<{ version: number }>(r)),

  pdf: async (ext: Extraction, quantity: number, client: string, overrides: Pricing) => {
    const r = await fetch("/quote/document", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ extraction: ext, quantity, client: client || null, pricing_overrides: overrides }),
    });
    if (!r.ok) await check(r);
    return r.blob();
  },
};
