import { useEffect, useState } from "react";
import { reviewCount, useJobs } from "../jobs";
import { pickToken } from "../pick";
import type { Job, OcrToken } from "../types";
import { PlanViewer, type PickState } from "./PlanViewer";
import { QuotePanel } from "./QuotePanel";
import { ReviewPanel } from "./ReviewPanel";
import { StatusBadge } from "./StatusBadge";
import { exportAnalysis } from "./TraceModal";

const eur = (x: number) => x.toLocaleString("fr-FR", { style: "currency", currency: "EUR" });

export function JobView({ job }: { job: Job }) {
  const { remove, retry } = useJobs();
  // Sélection affichée sur le plan. `fromPlan` : clic sur un cadre du plan → on amène la carte en vue
  // (seulement dans ce cas, pour ne jamais faire sauter le défilement pendant que l'utilisateur lit).
  const [sel, setSel] = useState<{ keys: string[]; fromPlan: boolean }>({ keys: [], fromPlan: false });
  // Mode « choisir sur le plan » : le champ à remplir et ce qui a déjà été ajouté (listes)
  const [pick, setPick] = useState<PickState | null>(null);
  const { fillFromPlan } = useJobs();
  const title = (job.ext?.fields.designation?.value as string) || job.file.name;

  const startPick = (key: string) => { setPick({ key, added: [] }); setSel({ keys: [key], fromPlan: false }); };
  const endPick = () => { if (pick) setSel({ keys: [pick.key], fromPlan: false }); setPick(null); };
  const onToken = (token: OcrToken) => {
    if (!pick || !job.ext) return;
    const { res, text } = pickToken(pick.key, token, job.ext.fields.units?.value);
    if (!res.ok) return;
    const field = job.ext.fields[pick.key];
    fillFromPlan(job.id, pick.key, { ...token, text }, res.value, { append: res.append, replace: res.append && !pick.added.length && field.source !== "user" });
    if (res.append) setPick({ ...pick, added: [...pick.added, res.label] }); // listes : on peut en cliquer plusieurs
    else { setPick(null); setSel({ keys: [pick.key], fromPlan: false }); }
  };

  return (
    <div className="jobview">
      <header className="job-head">
        <div className="job-title">
          <h1>{title}</h1>
          <div className="muted small">{job.file.name}{job.client ? ` · ${job.client}` : ""} · {job.quantity} pièce{job.quantity > 1 ? "s" : ""}</div>
        </div>
        <StatusBadge job={job} />
        {job.quote && job.ext && <div className="head-price"><span className="muted small">Total HT</span><b>{eur(job.quote.total_price)}</b></div>}
        {job.ext && <button className="btn ghost small" title="Toutes les valeurs et leur journal de décision (OCR, règles, IA, vos corrections)"
          onClick={() => exportAnalysis(job.file.name, job.ext!.fields, { lecture: job.ext!.text_source, ia: job.ext!.vlm_provider ?? null,
            ia_erreur: job.ext!.vlm_error ?? null, quantite: job.quantity, devis: job.quote ?? null })}>Exporter l'analyse (JSON)</button>}
        <button className="btn ghost small" onClick={() => remove(job.id)} title="Retirer ce dossier">Retirer</button>
      </header>

      <Stepper job={job} />

      {(job.status === "queued" || job.status === "analyzing") && <Analyzing job={job} />}

      {job.status === "error" && (
        <div className="card state">
          <h2>L'analyse a échoué</h2>
          <p className="error">{job.error}</p>
          <button className="btn primary" onClick={() => retry(job.id)}>Réessayer</button>
        </div>
      )}

      {job.ext && (job.status === "review" || job.status === "ready") && (
        <div className="work">
          <PlanViewer ext={job.ext} active={sel.keys} onPick={(keys) => setSel({ keys, fromPlan: true })}
            pick={pick} onToken={onToken} onEndPick={endPick} />
          <div className="side">
            <ReviewPanel job={job} active={sel.keys} scrollToActive={sel.fromPlan}
              onActive={(keys) => { if (!pick) setSel({ keys, fromPlan: false }); }}
              picking={pick?.key ?? null} onStartPick={startPick} onEndPick={endPick} />
            <QuotePanel job={job} />
          </div>
        </div>
      )}
    </div>
  );
}

function Stepper({ job }: { job: Job }) {
  const n = reviewCount(job);
  const analysed = !!job.ext;
  const steps = [
    { label: "Analyse du plan", state: analysed ? "done" : job.status === "error" ? "bad" : "now" },
    { label: n ? `Vérification (${n} restant${n > 1 ? "s" : ""})` : "Vérification", state: !analysed ? "todo" : n ? "now" : "done" },
    { label: job.pdfDownloaded ? "Devis téléchargé" : "Devis", state: !analysed ? "todo" : job.pdfDownloaded ? "done" : n ? "todo" : "now" },
  ];
  return (
    <ol className="stepper">
      {steps.map((s, i) => (
        <li key={i} className={s.state}><span className="num">{s.state === "done" ? "✓" : i + 1}</span>{s.label}</li>
      ))}
    </ol>
  );
}

function Analyzing({ job }: { job: Job }) {
  const { jobs } = useJobs();
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 500); return () => clearInterval(t); }, []);

  if (job.status === "queued") {
    const ahead = jobs.filter((j) => j.status === "queued" && j.addedAt < job.addedAt).length;
    const running = jobs.filter((j) => j.status === "analyzing");
    return (
      <div className="card state">
        <h2>En attente</h2>
        <p>
          {running.length === 1 ? <>Un autre plan est en cours d'analyse (<b>{running[0].file.name}</b>).</> : "D'autres plans sont en cours d'analyse."}
          {" "}Celui-ci démarrera ensuite{ahead ? `, après ${ahead} autre${ahead > 1 ? "s" : ""} plan${ahead > 1 ? "s" : ""} en attente` : ""}.
        </p>
        <p className="muted small">Inutile d'attendre ici : vous pouvez vérifier les dossiers déjà analysés dans la liste à gauche.</p>
      </div>
    );
  }
  const t = (now - (job.startedAt ?? now)) / 1000;
  // Le serveur ne renvoie pas encore d'avancement : étapes indicatives, basées sur la durée habituelle.
  const stages = [
    { label: "Lecture du fichier", at: 0 },
    { label: "Reconnaissance du texte et des cotes", at: 1.5 },
    { label: "Interprétation (matière, trous, plis…)", at: 8 },
    ...(job.useAi ? [{ label: "Complément par l'IA des informations manquantes", at: 12 }] : []),
  ];
  const cur = stages.filter((s) => t >= s.at).length - 1;
  return (
    <div className="card state">
      <h2>Analyse du plan…</h2>
      <ul className="stages">
        {stages.map((s, i) => (
          <li key={s.label} className={i < cur ? "done" : i === cur ? "now" : ""}>
            <span className="mark">{i < cur ? "✓" : i === cur ? <span className="dot-spin" /> : "·"}</span>{s.label}
          </li>
        ))}
      </ul>
      <p className="muted small">{Math.floor(t)} s — en général 10 à 30 s par plan. Vous pouvez ouvrir un autre dossier en attendant.</p>
    </div>
  );
}
