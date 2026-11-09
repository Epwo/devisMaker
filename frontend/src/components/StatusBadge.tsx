import { reviewCount } from "../jobs";
import type { Job } from "../types";

export const STATUS_LABEL: Record<Job["status"], string> = {
  queued: "En attente",
  analyzing: "Analyse en cours",
  review: "À vérifier",
  ready: "Prêt",
  error: "Échec",
};

export function StatusBadge({ job }: { job: Job }) {
  const n = reviewCount(job);
  const label = job.status === "review" ? `${n} point${n > 1 ? "s" : ""} à vérifier`
    : job.status === "ready" && job.pdfDownloaded ? "Devis téléchargé"
    : STATUS_LABEL[job.status];
  return (
    <span className={`status st-${job.status}`}>
      {job.status === "analyzing" && <span className="dot-spin" />}
      {label}
    </span>
  );
}
