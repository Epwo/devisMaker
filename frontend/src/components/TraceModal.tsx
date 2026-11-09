import { useEffect, useState } from "react";
import { fieldDef, formatValue } from "../fields";
import type { Field, TraceStep } from "../types";

const TITLES: Record<string, string> = {
  lecture: "1. Lecture du plan (OCR / texte PDF + règles)",
  regle_alerte: "Alerte de la règle",
  controle: "Contrôle de cohérence",
  routage: "2. Faut-il demander à l'IA ?",
  ia: "3. Réponse de l'IA",
  fusion: "4. Décision (fusion OCR + IA)",
  localisation: "Zone retrouvée sur le plan",
  resultat: "Résultat de l'analyse",
  utilisateur: "Action de l'utilisateur",
};

const pct = (x: unknown) => (typeof x === "number" ? `${Math.round(x * 100)} %` : "—");

/** Rend une étape du journal de façon lisible ; le JSON brut reste disponible en dessous. */
function Step({ k, s }: { k: string; s: TraceStep }) {
  const v = (x: unknown) => formatValue(k, x);
  const rows: [string, React.ReactNode][] = [];
  switch (s.etape) {
    case "lecture":
      if (s.resultat) rows.push(["Résultat", String(s.resultat)]);
      if (s.regle) rows.push(["Règle", String(s.regle)]);
      if (Array.isArray(s.textes_lus) && s.textes_lus.length)
        rows.push(["Textes lus", (s.textes_lus as any[]).map((t, i) => (
          <div key={i}>« {t.texte} » <span className="muted">OCR {pct(t.confiance_ocr)}{t.vertical ? ", vertical" : ""}</span></div>))]);
      if ("valeur" in s) rows.push(["Valeur", v(s.valeur)], ["Confiance", pct(s.confiance)]);
      if (s.detail) rows.push(["Détail", String(s.detail)]);
      break;
    case "regle_alerte":
      rows.push(["Alertes", (s.alertes as string[]).join(" · ")]);
      break;
    case "controle":
      rows.push(["Alerte", String(s.alerte)], ["Confiance", `${pct(s.confiance_avant)} → ${pct(s.confiance_apres)}`]);
      break;
    case "routage":
      rows.push(["Décision", String(s.decision)], ["Raison", String(s.raison)]);
      break;
    case "ia":
      if (s.erreur) { rows.push(["Erreur", String(s.erreur)], ["Conséquence", String(s.consequence)]); break; }
      rows.push(["Modèle", String(s.fournisseur)], ["Valeur proposée", v(s.valeur_proposee)],
        ["Confiance annoncée", pct(s.confiance_annoncee)], ["Justification", String(s.justification ?? "")]);
      if (s.contradiction) rows.push(["Contradiction", String(s.contradiction)]);
      if (Array.isArray(s.lecture_par_vues))
        rows.push(["Lecture vue par vue", (s.lecture_par_vues as any[]).map((w, i) => (
          <div key={i}>{w.view} : {w.overall_horizontal_mm ?? "?"} × {w.overall_vertical_mm ?? "?"} mm <span className="muted">— {w.evidence}</span></div>))]);
      break;
    case "fusion":
      rows.push(["Décision", String(s.decision)], ["Valeur", v(s.valeur)], ["Confiance", pct(s.confiance)]);
      if ((s.alertes as string[])?.length) rows.push(["Alertes", (s.alertes as string[]).join(" · ")]);
      break;
    case "localisation":
      rows.push(["Textes", (s.textes as string[]).map((t) => `« ${t} »`).join(", ")], ["Note", String(s.explication)]);
      break;
    case "resultat":
      rows.push(["Valeur", v(s.valeur)], ["Origine", String(s.source)], ["Confiance", pct(s.confiance)],
        ["À vérifier", s.a_verifier ? "oui" : "non"]);
      if ((s.alertes as string[])?.length) rows.push(["Alertes", (s.alertes as string[]).join(" · ")]);
      break;
    case "utilisateur":
      rows.push(["Action", String(s.action)], ["Valeur", v(s.valeur ?? s.valeur_deduite)]);
      if (s.texte_clique) rows.push(["Texte cliqué", `« ${s.texte_clique} »`]);
      if (s.date) rows.push(["Date", new Date(String(s.date)).toLocaleString("fr-FR")]);
      break;
    default:
      rows.push(["", JSON.stringify(s)]);
  }
  return (
    <li className={`tstep t-${s.etape}`}>
      <div className="tstep-title">{TITLES[s.etape] ?? s.etape}</div>
      <dl>{rows.map(([a, b], i) => <div key={i}><dt>{a}</dt><dd>{b}</dd></div>)}</dl>
    </li>
  );
}

export function TraceModal({ k, field, onClose }: { k: string; field: Field; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const json = JSON.stringify({ champ: k, valeur: field.value, source: field.source, confiance: field.confidence, journal: field.trace ?? [] }, null, 2);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" role="dialog" aria-label="Pourquoi cette valeur ?" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <h2>Pourquoi cette valeur ? — {fieldDef(k)?.label ?? k}</h2>
            <div className="muted small">Valeur actuelle : <b>{formatValue(k, field.value)}</b></div>
          </div>
          <button className="btn link" onClick={onClose} aria-label="Fermer">✕</button>
        </div>
        <ol className="timeline">{(field.trace ?? []).map((s, i) => <Step key={i} k={k} s={s} />)}</ol>
        {!(field.trace ?? []).length && <p className="muted">Pas de journal pour ce champ.</p>}
        <details className="raw">
          <summary>JSON brut</summary>
          <button className="btn small" onClick={() => { navigator.clipboard?.writeText(json); setCopied(true); }}>{copied ? "Copié ✓" : "Copier le JSON"}</button>
          <pre>{json}</pre>
        </details>
      </div>
    </div>
  );
}

/** Export de toute l'analyse d'un plan (valeurs + journaux) en JSON. */
export function exportAnalysis(fileName: string, fields: Record<string, Field>, meta: Record<string, unknown>) {
  const out = {
    plan: fileName, exporte_le: new Date().toISOString(), ...meta,
    champs: Object.fromEntries(Object.entries(fields).map(([k, f]) => [k, {
      valeur: f.value, source: f.source, confiance: f.confidence, alertes: f.issues, journal: f.trace ?? [],
    }])),
  };
  const url = URL.createObjectURL(new Blob([JSON.stringify(out, null, 2)], { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `analyse_${fileName.replace(/\.[^.]+$/, "")}.json`;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
