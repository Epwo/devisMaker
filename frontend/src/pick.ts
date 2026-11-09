/**
 * Convertit un texte lu sur le plan (OCR ou texte PDF) en valeur pour un champ.
 * Utilisé quand l'utilisateur clique un texte du plan au lieu de taper la valeur.
 */
import { MATERIALS, fieldDef } from "./fields";
import type { OcrToken } from "./types";

export type PickResult = { ok: true; value: unknown; append?: boolean; label: string } | { ok: false; reason: string };

const INCH = 25.4;
const NUM = /(\d*[.,]?\d+)/;

function clean(text: string) {
  return text.replace(/[ϕΦφø⌀∅]/g, "Ø").replace(/\s+/g, " ").trim();
}
const toNum = (s: string) => parseFloat(s.replace(",", "."));
const round = (x: number) => Math.round(x * 100) / 100;
const fmt = (x: number) => x.toLocaleString("fr-FR", { maximumFractionDigits: 2 });

/** Quantité « 4x », « 4 X », « (4 PL) », « 4 PLACES » (pas « .375 X 45° ») */
function qtyOf(t: string): number | null {
  const m = t.match(/(?<![\d.,])(\d+)\s*[xX×]\s/) || t.match(/(?<![\d.,])(\d+)\s*[xX×](?=\s*Ø|\s*R|\s*\d)/)
    || t.match(/\((\d+)\s*(?:X|PL)/i) || t.match(/(\d+)\s*PLACES?/i);
  return m ? parseInt(m[1]) : null;
}

/** Ce texte ressemble-t-il à une cote ? (écarte notes, dates, échelles, tolérances, cartouche…) */
function notADimension(t: string): string | null {
  if (t.split(" ").length > 5) return "Phrase ou note, pas une cote";
  if (/\d+\s*\/\s*\d+\s*\/\s*\d+/.test(t)) return "Date";
  if (/\b\d+\s*:\s*\d+\b/.test(t)) return "Échelle";
  if (/\+\/-|±/.test(t)) return "Tolérance";
  if (/SHEET|PAGE|\bOF\b|FEUILLE|REV/i.test(t)) return "Texte du cartouche";
  if (/^\d+\s*[.)]+\s*\D/.test(t)) return "Note numérotée";
  if (/^\(?\s*\d+\s*(PLACES?|PL\.?)\s*\)?$/i.test(t)) return "Quantité seule, pas une dimension";
  if (/^0\d+$/.test(t)) return "Numéro (pas une cote)";
  return null;
}

const FAMILIES: [RegExp, string][] = [
  [/inox|stainless|30[4-9]|316|1\.4\d{3}/i, "inox"],
  [/alu|5754|5083|5052|60[0-9]{2}|almg/i, "aluminium"],
  [/galva|zingu/i, "acier galvanisé"],
  [/copper|cuivre|brass|laiton/i, "cuivre/laiton"],
  [/steel|acier|s235|s275|s355|dc0\d|dd1\d|a36|xc\d|e24|стомана/i, "acier"],
];

/** Texte cliqué sur le plan -> valeur. Si la lecture retenue ne donne rien (ex. « 🔍 » lu par Mistral pour un « 6 »),
 *  on essaie l'autre lecture du même texte (OCR local). `text` = lecture effectivement utilisée. */
export function pickToken(key: string, token: OcrToken, units: unknown): { res: PickResult; text: string } {
  const res = parsePick(key, token.text, units);
  if (res.ok || !token.alt) return { res, text: token.text };
  const alt = parsePick(key, token.alt, units);
  return alt.ok ? { res: { ...alt, label: `${alt.label} (lu « ${token.alt} »)` }, text: token.alt } : { res, text: token.text };
}

export function parsePick(key: string, raw: string, units: unknown): PickResult {
  const text = clean(raw);
  const k = units === "in" ? INCH : 1;
  const def = fieldDef(key);
  if (!def) return { ok: false, reason: "Champ inconnu" };

  const dimFields = def.kind === "num" || key === "holes" || key === "bends";
  const notDim = dimFields ? notADimension(text) : null;
  // une jauge (« 16 GAUGE A36 STEEL ») reste acceptée pour l'épaisseur
  if (notDim && !(key === "thickness_mm" && /GAUGE|\bGA\b/i.test(text))) return { ok: false, reason: notDim };

  // Champs numériques en mm
  if (def.kind === "num") {
    if (/°/.test(text) && !/R\s*\d/i.test(text)) return { ok: false, reason: "Angle, pas une longueur" };
    const m = text.match(NUM);
    if (!m) return { ok: false, reason: "Ce texte ne contient pas de nombre" };
    let n = toNum(m[1]);
    // Jauge acier : « 16 GAUGE »
    const g = text.match(/(\d{1,2})\s*(?:GAUGE|GA\b)/i);
    if (key === "thickness_mm" && g) {
      const gauge: Record<number, number> = { 10: 3.416, 11: 3.038, 12: 2.657, 14: 1.897, 16: 1.519, 18: 1.214, 20: 0.912, 22: 0.759, 24: 0.607 };
      if (gauge[+g[1]]) return { ok: true, value: gauge[+g[1]], label: `${fmt(gauge[+g[1]])} mm (jauge ${g[1]})` };
    }
    const unitInText = /"|\bin\b|inch/i.test(text) ? INCH : /mm\b/i.test(text) ? 1 : k;
    n = round(n * unitInText);
    return { ok: true, value: n, label: `${fmt(n)} mm${unitInText !== 1 ? " (converti depuis les pouces)" : ""}` };
  }

  if (def.kind === "material") {
    const fam = FAMILIES.find(([rx]) => rx.test(text));
    if (fam) return { ok: true, value: fam[1], label: fam[1] };
    const exact = MATERIALS.find((mat) => text.toLowerCase().includes(mat));
    return exact ? { ok: true, value: exact, label: exact } : { ok: true, value: text, label: text };
  }

  if (key === "holes") {
    if (/°/.test(text)) return { ok: false, reason: "Chanfrein ou angle, pas un trou" };
    if (/^\s*R\s*[\d.]/i.test(text)) return { ok: false, reason: "Rayon, pas un trou" };
    const m =text.match(/Ø\s*(\d*[.,]?\d+)/) || text.match(/(?:\d+\s*[xX×]\s*)(\d*[.,]?\d+)/) || text.match(NUM);
    if (!m) return { ok: false, reason: "Aucun diamètre dans ce texte" };
    const thread = text.match(/\bM(\d{1,2})\b/);
    const d = thread ? +thread[1] : round(toNum(m[1]) * k);
    const q = qtyOf(text) ?? 1;
    return { ok: true, append: true, value: { diameter_mm: d, quantity: q, kind: thread ? "threaded" : "through" }, label: `${q} × Ø${fmt(d)} mm` };
  }

  if (key === "bends") {
    const a = text.match(/(\d+(?:[.,]\d+)?)\s*(?:°|DEG)/i);
    const r = text.match(/R\s*(\d*[.,]?\d+)/i);
    if (!a && !r) return { ok: false, reason: "Ni angle (°) ni rayon (R) dans ce texte" };
    const q = qtyOf(text) ?? 1;
    const value = { angle_deg: a ? toNum(a[1]) : null, radius_mm: r ? round(toNum(r[1]) * k) : null, quantity: q };
    return { ok: true, append: true, value, label: `${q} × ${a ? fmt(value.angle_deg!) + "°" : "?°"}${r ? ` R${fmt(value.radius_mm!)}` : ""}` };
  }

  if (key === "cutouts" || key === "components") {
    const q = qtyOf(text) ?? 1;
    const desc = text.replace(/^\s*\d+\s*[xX×]\s*/, "");
    const value = key === "cutouts" ? { description: desc, quantity: q } : { designation: desc, quantity: q };
    return { ok: true, append: true, value, label: `${q} × ${desc}` };
  }

  if (key === "notes") return { ok: true, append: true, value: text, label: text };

  // Texte : on retire un éventuel libellé « MATÉRIAU : », « TITLE: »…
  const v = text.replace(/^[^:]{2,25}:\s*/, "").trim() || text;
  return { ok: true, value: v, label: v };
}
