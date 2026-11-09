import type { Field, Source } from "./types";

export type Kind = "text" | "num" | "material" | "list" | "notes" | "readonly";

export interface FieldDef {
  key: string;
  label: string;
  kind: Kind;
  unit?: string;
  /** Aide en langage simple, affichée quand le champ est à vérifier */
  help: string;
  /** Influence le prix ? */
  pricing?: boolean;
  /** Indispensable pour chiffrer : bloque le statut « Prêt » tant que non vérifié.
   *  « bent » : seulement pour une pièce pliée dont le développé n'est pas coté (voir isEssential). */
  essential?: boolean | "bent";
}

export const FIELD_GROUPS: { title: string; fields: FieldDef[] }[] = [
  {
    title: "Pièce",
    fields: [
      { key: "designation", label: "Désignation", kind: "text", help: "Nom de la pièce, souvent dans le cartouche ou la nomenclature." },
      { key: "reference", label: "Référence plan", kind: "text", help: "Numéro de plan / article, dans le cartouche." },
      { key: "client", label: "Client (sur le plan)", kind: "text", help: "Nom ou logo du donneur d'ordre dans le cartouche." },
    ],
  },
  {
    title: "Matière",
    fields: [
      { key: "material", label: "Matière", kind: "material", pricing: true, essential: true, help: "Acier, inox, aluminium… Change le prix au kilo." },
      { key: "material_grade", label: "Nuance", kind: "text", help: "Ex. S235JR, 304L, 5754." },
      { key: "thickness_mm", label: "Épaisseur", kind: "num", unit: "mm", pricing: true, essential: true, help: "Épaisseur de la tôle. Indispensable au prix (poids, vitesse de découpe)." },
      { key: "surface_treatment", label: "Traitement", kind: "text", pricing: true, essential: true, help: "Peinture, zingage… ou « aucun »." },
    ],
  },
  {
    title: "Dimensions",
    fields: [
      { key: "length_mm", label: "Longueur", kind: "num", unit: "mm", pricing: true, essential: true, help: "Plus grande dimension de la pièce finie." },
      { key: "width_mm", label: "Largeur", kind: "num", unit: "mm", pricing: true, essential: true, help: "Deuxième dimension de la pièce finie." },
      { key: "height_mm", label: "Hauteur", kind: "num", unit: "mm", pricing: true, essential: "bent", help: "Hauteur après pliage. Sert à estimer la tôle à plat quand le développé n'est pas coté." },
      { key: "flat_length_mm", label: "Développé — longueur", kind: "num", unit: "mm", pricing: true, help: "Longueur de la tôle à plat avant pliage. Si vide, estimée à partir des dimensions." },
      { key: "flat_width_mm", label: "Développé — largeur", kind: "num", unit: "mm", pricing: true, help: "Largeur de la tôle à plat avant pliage." },
      { key: "units", label: "Unité du plan", kind: "readonly", help: "Les cotes en pouces sont converties en mm." },
    ],
  },
  {
    title: "Fabrication",
    fields: [
      { key: "holes", label: "Trous", kind: "list", pricing: true, essential: true, help: "Diamètre et nombre de trous (temps de découpe)." },
      { key: "bends", label: "Plis", kind: "list", pricing: true, essential: true, help: "Nombre de plis, angle et rayon (temps de pliage)." },
      { key: "cutouts", label: "Chanfreins / découpes", kind: "list", help: "Chanfreins, encoches particulières." },
      { key: "components", label: "Composants", kind: "list", pricing: true, help: "Écrous à sertir, inserts… à acheter et poser." },
    ],
  },
  {
    title: "Divers",
    fields: [
      { key: "tolerance_standard", label: "Tolérances", kind: "text", help: "Norme de tolérances générales." },
      { key: "notes", label: "Notes du plan", kind: "notes", help: "Remarques utiles à l'atelier." },
    ],
  },
];

export const ALL_FIELDS = FIELD_GROUPS.flatMap((g) => g.fields);
export const fieldDef = (key: string) => ALL_FIELDS.find((f) => f.key === key);

export const LIST_COLUMNS: Record<string, { key: string; label: string; type: "num" | "int" | "text" | string[] }[]> = {
  holes: [
    { key: "diameter_mm", label: "Ø (mm)", type: "num" },
    { key: "quantity", label: "Nombre", type: "int" },
    { key: "kind", label: "Type", type: ["through", "threaded", "insert"] },
  ],
  bends: [
    { key: "angle_deg", label: "Angle (°)", type: "num" },
    { key: "radius_mm", label: "Rayon (mm)", type: "num" },
    { key: "quantity", label: "Nombre", type: "int" },
  ],
  cutouts: [
    { key: "description", label: "Description", type: "text" },
    { key: "quantity", label: "Nombre", type: "int" },
  ],
  components: [
    { key: "designation", label: "Composant", type: "text" },
    { key: "quantity", label: "Nombre", type: "int" },
  ],
};

export const OPTION_LABELS: Record<string, string> = { through: "débouchant", threaded: "taraudé", insert: "sertissage" };
export const MATERIALS = ["acier", "acier galvanisé", "inox", "aluminium", "cuivre/laiton"];

/** Libellés simples pour l'origine d'une valeur. */
export const SOURCE_INFO: Record<Source, { label: string; tone: string }> = {
  ocr: { label: "Lu sur le plan", tone: "read" },
  "ocr+vlm": { label: "Lu et confirmé par l'IA", tone: "read" },
  vlm: { label: "Proposé par l'IA", tone: "ai" },
  default: { label: "Valeur par défaut", tone: "default" },
  user: { label: "Vérifié par vous", tone: "user" },
  none: { label: "Introuvable", tone: "missing" },
};

export const isEmpty = (v: unknown) => v === null || v === undefined || v === "" || (Array.isArray(v) && v.length === 0);
// Une valeur proposée par l'IA seule doit être confirmée d'un clic (l'IA se trompe parfois sur les cotes hors-tout) ;
// si l'IA confirme une lecture OCR (« ocr+vlm »), elle est acceptée.
export const needsReview = (f: Field) =>
  f.source !== "user" && (isEmpty(f.value) || f.confidence < 0.7 || f.issues.length > 0 || f.source === "vlm");

/** Pourquoi on demande de vérifier, en une phrase. */
export function reviewReason(f: Field): string {
  if (isEmpty(f.value)) return "Introuvable sur le plan : à saisir.";
  if (f.issues.length) return f.issues[0];
  if (f.source === "vlm") return "Proposé par l'IA : confirmez ou corrigez.";
  return "Lecture incertaine : confirmez ou corrigez.";
}

export function formatValue(key: string, v: unknown): string {
  if (isEmpty(v)) return "—";
  const def = fieldDef(key);
  if (Array.isArray(v)) {
    if (key === "holes") return v.map((h: any) => `${h.quantity ?? 1} × Ø${h.diameter_mm}`).join(", ");
    if (key === "bends") return v.map((b: any) => `${b.quantity ?? 1} × ${b.angle_deg ?? "?"}°${b.radius_mm != null ? ` R${b.radius_mm}` : ""}`).join(", ");
    if (key === "cutouts") return v.map((c: any) => `${c.quantity ?? 1} × ${c.description}`).join(", ");
    if (key === "components") return v.map((c: any) => `${c.quantity ?? 1} × ${c.designation}`).join(", ");
    return v.join(" · ");
  }
  if (typeof v === "number") return `${Number(v.toFixed(2)).toLocaleString("fr-FR")}${def?.unit ? " " + def.unit : ""}`;
  return String(v);
}

/** Indispensable au chiffrage pour CETTE pièce. La hauteur ne compte que pour une pièce pliée sans développé coté :
 *  le chiffrage l'ajoute alors à la longueur du flan (devis/costing.py). */
export function isEssential(def: FieldDef | undefined, fields: Record<string, Field>): boolean {
  if (def?.essential !== "bent") return !!def?.essential;
  const bent = !isEmpty(fields.bends?.value);
  const flatGiven = !isEmpty(fields.flat_length_mm?.value) && !isEmpty(fields.flat_width_mm?.value);
  return bent && !flatGiven;
}

/** À vérifier ET indispensable au chiffrage. */
export const isBlocking = (key: string, f: Field, fields: Record<string, Field>) => isEssential(fieldDef(key), fields) && needsReview(f);
