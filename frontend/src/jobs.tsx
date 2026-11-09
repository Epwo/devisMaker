/**
 * Dossiers (un par plan) et file de traitement parallèle.
 *
 * Côté serveur, chaque /extract est une requête indépendante : on peut donc en lancer
 * plusieurs à la fois. La file est gérée ici, côté client, avec une limite de
 * MAX_PARALLEL analyses simultanées. Plus tard, le back pourra exposer une vraie file
 * (job id + statut), sans changer cette interface.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, type ReactNode } from "react";
import { api } from "./api";
import { isBlocking, isEmpty } from "./fields";
import type { Field, Job, OcrToken, Pricing, TraceStep } from "./types";

/** Analyses simultanées. POC : 1 (le back traite un plan à la fois). L'UI gère déjà N > 1. */
export const MAX_PARALLEL = 1;

type Action =
  | { type: "add"; jobs: Job[] }
  | { type: "patch"; id: string; patch: Partial<Job> }
  | { type: "field"; id: string; key: string; patch: Partial<Field>; step?: TraceStep }
  | { type: "remove"; id: string }
  | { type: "select"; id: string | null };

interface State { jobs: Job[]; selected: string | null }

/** Statut déduit de l'extraction : reste-t-il des points indispensables à vérifier ? */
export const reviewCount = (job: Job) => (job.ext ? Object.entries(job.ext.fields).filter(([k, f]) => isBlocking(k, f, job.ext!.fields)).length : 0);
const statusAfterReview = (job: Job): Job["status"] => (reviewCount(job) ? "review" : "ready");

function reducer(s: State, a: Action): State {
  switch (a.type) {
    case "add":
      return { jobs: [...s.jobs, ...a.jobs], selected: s.selected ?? a.jobs[0]?.id ?? null };
    case "patch":
      return { ...s, jobs: s.jobs.map((j) => (j.id === a.id ? { ...j, ...a.patch } : j)) };
    case "field":
      return {
        ...s,
        jobs: s.jobs.map((j) => {
          if (j.id !== a.id || !j.ext) return j;
          const prev = j.ext.fields[a.key];
          const trace = a.step ? [...(prev.trace ?? []), { ...a.step, date: new Date().toISOString() }] : prev.trace;
          const fields = { ...j.ext.fields, [a.key]: { ...prev, ...a.patch, trace } };
          const next = { ...j, ext: { ...j.ext, fields }, pdfDownloaded: false };
          return { ...next, status: statusAfterReview(next) };
        }),
      };
    case "remove": {
      const jobs = s.jobs.filter((j) => j.id !== a.id);
      return { jobs, selected: s.selected === a.id ? jobs[0]?.id ?? null : s.selected };
    }
    case "select":
      return { ...s, selected: a.id };
  }
}

interface Ctx {
  jobs: Job[];
  selected: Job | null;
  pricing: Pricing | null;
  /** Relit les paramètres atelier en vigueur (après enregistrement d'une nouvelle version). */
  refreshPricing: () => Promise<void>;
  aiAvailable: boolean;
  addFiles: (files: File[], opts: { useAi: boolean; quantity: number; client: string }) => void;
  select: (id: string | null) => void;
  remove: (id: string) => void;
  retry: (id: string) => void;
  patch: (id: string, patch: Partial<Job>) => void;
  setField: (id: string, key: string, value: unknown) => void;
  confirmField: (id: string, key: string) => void;
  /** Remplit un champ avec un texte cliqué sur le plan. `replace` : la 1re sélection remplace la liste lue automatiquement. */
  fillFromPlan: (id: string, key: string, token: OcrToken, value: unknown, opts: { append?: boolean; replace?: boolean }) => void;
}

const JobsContext = createContext<Ctx | null>(null);
export const useJobs = () => {
  const c = useContext(JobsContext);
  if (!c) throw new Error("useJobs hors JobsProvider");
  return c;
};

let seq = 0;
const newId = () => `${Date.now().toString(36)}-${++seq}`;

export function JobsProvider({ children, pricing, aiAvailable, refreshPricing }: { children: ReactNode; pricing: Pricing | null; aiAvailable: boolean; refreshPricing: () => Promise<void> }) {
  const [state, dispatch] = useReducer(reducer, { jobs: [], selected: null });
  const aborters = useRef(new Map<string, AbortController>());
  const quoteTimers = useRef(new Map<string, number>());
  const jobsRef = useRef(state.jobs);
  jobsRef.current = state.jobs;

  const patch = useCallback((id: string, p: Partial<Job>) => dispatch({ type: "patch", id, patch: p }), []);

  // ------------------------------------------------------------- journal d'apprentissage
  // Chaque décision de l'utilisateur sur un champ est envoyée au serveur (data/learning/feedback.jsonl),
  // qui la compare à la proposition de la machine. Les saisies au clavier sont regroupées (1,5 s).
  const fbTimers = useRef(new Map<string, number>());
  const learn = useCallback((id: string, key: string, action: "confirmation" | "saisie" | "choix_sur_plan", detail?: Record<string, unknown>) => {
    const k = `${id}|${key}`;
    window.clearTimeout(fbTimers.current.get(k));
    fbTimers.current.set(k, window.setTimeout(() => {
      fbTimers.current.delete(k);
      const job = jobsRef.current.find((j) => j.id === id);
      const aid = job?.ext?.analysis_id;
      if (aid != null) api.feedback({ analysis_id: aid, field: key, value: job!.ext!.fields[key]?.value ?? null, action, detail });
    }, action === "saisie" ? 1500 : 300));
  }, []);

  // ------------------------------------------------------------- chiffrage (debounce par dossier)
  const requote = useCallback((id: string) => {
    window.clearTimeout(quoteTimers.current.get(id));
    quoteTimers.current.set(id, window.setTimeout(async () => {
      const job = jobsRef.current.find((j) => j.id === id);
      if (!job?.ext) return;
      const fields = job.ext.fields;
      const values = Object.fromEntries(Object.entries(fields).map(([k, f]) => [k, f.value]));
      const conf = Object.fromEntries(Object.entries(fields).filter(([, f]) => !isEmpty(f.value)).map(([k, f]) => [k, f.confidence]));
      try {
        const quote = await api.quote(values, job.quantity, job.overrides, conf);
        patch(id, { quote, quoteError: undefined });
      } catch (e) {
        patch(id, { quoteError: (e as Error).message });
      }
    }, 250));
  }, [patch]);

  // Rechiffre quand l'extraction, la quantité ou les paramètres d'un dossier changent
  const quoteKeys = state.jobs.map((j) => `${j.id}|${j.ext ? JSON.stringify(j.ext.fields) : ""}|${j.quantity}|${JSON.stringify(j.overrides)}`).join("§");
  const lastKeys = useRef(new Map<string, string>());
  useEffect(() => {
    for (const j of state.jobs) {
      if (!j.ext) continue;
      const k = `${JSON.stringify(j.ext.fields)}|${j.quantity}|${JSON.stringify(j.overrides)}|${JSON.stringify(pricing)}`;
      if (lastKeys.current.get(j.id) !== k) {
        lastKeys.current.set(j.id, k);
        requote(j.id);
      }
    }
  }, [quoteKeys, pricing]); // eslint-disable-line react-hooks/exhaustive-deps

  // ------------------------------------------------------------- file d'analyse
  const started = useRef(new Set<string>()); // évite un double lancement (StrictMode, re-rendus)
  const run = useCallback(async (job: Job) => {
    const key = `${job.id}@${job.addedAt}`; // addedAt change à chaque "Réessayer"
    if (started.current.has(key)) return;
    started.current.add(key);
    const ctrl = new AbortController();
    aborters.current.set(job.id, ctrl);
    patch(job.id, { status: "analyzing", startedAt: Date.now(), error: undefined });
    try {
      const ext = await api.extract(job.file, job.useAi, ctrl.signal);
      const done = { ...job, ext };
      patch(job.id, { ext, status: statusAfterReview(done) });
    } catch (e) {
      if (ctrl.signal.aborted) return;
      patch(job.id, { status: "error", error: (e as Error).message });
    } finally {
      aborters.current.delete(job.id);
    }
  }, [patch]);

  useEffect(() => {
    const running = state.jobs.filter((j) => j.status === "analyzing").length;
    const waiting = state.jobs.filter((j) => j.status === "queued").sort((a, b) => a.addedAt - b.addedAt);
    waiting.slice(0, Math.max(0, MAX_PARALLEL - running)).forEach(run);
  }, [state.jobs, run]);

  // ------------------------------------------------------------- actions
  const addFiles: Ctx["addFiles"] = useCallback((files, { useAi, quantity, client }) => {
    const now = Date.now();
    const jobs: Job[] = files.map((file, i) => ({
      id: newId(), file, addedAt: now + i, status: "queued", useAi, quantity, client, overrides: {},
    }));
    dispatch({ type: "add", jobs });
    if (jobs.length) dispatch({ type: "select", id: jobs[0].id });
  }, []);

  const remove = useCallback((id: string) => {
    aborters.current.get(id)?.abort();
    window.clearTimeout(quoteTimers.current.get(id));
    dispatch({ type: "remove", id });
  }, []);

  const value: Ctx = useMemo(() => ({
    jobs: state.jobs,
    selected: state.jobs.find((j) => j.id === state.selected) ?? null,
    pricing,
    refreshPricing,
    aiAvailable,
    addFiles,
    select: (id) => dispatch({ type: "select", id }),
    remove,
    retry: (id) => patch(id, { status: "queued", error: undefined, addedAt: Date.now() }),
    patch: (id, p) => patch(id, { pdfDownloaded: false, ...p }),
    setField: (id, key, v) => {
      dispatch({ type: "field", id, key, patch: { value: v, source: "user", confidence: 1, issues: [] },
        step: { etape: "utilisateur", action: "saisie manuelle", valeur: v } });
      learn(id, key, "saisie");
    },
    confirmField: (id, key) => {
      const cur = jobsRef.current.find((j) => j.id === id)?.ext?.fields[key];
      dispatch({ type: "field", id, key, patch: { source: "user", confidence: 1, issues: [] },
        step: { etape: "utilisateur", action: "valeur confirmée (« C'est correct »)", valeur: cur?.value, source_avant: cur?.source } });
      learn(id, key, "confirmation");
    },
    fillFromPlan: (id, key, token, value, { append, replace }) => {
      const cur = jobsRef.current.find((j) => j.id === id)?.ext?.fields[key];
      const keep = append && !replace && Array.isArray(cur?.value);
      const list = keep ? [...(cur!.value as unknown[]), value] : append ? [value] : value;
      const prevBoxes = keep ? cur?.boxes ?? [] : [];
      const prevEvidence = keep && cur?.evidence ? cur.evidence + " | " : "";
      dispatch({
        type: "field", id, key,
        patch: { value: list, source: "user", confidence: 1, issues: [], evidence: prevEvidence + token.text, box: token.box, boxes: [...prevBoxes, token.box] },
        step: { etape: "utilisateur", action: "texte choisi sur le plan", texte_clique: token.text, valeur_deduite: value,
                zone: [token.box.x0, token.box.y0, token.box.x1, token.box.y1].map(Math.round) },
      });
      learn(id, key, "choix_sur_plan", { texte_clique: token.text, zone: [token.box.x0, token.box.y0, token.box.x1, token.box.y1].map(Math.round) });
    },
  }), [state, pricing, refreshPricing, aiAvailable, addFiles, remove, patch, learn]);

  return <JobsContext.Provider value={value}>{children}</JobsContext.Provider>;
}
