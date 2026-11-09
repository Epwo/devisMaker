import { useState } from "react";
import { MAX_PARALLEL, useJobs } from "../jobs";
import { downloadQuote } from "./download";

/** Vue d'ensemble des traitements parallèles + action groupée. */
export function Summary() {
  const { jobs, patch } = useJobs();
  const [busy, setBusy] = useState(false);
  const n = (s: string) => jobs.filter((j) => j.status === s).length;
  const ready = jobs.filter((j) => j.status === "ready" && j.ext);
  const running = n("analyzing"), queued = n("queued");

  const downloadAll = async () => {
    setBusy(true);
    for (const j of ready) {
      try { await downloadQuote(j); patch(j.id, { pdfDownloaded: true }); } catch { /* signalé dans le dossier */ }
    }
    setBusy(false);
  };

  return (
    <div className="summary">
      {(running > 0 || queued > 0) && (
        <span className="sum running" title={MAX_PARALLEL === 1 ? "Un plan analysé à la fois" : `${MAX_PARALLEL} analyses au maximum en parallèle`}>
          <span className="dot-spin" /> {running} en analyse{queued ? ` · ${queued} en attente` : ""}
        </span>
      )}
      {n("review") > 0 && <span className="sum review">{n("review")} à vérifier</span>}
      {n("error") > 0 && <span className="sum error">{n("error")} en échec</span>}
      {ready.length > 0 && (
        <button className="btn ghost" onClick={downloadAll} disabled={busy}>
          {busy ? "Téléchargement…" : `Télécharger ${ready.length > 1 ? `les ${ready.length} devis prêts` : "le devis prêt"}`}
        </button>
      )}
    </div>
  );
}
