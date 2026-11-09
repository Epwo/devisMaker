import { useState } from "react";
import { useJobs } from "../jobs";
import type { Job } from "../types";
import { StatusBadge } from "./StatusBadge";

const FILTERS: { key: string; label: string; test: (j: Job) => boolean }[] = [
  { key: "all", label: "Tous", test: () => true },
  { key: "todo", label: "À traiter", test: (j) => j.status === "review" || j.status === "error" },
  { key: "running", label: "En cours", test: (j) => j.status === "queued" || j.status === "analyzing" },
  { key: "ready", label: "Prêts", test: (j) => j.status === "ready" },
];

const eur = (x: number) => x.toLocaleString("fr-FR", { style: "currency", currency: "EUR" });

export function JobList({ onPick }: { onPick: () => void }) {
  const { jobs, selected, select } = useJobs();
  const [filter, setFilter] = useState("all");
  const f = FILTERS.find((x) => x.key === filter)!;
  // Ordre : ce qui demande une action d'abord, puis en cours, puis prêts
  const rank: Record<Job["status"], number> = { error: 0, review: 1, analyzing: 2, queued: 3, ready: 4 };
  const shown = jobs.filter(f.test).sort((a, b) => rank[a.status] - rank[b.status] || a.addedAt - b.addedAt);

  return (
    <div className="joblist">
      <div className="filters" role="tablist">
        {FILTERS.map((x) => {
          const n = jobs.filter(x.test).length;
          return (
            <button key={x.key} role="tab" aria-selected={filter === x.key} className={`tab ${filter === x.key ? "on" : ""}`} onClick={() => setFilter(x.key)}>
              {x.label}{x.key !== "all" && n > 0 && <span className="count">{n}</span>}
            </button>
          );
        })}
      </div>
      <ul>
        {shown.map((j) => (
          <li key={j.id}>
            <button className={`job ${selected?.id === j.id ? "on" : ""}`} onClick={() => { select(j.id); onPick(); }}>
              <span className="job-name" title={j.file.name}>{j.ext?.fields.designation?.value as string || j.file.name}</span>
              <span className="job-sub muted small">{j.file.name}{j.client ? ` · ${j.client}` : ""}</span>
              <span className="job-foot">
                <StatusBadge job={j} />
                {j.quote && j.status !== "analyzing" && <span className="job-price">{eur(j.quote.total_price)}</span>}
              </span>
            </button>
          </li>
        ))}
        {!shown.length && <li className="muted small empty">Aucun dossier ici.</li>}
      </ul>
    </div>
  );
}
