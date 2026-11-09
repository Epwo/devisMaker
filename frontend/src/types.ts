// Miroir des modèles Pydantic du back (devis/schemas.py, devis/costing.py)

export type Source = "ocr" | "vlm" | "ocr+vlm" | "default" | "user" | "none";

export interface Box { x0: number; y0: number; x1: number; y1: number }

export interface Field {
  value: unknown;
  confidence: number;
  source: Source;
  evidence?: string | null;
  box?: Box | null;
  boxes?: Box[];   // une zone par texte lu
  trace?: TraceStep[]; // journal de décision : lecture, règle, contrôle, IA, fusion, utilisateur
  issues: string[];
}

export type TraceStep = { etape: string; [k: string]: unknown };

export interface OcrToken { text: string; conf: number; box: Box; vertical: boolean; alt?: string | null } // alt : autre lecture (OCR local vs Mistral)

export interface Extraction {
  file_name: string;
  fields: Record<string, Field>;
  ocr_tokens: OcrToken[];
  text_source: string; // "pdf-text", "ocr" (local) ou "ocr:<moteur>"
  preview_png_b64?: string | null;
  preview_scale: number;
  vlm_used: boolean;
  vlm_fields_requested: string[];
  vlm_error?: string | null;
  vlm_provider?: string | null;
  warnings: string[];
  plan_id?: string | null;     // empreinte du fichier (table plans)
  analysis_id?: number | null; // analyse en base : référence du journal d'apprentissage
}

export interface QuoteLine { category: string; label: string; detail: string; unit_cost: number }

export interface Quote {
  quantity: number;
  currency: string;
  lines: QuoteLine[];
  unit_cost: number;
  margin_rate: number;
  unit_price: number;
  total_price: number;
  mass_kg: number;
  blank_mm: [number, number];
  cut_length_mm: number;
  assumptions: string[];
  warnings: string[];
}

export type Pricing = Record<string, any>; // config/pricing.json

/** Un dossier = un plan à deviser. Plusieurs dossiers sont traités en parallèle. */
export type JobStatus =
  | "queued"     // en attente d'un créneau de traitement
  | "analyzing"  // lecture du plan (OCR / texte PDF / IA) côté serveur
  | "review"     // des informations sont à vérifier par l'utilisateur
  | "ready"      // tout est vérifié, le devis peut partir
  | "error";

export interface Job {
  id: string;
  file: File;
  addedAt: number;
  startedAt?: number;
  status: JobStatus;
  error?: string;
  useAi: boolean;
  ext?: Extraction;
  quantity: number;
  client: string;
  overrides: Pricing;
  quote?: Quote;
  quoteError?: string;
  pdfDownloaded?: boolean;
}
