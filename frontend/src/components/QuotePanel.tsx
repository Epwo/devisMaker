import { useState } from "react";
import { api } from "../api";
import { reviewCount, useJobs } from "../jobs";
import type { Job, Pricing } from "../types";
import { downloadQuote } from "./download";

const eur = (x: number) => x.toLocaleString("fr-FR", { style: "currency", currency: "EUR" });

export function QuotePanel({ job }: { job: Job }) {
  const { patch, pricing } = useJobs();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [detail, setDetail] = useState(true);
  const q = job.quote;
  const left = reviewCount(job);
  const margin = job.overrides.margin_rate ?? pricing?.margin_rate ?? 0.25;

  const download = async () => {
    setBusy(true); setErr(null);
    try { await downloadQuote(job); patch(job.id, { pdfDownloaded: true }); }
    catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  };

  return (
    <section className="card quote">
      <div className="sec-head"><h2>Devis</h2></div>

      <div className="row">
        <label className="fld">Quantité
          <input type="number" min={1} max={100000} defaultValue={job.quantity}
            onBlur={(e) => patch(job.id, { quantity: Math.min(100000, Math.max(1, parseInt(e.target.value) || 1)) })}
            onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} />
        </label>
        <label className="fld">Marge
          <div className="inp"><input type="number" min={0} step={1} defaultValue={Math.round(margin * 100)}
            onBlur={(e) => patch(job.id, { overrides: { ...job.overrides, margin_rate: (parseFloat(e.target.value) || 0) / 100 } })} /><span className="unit">%</span></div>
        </label>
        <label className="fld grow">Client
          <input defaultValue={job.client} placeholder="facultatif" onBlur={(e) => patch(job.id, { client: e.target.value.trim() })} />
        </label>
      </div>

      {job.quoteError && <p className="error">Chiffrage impossible : {job.quoteError}</p>}
      {q && (
        <>
          <div className="price">
            <div><span>Prix unitaire HT</span><b>{eur(q.unit_price)}</b></div>
            <div className="total"><span>Total HT · {q.quantity} pcs</span><b>{eur(q.total_price)}</b></div>
          </div>

          {q.warnings.length > 0 && (
            <div className="alert warn"><b>Attention</b><ul>{q.warnings.map((w) => <li key={w}>{w}</li>)}</ul></div>
          )}

          <button className="btn link toggle" onClick={() => setDetail((v) => !v)}>{detail ? "▾" : "▸"} Détail du prix (par pièce)</button>
          {detail && <Breakdown q={q} />}

          {q.assumptions.length > 0 && (
            <div className="alert info"><b>Valeurs supposées</b> (modifiables ci-dessus ou dans les paramètres)
              <ul>{q.assumptions.map((w) => <li key={w}>{w}</li>)}</ul></div>
          )}
        </>
      )}

      <Params job={job} pricing={pricing} />

      <div className="download">
        {left > 0 && <p className="warn-text small">{left} point{left > 1 ? "s" : ""} non vérifié{left > 1 ? "s" : ""} : le devis indiquera les valeurs à confirmer.</p>}
        <button className={`btn big ${left ? "" : "primary"}`} onClick={download} disabled={busy || !q}>
          {busy ? "Génération…" : left ? "Télécharger quand même" : job.pdfDownloaded ? "Télécharger à nouveau" : "⬇ Télécharger le devis PDF"}
        </button>
        {err && <p className="error small">{err}</p>}
      </div>
    </section>
  );
}

function Breakdown({ q }: { q: NonNullable<Job["quote"]> }) {
  const max = Math.max(...q.lines.map((l) => l.unit_cost), 0.01);
  return (
    <div className="lines">
      {q.lines.map((l) => (
        <div key={l.label} className="line">
          <span>{l.label}</span>
          <b>{eur(l.unit_cost)}</b>
          <div className="bar"><i style={{ width: `${(l.unit_cost / max) * 100}%` }} /></div>
          <span className="det">{l.detail}</span>
        </div>
      ))}
      <div className="line sum"><span>Coût de revient</span><b>{eur(q.unit_cost)}</b></div>
      <div className="line sum"><span>Marge {Math.round(q.margin_rate * 100)} %</span><b>{eur(q.unit_price - q.unit_cost)}</b></div>
      <div className="muted small meta">Masse {Math.round(q.mass_kg * 1000)} g · flan {q.blank_mm[0]} × {q.blank_mm[1]} mm · découpe {(q.cut_length_mm / 1000).toFixed(2)} m</div>
    </div>
  );
}

/** Paramètres atelier (taux, prix matière) — par dossier. */
function Params({ job, pricing }: { job: Job; pricing: Pricing | null }) {
  const { patch, refreshPricing } = useJobs();
  const [saved, setSaved] = useState<string | null>(null);
  if (!pricing) return null;
  const o = job.overrides;
  const mat = String(job.ext?.fields.material?.value ?? pricing.defaults.material).toLowerCase();
  const matKey = Object.keys(pricing.materials).find((k) => mat.includes(k)) ?? pricing.defaults.material;
  const items: { label: string; path: string[]; mult?: number }[] = [
    { label: "Découpe laser (€/h)", path: ["laser", "rate_eur_h"] },
    { label: "Pliage (€/h)", path: ["bending", "rate_eur_h"] },
    { label: "Main d'œuvre (€/h)", path: ["labor", "rate_eur_h"] },
    { label: `Prix ${matKey} (€/kg)`, path: ["materials", matKey, "price_eur_kg"] },
    { label: "Chute matière (%)", path: ["scrap_rate"], mult: 100 },
    { label: "Programmation (min)", path: ["programming", "cam_min_per_order"] },
  ];
  const get = (obj: any, path: string[]) => path.reduce((a, k) => (a == null ? undefined : a[k]), obj);
  const set = (path: string[], v: number) => {
    // le serveur fusionne au 1er niveau : on renvoie les sous-objets complets
    let next: Pricing;
    if (path.length === 1) next = { ...o, [path[0]]: v };
    else if (path[0] === "materials") next = { ...o, materials: { ...(o.materials ?? {}), [path[1]]: { ...pricing.materials[path[1]], ...(o.materials?.[path[1]] ?? {}), [path[2]]: v } } };
    else next = { ...o, [path[0]]: { ...(o[path[0]] ?? {}), [path[1]]: v } };
    patch(job.id, { overrides: next });
  };
  return (
    <details className="params">
      <summary>Paramètres atelier</summary>
      <div className="grid2">
        {items.map(({ label, path, mult = 1 }) => (
          <label key={label} className="fld">{label}
            <input type="number" step="any" min={0} defaultValue={+((get(o, path) ?? get(pricing, path)) * mult).toFixed(3)}
              onBlur={(e) => { const v = parseFloat(e.target.value); if (Number.isFinite(v)) set(path, v / mult); }} />
          </label>
        ))}
      </div>
      {Object.keys(o).length > 0 && (
        <div className="params-actions">
          <button className="btn link small" onClick={() => patch(job.id, { overrides: {} })}>Revenir aux valeurs de l'atelier</button>
          <button className="btn small" title="Ces valeurs s'appliqueront à tous les prochains devis. Les devis déjà émis gardent les leurs."
            onClick={async () => {
              try {
                const { version } = await api.savePricing(o, `depuis le dossier ${job.file.name}`);
                await refreshPricing();
                patch(job.id, { overrides: {} });
                setSaved(`Enregistré pour tout l'atelier (version ${version})`);
              } catch (e) { setSaved(`Échec : ${(e as Error).message}`); }
            }}>Enregistrer pour tout l'atelier</button>
        </div>
      )}
      {saved && <p className="muted small">{saved}</p>}
    </details>
  );
}
